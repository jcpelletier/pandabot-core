"""
pandabot_core.active_qa.rules
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The universal UX rulebook, U1 to U12, as mechanical detectors.

Exploratory testing without fixed grounding drifts into opinion, and an agent
whose findings are opinions is one you stop reading. The rulebook is the
grounding: a set of product-independent rules that hold for any UI, each with an
id a finding can cite. None of them consults a model, which is deliberate --
they are cheap enough to run on every step, and adding rules costs wall-clock
rather than model time, which is the right place for the cost to land given the
loop's throughput ceiling (ACTIVE_QA.md 6 and 8).

Detectors are tuned for **recall, not precision**. They emit :class:`Finding` s,
which triage then clusters and replay-confirms before anything reaches a report.
A detector that stays quiet to avoid a false positive is worse than useless,
because the failure is invisible.

Three kinds, by what they need to see:

``StepDetector``     one action and its before/after -- U5, U6, U7, U10
``StateDetector``    one state plus its interaction probes -- U1, U2, U3
``SessionDetector``  the whole coverage map at the end -- U12

Not every rule is implemented yet. :data:`RULES` is the full declared set with
per-rule severity and status, so a report can state exactly which rules were in
force, which were waived by the project, and which are not built yet. Claiming
coverage a run did not have is the one thing a QA tool must never do.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Protocol, Sequence

from pandabot_core.active_qa.contract import Action, Element, Observation
from pandabot_core.active_qa.coverage import CoverageMap

__all__ = [
    "Severity", "BLOCKER", "MAJOR", "MINOR", "POLISH",
    "Rule", "RULES", "rule", "implemented_rules",
    "Finding", "StepContext", "StateProbe", "StateContext",
    "StepDetector", "StateDetector", "SessionDetector",
    "PressedStateRule", "HoverStateRule", "FocusIndicatorRule",
    "DisabledMeansDisabledRule", "InputEchoRule", "NoOpRule",
    "LayoutShiftRule", "DeadEndRule",
    "step_detectors", "state_detectors", "session_detectors",
    "run_step_detectors", "run_state_detectors", "run_session_detectors",
]


# --- Severity ---------------------------------------------------------------

Severity = str
BLOCKER = "blocker"   # the product is unusable along this path
MAJOR = "major"       # a real defect a user will hit and be blocked or misled by
MINOR = "minor"       # a real defect that degrades the experience
POLISH = "polish"     # worth knowing, not worth blocking on

_SEVERITY_ORDER = {BLOCKER: 0, MAJOR: 1, MINOR: 2, POLISH: 3}


def severity_rank(sev: Severity) -> int:
    """Sort key: lower is worse. Used to order the report."""
    return _SEVERITY_ORDER.get(sev, 99)


# --- The rule registry ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rule:
    """One declared rule: its id, what it means, and how bad breaking it is."""

    id: str
    title: str
    severity: Severity
    #: False for rules that are declared but not built yet. The report must say
    #: so rather than let absence read as compliance.
    implemented: bool = True
    #: Action kinds a driver must support for this rule to be checkable at all.
    #: A touch-only target has no hover, so U2 is *inapplicable* there, which is
    #: a different thing from passing.
    requires: tuple[str, ...] = ()


RULES: dict[str, Rule] = {
    r.id: r for r in [
        Rule("U1", "Every visual button has a distinct pressed state",
             MINOR, requires=("press_hold",)),
        Rule("U2", "Every interactive control has a hover state",
             MINOR, requires=("hover",)),
        Rule("U3", "Every focusable control has a visible focus indicator",
             MINOR, requires=("focus",)),
        Rule("U4", "The selected option in a group is visually distinguishable",
             MAJOR, implemented=False),
        Rule("U5", "Disabled means disabled, and inert must not look enabled",
             MAJOR),
        Rule("U6", "Input fields display what is typed",
             BLOCKER),
        Rule("U7", "No button whose press has no understood output",
             MAJOR),
        Rule("U8", "Destructive actions confirm before acting",
             MAJOR, implemented=False),
        Rule("U9", "Errors are specific and adjacent to the offending field",
             MINOR, implemented=False),
        Rule("U10", "No unintended layout shift",
             MINOR),
        Rule("U11", "Long actions show progress and resist double submission",
             MAJOR, implemented=False),
        Rule("U12", "Nothing is a dead end",
             MAJOR),
    ]
}


def rule(rule_id: str) -> Rule:
    return RULES[rule_id]


def implemented_rules() -> list[Rule]:
    return [r for r in RULES.values() if r.implemented]


# --- Findings ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    """A candidate defect. Not yet confirmed, not yet deduped, not yet a report entry."""

    rule: str
    summary: str
    observed: str
    expected: str
    state: str = ""
    element_identity: str = ""
    step_index: int = -1
    screen_id: str = ""
    severity: Severity = MINOR
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def cluster_key(self) -> tuple[str, str, str]:
        """Identity for dedup: the same rule, control and state is one defect,
        however many times the run walked into it."""
        return (self.rule, self.element_identity, self.state)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- What detectors get to see ----------------------------------------------


@dataclass(frozen=True, slots=True)
class StepContext:
    """One action and what surrounded it."""

    action: Action
    before: Observation
    after: Observation
    before_fingerprint: str
    after_fingerprint: str
    step_index: int = -1
    ok: bool = True
    duration_ms: int = 0
    #: What the model said this control appeared to promise, keyed by element
    #: identity. Written once when a state is first seen, then read for free on
    #: every later step (ACTIVE_QA.md 7).
    expectations: dict[str, str] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return self.before_fingerprint != self.after_fingerprint

    def target(self) -> Element | None:
        if not self.action.target:
            return None
        return self.before.element(self.action.target)


@dataclass(frozen=True, slots=True)
class StateProbe:
    """The result of interacting with one element to see it change appearance.

    Rules U1 to U3 are pixel questions ("does this look different while
    pressed"), so the driver answers them with a render hash and the detector
    compares. ``supported`` False means the driver cannot do this interaction at
    all, which makes the rule inapplicable rather than failed.
    """

    element_identity: str
    kind: str                       # press_hold | hover | focus
    at_rest: str | None = None      # render hash before
    during: str | None = None       # render hash during the interaction
    supported: bool = True

    @property
    def has_evidence(self) -> bool:
        return self.at_rest is not None and self.during is not None

    @property
    def changed(self) -> bool:
        return self.has_evidence and self.at_rest != self.during


@dataclass(frozen=True, slots=True)
class StateContext:
    """One state, plus whatever probes the loop ran in it."""

    observation: Observation
    fingerprint: str
    probes: tuple[StateProbe, ...] = ()
    step_index: int = -1

    def probes_of(self, kind: str) -> tuple[StateProbe, ...]:
        return tuple(p for p in self.probes if p.kind == kind)


class StepDetector(Protocol):
    rule_id: str
    def inspect(self, ctx: StepContext) -> list[Finding]: ...


class StateDetector(Protocol):
    rule_id: str
    def inspect(self, ctx: StateContext) -> list[Finding]: ...


class SessionDetector(Protocol):
    rule_id: str
    def inspect(self, coverage: CoverageMap) -> list[Finding]: ...


# --- U1 / U2 / U3: interaction states ---------------------------------------


class _RenderDeltaRule:
    """Shared machinery for "this control should look different while X"."""

    rule_id = ""
    kind = ""
    _what = ""

    def inspect(self, ctx: StateContext) -> list[Finding]:
        r = RULES[self.rule_id]
        out: list[Finding] = []
        for probe in ctx.probes_of(self.kind):
            if not probe.supported or not probe.has_evidence:
                # Inapplicable or unmeasured. Silence here is correct: the
                # report lists the rule as not-exercised rather than passed.
                continue
            if probe.changed:
                continue
            el = ctx.observation.by_identity(probe.element_identity)
            label = (el.label if el and el.label else probe.element_identity)
            out.append(Finding(
                rule=self.rule_id,
                severity=r.severity,
                summary=f'"{label}" has no {self._what}',
                observed=(f"The control renders identically at rest and while "
                          f"{self._what.replace(' state', '').replace(' indicator', '')}, "
                          f"so there is no feedback that it is interactive."),
                expected=r.title,
                state=ctx.fingerprint,
                element_identity=probe.element_identity,
                screen_id=ctx.observation.screen_id,
                step_index=ctx.step_index,
                details={"probe": self.kind, "render_hash": probe.at_rest},
            ))
        return out


class PressedStateRule(_RenderDeltaRule):
    """U1. A button that does not visibly depress gives no press feedback."""
    rule_id = "U1"
    kind = "press_hold"
    _what = "pressed state"


class HoverStateRule(_RenderDeltaRule):
    """U2. Pointer targets only; a touch-only driver simply never probes."""
    rule_id = "U2"
    kind = "hover"
    _what = "hover state"


class FocusIndicatorRule(_RenderDeltaRule):
    """U3. Also WCAG 2.4.7."""
    rule_id = "U3"
    kind = "focus"
    _what = "focus indicator"


# --- U5: disabled means disabled --------------------------------------------


class DisabledMeansDisabledRule:
    """U5, both directions.

    A control marked disabled that nonetheless changes state is the dangerous
    half: the UI says "you cannot do this" and the app does it anyway.
    """

    rule_id = "U5"

    def inspect(self, ctx: StepContext) -> list[Finding]:
        el = ctx.target()
        if el is None or el.enabled:
            return []
        if ctx.action.kind not in ("click", "type", "press_hold"):
            return []
        if not ctx.changed:
            return []
        r = RULES[self.rule_id]
        label = el.label or el.identity
        return [Finding(
            rule=self.rule_id,
            severity=r.severity,
            summary=f'Disabled control "{label}" still acted when used',
            observed=("The element reports itself disabled, but acting on it "
                      "changed the application state."),
            expected=("A disabled control must not respond. Either enable it, "
                      "or make it genuinely inert."),
            state=ctx.before_fingerprint,
            element_identity=el.identity,
            screen_id=ctx.before.screen_id,
            step_index=ctx.step_index,
            details={"action": ctx.action.kind},
        )]


# --- U6: input fields display what is typed ---------------------------------


class InputEchoRule:
    """U6. A field that silently eats input is fatal, hence BLOCKER.

    Checks the readback rather than the pixels: what the tree reports the field
    contains after typing. Truncation and mangling are caught as well as total
    silence, and each is reported with what actually landed.
    """

    rule_id = "U6"

    def inspect(self, ctx: StepContext) -> list[Finding]:
        if ctx.action.kind != "type" or not ctx.ok:
            return []
        el = ctx.target()
        if el is None:
            return []
        after = ctx.after.by_identity(el.identity)
        if after is None or after.text is None:
            # The driver cannot read this field back, so the rule is
            # unmeasurable here rather than broken.
            return []
        typed = str(ctx.action.value)
        got = after.text
        if got == typed:
            return []
        r = RULES[self.rule_id]
        label = el.label or el.identity
        if not got:
            what = "showed nothing at all"
        elif typed.startswith(got):
            what = f"showed only the first {len(got)} of {len(typed)} characters"
        else:
            what = "showed different text"
        return [Finding(
            rule=self.rule_id,
            severity=r.severity,
            summary=f'Field "{label}" does not display what was typed',
            observed=f"Typed {typed!r}; the field {what} ({got!r}).",
            expected="A field must display exactly the text entered into it.",
            state=ctx.before_fingerprint,
            element_identity=el.identity,
            screen_id=ctx.before.screen_id,
            step_index=ctx.step_index,
            details={"typed": typed, "displayed": got},
        )]


# --- U7: no button with no understood output --------------------------------


class NoOpRule:
    """U7, the marquee rule, and the one whose precision decides whether the
    whole harness is worth reading.

    Fires when an action that was supposed to do something produced no
    observable change. Three deliberate restrictions keep the false-positive
    rate survivable:

    * Only *pointer* activation counts. ``hover`` and ``focus`` are probes, and
      a hover that changes no application state is correct behaviour. ``key`` is
      excluded too: a control that ignores Enter is a keyboard-reachability
      defect for the keyboard pack, not a dead affordance.
    * A failed action is not a no-op. The driver already said why it did not
      land, and that is a different finding.
    * The expectation record, when one exists, is quoted in the finding. That is
      what turns "this button did nothing" into "this button did nothing, and
      the docs say it should deduct credits" -- the difference between a report
      a developer argues with and one they act on.

    What it cannot see is a change the fingerprint policy excludes. That is the
    correct trade: a policy that noticed everything would fire on a swaying
    boss, and a detector that fires constantly gets ignored, which is a worse
    failure than a miss.
    """

    rule_id = "U7"
    # Pointer and text activation only. `key` is deliberately absent: "Enter
    # does not activate this control" is a keyboard-reachability defect (WCAG
    # 2.1.1), which belongs to the keyboard pack, not here. Folding it in would
    # make U7 fire on every control of every app that lacks keyboard handlers
    # the moment the keyboard-only card is played, burying the real findings.
    _ACTIVATING = frozenset({"click", "type", "drag", "back"})

    def inspect(self, ctx: StepContext) -> list[Finding]:
        if ctx.action.kind not in self._ACTIVATING or not ctx.ok or ctx.changed:
            return []
        el = ctx.target()
        if el is None or not el.enabled:
            return []
        r = RULES[self.rule_id]
        label = el.label or el.identity
        promise = ctx.expectations.get(el.identity, "")
        observed = ("Acting on the control produced no observable change: same "
                    "UI state, no navigation, no message.")
        if promise:
            observed += f' Expected behaviour on record: "{promise}"'
        return [Finding(
            rule=self.rule_id,
            severity=r.severity,
            summary=f'"{label}" does nothing when used',
            observed=observed,
            expected=(promise or
                      "A control must produce a state change, a navigation, a "
                      "message, or a progress indicator."),
            state=ctx.before_fingerprint,
            element_identity=el.identity,
            screen_id=ctx.before.screen_id,
            step_index=ctx.step_index,
            details={"action": ctx.action.kind, "had_expectation": bool(promise)},
        )]


# --- U10: no unintended layout shift ----------------------------------------


class LayoutShiftRule:
    """U10. Promoted from the goal-loop-smoke QA skill, which checked exactly
    this by hand for one project.

    Only fires when the *set of controls did not change*. A drawer opening moves
    things legitimately, and that shows up as new or departed elements, so
    requiring a stable element set is what separates intentional UI from a jump.
    """

    rule_id = "U10"

    def __init__(self, tolerance_px: int = 6) -> None:
        self.tolerance_px = tolerance_px

    def inspect(self, ctx: StepContext) -> list[Finding]:
        before = {e.identity: e for e in ctx.before.elements if e.bounds}
        after = {e.identity: e for e in ctx.after.elements if e.bounds}
        if not before or set(before) != set(after):
            return []   # elements came or went: this is intentional UI
        r = RULES[self.rule_id]
        out: list[Finding] = []
        for identity, b in before.items():
            a = after[identity]
            delta = max(abs(x - y) for x, y in zip(b.bounds, a.bounds))
            if delta <= self.tolerance_px:
                continue
            label = b.label or identity
            out.append(Finding(
                rule=self.rule_id,
                severity=r.severity,
                summary=f'"{label}" moved when the state changed',
                observed=(f"Bounds went from {tuple(b.bounds)} to {tuple(a.bounds)}, "
                          f"a shift of {delta}px, with no element added or removed."),
                expected=("Primary framing must not move between states unless "
                          "intentional UI (a drawer or modal) is opening."),
                state=ctx.before_fingerprint,
                element_identity=identity,
                screen_id=ctx.before.screen_id,
                step_index=ctx.step_index,
                details={"before": list(b.bounds), "after": list(a.bounds),
                         "delta_px": delta, "tolerance_px": self.tolerance_px},
            ))
        return out


# --- U12: nothing is a dead end ---------------------------------------------


class DeadEndRule:
    """U12. A whole-session question, so it runs once at the end against the map."""

    rule_id = "U12"

    def inspect(self, coverage: CoverageMap) -> list[Finding]:
        r = RULES[self.rule_id]
        out: list[Finding] = []
        for fingerprint in coverage.dead_ends():
            node = coverage.states.get(fingerprint)
            if node is None or node.visits == 0:
                continue
            screen = node.screen_id or fingerprint[:16]
            out.append(Finding(
                rule=self.rule_id,
                severity=r.severity,
                summary=f'No way out of "{screen}"',
                observed=(f"Every action tried in this state left it unchanged "
                          f"across {node.visits} visit(s). Nothing navigated away."),
                expected="Every state needs a route out that is not a reload.",
                state=fingerprint,
                screen_id=node.screen_id,
                details={"visits": node.visits,
                         "elements_tried": sum(node.element_touches.values())},
            ))
        return out


# --- Assembly ---------------------------------------------------------------


def step_detectors() -> list[StepDetector]:
    return [DisabledMeansDisabledRule(), InputEchoRule(), NoOpRule(), LayoutShiftRule()]


def state_detectors() -> list[StateDetector]:
    return [PressedStateRule(), HoverStateRule(), FocusIndicatorRule()]


def session_detectors() -> list[SessionDetector]:
    return [DeadEndRule()]


def _filter(findings: Iterable[Finding], waived: Iterable[str]) -> list[Finding]:
    waived = set(waived)
    return [f for f in findings if f.rule not in waived]


def run_step_detectors(ctx: StepContext, *, waived: Iterable[str] = (),
                       detectors: Sequence[StepDetector] | None = None) -> list[Finding]:
    """Run every per-step rule. ``waived`` drops rules a project has excused.

    A waiver silences a rule here, but the orchestrator must still echo it in
    the report header (ACTIVE_QA.md 11.3) -- suppression has to be visible, or a
    project can quietly waive its way to a clean report.
    """
    out: list[Finding] = []
    for det in (detectors if detectors is not None else step_detectors()):
        out.extend(det.inspect(ctx))
    return _filter(out, waived)


def run_state_detectors(ctx: StateContext, *, waived: Iterable[str] = (),
                        detectors: Sequence[StateDetector] | None = None) -> list[Finding]:
    out: list[Finding] = []
    for det in (detectors if detectors is not None else state_detectors()):
        out.extend(det.inspect(ctx))
    return _filter(out, waived)


def run_session_detectors(coverage: CoverageMap, *, waived: Iterable[str] = (),
                          detectors: Sequence[SessionDetector] | None = None) -> list[Finding]:
    out: list[Finding] = []
    for det in (detectors if detectors is not None else session_detectors()):
        out.extend(det.inspect(coverage))
    return _filter(out, waived)
