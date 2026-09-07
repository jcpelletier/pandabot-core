"""Tests for the universal UX rulebook.

Each rule is tested in both directions: it fires on the defect, and it stays
quiet on the correct behaviour. The second half matters more than the first --
these detectors run on every step of a multi-thousand-action session, so a rule
that fires spuriously buries the real findings and the report stops being read.
"""

from __future__ import annotations

import pytest

from pandabot_core.active_qa import (
    Action, CoverageMap, Element, FakeDriver, FakeScreen, Observation,
)
from pandabot_core.active_qa.coverage import Candidate
from pandabot_core.active_qa.rules import (
    BLOCKER, MAJOR, MINOR, RULES,
    DeadEndRule, DisabledMeansDisabledRule, Finding, FocusIndicatorRule,
    HoverStateRule, InputEchoRule, LayoutShiftRule, NoOpRule, PressedStateRule,
    StateContext, StateProbe, StepContext,
    implemented_rules, rule, run_session_detectors, run_state_detectors,
    run_step_detectors, severity_rank,
)


def _obs(*elements: Element, screen_id: str = "home") -> Observation:
    return Observation(elements=elements, screen_id=screen_id)


def _btn(name="go", **kw) -> Element:
    kw.setdefault("role", "button")
    kw.setdefault("label", name.title())
    return Element(id=f"e_{name}", handle=f"#{name}", **kw)


def _step(action, before, after, *, before_fp="fp:a", after_fp=None, **kw) -> StepContext:
    return StepContext(
        action=action, before=before, after=after,
        before_fingerprint=before_fp,
        after_fingerprint=after_fp if after_fp is not None else before_fp,
        **kw,
    )


# --- The registry ---------------------------------------------------------

def test_all_twelve_rules_are_declared():
    assert [r.id for r in RULES.values()] == [f"U{n}" for n in range(1, 13)]


def test_unimplemented_rules_are_declared_but_flagged():
    """Absence must not read as compliance in the report."""
    pending = {r.id for r in RULES.values() if not r.implemented}
    assert pending == {"U4", "U8", "U9", "U11"}
    assert {r.id for r in implemented_rules()}.isdisjoint(pending)


def test_input_echo_is_the_most_severe_rule():
    """A field that eats input is fatal; a missing hover state is not."""
    assert rule("U6").severity == BLOCKER
    assert severity_rank(rule("U6").severity) < severity_rank(rule("U2").severity)


def test_rules_declare_the_actions_they_need():
    assert rule("U1").requires == ("press_hold",)
    assert rule("U7").requires == ()


# --- U1 / U2 / U3: interaction states -------------------------------------

def test_u1_fires_when_a_button_does_not_depress():
    ctx = StateContext(
        observation=_obs(_btn("go")), fingerprint="fp:a",
        probes=(StateProbe("h:#go", "press_hold", at_rest="px:1", during="px:1"),),
    )
    findings = PressedStateRule().inspect(ctx)
    assert len(findings) == 1
    assert findings[0].rule == "U1" and "pressed state" in findings[0].summary
    assert findings[0].element_identity == "h:#go"


def test_u1_is_quiet_when_the_button_depresses():
    ctx = StateContext(
        observation=_obs(_btn("go")), fingerprint="fp:a",
        probes=(StateProbe("h:#go", "press_hold", at_rest="px:1", during="px:2"),),
    )
    assert PressedStateRule().inspect(ctx) == []


def test_u2_is_silent_when_the_driver_cannot_hover():
    """A touch-only target makes U2 inapplicable, which is not the same as
    passing and not the same as failing."""
    ctx = StateContext(
        observation=_obs(_btn("go")), fingerprint="fp:a",
        probes=(StateProbe("h:#go", "hover", supported=False),),
    )
    assert HoverStateRule().inspect(ctx) == []


def test_a_probe_with_no_evidence_does_not_fire():
    """Unmeasured must never be reported as failed."""
    ctx = StateContext(
        observation=_obs(_btn("go")), fingerprint="fp:a",
        probes=(StateProbe("h:#go", "focus", at_rest=None, during=None),),
    )
    assert FocusIndicatorRule().inspect(ctx) == []


def test_each_render_rule_only_reads_its_own_probe_kind():
    ctx = StateContext(
        observation=_obs(_btn("go")), fingerprint="fp:a",
        probes=(
            StateProbe("h:#go", "press_hold", at_rest="a", during="a"),
            StateProbe("h:#go", "hover", at_rest="a", during="b"),
        ),
    )
    assert [f.rule for f in PressedStateRule().inspect(ctx)] == ["U1"]
    assert HoverStateRule().inspect(ctx) == []


