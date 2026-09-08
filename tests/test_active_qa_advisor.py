"""Tests for the model advisor.

The failure behaviour matters more than the happy path, and the two directions
are deliberately opposite: orient fails empty (a worse run), judge fails open (a
kept candidate). Getting judge backwards would lose real defects to an HTTP
timeout, silently.
"""

from __future__ import annotations

import json

import pytest

from pandabot_core.active_qa import (
    Action, Budgets, Deck, Element, ExplorationLoop, FakeDriver, FakeScreen,
    Observation,
)
from pandabot_core.active_qa.advisor import (
    AdvisorStats, LocalAdvisor, describe_observation,
)
from pandabot_core.active_qa.rules import Finding, StepContext


class FakeChat:
    """A scriptable stand-in for the chat endpoint."""

    def __init__(self, *answers: str, fail: Exception | None = None) -> None:
        self.answers = list(answers)
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.fail is not None:
            raise self.fail
        return self.answers.pop(0) if self.answers else "{}"


def _obs(*elements: Element, **kw) -> Observation:
    return Observation(elements=elements or (
        Element(id="e1", role="button", label="Confirm", handle="#confirm"),
        Element(id="e2", role="textbox", label="Name", handle="#name", text=""),
    ), **kw)


def _finding(**kw) -> Finding:
    kw.setdefault("rule", "U7")
    kw.setdefault("summary", '"Confirm" does nothing when used')
    kw.setdefault("observed", "No observable change.")
    kw.setdefault("expected", "A control must produce a state change.")
    kw.setdefault("element_identity", "h:#confirm")
    return Finding(**kw)


def _ctx() -> StepContext:
    obs = _obs()
    return StepContext(action=Action(kind="click", target="e1"),
                       before=obs, after=obs,
                       before_fingerprint="a", after_fingerprint="a")


# --- Prompt building ------------------------------------------------------

def test_a_screen_renders_as_compact_text():
    """Text, not a screenshot: a vision turn costs several times as much."""
    text = describe_observation(_obs(screen_id="home", text="Welcome"))
    assert "Screen: home" in text
    assert "h:#confirm" in text and 'labelled "Confirm"' in text
    assert "Welcome" in text


def test_disabled_and_filled_controls_are_marked():
    text = describe_observation(_obs(
        Element(id="e1", role="button", label="Go", handle="#go", enabled=False),
        Element(id="e2", role="textbox", handle="#n", text="abc")))
    assert "DISABLED" in text and "'abc'" in text


def test_a_long_screen_says_it_was_truncated():
    """An orient that saw half the screen must not read as one that saw it all."""
    els = tuple(Element(id=f"e{i}", role="button", handle=f"#b{i}") for i in range(80))
    text = describe_observation(_obs(*els), max_elements=10)
    assert "showing 10 of 80 controls" in text


# --- Orient ---------------------------------------------------------------

def test_orient_returns_expectations_and_curiosity():
    chat = FakeChat(json.dumps({
        "screen": "checkout", "notes": "A checkout screen.",
        "expectations": {"h:#confirm": "should place the order and show a receipt"},
        "curiosity": {"h:#confirm": 0.8}}))
    o = LocalAdvisor(chat).orient(_obs(), "fp")
    assert o.expectations == {"h:#confirm": "should place the order and show a receipt"}
    assert o.curiosity == {"h:#confirm": 0.8}
    assert o.notes == "A checkout screen."


def test_orient_drops_expectations_for_elements_that_do_not_exist():
    """A hallucinated id would attach a promise to a control that is not there,
    and the harness would then report against it."""
    chat = FakeChat(json.dumps({"expectations": {
        "h:#confirm": "real", "h:#invented": "hallucinated"}}))
    o = LocalAdvisor(chat).orient(_obs(), "fp")
    assert set(o.expectations) == {"h:#confirm"}


def test_orient_clamps_curiosity_and_ignores_nonsense():
    chat = FakeChat(json.dumps({"curiosity": {
        "h:#confirm": 5.0, "h:#name": "very"}}))
    o = LocalAdvisor(chat).orient(_obs(), "fp")
    assert o.curiosity == {"h:#confirm": 1.0}


def test_orient_survives_prose_around_the_json():
    chat = FakeChat('Sure! Here is the analysis:\n```json\n'
                    '{"notes": "a form", "expectations": {}}\n```\nHope that helps.')
    assert LocalAdvisor(chat).orient(_obs(), "fp").notes == "a form"


def test_orient_ignores_a_thinking_block():
    """A thinking model emits reasoning first; the answer is the last object."""
    chat = FakeChat('<think>{"expectations": {"h:#confirm": "draft, discard me"}}'
                    '</think>{"expectations": {"h:#confirm": "the real answer"}}')
    o = LocalAdvisor(chat).orient(_obs(), "fp")
    assert o.expectations["h:#confirm"] == "the real answer"


def test_orient_fails_empty_when_the_model_errors():
    """No expectations is a worse run, not a broken one."""
    advisor = LocalAdvisor(FakeChat(fail=RuntimeError("connection refused")))
    o = advisor.orient(_obs(), "fp")
    assert o.expectations == {} and o.curiosity == {}
    assert advisor.stats.failures == 1


