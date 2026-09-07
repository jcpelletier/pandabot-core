"""Tests for the exploration loop and the strategy deck.

The loop is driven with an injected clock throughout, so a "two hour run" is a
few milliseconds of test and the budget logic is tested for real rather than
approximated.
"""

from __future__ import annotations

import pytest

from pandabot_core.active_qa import Element, FakeDriver, FakeScreen, Observation
from pandabot_core.active_qa.loop import (
    Advisor, Budgets, ExplorationLoop, LoopStats, Orientation, StopReason,
)
from pandabot_core.active_qa.rules import Finding
from pandabot_core.active_qa.strategies import (
    DECK, Card, Deck, boundary_values, empty_and_blank, idle, implemented_cards,
    keyboard_only, long_and_weird, rapid_fire,
)


class FakeClock:
    """A clock the test advances by hand, so budgets are exercised honestly."""

    def __init__(self, step: float = 0.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _obs(*elements: Element) -> Observation:
    return Observation(elements=elements, screen_id="home")


def _field(name="name") -> Element:
    return Element(id=f"e_{name}", role="textbox", label="Name", handle=f"#{name}", text="")


def _btn(name="go") -> Element:
    return Element(id=f"e_{name}", role="button", label=name.title(), handle=f"#{name}")


def _two_screen_app(**kw) -> FakeDriver:
    home = FakeScreen("home", [_btn("open"), _btn("dead")])
    panel = FakeScreen("panel", [_btn("back")])
    home.transitions[("h:#open", "click")] = "panel"
    panel.transitions[("h:#back", "click")] = "home"
    return FakeDriver({"home": home, "panel": panel}, entry="home",
                      actions=frozenset({"click"}), **kw)


# --- The deck -------------------------------------------------------------

def test_boundary_values_covers_the_edges():
    values = {c.value for c in boundary_values(_obs(_field()), "fp")}
    assert {"0", "-1", "0.0001"} <= values
    assert any(len(v) > 15 for v in values)     # past 64-bit


def test_boundary_values_needs_a_text_field():
    assert boundary_values(_obs(_btn()), "fp") == []


def test_empty_and_blank_also_tries_submitting_afterwards():
    """Clearing a field is only half the experiment."""
    cands = empty_and_blank(_obs(_field(), _btn("submit")), "fp")
    assert "" in {c.value for c in cands if c.kind == "type"}
    assert any(c.kind == "click" for c in cands)


def test_long_and_weird_covers_the_usual_suspects():
    values = [str(c.value) for c in long_and_weird(_obs(_field()), "fp")]
    assert any(len(v) >= 10_000 for v in values)
    assert any("script" in v for v in values)
    assert any("\n" in v for v in values)


def test_rapid_fire_produces_distinct_candidates():
    """Each repeat must be its own candidate or the frontier dedups them away."""
    cands = rapid_fire(_obs(_btn()), "fp", repeats=4)
    assert len({c.key for c in cands}) == len(cands) == 3


def test_keyboard_only_probes_focus_and_activation():
    kinds = {c.kind for c in keyboard_only(_obs(_btn(), _field()), "fp")}
    assert kinds == {"focus", "key"}


def test_idle_waits_in_milliseconds():
    card = idle(_obs(_btn()), "fp", seconds=60)[0]
    assert card.kind == "wait" and card.value == 60_000


def test_cards_are_tagged_with_their_source():
    assert all(c.source == "boundary values" for c in boundary_values(_obs(_field()), "fp"))


def test_unimplemented_cards_are_declared_not_hidden():
    pending = {c.name for c in DECK if not c.implemented}
    assert pending == {"denial", "round trip", "out of order"}
    assert pending.isdisjoint({c.name for c in implemented_cards()})


def test_deck_deals_each_card_once_per_state():
    deck = Deck()
    obs = _obs(_field(), _btn())
    seen = []
    while True:
        card, cands = deck.play(obs, "fp")
        if card is None:
            break
        seen.append(card.name)
    assert len(seen) == len(set(seen))
    assert deck.exhausted("fp")


def test_deck_skips_cards_that_yield_nothing_here():
    """A screen with no text fields should not waste a stall on boundary values."""
    deck = Deck()
    card, cands = deck.play(_obs(_btn()), "fp")
    assert cands, "a card that deals nothing should be skipped past"
    assert card.name != "boundary values"


def test_deck_is_per_state():
    deck = Deck()
    deck.play(_obs(_field()), "fp1")
    assert not deck.exhausted("fp2")


def test_deck_reports_what_it_played():
    deck = Deck()
    deck.play(_obs(_field(), _btn()), "fp")
    assert deck.played_cards()


# --- Budgets --------------------------------------------------------------

def test_loop_stops_at_the_action_budget():
    loop = ExplorationLoop(_two_screen_app(), budgets=Budgets(
        min_seconds=0, max_actions=5), clock=FakeClock())
    result = loop.run()
    assert result.stop_reason == StopReason.BUDGET_ACTIONS
    assert result.stats.actions >= 5


def test_loop_stops_at_the_time_budget():
    loop = ExplorationLoop(_two_screen_app(), budgets=Budgets(
        min_seconds=0, max_seconds=1.0, max_actions=10_000),
        clock=FakeClock(step=0.1))
    result = loop.run()
    assert result.stop_reason == StopReason.BUDGET_TIME


def test_loop_stops_when_exhausted_past_the_floor():
    loop = ExplorationLoop(_two_screen_app(), budgets=Budgets(min_seconds=0),
                           deck=Deck(cards=()), clock=FakeClock())
    result = loop.run()
    assert result.stop_reason == StopReason.EXHAUSTED


def test_running_dry_below_the_floor_does_not_stop_the_run():
    """The brief is to keep pressing. Exhausting the frontier early must lead to
    cards and relaunches, not an early finish."""
    driver = _two_screen_app()
    loop = ExplorationLoop(driver, budgets=Budgets(
        min_seconds=1000.0, max_seconds=100_000, max_actions=200,
        max_barren_relaunches=2), clock=FakeClock(step=1.0))
    result = loop.run()
    assert result.stop_reason != StopReason.EXHAUSTED
    assert driver.relaunches > 0 or result.stats.cards_played > 0


def test_operator_stop_is_honoured():
    driver = _two_screen_app()
    loop = ExplorationLoop(driver, budgets=Budgets(min_seconds=10_000),
                           clock=FakeClock())
    loop.stop()
    result = loop.run()
    assert result.stop_reason == StopReason.STOPPED


def test_pause_is_distinguished_from_stop():
    """The model being unloaded pauses a run; it is not the operator stopping it."""
    loop = ExplorationLoop(_two_screen_app(), budgets=Budgets(min_seconds=10_000),
                           clock=FakeClock())
    loop.pause()
    assert loop.run().stop_reason == StopReason.PAUSED


# --- Exploration behaviour ------------------------------------------------

def test_loop_discovers_every_reachable_state():
    loop = ExplorationLoop(_two_screen_app(), budgets=Budgets(
        min_seconds=0, max_actions=100), deck=Deck(cards=()), clock=FakeClock())
    result = loop.run()
    assert {n.screen_id for n in result.coverage.states.values()} == {"home", "panel"}


def test_loop_finds_the_planted_dead_control():
    loop = ExplorationLoop(_two_screen_app(), budgets=Budgets(
        min_seconds=0, max_actions=100), deck=Deck(cards=()), clock=FakeClock())
    result = loop.run()
    u7 = [f for f in result.findings if f.rule == "U7"]
    assert [f.element_identity for f in u7] == ["h:#dead"]


def test_a_well_behaved_app_produces_no_findings():
    """The precision half, at loop level."""
    home = FakeScreen("home", [_btn("open")])
    panel = FakeScreen("panel", [_btn("back")])
    home.transitions[("h:#open", "click")] = "panel"
    panel.transitions[("h:#back", "click")] = "home"
    driver = FakeDriver({"home": home, "panel": panel}, entry="home",
                        actions=frozenset({"click"}))
    loop = ExplorationLoop(driver, budgets=Budgets(min_seconds=0, max_actions=50),
                           deck=Deck(cards=()), clock=FakeClock())
    assert loop.run().findings == []


def test_every_action_is_recorded_from_a_launch():
    loop = ExplorationLoop(_two_screen_app(), budgets=Budgets(
        min_seconds=0, max_actions=20), deck=Deck(cards=()), clock=FakeClock())
    result = loop.run()
    assert len(result.recorder) > 0
    for step in result.recorder:
        trace = result.recorder.trace_for(step.index)
        assert trace.steps[0].epoch == step.epoch
        assert trace.target.index == step.index


def test_crash_is_recorded_and_the_app_relaunched():
    home = FakeScreen("home", [_btn("go")])
    driver = FakeDriver({"home": home}, entry="home",
                        actions=frozenset({"click"}), crash_after=0)
    loop = ExplorationLoop(driver, budgets=Budgets(min_seconds=0, max_actions=20),
                           deck=Deck(cards=()), clock=FakeClock())
    result = loop.run()
    assert any(f.rule == "crash" for f in result.findings)
    assert driver.relaunches > 0


def test_ambient_motion_does_not_manufacture_states():
    """The risk-2 guarantee at loop level: a never-still app must still resolve
    to a small number of states, not one per observation."""
    from pandabot_core.active_qa import ambient_noise
    home = FakeScreen("home", [_btn("open")])
    panel = FakeScreen("panel", [_btn("back")])
    home.transitions[("h:#open", "click")] = "panel"
    panel.transitions[("h:#back", "click")] = "home"
    driver = FakeDriver({"home": home, "panel": panel}, entry="home",
                        actions=frozenset({"click"}), ambient=ambient_noise)
    loop = ExplorationLoop(driver, budgets=Budgets(min_seconds=0, max_actions=50),
                           deck=Deck(cards=()), clock=FakeClock())
    result = loop.run()
    assert len(result.coverage.states) == 2


def test_strategy_cards_are_drawn_when_novelty_stalls():
    driver = _two_screen_app()
    loop = ExplorationLoop(driver, budgets=Budgets(
        min_seconds=0, max_actions=80, stall_threshold=2), clock=FakeClock())
    result = loop.run()
    assert result.stats.cards_played > 0


# --- The advisor ----------------------------------------------------------

class RecordingAdvisor:
    def __init__(self, expectations=None, curiosity=None, verdict=True):
        self.oriented: list[str] = []
        self.judged: list[Finding] = []
        self._expectations = expectations or {}
        self._curiosity = curiosity or {}
        self._verdict = verdict

    def orient(self, observation, fingerprint):
        self.oriented.append(fingerprint)
        return Orientation(expectations=dict(self._expectations),
                           curiosity=dict(self._curiosity), notes="seen")

    def judge(self, finding, context):
        self.judged.append(finding)
        return self._verdict


def test_the_loop_runs_without_a_model_at_all():
    result = ExplorationLoop(_two_screen_app(), budgets=Budgets(
        min_seconds=0, max_actions=30), deck=Deck(cards=()),
        clock=FakeClock()).run()
    assert result.stats.model_calls == 0
    assert result.stats.states_discovered == 2


def test_the_model_is_consulted_once_per_new_state():
    """The budget the whole design turns on."""
    advisor = RecordingAdvisor()
    loop = ExplorationLoop(_two_screen_app(), budgets=Budgets(
        min_seconds=0, max_actions=60), advisor=advisor, deck=Deck(cards=()),
        clock=FakeClock())
    result = loop.run()
    assert len(advisor.oriented) == len(set(advisor.oriented)) == 2
    # Model calls are exactly one orient per new state plus one judge per
    # detector firing -- never one per action, which is the budget that matters.
    assert result.stats.model_calls == len(advisor.oriented) + len(advisor.judged)
    assert result.stats.model_calls < result.stats.actions + len(advisor.judged) + 1


def test_expectations_reach_the_no_op_finding():
    """What turns "this did nothing" into something a developer acts on."""
    advisor = RecordingAdvisor(
        expectations={"h:#dead": "should open the settings panel"})
    loop = ExplorationLoop(_two_screen_app(), budgets=Budgets(
        min_seconds=0, max_actions=60), advisor=advisor, deck=Deck(cards=()),
        clock=FakeClock())
    result = loop.run()
    u7 = [f for f in result.findings if f.rule == "U7"]
    assert u7 and u7[0].expected == "should open the settings panel"
    assert u7[0].details["had_expectation"] is True


def test_the_advisor_can_veto_a_finding():
    advisor = RecordingAdvisor(verdict=False)
    loop = ExplorationLoop(_two_screen_app(), budgets=Budgets(
        min_seconds=0, max_actions=60), advisor=advisor, deck=Deck(cards=()),
        clock=FakeClock())
    result = loop.run()
    assert advisor.judged, "the advisor should have been asked"
    assert [f for f in result.findings if f.rule == "U7"] == []


def test_curiosity_reaches_the_frontier():
    advisor = RecordingAdvisor(curiosity={"h:#dead": 0.9})
    loop = ExplorationLoop(_two_screen_app(), budgets=Budgets(
        min_seconds=0, max_actions=60), advisor=advisor, deck=Deck(cards=()),
        clock=FakeClock())
    loop.run()
    assert loop.stats.model_calls > 0


# --- Waivers and stats ----------------------------------------------------

def test_waived_rules_are_silenced():
    loop = ExplorationLoop(_two_screen_app(), budgets=Budgets(
        min_seconds=0, max_actions=60), deck=Deck(cards=()),
        waived_rules=["U7", "U12"], clock=FakeClock())
    assert loop.run().findings == []


def test_stats_measure_the_two_numbers_the_slice_exists_for():
    loop = ExplorationLoop(_two_screen_app(), budgets=Budgets(
        min_seconds=0, max_actions=40), deck=Deck(cards=()),
        clock=FakeClock(step=0.01))
    result = loop.run()
    d = result.stats.to_dict()
    assert d["actions"] > 0
    assert d["actions_per_hour"] > 0
    assert "actions_per_model_call" in d
    assert d["mechanical_seconds"] > 0


def test_phase_timings_are_broken_out():
    """So we can see whether the rulebook probes dominate the wall-clock."""
    loop = ExplorationLoop(_two_screen_app(), budgets=Budgets(
        min_seconds=0, max_actions=20), deck=Deck(cards=()),
        clock=FakeClock(step=0.01))
    result = loop.run()
    assert result.stats.observe_seconds > 0
    assert result.stats.act_seconds > 0
    assert result.stats.mechanical_seconds >= result.stats.observe_seconds


def test_result_serialises_for_the_report():
    loop = ExplorationLoop(_two_screen_app(), budgets=Budgets(
        min_seconds=0, max_actions=20), deck=Deck(cards=()), clock=FakeClock())
    d = loop.run().to_dict()
    assert set(d) == {"stop_reason", "stats", "coverage", "session", "findings"}