def test_findings_use_the_element_label_not_its_identity():
    ctx = StateContext(
        observation=_obs(_btn("go", label="Confirm order")), fingerprint="fp:a",
        probes=(StateProbe("h:#go", "press_hold", at_rest="a", during="a"),),
    )
    assert "Confirm order" in PressedStateRule().inspect(ctx)[0].summary


# --- U5: disabled means disabled ------------------------------------------

def test_u5_fires_when_a_disabled_control_still_acts():
    el = _btn("go", enabled=False)
    ctx = _step(Action(kind="click", target="e_go"), _obs(el), _obs(el),
                before_fp="fp:a", after_fp="fp:b")
    findings = DisabledMeansDisabledRule().inspect(ctx)
    assert len(findings) == 1 and findings[0].rule == "U5"
    assert findings[0].severity == MAJOR


def test_u5_is_quiet_when_a_disabled_control_is_inert():
    el = _btn("go", enabled=False)
    ctx = _step(Action(kind="click", target="e_go"), _obs(el), _obs(el))
    assert DisabledMeansDisabledRule().inspect(ctx) == []


def test_u5_ignores_enabled_controls():
    el = _btn("go")
    ctx = _step(Action(kind="click", target="e_go"), _obs(el), _obs(el),
                before_fp="fp:a", after_fp="fp:b")
    assert DisabledMeansDisabledRule().inspect(ctx) == []


# --- U6: input echo -------------------------------------------------------

def _field(text="", **kw) -> Element:
    return Element(id="e_name", role="textbox", label="Name", handle="#name",
                   text=text, **kw)


def test_u6_fires_when_a_field_swallows_input():
    ctx = _step(Action(kind="type", target="e_name", value="hello"),
                _obs(_field("")), _obs(_field("")))
    findings = InputEchoRule().inspect(ctx)
    assert len(findings) == 1
    assert findings[0].rule == "U6" and findings[0].severity == BLOCKER
    assert "nothing at all" in findings[0].observed


def test_u6_reports_truncation_with_the_counts():
    ctx = _step(Action(kind="type", target="e_name", value="abcdefgh"),
                _obs(_field("")), _obs(_field("abc")))
    observed = InputEchoRule().inspect(ctx)[0].observed
    assert "first 3 of 8 characters" in observed


def test_u6_reports_mangled_text():
    ctx = _step(Action(kind="type", target="e_name", value="hello"),
                _obs(_field("")), _obs(_field("HELLO")))
    assert "different text" in InputEchoRule().inspect(ctx)[0].observed


def test_u6_is_quiet_when_the_field_echoes():
    ctx = _step(Action(kind="type", target="e_name", value="hello"),
                _obs(_field("")), _obs(_field("hello")))
    assert InputEchoRule().inspect(ctx) == []


def test_u6_does_not_fire_on_an_unreadable_field():
    """text=None means the driver cannot read it back: unmeasurable, not broken."""
    ctx = _step(Action(kind="type", target="e_name", value="hello"),
                _obs(_field(None)), _obs(_field(None)))
    assert InputEchoRule().inspect(ctx) == []


def test_u6_ignores_non_typing_actions():
    ctx = _step(Action(kind="click", target="e_name"), _obs(_field()), _obs(_field()))
    assert InputEchoRule().inspect(ctx) == []


def test_u6_ignores_an_action_that_failed_to_land():
    ctx = _step(Action(kind="type", target="e_name", value="x"),
                _obs(_field("")), _obs(_field("")), ok=False)
    assert InputEchoRule().inspect(ctx) == []


# --- U7: no-op ------------------------------------------------------------

def test_u7_fires_on_a_dead_control():
    el = _btn("dead", label="Confirm")
    ctx = _step(Action(kind="click", target="e_dead"), _obs(el), _obs(el))
    findings = NoOpRule().inspect(ctx)
    assert len(findings) == 1
    assert findings[0].rule == "U7" and "does nothing" in findings[0].summary


def test_u7_is_quiet_when_something_changed():
    el = _btn("go")
    ctx = _step(Action(kind="click", target="e_go"), _obs(el), _obs(el),
                before_fp="fp:a", after_fp="fp:b")
    assert NoOpRule().inspect(ctx) == []


def test_u7_ignores_probe_actions():
    """A hover that changes no application state is correct, not a defect. This
    is the single biggest false-positive source if you get it wrong: the loop
    hovers and focuses constantly to feed U1 to U3."""
    el = _btn("go")
    for kind in ("hover", "focus", "press_hold"):
        ctx = _step(Action(kind=kind, target="e_go"), _obs(el), _obs(el))
        assert NoOpRule().inspect(ctx) == [], kind


