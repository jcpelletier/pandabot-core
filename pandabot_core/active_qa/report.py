"""
pandabot_core.active_qa.report
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The markdown report and its evidence bundle.

Findings do not go straight to GitHub issues. A session writes a report plus the
evidence to back it, and a person decides what becomes an issue -- direct filing
comes later, once the harness has earned it (ACTIVE_QA.md 13).

Every bug carries **Title, Description, Repro steps, Observed, Expected,
Screenshot**, plus the replayable trace. Those are the fields that make a finding
actionable rather than merely true.

The other half of the job is saying what the run did *not* establish, and this
module treats that as load-bearing rather than decorative:

* **Waivers and exclusions are echoed in the header**, with their stated reasons.
  A project can waive rules in its ``PANDABOTQA.md``, and suppression that is
  invisible is how a build waives its way to a clean report (§11.3).
* **Rules that are not implemented are listed as such.** Absence must never read
  as compliance.
* **Rules the target could not support are listed separately again.** A
  touch-only device cannot check hover, and "inapplicable" is neither a pass nor
  a failure.
* **Unconfirmed findings get their own section**, split by whether they failed to
  reproduce or could not be replayed at all.
* **What was not reached** closes the report, because coverage a run did not have
  is the first thing a reader will otherwise assume it did.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pandabot_core.active_qa.rules import RULES, severity_rank
from pandabot_core.active_qa.triage import TriageReport, TriagedFinding

__all__ = ["SessionInfo", "write_report", "render_report"]

_SEVERITY_ORDER = ("blocker", "major", "minor", "polish")


@dataclass
class SessionInfo:
    """The header facts: what ran, against what, for how long, under which rules."""

    session_id: str = ""
    repo: str = ""
    target: str = ""              # web | android | roblox
    runner: str = ""              # panda | devbox
    model: str = ""
    started_at: str = ""
    duration_seconds: float = 0.0
    stop_reason: str = ""
    brief: str = ""               # what the operator asked for
    #: Rule ids waived by the project, with the reason each was waived.
    waivers: Mapping[str, str] = field(default_factory=dict)
    #: Rule ids the target cannot support (no hover on touch, and so on).
    inapplicable: Mapping[str, str] = field(default_factory=dict)
    out_of_scope: Sequence[str] = ()
    known_issues: Sequence[str] = ()
    #: True when the driver could not address elements reliably (Compose or
    #: Flutter without test tags, or vision-only mode).
    degraded_identity: bool = False
    degraded_note: str = ""
    not_reached: Sequence[str] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id, "repo": self.repo,
            "target": self.target, "runner": self.runner, "model": self.model,
            "started_at": self.started_at,
            "duration_seconds": round(self.duration_seconds, 1),
            "stop_reason": self.stop_reason,
            "waivers": dict(self.waivers),
            "inapplicable": dict(self.inapplicable),
            "degraded_identity": self.degraded_identity,
        }


def _hms(seconds: float) -> str:
    total = int(seconds)
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    return f"{h}h{m:02d}m" if h else (f"{m}m{s:02d}s" if m else f"{s}s")


def _rule_label(rule_id: str) -> str:
    rule = RULES.get(rule_id)
    return f"{rule_id} ({rule.title.lower()})" if rule else rule_id


# --- Evidence ---------------------------------------------------------------


def _copy_evidence(triaged: TriagedFinding, tag: str, evidence: Path) -> dict[str, str]:
    """Put this finding's screenshots and trace beside the report.

    Copied rather than referenced: the harness's own screenshot directory is
    temporary, and a report whose evidence has been cleaned up is not evidence.
    """
    out: dict[str, str] = {}
    trace = triaged.trace
    if trace is not None:
        target = trace.target
        for label, src in (("before", target.before_screenshot),
                           ("after", target.after_screenshot)):
            if not src:
                continue
            path = Path(src)
            if not path.exists():
                continue
            dest = evidence / f"{tag}-{label}{path.suffix or '.png'}"
            try:
                shutil.copyfile(path, dest)
                out[label] = f"evidence/{dest.name}"
            except OSError:
                pass
        trace_path = evidence / f"{tag}-trace.json"
        trace_path.write_text(trace.to_json(), encoding="utf-8")
        out["trace"] = f"evidence/{trace_path.name}"
    return out


# --- Rendering --------------------------------------------------------------


def _short_repro(t: TriagedFinding, coverage: Any, start: str) -> list[str] | None:
    """The shortest observed route to the defect, if the map knows one."""
    if coverage is None or t.trace is None or not start:
        return None
    path = coverage.path_between(start, t.finding.state)
    if path is None:
        return None
    from pandabot_core.active_qa.recorder import humanise_identity

    # Coverage edges record the element and the action kind but not the value,
    # so a `type` step here must not read as though the element name were the
    # text that was typed. Only the final step (a real recorded Step) knows it.
    def _edge_line(i: int, e: Any) -> str:
        who = humanise_identity(e.element_identity)
        if e.kind == "click":
            return f'{i}. click "{who}"'
        if e.kind == "type":
            return f'{i}. enter text into "{who}"'
        return f'{i}. {e.kind.replace("_", " ")} "{who}"'

    lines = [_edge_line(i, e) for i, e in enumerate(path, start=1)]
    final = t.trace.target
    from pandabot_core.active_qa.recorder import _describe
    lines.append(f"{len(lines) + 1}. {_describe(final)}")
    return lines


def _finding_section(n: int, t: TriagedFinding, evidence: dict[str, str],
                     coverage: Any = None, start: str = "") -> str:
    tag = f"AQ-{n}"
    f = t.finding
    title = f.summary or f"{f.rule} on {f.element_identity}"
    lines = [f"### {tag} · {title}", ""]

    meta = [f"**Severity:** {f.severity}",
            f"**Rule:** {_rule_label(f.rule)}"]
    if f.screen_id:
        meta.append(f"**Screen:** `{f.screen_id}`")
    lines.append("   ".join(meta))

    if t.confirmed:
        lines.append(f"**Confirmed:** yes ({t.confirmations}/{t.attempts} replays)")
    elif t.diverged:
        lines.append("**Confirmed:** could not be replayed "
                     "(the run diverged before reaching this step)")
    elif t.attempts:
        lines.append(f"**Confirmed:** no (0/{t.attempts} replays reproduced it)")
    else:
        lines.append("**Confirmed:** not attempted")
    if t.occurrences > 1:
        lines.append(f"**Seen:** {t.occurrences} times during the run")
    lines.append("")

    lines += ["**Description**", "",
              f.observed.split(".")[0].strip() + "." if f.observed else title, ""]

    # Always present, even when empty: the six fields are the contract, and a
    # missing section reads as an oversight rather than as missing evidence.
    lines += ["**Repro steps**", ""]
    short = _short_repro(t, coverage, start)
    full = t.trace.repro_steps() if t.trace is not None else []
    if short:
        lines += short
        if len(full) > len(short):
            lines += ["",
                      f"*(Shortest route the run observed. It actually arrived "
                      f"here after {len(full)} actions; the full sequence is in "
                      f"the trace.)*"]
    elif full:
        lines += full
    else:
        lines.append("(no trace was recorded for this finding, so it cannot be "
                     "replayed step by step)")
    lines.append("")

    lines += ["**Observed**", "", f.observed or "(not recorded)", "",
              "**Expected**", "", f.expected or "(not recorded)", ""]

    if evidence.get("after"):
        shot = f"**Screenshot**  `{evidence['after']}`"
        if evidence.get("before"):
            shot += f"  (before: `{evidence['before']}`)"
        lines.append(shot)
    else:
        lines.append("**Screenshot**  none captured for this step")
    if evidence.get("trace"):
        lines.append(f"**Trace**       `{evidence['trace']}`")
    lines.append("")
    return "\n".join(lines)


def render_report(
    info: SessionInfo,
    triage_report: TriageReport,
    *,
    evidence_by_tag: Mapping[str, dict[str, str]] | None = None,
    stats: Mapping[str, Any] | None = None,
    coverage: Mapping[str, Any] | None = None,
    journal_path: str | None = None,
    coverage_map: Any = None,
    entry_state: str = "",
) -> str:
    """Render the markdown. Pure, so it can be tested without touching disk.

    ``coverage_map`` and ``entry_state``, when given, let each finding show the
    shortest route the run observed rather than the whole wandering epoch.
    """
    evidence_by_tag = evidence_by_tag or {}
    stats = stats or {}
    coverage = coverage or {}
    confirmed = triage_report.confirmed
    unconfirmed = triage_report.unconfirmed

    out: list[str] = []
    heading = f"# Active QA report — {info.repo or 'unknown repo'}"
    out += [heading, ""]

    facts = []
    if info.session_id:
        facts.append(f"`{info.session_id}`")
    if info.target:
        facts.append(f"target: {info.target}")
    if info.runner:
        facts.append(f"runner: {info.runner}")
    if info.model:
        facts.append(f"model: {info.model}")
    if info.duration_seconds:
        facts.append(_hms(info.duration_seconds))
    if stats.get("actions"):
        facts.append(f"{stats['actions']} actions")
    if coverage.get("states"):
        facts.append(f"{coverage['states']} states")
    out += [" · ".join(facts), ""]
    if info.brief:
        out += [f"**Brief:** {info.brief}", ""]
    if info.stop_reason:
        out += [f"**Ended because:** {info.stop_reason}", ""]

    # --- Summary ---
    out += ["## Summary", ""]
    if confirmed:
        counts = {s: sum(1 for t in confirmed if t.severity == s)
                  for s in _SEVERITY_ORDER}
        out += ["| Severity | Confirmed |", "|---|---|"]
        out += [f"| {s} | {counts[s]} |" for s in _SEVERITY_ORDER if counts[s]]
        out.append("")
    else:
        out += ["No confirmed findings.", ""]
    if unconfirmed:
        out += [f"{len(unconfirmed)} further candidate(s) did not survive "
                "confirmation; they are listed at the end.", ""]

    # --- What this run did and did not check ---
    out += ["## What this run checked", ""]
    active = [r.id for r in RULES.values()
              if r.implemented and r.id not in info.waivers
              and r.id not in info.inapplicable]
    out.append(f"**Rules in force:** {', '.join(active) if active else 'none'}")
    out.append("")
    pending = [r.id for r in RULES.values() if not r.implemented]
    if pending:
        out += [f"**Not yet implemented:** {', '.join(pending)}. These were not "
                "checked at all, and their absence from the findings above means "
                "nothing.", ""]
    if info.inapplicable:
        out += ["**Inapplicable to this target:**", ""]
        out += [f"- `{rid}` — {why}" for rid, why in sorted(info.inapplicable.items())]
        out.append("")
    if info.waivers:
        out += ["**Waived by the project:**", ""]
        out += [f"- `{rid}` — {why}" for rid, why in sorted(info.waivers.items())]
        out.append("")
    if info.out_of_scope:
        out += ["**Declared out of scope:**", ""]
        out += [f"- {item}" for item in info.out_of_scope]
        out.append("")
    if info.degraded_identity:
        out += ["> **Degraded run.** " + (info.degraded_note or
                "The driver could not address elements reliably, so element "
                "identity fell back to position. Findings are weaker than a run "
                "against a target with stable test ids."), ""]

    # --- Findings ---
    if confirmed:
        out += [f"## Confirmed findings ({len(confirmed)})", ""]
        for n, t in enumerate(sorted(confirmed,
                                     key=lambda x: (severity_rank(x.severity),
                                                    -x.occurrences)), start=1):
            out.append(_finding_section(
                n, t, evidence_by_tag.get(f"AQ-{n}", {}),
                coverage=coverage_map, start=entry_state))

    if unconfirmed:
        out += [f"## Unconfirmed candidates ({len(unconfirmed)})", "",
                "These fired during exploration but did not survive replay. They "
                "are listed for completeness, not as defects.", ""]
        out += ["| Rule | Control | Why not confirmed | Summary |", "|---|---|---|---|"]
        for t in unconfirmed:
            out.append(f"| {t.rule} | `{t.finding.element_identity}` | "
                       f"{t.confidence} | {t.finding.summary} |")
        out.append("")

    # --- Coverage ---
    out += ["## Coverage", ""]
    bits = []
    for label, key in (("States visited", "states"), ("Transitions", "transitions"),
                       ("Screens", "screens"), ("Dead ends", "dead_ends")):
        if key in coverage:
            bits.append(f"| {label} | {coverage[key]} |")
    for label, key in (("Actions", "actions"), ("Model calls", "model_calls"),
                       ("Strategy cards played", "cards_played"),
                       ("Relaunches", "relaunches")):
        if key in stats:
            bits.append(f"| {label} | {stats[key]} |")
    if bits:
        out += ["| | |", "|---|---|"] + bits + [""]
    if triage_report.replays:
        out += [f"Confirmation replayed {triage_report.replays} time(s) across "
                f"{triage_report.relaunches} cold start(s).", ""]
    if journal_path:
        out += [f"A full narrative of the run, with frames, is in "
                f"[`{journal_path}`]({journal_path}).", ""]

    # --- Not reached ---
    out += ["## Not reached", ""]
    if info.not_reached:
        out += ["The run did not get to these, so nothing here is a statement "
                "about them:", ""]
        out += [f"- {item}" for item in info.not_reached]
    else:
        out.append("Nothing was explicitly recorded as unreachable. That is not "
                   "the same as full coverage: it means the run did not identify "
                   "what it missed.")
    out.append("")
    if info.known_issues:
        out += ["## Known issues suppressed", "",
                "Declared in the project's `PANDABOTQA.md` and therefore not "
                "reported above:", ""]
        out += [f"- {item}" for item in info.known_issues]
        out.append("")

    return "\n".join(out).rstrip() + "\n"


# --- Writing ----------------------------------------------------------------


def write_report(
    directory: str | Path,
    info: SessionInfo,
    triage_report: TriageReport,
    *,
    stats: Mapping[str, Any] | None = None,
    coverage: Mapping[str, Any] | None = None,
    journal_path: str | None = None,
    coverage_map: Any = None,
    entry_state: str = "",
) -> Path:
    """Write ``report.md`` plus an ``evidence/`` bundle. Returns the report path."""
    root = Path(directory)
    evidence = root / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)

    ordered = sorted(triage_report.confirmed,
                     key=lambda t: (severity_rank(t.severity), -t.occurrences))
    evidence_by_tag: dict[str, dict[str, str]] = {}
    for n, t in enumerate(ordered, start=1):
        tag = f"AQ-{n}"
        evidence_by_tag[tag] = _copy_evidence(t, tag, evidence)

    (root / "findings.json").write_text(
        json.dumps({"session": info.to_dict(),
                    "stats": dict(stats or {}),
                    "coverage": dict(coverage or {}),
                    "triage": triage_report.stats(),
                    "confirmed": [t.to_dict() for t in ordered],
                    "unconfirmed": [t.to_dict() for t in triage_report.unconfirmed]},
                   indent=2, sort_keys=True, default=str),
        encoding="utf-8")

    path = root / "report.md"
    path.write_text(
        render_report(info, triage_report, evidence_by_tag=evidence_by_tag,
                      stats=stats, coverage=coverage, journal_path=journal_path,
                      coverage_map=coverage_map, entry_state=entry_state),
        encoding="utf-8")
    return path
