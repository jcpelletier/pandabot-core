"""
pandabot_core.active_qa.loop
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The exploration loop: the thing that actually runs for an hour.

It ties the driver, the recorder, the coverage map, the frontier, the rulebook
and the strategy deck together, and it is deliberately **policy-driven with the
model as an occasional advisor**. The model is consulted when a genuinely new
state appears and when a detector fires; everything else is chosen by the
frontier with no model call at all. That ratio is the budget: at ~30 tok/s a
call per action buys ~140 actions an hour, which is not exploration
(ACTIVE_QA.md 5 and 8).

**Minimum operating time is a first-class budget.** A loop that has clicked
everything has run out of exploration long before it has run out of time, so
running dry is not a reason to stop: it draws the next strategy card instead.
Only when the deck is exhausted *and* the frontier is empty *and* the floor has
elapsed does the run end on its own.

Every phase is timed. The walking skeleton exists to produce two numbers --
actions per hour, and the U7 false-positive rate -- so :class:`LoopStats` counts
where the wall-clock went from the first run rather than being retrofitted after
the answer is already inconvenient.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol, Sequence

from pandabot_core.active_qa.contract import (
    Action, Driver, Observation, fingerprint_policy_for, supported_actions,
)
from pandabot_core.active_qa.coverage import (
    Candidate, CoverageMap, Frontier, PriorityWeights, default_candidates,
)
from pandabot_core.active_qa.journal import EventKind, Journal
from pandabot_core.active_qa.recorder import SessionRecorder, Step
from pandabot_core.active_qa.rules import (
    Finding, StateContext, StateProbe, StepContext,
    run_session_detectors, run_state_detectors, run_step_detectors,
)
from pandabot_core.active_qa.strategies import Card, Deck

__all__ = [
    "Budgets", "LoopStats", "StopReason", "Advisor", "Orientation",
    "ExplorationLoop", "LoopResult",
]


# --- Budgets and outcomes ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class Budgets:
    """What bounds a run.

    One hour is a target, not a rule: ``min_seconds`` is a floor the loop
    refuses to stop before, and a long overnight run is set by raising
    ``max_seconds``. The condition worth tuning toward is novelty exhaustion,
    not the clock, but until that curve is measured on a real project the clock
    is the safer bound.
    """

    min_seconds: float = 3600.0
    max_seconds: float = 10800.0
    max_actions: int = 6000
    #: Actions without a newly discovered state before a strategy card is drawn.
    stall_threshold: int = 25
    #: Give up entirely after this many consecutive relaunches find nothing new.
    max_barren_relaunches: int = 3
    #: How many times to fight a system dialog for the foreground before
    #: concluding the app is unreachable.
    max_foreign_escapes: int = 5


class StopReason:
    BUDGET_TIME = "max time reached"
    BUDGET_ACTIONS = "max actions reached"
    EXHAUSTED = "nothing left to try and the minimum time had elapsed"
    CRASHED = "the app under test crashed and could not be recovered"
    STOPPED = "stopped by the operator"
    PAUSED = "paused"


@dataclass
class LoopStats:
    """Where the wall-clock went. The point of the first slice."""

    actions: int = 0
    states_discovered: int = 0
    model_calls: int = 0
    relaunches: int = 0
    cards_played: int = 0
    findings: int = 0
    # Phase timings, in seconds.
    observe_seconds: float = 0.0
    act_seconds: float = 0.0
    detect_seconds: float = 0.0
    model_seconds: float = 0.0
    probe_seconds: float = 0.0
    elapsed_seconds: float = 0.0

    @property
    def actions_per_hour(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return self.actions * 3600.0 / self.elapsed_seconds

    @property
    def actions_per_model_call(self) -> float:
        """The ratio the whole design turns on. Target is 10 to 20."""
        if self.model_calls == 0:
            return float(self.actions)
        return self.actions / self.model_calls

    @property
    def mechanical_seconds(self) -> float:
        return (self.observe_seconds + self.act_seconds
                + self.detect_seconds + self.probe_seconds)

    def to_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in (
            "actions", "states_discovered", "model_calls", "relaunches",
            "cards_played", "findings", "observe_seconds", "act_seconds",
            "detect_seconds", "model_seconds", "probe_seconds", "elapsed_seconds",
        )}
        d["actions_per_hour"] = round(self.actions_per_hour, 1)
        d["actions_per_model_call"] = round(self.actions_per_model_call, 1)
        d["mechanical_seconds"] = round(self.mechanical_seconds, 2)
        return d


# --- The advisor (the model) ------------------------------------------------


@dataclass(frozen=True, slots=True)
class Orientation:
    """What the model says about a state it has just been shown."""

    #: Element identity -> what this control appears to promise. Written once
    #: per state and read for free forever after, which is what makes rule U7
    #: affordable (ACTIVE_QA.md 7).
    expectations: dict[str, str] = field(default_factory=dict)
    #: Element identity -> how interesting it looks, roughly 0 to 1. Enters the
    #: frontier as one term in a priority, never as the decision itself.
    curiosity: dict[str, float] = field(default_factory=dict)
    notes: str = ""


class Advisor(Protocol):
    """The model, behind an interface the loop can run without."""

    def orient(self, observation: Observation, fingerprint: str) -> Orientation:
        """Called once per newly discovered state."""
        ...

    def judge(self, finding: Finding, context: StepContext) -> bool:
        """Is this candidate a real defect? Called only when a detector fires."""
        ...


class _NoAdvisor:
    """The null advisor. The loop is fully functional without a model, which is
    what lets the whole engine be tested at unit speed."""

    def orient(self, observation: Observation, fingerprint: str) -> Orientation:
        return Orientation()

    def judge(self, finding: Finding, context: StepContext) -> bool:
        return True


# --- Result -----------------------------------------------------------------


@dataclass
class LoopResult:
    stop_reason: str
    stats: LoopStats
    findings: list[Finding]
    coverage: CoverageMap
    recorder: SessionRecorder

    def to_dict(self) -> dict[str, Any]:
        return {
            "stop_reason": self.stop_reason,
            "stats": self.stats.to_dict(),
            "coverage": self.coverage.stats(),
            "session": self.recorder.stats(),
            "findings": len(self.findings),
        }


# --- The loop ---------------------------------------------------------------


class ExplorationLoop:
    """One exploration session against one driver.

    The caller owns the driver, the clock and the stop signal; this owns the
    decision of what to do next.
    """

    def __init__(
        self,
        driver: Driver,
        *,
        budgets: Budgets | None = None,
        advisor: Advisor | None = None,
        deck: Deck | None = None,
        weights: PriorityWeights | None = None,
        waived_rules: Iterable[str] = (),
        session_id: str = "",
        journal: Journal | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
        probe_kinds: Sequence[str] = ("press_hold", "hover", "focus"),
    ) -> None:
        self.driver = driver
        self.budgets = budgets or Budgets()
        self.advisor = advisor or _NoAdvisor()
        self.deck = deck if deck is not None else Deck()
        self.waived_rules = tuple(waived_rules)
        self.journal = journal
        self.clock = clock
        # Waiting is a real action (the idle card), but tests must not actually
        # sleep for 60s, so it is injectable and defaults to not sleeping at all
        # -- a driver's own act() is what really waits in production.
        self.sleep = sleep or (lambda _s: None)
        self.probe_kinds = tuple(probe_kinds)

        self.coverage = CoverageMap()
        self.frontier = Frontier(self.coverage, weights or PriorityWeights())
        self.recorder = SessionRecorder(session_id)
        self.stats = LoopStats()
        self.findings: list[Finding] = []
        # One defect is one finding, however many times the run walks into it.
        # A long session revisits the same dead control dozens of times, and
        # without this the report would carry a finding per encounter.
        self._seen_findings: set[tuple[str, str, str]] = set()
        self.expectations: dict[str, dict[str, str]] = {}

        self._supported = supported_actions(driver)
        self._policy = fingerprint_policy_for(driver)
        self._stop = False
        self._pause = False
        self._started_at = 0.0
        self._steps_since_new_state = 0
        self._barren_relaunches = 0
        self._foreign_escapes = 0

    # -- journal -----------------------------------------------------------
    def _log(self, kind: str, text: str, **fields: Any) -> None:
        if self.journal is not None:
            self.journal.record(kind, text, **fields)

    # -- findings ----------------------------------------------------------
    def _add(self, findings: Iterable[Finding]) -> int:
        """Keep the first sighting of each distinct defect, drop repeats.

        Triage does the full clustering and replay-confirmation later; this is
        just so the loop does not accumulate hundreds of copies of one defect in
        memory, and so `stats.findings` counts defects rather than encounters.
        """
        added = 0
        for finding in findings:
            if finding.cluster_key in self._seen_findings:
                continue
            self._seen_findings.add(finding.cluster_key)
            self.findings.append(finding)
            added += 1
        return added

    # -- control -----------------------------------------------------------
    def stop(self) -> None:
        """Ask the loop to finish after the current action. Safe from a thread."""
        self._stop = True

    def pause(self) -> None:
        """Stop and keep everything, for a resume (e.g. the model was unloaded)."""
        self._pause = True

    @property
    def elapsed(self) -> float:
        return self.clock() - self._started_at if self._started_at else 0.0

    # -- the run -----------------------------------------------------------
    def run(self) -> LoopResult:
        self._started_at = self.clock()
        self.recorder.mark_launch()
        self._log(EventKind.SESSION_START,
                  f"Starting exploration of a {self.driver.kind} target. "
                  f"Floor {self.budgets.min_seconds / 60:.0f} min, "
                  f"ceiling {self.budgets.max_seconds / 60:.0f} min, "
                  f"at most {self.budgets.max_actions} actions.")

        stop_reason = StopReason.EXHAUSTED
        while True:
            reason = self._check_budgets()
            if reason:
                stop_reason = reason
                break

            observation, fingerprint = self._observe()

            # Something that is not the app under test is on screen: an Android
            # permission dialog, a share sheet, a browser popup. Exploring it
            # means testing someone else's software and filing findings against
            # our own, so recover instead. A driver that cannot say is assumed
            # to be in its app.
            if observation.meta.get("in_app") is False:
                if not self._escape_foreign_ui(observation):
                    stop_reason = StopReason.CRASHED
                    break
                continue

            node, is_new = self.coverage.observe(
                observation, fingerprint, step=len(self.recorder))
            if is_new:
                self.stats.states_discovered += 1
                self._steps_since_new_state = 0
                self._barren_relaunches = 0
                self._on_new_state(observation, fingerprint, node)

            candidate = self.frontier.pop(state=fingerprint)
            if candidate is None:
                if not self._find_more_work(observation, fingerprint):
                    if self.elapsed >= self.budgets.min_seconds:
                        stop_reason = StopReason.EXHAUSTED
                        break
                    # Below the floor with nothing to do: relaunch and re-walk
                    # rather than stop early. The brief is to keep pressing.
                    if not self._relaunch():
                        stop_reason = StopReason.CRASHED
                        break
                continue

            if not self._take(candidate, observation, fingerprint):
                stop_reason = StopReason.CRASHED
                break

        self.stats.elapsed_seconds = self.elapsed
        if self._pause:
            stop_reason = StopReason.PAUSED
        elif self._stop:
            stop_reason = StopReason.STOPPED

        detect_start = self.clock()
        self._add(run_session_detectors(self.coverage, waived=self.waived_rules))
        self.stats.detect_seconds += self.clock() - detect_start
        self.stats.findings = len(self.findings)

        self._log(EventKind.SESSION_END,
                  f"Finished: {stop_reason}. "
                  f"{self.stats.actions} actions, "
                  f"{self.stats.states_discovered} screens, "
                  f"{len(self.findings)} findings, "
                  f"{self.stats.model_calls} model calls.")
        if self.journal is not None:
            self.journal.render()
        return LoopResult(stop_reason, self.stats, self.findings,
                          self.coverage, self.recorder)

    def _escape_foreign_ui(self, observation: Observation) -> bool:
        """Try to get back into the app. Returns False when it is hopeless."""
        foreign = observation.meta.get("foreground_package", "something else")
        self._foreign_escapes += 1
        if self._foreign_escapes > self.budgets.max_foreign_escapes:
            self._log(EventKind.NOTE,
                      f"Giving up: {foreign} keeps taking the foreground and the "
                      "app under test cannot be reached.")
            return False
        self._log(EventKind.NOTE,
                  f"{foreign} is in the foreground, not the app under test. "
                  "Recovering rather than exploring it.",
                  screenshot=observation.screenshot,
                  detail={"foreground": foreign})
        recover = getattr(self.driver, "recover", None)
        if callable(recover) and recover():
            return True
        return self._relaunch()

    # -- phases ------------------------------------------------------------
    def _check_budgets(self) -> str | None:
        if self._stop or self._pause:
            return StopReason.STOPPED
        if self.elapsed >= self.budgets.max_seconds:
            return StopReason.BUDGET_TIME
        if self.stats.actions >= self.budgets.max_actions:
            return StopReason.BUDGET_ACTIONS
        return None

    def _observe(self) -> tuple[Observation, str]:
        start = self.clock()
        observation = self.driver.observe()
        fingerprint = observation.compute_fingerprint(self._policy)
        self.stats.observe_seconds += self.clock() - start
        return observation, fingerprint

    def _on_new_state(self, observation: Observation, fingerprint: str, node) -> None:
        """Seed a newly discovered state: baseline candidates, rulebook probes,
        and the single model call this state will ever get."""
        seeded = self.frontier.extend(default_candidates(
            observation, fingerprint, supported=self._supported))
        controls = len(observation.interactive())
        self._log(EventKind.STATE_NEW,
                  (f"New screen. {controls} interactive control(s); "
                   f"queued {seeded} thing(s) to try."
                   if not observation.is_blind else
                   "New screen, but the driver could supply no element tree, "
                   "so this state is being explored blind."),
                  screen=observation.screen_id, screenshot=observation.screenshot,
                  step=len(self.recorder))

        probes = self._run_probes(observation)
        if probes:
            start = self.clock()
            self._add(run_state_detectors(
                StateContext(observation=observation, fingerprint=fingerprint,
                             probes=probes, step_index=len(self.recorder)),
                waived=self.waived_rules))
            self.stats.detect_seconds += self.clock() - start

        start = self.clock()
        orientation = self.advisor.orient(observation, fingerprint)
        self.stats.model_seconds += self.clock() - start
        if orientation.expectations or orientation.curiosity or orientation.notes:
            self.stats.model_calls += 1
        node.oriented = True
        if orientation.notes or orientation.expectations:
            promises = "; ".join(f"{k} -> {v}" for k, v in
                                 list(orientation.expectations.items())[:6])
            self._log(EventKind.MODEL_ORIENT,
                      orientation.notes or "Oriented on this screen.",
                      screen=observation.screen_id,
                      detail={"expectations": len(orientation.expectations),
                              **({"promises": promises} if promises else {})})
        if orientation.expectations:
            self.expectations[fingerprint] = dict(orientation.expectations)
        for identity, score in orientation.curiosity.items():
            el = observation.by_identity(identity)
            if el is None:
                continue
            self.frontier.push(Candidate(
                state=fingerprint, element_identity=identity, kind="click",
                curiosity=score, reason="the model flagged this as worth trying"))

    def _run_probes(self, observation: Observation) -> tuple[StateProbe, ...]:
        """Capture each interactive control at rest and mid-interaction.

        This is the rulebook's most expensive habit: three captures per control
        per new state, which on a thirty-control screen is ninety driver
        round-trips before a single exploratory action. It is timed separately
        precisely so we can see whether it has to become sampled rather than
        exhaustive.
        """
        start = self.clock()
        probes: list[StateProbe] = []
        for el in observation.interactive():
            for kind in self.probe_kinds:
                if kind not in self._supported:
                    probes.append(StateProbe(el.identity, kind, supported=False))
                    continue
                at_rest = el.render_hash
                if at_rest is None:
                    probes.append(StateProbe(el.identity, kind))
                    continue
                self.driver.act(Action(kind=kind, target=el.id,
                                       reason=f"rulebook probe: {kind}"))
                during = self.driver.observe().by_identity(el.identity)
                probes.append(StateProbe(
                    el.identity, kind, at_rest=at_rest,
                    during=during.render_hash if during else None))
        self.stats.probe_seconds += self.clock() - start
        return tuple(probes)

    def _take(self, candidate: Candidate, observation: Observation,
              fingerprint: str) -> bool:
        """Act on one candidate, record it, and run the per-step rulebook.

        Returns False only when the app has crashed unrecoverably.
        """
        action = candidate.to_action(observation)
        if action is None:
            # The element vanished between queueing and now, which is normal.
            self.coverage.record_transition(fingerprint, candidate, fingerprint)
            return True

        repeats = 1
        if isinstance(candidate.value, dict) and "repeat" in candidate.value:
            repeats = int(candidate.value["repeat"])
            action = Action(kind=action.kind, target=action.target,
                            reason=action.reason)

        start = self.clock()
        for _ in range(repeats):
            result = self.driver.act(action)
        if action.kind == "wait" and action.value:
            self.sleep(float(action.value) / 1000.0)
        act_seconds = self.clock() - start
        self.stats.act_seconds += act_seconds
        self.stats.actions += 1
        self._steps_since_new_state += 1

        after, after_fingerprint = self._observe()
        self.coverage.record_transition(fingerprint, candidate, after_fingerprint)

        outcome = ("state changed" if after_fingerprint != fingerprint
                   else "nothing changed")
        if not result.ok:
            outcome = f"did not land: {result.error}"
        self._log(EventKind.ACTION,
                  f"{action.describe()} — {outcome}.",
                  screen=observation.screen_id, screenshot=after.screenshot,
                  step=len(self.recorder),
                  detail={"why": action.reason} if action.reason else {})

        step = self.recorder.record(
            action,
            before_fingerprint=fingerprint,
            after_fingerprint=after_fingerprint,
            result=result,
            before_screenshot=observation.screenshot,
            after_screenshot=after.screenshot,
            logs=after.logs,
            screen_id=observation.screen_id,
            duration_ms=int(act_seconds * 1000),
            element_identity=candidate.element_identity,
        )

        self._detect(action, observation, after, fingerprint,
                     after_fingerprint, result, step)

        health = self.driver.health()
        if health.crashed:
            self._add([Finding(
                rule="crash", severity="blocker",
                summary=f"The app crashed: {health.message or 'no message'}",
                observed=f"Crashed after {action.describe()}.",
                expected="The app should not crash.",
                state=fingerprint, step_index=step.index,
                screen_id=observation.screen_id,
                element_identity=candidate.element_identity,
            )])
            self._log(EventKind.CRASH,
                      f"The app crashed: {health.message or 'no message'}",
                      severity="blocker", rule="crash",
                      screen=observation.screen_id, step=step.index)
            return self._relaunch()
        return True

    def _detect(self, action, before, after, before_fp, after_fp, result,
                step: Step) -> None:
        start = self.clock()
        ctx = StepContext(
            action=action, before=before, after=after,
            before_fingerprint=before_fp, after_fingerprint=after_fp,
            step_index=step.index, ok=result.ok,
            duration_ms=step.duration_ms,
            expectations=self.expectations.get(before_fp, {}),
        )
        found = run_step_detectors(ctx, waived=self.waived_rules)
        self.stats.detect_seconds += self.clock() - start

        for finding in found:
            model_start = self.clock()
            keep = self.advisor.judge(finding, ctx)
            elapsed = self.clock() - model_start
            self.stats.model_seconds += elapsed
            if not isinstance(self.advisor, _NoAdvisor):
                self.stats.model_calls += 1
            if keep and self._add([finding]):
                self._log(EventKind.FINDING, finding.summary,
                          rule=finding.rule, severity=finding.severity,
                          screen=finding.screen_id, step=finding.step_index,
                          screenshot=after.screenshot)
            elif not keep:
                self._log(EventKind.MODEL_JUDGE,
                          f"Dismissed a {finding.rule} candidate: {finding.summary}",
                          rule=finding.rule, screen=finding.screen_id)

    def _find_more_work(self, observation: Observation, fingerprint: str) -> bool:
        """Nothing queued here. Draw a card, walk somewhere, or relaunch."""
        if self._steps_since_new_state >= self.budgets.stall_threshold or \
                not self.frontier.has_work():
            card, candidates = self.deck.play(observation, fingerprint)
            if card is not None:
                self.frontier.extend(candidates)
                self._log(EventKind.CARD,
                          f'Novelty stalled, so playing "{card.name}": '
                          f'{card.description.lower()}. '
                          f"Queued {len(candidates)} new thing(s) to try.",
                          screen=observation.screen_id,
                          detail={"card": card.name})
                self.stats.cards_played += 1
                self._steps_since_new_state = 0
                return True

        if self.frontier.has_work():
            route = self.coverage.route_to_open_work(
                fingerprint, self.frontier.open_states())
            if route:
                edge = route[0]
                el = observation.by_identity(edge.element_identity)
                if el is not None:
                    self.driver.act(Action(kind=edge.kind, target=el.id,
                                           reason="walking toward open work"))
                    self.stats.actions += 1
                    return True
            if route is None:
                # Queued work we cannot walk to. With no checkpoints, a cold
                # start is the only other route, so drop it rather than spin.
                for state in list(self.frontier.open_states()):
                    if state != fingerprint:
                        self.frontier.drop_state(state)
                return self._relaunch()
        return False

    def _relaunch(self) -> bool:
        if self._barren_relaunches >= self.budgets.max_barren_relaunches:
            return False
        self._barren_relaunches += 1
        self._log(EventKind.RELAUNCH,
                  "Nothing reachable left to try, and there are no saved states "
                  "to jump to, so restarting from a cold start and re-walking.",
                  detail={"relaunch": self._barren_relaunches})
        self.driver.relaunch()
        self.recorder.mark_launch()
        self.stats.relaunches += 1
        self._steps_since_new_state = 0
        health = self.driver.health()
        return not health.crashed