def test_u7_ignores_actions_that_failed_to_land():
    el = _btn("go")
    ctx = _step(Action(kind="click", target="e_go"), _obs(el), _obs(el), ok=False)
    assert NoOpRule().inspect(ctx) == []


def test_u7_ignores_disabled_controls():
    """A disabled button doing nothing is correct. U5 owns the other direction."""
    el = _btn("go", enabled=False)
    ctx = _step(Action(kind="click", target="e_go"), _obs(el), _obs(el))
    assert NoOpRule().inspect(ctx) == []


def test_u7_quotes_the_expectation_when_one_exists():
    """What turns "this did nothing" into something a developer acts on."""
    el = _btn("confirm", label="Confirm")
    ctx = _step(Action(kind="click", target="e_confirm"), _obs(el), _obs(el),
                expectations={"h:#confirm": "should close the panel and deduct credits"})
    finding = NoOpRule().inspect(ctx)[0]
    assert "deduct credits" in finding.observed
    assert finding.expected == "should close the panel and deduct credits"
    assert finding.details["had_expectation"] is True


def test_u7_still_fires_without_an_expectation():
    el = _btn("go")
    ctx = _step(Action(kind="click", target="e_go"), _obs(el), _obs(el))
    finding = NoOpRule().inspect(ctx)[0]
    assert finding.details["had_expectation"] is False
    assert "state change" in finding.expected


# --- U10: layout shift ----------------------------------------------------

def test_u10_fires_when_a_stable_element_moves():
    before = _btn("go", bounds=(0, 0, 100, 40))
    after = _btn("go", bounds=(0, 60, 100, 100))
    ctx = _step(Action(kind="click", target="e_go"), _obs(before), _obs(after),
                before_fp="fp:a", after_fp="fp:b")
    findings = LayoutShiftRule().inspect(ctx)
    assert len(findings) == 1 and findings[0].rule == "U10"
    assert findings[0].details["delta_px"] == 60


def test_u10_tolerates_sub_pixel_jitter():
    before = _btn("go", bounds=(0, 0, 100, 40))
    after = _btn("go", bounds=(2, 1, 102, 41))
    ctx = _step(Action(kind="click", target="e_go"), _obs(before), _obs(after))
    assert LayoutShiftRule().inspect(ctx) == []


def test_u10_ignores_intentional_ui():
    """A drawer opening adds elements, which is how deliberate movement is
    distinguished from a jump."""
    before = _btn("go", bounds=(0, 0, 100, 40))
    moved = _btn("go", bounds=(0, 200, 100, 240))
    drawer = _btn("close", bounds=(0, 0, 100, 40))
    ctx = _step(Action(kind="click", target="e_go"),
                _obs(before), _obs(moved, drawer),
                before_fp="fp:a", after_fp="fp:b")
    assert LayoutShiftRule().inspect(ctx) == []


def test_u10_tolerance_is_configurable():
    before = _btn("go", bounds=(0, 0, 100, 40))
    after = _btn("go", bounds=(0, 20, 100, 60))
    ctx = _step(Action(kind="click", target="e_go"), _obs(before), _obs(after))
    assert LayoutShiftRule(tolerance_px=30).inspect(ctx) == []
    assert LayoutShiftRule(tolerance_px=5).inspect(ctx) != []


def test_u10_needs_bounds():
    el = _btn("go")
    ctx = _step(Action(kind="click", target="e_go"), _obs(el), _obs(el))
    assert LayoutShiftRule().inspect(ctx) == []


# --- U12: dead ends -------------------------------------------------------

def test_u12_fires_on_a_state_with_no_exit():
    m = CoverageMap()
    for fp in ("a", "trap"):
        m.observe(_obs(_btn("x")), fp)
    m.record_transition("a", Candidate(state="a", element_identity="h:#x", kind="click"), "trap")
    m.record_transition("trap", Candidate(state="trap", element_identity="h:#x", kind="click"), "trap")
    findings = DeadEndRule().inspect(m)
    assert [f.state for f in findings] == ["trap"]
    assert findings[0].rule == "U12"


def test_u12_is_quiet_when_every_state_has_an_exit():
    m = CoverageMap()
    for fp in ("a", "b"):
        m.observe(_obs(_btn("x")), fp)
    m.record_transition("a", Candidate(state="a", element_identity="h:#x", kind="click"), "b")
    m.record_transition("b", Candidate(state="b", element_identity="h:#x", kind="click"), "a")
    assert DeadEndRule().inspect(m) == []


# --- Findings and assembly ------------------------------------------------

