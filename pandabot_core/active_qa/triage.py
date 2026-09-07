"""
pandabot_core.active_qa.triage
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Turning candidates into findings worth reading.

Detectors are tuned for recall, so what comes out of a run is a pile of
*candidates*: duplicated, sometimes coincidental, occasionally the same defect
seen under two rules. Triage is what stands between that pile and the report,
and it does three things.

**Cluster.** The same rule, on the same control, in the same state is one defect
however many times the run walked into it.

**Confirm by replay.** Relaunch from a cold start, replay the recorded trace, and
require the detector to fire again. A finding that will not reproduce is not a
finding, and an exploratory agent that files two hundred unverified issues is
worse than no agent at all (ACTIVE_QA.md 13).

**Rank.** Severity comes from the rule, not from enthusiasm. The model may raise
it, never lower it -- a judge that can talk itself out of a blocker is not a
gate.

The expensive part is replay, and with no checkpoints (§5.2) every confirmation
starts from a cold start. Doing that per finding would be ruinous: an hour's run
on Roblox rebuilds a whole world each time. So confirmation is **batched** -- one
relaunch replays the longest trace in an epoch and collects every candidate that
re-fires along the way, because a shorter trace is a prefix of a longer one in
the same epoch. Twenty findings in one epoch cost one relaunch, not twenty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from pandabot_core.active_qa.contract import (
    DEFAULT_FINGERPRINT_POLICY, Driver, FingerprintPolicy,
    fingerprint_policy_for,
)
from pandabot_core.active_qa.recorder import SessionRecorder, Trace
from pandabot_core.active_qa.rules import (
    RULES, Finding, StepContext, run_step_detectors, severity_rank,
)

__all__ = [
    "ClusterKey", "FindingCluster", "TriagedFinding", "TriageReport",
    "cluster_findings", "triage", "replay_once",
]

ClusterKey = tuple[str, str, str]


# --- Clustering -------------------------------------------------------------


@dataclass
class FindingCluster:
    """Every sighting of one defect."""

    key: ClusterKey
    findings: list[Finding] = field(default_factory=list)

    @property
    def representative(self) -> Finding:
        """The sighting to report. Most severe first, then the earliest.

        Earliest matters: the first time a run met a defect it had done the
        least to the app, so its trace is the shortest and the repro steps are
        the ones a person can actually follow.
        """
        return min(self.findings,
                   key=lambda f: (severity_rank(f.severity), f.step_index
                                  if f.step_index >= 0 else 10**9))

    @property
    def occurrences(self) -> int:
        return len(self.findings)

    @property
    def rule(self) -> str:
        return self.key[0]


def cluster_findings(findings: Iterable[Finding]) -> list[FindingCluster]:
    """Group sightings into defects, most severe first."""
    clusters: dict[ClusterKey, FindingCluster] = {}
    for finding in findings:
        clusters.setdefault(finding.cluster_key,
                            FindingCluster(finding.cluster_key)).findings.append(finding)
    return sorted(
        clusters.values(),
        key=lambda c: (severity_rank(c.representative.severity),
                       -c.occurrences, c.representative.step_index),
    )


# --- Results ----------------------------------------------------------------


@dataclass
class TriagedFinding:
    """One clustered defect plus what confirmation made of it."""

    finding: Finding
    occurrences: int = 1
    attempts: int = 0
    confirmations: int = 0
    confirmed: bool = False
    #: The replay stopped matching the recorded run before reaching this step,
    #: so the attempt proves nothing either way. Distinct from "did not
    #: reproduce", and reported as such.
    diverged: bool = False
    trace: Trace | None = None

    @property
    def rule(self) -> str:
        return self.finding.rule

    @property
    def severity(self) -> str:
        return self.finding.severity

    @property
    def confidence(self) -> str:
        if self.confirmed:
            return "confirmed"
        if self.diverged:
            return "unverifiable"
        if self.attempts == 0:
            return "unverified"
        return "did not reproduce"

    def to_dict(self) -> dict[str, Any]:
        d = self.finding.to_dict()
        d.update({
            "occurrences": self.occurrences,
            "attempts": self.attempts,
            "confirmations": self.confirmations,
            "confirmed": self.confirmed,
            "diverged": self.diverged,
            "confidence": self.confidence,
            "repro_steps": self.trace.repro_steps() if self.trace else [],
        })
        return d


@dataclass
class TriageReport:
    confirmed: list[TriagedFinding] = field(default_factory=list)
    unconfirmed: list[TriagedFinding] = field(default_factory=list)
    replays: int = 0
    relaunches: int = 0

    @property
    def all(self) -> list[TriagedFinding]:
        return self.confirmed + self.unconfirmed

    def stats(self) -> dict[str, Any]:
        by_rule: dict[str, int] = {}
        for t in self.confirmed:
            by_rule[t.rule] = by_rule.get(t.rule, 0) + 1
        by_severity: dict[str, int] = {}
        for t in self.confirmed:
            by_severity[t.severity] = by_severity.get(t.severity, 0) + 1
        return {
            "confirmed": len(self.confirmed),
            "unconfirmed": len(self.unconfirmed),
            "diverged": sum(1 for t in self.unconfirmed if t.diverged),
            "confirmed_by_rule": dict(sorted(by_rule.items())),
            "confirmed_by_severity": by_severity,
            "replays": self.replays,
            "relaunches": self.relaunches,
        }


# --- Replay -----------------------------------------------------------------


def replay_once(
    driver: Driver,
    trace: Trace,
    *,
    policy: FingerprintPolicy | None = None,
    waived: Iterable[str] = (),
    expectations: dict[str, dict[str, str]] | None = None,
    strict: bool = False,
) -> tuple[set[ClusterKey], int]:
    """Relaunch, replay ``trace``, and report every cluster key that fired.

    Returns ``(fired keys, steps completed)``. Fewer steps completed than the
    trace holds means the replay diverged: an element was gone, or (under
    ``strict``) the state stopped matching what was recorded. Divergence is not
    a failure to reproduce -- it means the attempt proved nothing -- so the
    caller reports the two differently.
    """
    policy = policy or fingerprint_policy_for(driver)
    expectations = expectations or {}
    fired: set[ClusterKey] = set()

    driver.relaunch()
    for i in range(len(trace)):
        before = driver.observe()
        before_fp = before.compute_fingerprint(policy)
        recorded = trace.steps[i]

        if strict and recorded.before_fingerprint and \
                before_fp != recorded.before_fingerprint:
            return fired, i

        action = trace.rebind(i, before)
        if action is None:
            return fired, i

        result = driver.act(action)
        after = driver.observe()
        after_fp = after.compute_fingerprint(policy)

        for finding in run_step_detectors(
            StepContext(action=action, before=before, after=after,
                        before_fingerprint=before_fp, after_fingerprint=after_fp,
                        step_index=recorded.index, ok=result.ok,
                        duration_ms=result.duration_ms,
                        expectations=expectations.get(before_fp, {})),
            waived=waived,
        ):
            fired.add(finding.cluster_key)

    return fired, len(trace)


# --- The pass ---------------------------------------------------------------


def triage(
    findings: Iterable[Finding],
    *,
    driver: Driver | None = None,
    recorder: SessionRecorder | None = None,
    attempts: int = 3,
    required: int = 2,
    policy: FingerprintPolicy | None = None,
    waived: Iterable[str] = (),
    expectations: dict[str, dict[str, str]] | None = None,
    strict: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> TriageReport:
    """Cluster, confirm and rank. The gate between a run and its report.

    Without a ``driver`` and ``recorder`` this clusters and ranks only, and
    everything comes back unconfirmed -- honest, and what a paused or crashed
    session can still produce.

    ``required`` of ``attempts`` replays must re-fire a defect for it to count as
    confirmed. Two of three is the default: one reproduction can be luck on a
    timing-dependent UI, and three attempts on a cold-start replay is already
    expensive.
    """
    clusters = cluster_findings(findings)
    report = TriageReport()

    triaged: dict[ClusterKey, TriagedFinding] = {}
    for c in clusters:
        rep = c.representative
        trace = None
        if recorder is not None and rep.step_index >= 0:
            try:
                trace = recorder.trace_for(rep.step_index)
            except IndexError:
                trace = None
        triaged[c.key] = TriagedFinding(finding=rep, occurrences=c.occurrences,
                                        trace=trace)

    if driver is None or recorder is None:
        report.unconfirmed = list(triaged.values())
        return report

    # Batch by epoch: within one epoch a shorter trace is a prefix of a longer
    # one, so replaying the longest exercises every candidate in it. That is the
    # difference between one relaunch per epoch and one per finding.
    by_epoch: dict[int, list[ClusterKey]] = {}
    for key, t in triaged.items():
        if t.trace is not None:
            by_epoch.setdefault(t.trace.epoch, []).append(key)

    for epoch, keys in sorted(by_epoch.items()):
        longest = max((triaged[k].trace for k in keys),
                      key=lambda tr: len(tr))  # type: ignore[arg-type]
        assert longest is not None
        if on_progress:
            on_progress(f"confirming {len(keys)} candidate(s) from epoch {epoch}: "
                        f"{attempts} replays of {len(longest)} step(s)")

        for _ in range(attempts):
            fired, completed = replay_once(
                driver, longest, policy=policy, waived=waived,
                expectations=expectations, strict=strict)
            report.replays += 1
            report.relaunches += 1
            for key in keys:
                t = triaged[key]
                target = t.trace.target.index if t.trace else -1
                reached = completed >= (t.trace.target_index + 1 if t.trace else 0)
                if not reached:
                    t.diverged = True
                    continue
                t.attempts += 1
                if key in fired:
                    t.confirmations += 1
            # Stop early once every candidate has enough confirmations.
            if all(triaged[k].confirmations >= required for k in keys):
                break

    for t in triaged.values():
        t.confirmed = t.confirmations >= required
        if t.confirmed:
            t.diverged = False
            report.confirmed.append(t)
        else:
            report.unconfirmed.append(t)

    report.confirmed.sort(key=lambda t: (severity_rank(t.severity), -t.occurrences))
    report.unconfirmed.sort(key=lambda t: (severity_rank(t.severity), -t.occurrences))
    return report