def test_orient_fails_empty_on_unparseable_output():
    advisor = LocalAdvisor(FakeChat("I'm afraid I can't do that."))
    assert advisor.orient(_obs(), "fp").expectations == {}
    assert advisor.stats.failures == 1


def test_orient_skips_a_blind_observation():
    """Nothing to describe and no ids to hang expectations on."""
    chat = FakeChat()
    LocalAdvisor(chat).orient(Observation(), "fp")
    assert chat.calls == []


def test_project_notes_are_marked_authoritative():
    chat = FakeChat("{}")
    LocalAdvisor(chat, project_context="Oxygen falls when crew exceed life support."
                 ).orient(_obs(), "fp")
    _system, user = chat.calls[0]
    assert "authoritative" in user
    assert "Oxygen falls" in user


# --- Judge ----------------------------------------------------------------

def test_judge_keeps_a_real_defect():
    chat = FakeChat(json.dumps({"verdict": "defect", "why": "nothing happened"}))
    assert LocalAdvisor(chat).judge(_finding(), _ctx()) is True


def test_judge_can_dismiss_expected_behaviour():
    chat = FakeChat(json.dumps({"verdict": "not a defect",
                                "why": "the notes say this is the mechanic"}))
    advisor = LocalAdvisor(chat)
    assert advisor.judge(_finding(), _ctx()) is False
    assert advisor.stats.dismissed == 1


def test_judge_fails_open_when_the_model_errors():
    """The direction that matters. Discarding a real defect because an HTTP call
    failed is far worse than carrying a candidate into triage, which replays it
    anyway."""
    advisor = LocalAdvisor(FakeChat(fail=TimeoutError("took too long")))
    assert advisor.judge(_finding(), _ctx()) is True
    assert advisor.stats.failures == 1


def test_judge_fails_open_on_unparseable_output():
    advisor = LocalAdvisor(FakeChat("hmm, hard to say"))
    assert advisor.judge(_finding(), _ctx()) is True


def test_judge_fails_open_on_an_unrecognised_verdict():
    advisor = LocalAdvisor(FakeChat(json.dumps({"verdict": "maybe"})))
    assert advisor.judge(_finding(), _ctx()) is True


def test_judge_can_be_disabled_entirely():
    chat = FakeChat()
    advisor = LocalAdvisor(chat, enable_judge=False)
    assert advisor.judge(_finding(), _ctx()) is True
    assert chat.calls == [], "no model call should be made"


def test_the_judge_prompt_carries_the_evidence():
    chat = FakeChat("{}")
    LocalAdvisor(chat).judge(_finding(), _ctx())
    _system, user = chat.calls[0]
    assert "U7" in user and "does nothing" in user
    assert "click on e1" in user
    assert "State changed: no" in user


# --- Stats ----------------------------------------------------------------

def test_stats_count_both_kinds_of_call():
    chat = FakeChat("{}", "{}", "{}")
    advisor = LocalAdvisor(chat)
    advisor.orient(_obs(), "fp")
    advisor.judge(_finding(), _ctx())
    advisor.judge(_finding(), _ctx())
    s = advisor.stats.to_dict()
    assert s["orient_calls"] == 1 and s["judge_calls"] == 2
    assert s["prompt_chars"] > 0


# --- In the loop ----------------------------------------------------------

def _app():
    home = FakeScreen("home", [
        Element(id="e1", role="button", label="Open", handle="#open"),
        Element(id="e2", role="button", label="Dead", handle="#dead"),
    ])
    panel = FakeScreen("panel", [Element(id="e1", role="button", label="Back", handle="#back")])
    home.transitions[("h:#open", "click")] = "panel"
    panel.transitions[("h:#back", "click")] = "home"
    return FakeDriver({"home": home, "panel": panel}, entry="home",
                      actions=frozenset({"click"}))


def test_expectations_reach_the_finding_through_a_real_advisor():
    """End to end: the model's promise becomes the finding's Expected field,
    which is what makes U7 actionable rather than merely true."""
    chat = FakeChat(*[json.dumps({
        "notes": "the home screen",
        "expectations": {"h:#dead": "should open the settings panel"},
    })] * 8)
    driver = _app()
    result = ExplorationLoop(driver, advisor=LocalAdvisor(chat),
                             budgets=Budgets(min_seconds=0, max_actions=40),
                             deck=Deck(cards=()), clock=lambda: 0.0).run()
    u7 = [f for f in result.findings if f.rule == "U7"]
    assert u7, "the planted dead control should still be found"
    assert u7[0].expected == "should open the settings panel"
    assert "should open the settings panel" in u7[0].observed


def test_a_dead_model_does_not_stop_the_run():
    """The loop must survive the model going away mid-session -- the GPU can be
    unloaded out from under it."""
    driver = _app()
    advisor = LocalAdvisor(FakeChat(fail=ConnectionError("gone")))
    result = ExplorationLoop(driver, advisor=advisor,
                             budgets=Budgets(min_seconds=0, max_actions=40),
                             deck=Deck(cards=()), clock=lambda: 0.0).run()
    assert result.stats.actions > 0
    assert [f for f in result.findings if f.rule == "U7"], \
        "detection is mechanical and must not depend on the model"