def test_findings_cluster_by_rule_element_and_state():
    """Walking into the same defect ten times is one finding, not ten."""
    a = Finding(rule="U7", summary="x", observed="", expected="",
                element_identity="h:#go", state="fp:a")
    b = Finding(rule="U7", summary="y", observed="", expected="",
                element_identity="h:#go", state="fp:a", step_index=99)
    c = Finding(rule="U7", summary="x", observed="", expected="",
                element_identity="h:#other", state="fp:a")
    assert a.cluster_key == b.cluster_key
    assert a.cluster_key != c.cluster_key


def test_run_step_detectors_gathers_every_rule():
    el = _btn("dead", enabled=False, bounds=(0, 0, 10, 10))
    after = _btn("dead", enabled=False, bounds=(0, 90, 10, 100))
    ctx = _step(Action(kind="click", target="e_dead"), _obs(el), _obs(after),
                before_fp="fp:a", after_fp="fp:b")
    rules = {f.rule for f in run_step_detectors(ctx)}
    assert rules == {"U5", "U10"}


def test_waivers_silence_a_rule():
    el = _btn("dead")
    ctx = _step(Action(kind="click", target="e_dead"), _obs(el), _obs(el))
    assert run_step_detectors(ctx) != []
    assert run_step_detectors(ctx, waived=["U7"]) == []


def test_waivers_apply_to_state_and_session_detectors_too():
    state_ctx = StateContext(
        observation=_obs(_btn("go")), fingerprint="fp:a",
        probes=(StateProbe("h:#go", "press_hold", at_rest="a", during="a"),),
    )
    assert run_state_detectors(state_ctx, waived=["U1"]) == []

    m = CoverageMap()
    m.observe(_obs(_btn("x")), "trap")
    m.record_transition("trap", Candidate(state="trap", element_identity="h:#x", kind="click"), "trap")
    assert run_session_detectors(m, waived=["U12"]) == []


# --- Against the fake driver ----------------------------------------------

def test_a_planted_dead_control_is_caught_end_to_end():
    """Construct the defect on purpose, drive it, and confirm exactly one rule
    fires -- the measurable-precision property the walking skeleton exists for."""
    home = FakeScreen("home", [
        Element(id="e1", role="button", label="Works", handle="#works"),
        Element(id="e2", role="button", label="Dead", handle="#dead"),
    ])
    other = FakeScreen("other", [Element(id="e1", role="button", handle="#back")])
    home.transitions[("h:#works", "click")] = "other"

    d = FakeDriver({"home": home, "other": other}, entry="home")
    findings: list[Finding] = []
    for target, expect_finding in (("e2", True), ("e1", False)):
        before = d.observe()
        action = Action(kind="click", target=target)
        result = d.act(action)
        after = d.observe()
        got = run_step_detectors(_step(
            action, before, after,
            before_fp=before.compute_fingerprint(),
            after_fp=after.compute_fingerprint(),
            ok=result.ok,
        ))
        assert bool(got) is expect_finding
        findings.extend(got)

    assert [f.rule for f in findings] == ["U7"]
    assert findings[0].element_identity == "h:#dead"


def test_a_planted_swallowing_field_is_caught_end_to_end():
    home = FakeScreen("home", [
        Element(id="e1", role="textbox", label="Name", handle="#name", text=""),
    ])
    d = FakeDriver({"home": home}, entry="home")
    d.echo_typing = False

    before = d.observe()
    action = Action(kind="type", target="e1", value="hello")
    result = d.act(action)
    after = d.observe()
    findings = run_step_detectors(_step(
        action, before, after,
        before_fp=before.compute_fingerprint(),
        after_fp=after.compute_fingerprint(),
        ok=result.ok,
    ))
    assert [f.rule for f in findings] == ["U6", "U7"]
    assert findings[0].severity == BLOCKER


def test_no_findings_on_a_well_behaved_app():
    """The precision half. A correct app must produce a silent run."""
    home = FakeScreen("home", [
        Element(id="e1", role="button", label="Open", handle="#open"),
        Element(id="e2", role="textbox", label="Name", handle="#name", text=""),
    ])
    panel = FakeScreen("panel", [Element(id="e1", role="button", handle="#back")])
    home.transitions[("h:#open", "click")] = "panel"
    panel.transitions[("h:#back", "click")] = "home"

    d = FakeDriver({"home": home, "panel": panel}, entry="home")
    plan = [
        Action(kind="type", target="e2", value="abc"),
        Action(kind="click", target="e1"),
        Action(kind="click", target="e1"),
    ]
    findings: list[Finding] = []
    for action in plan:
        before = d.observe()
        result = d.act(action)
        after = d.observe()
        findings.extend(run_step_detectors(_step(
            action, before, after,
            before_fp=before.compute_fingerprint(),
            after_fp=after.compute_fingerprint(),
            ok=result.ok,
        )))
    assert findings == []
