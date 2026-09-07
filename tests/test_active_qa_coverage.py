"""Tests for the coverage map and the frontier policy.

The behaviour that matters here is that an hour of exploration actually
explores: new states get worked, worked states get deprioritised, and when the
current screen is exhausted the loop knows where else to go.
"""

from __future__ import annotations

import pytest

from pandabot_core.active_qa import Element, FakeDriver, FakeScreen, Observation
from pandabot_core.active_qa.coverage import (
    Candidate, CoverageMap, Frontier, PriorityWeights, default_candidates,
)


def _obs(*elements: Element, screen_id: str = "home") -> Observation:
    return Observation(elements=elements, screen_id=screen_id)


def _btn(name: str, **kw) -> Element:
    return Element(id=f"e_{name}", role="button", label=name, handle=f"#{name}", **kw)


def _cand(state="s1", element="h:#a", kind="click", **kw) -> Candidate:
    return Candidate(state=state, element_identity=element, kind=kind, **kw)


# --- CoverageMap: states ---------------------------------------------------

def test_first_sight_of_a_state_is_new():
    m = CoverageMap()
    node, is_new = m.observe(_obs(_btn("a")), "fp1", step=0)
    assert is_new and node.visits == 1 and node.first_seen_step == 0


def test_revisiting_is_not_new_and_counts_up():
    m = CoverageMap()
    m.observe(_obs(_btn("a")), "fp1")
    node, is_new = m.observe(_obs(_btn("a")), "fp1")
    assert not is_new and node.visits == 2


def test_elements_are_registered_on_sight():
    m = CoverageMap()
    node, _ = m.observe(_obs(_btn("a"), _btn("b")), "fp1")
    assert set(node.element_touches) == {"h:#a", "h:#b"}
    assert all(v == 0 for v in node.element_touches.values())


def test_non_interactive_elements_are_not_frontier_work():
    m = CoverageMap()
    node, _ = m.observe(_obs(_btn("a"), Element(id="t", role="text", label="hi")), "fp1")
    assert set(node.element_touches) == {"h:#a"}


# --- CoverageMap: transitions ---------------------------------------------

def test_transition_marks_the_candidate_tried():
    m = CoverageMap()
    m.observe(_obs(_btn("a")), "fp1")
    c = _cand(state="fp1", element="h:#a")
    assert not m.has_tried(c)
    m.record_transition("fp1", c, "fp2")
    assert m.has_tried(c)
    assert m.touches("fp1", "h:#a") == 1


def test_self_loop_is_recognised():
    m = CoverageMap()
    m.observe(_obs(_btn("a")), "fp1")
    edge = m.record_transition("fp1", _cand(state="fp1"), "fp1")
    assert edge.is_self_loop


def test_the_same_element_with_a_different_value_is_a_separate_candidate():
    """Typing 'a' and typing 10k characters are not the same experiment."""
    m = CoverageMap()
    first = _cand(kind="type", value="a")
    second = _cand(kind="type", value="b")
    m.record_transition("s1", first, "s1")
    assert m.has_tried(first) and not m.has_tried(second)


def test_kind_payoff_starts_optimistic_then_learns():
    m = CoverageMap()
    assert m.kind_payoff("scroll") == 1.0          # untried kinds get a chance
    for _ in range(3):
        m.record_transition("s1", _cand(kind="scroll", value=object()), "s1")
    assert m.kind_payoff("scroll") == 0.0          # never changed anything
    m.record_transition("s1", _cand(kind="click"), "s2")
    assert m.kind_payoff("click") == 1.0


# --- Dead ends and routing -------------------------------------------------

def test_dead_end_is_a_state_with_no_way_out():
    m = CoverageMap()
    for fp in ("a", "b"):
        m.observe(_obs(_btn("x")), fp)
    m.record_transition("a", _cand(state="a"), "b")
    m.record_transition("b", _cand(state="b"), "b")   # only a self-loop
    assert m.dead_ends() == ["b"]


def test_route_finds_the_shortest_known_walk():
    m = CoverageMap()
    for fp in ("a", "b", "c"):
        m.observe(_obs(_btn("x")), fp)
    m.record_transition("a", _cand(state="a", element="h:#ab"), "b")
    m.record_transition("b", _cand(state="b", element="h:#bc"), "c")

    route = m.route_to_open_work("a", {"c"})
    assert [e.dst for e in route] == ["b", "c"]


def test_route_is_empty_when_already_there():
    m = CoverageMap()
    m.observe(_obs(_btn("x")), "a")
    assert m.route_to_open_work("a", {"a"}) == []


def test_route_is_none_when_unreachable():
    """None is the signal to relaunch: with no checkpoints, walking is the only
    other way to reach a state."""
    m = CoverageMap()
    for fp in ("a", "b"):
        m.observe(_obs(_btn("x")), fp)
    assert m.route_to_open_work("a", {"b"}) is None


def test_route_ignores_self_loops():
    m = CoverageMap()
    for fp in ("a", "b"):
        m.observe(_obs(_btn("x")), fp)
    m.record_transition("a", _cand(state="a", element="h:#loop"), "a")
    assert m.route_to_open_work("a", {"b"}) is None


def test_coverage_round_trips():
    m = CoverageMap()
    m.observe(_obs(_btn("a")), "fp1", step=3)
    m.record_transition("fp1", _cand(state="fp1", element="h:#a"), "fp2")
    restored = CoverageMap.from_dict(m.to_dict())
    assert restored.stats() == m.stats()
    assert restored.has_tried(_cand(state="fp1", element="h:#a"))


# --- Frontier --------------------------------------------------------------

def test_untried_work_is_queued_once():
    f = Frontier(CoverageMap())
    c = _cand()
    assert f.push(c)
    assert not f.push(c)          # already queued
    assert len(f) == 1


def test_already_tried_work_is_not_queued():
    m = CoverageMap()
    c = _cand()
    m.record_transition("s1", c, "s1")
    assert not Frontier(m).push(c)


def test_untouched_elements_outrank_touched_ones():
    m = CoverageMap()
    m.observe(_obs(_btn("a"), _btn("b")), "s1")
    m.record_transition("s1", _cand(state="s1", element="h:#a", value=1), "s1")
    f = Frontier(m)
    assert f.priority(_cand(state="s1", element="h:#b")) > \
           f.priority(_cand(state="s1", element="h:#a", value=2))


def test_curiosity_lifts_a_candidate():
    f = Frontier(CoverageMap())
    assert f.priority(_cand(curiosity=1.0)) > f.priority(_cand(curiosity=0.0))


def test_strategy_cards_sit_behind_plain_exploration():
    """Adversarial cards exist for when novelty stalls, not to pre-empt a fresh
    screen."""
    f = Frontier(CoverageMap())
    assert f.priority(_cand(source="boundary values")) < f.priority(_cand())


def test_heavily_visited_states_are_deprioritised():
    """What stops the loop grinding one screen for an hour."""
    m = CoverageMap()
    for _ in range(20):
        m.observe(_obs(_btn("a")), "busy")
    m.observe(_obs(_btn("a")), "fresh")
    f = Frontier(m)
    assert f.priority(_cand(state="fresh", element="h:#a")) > \
           f.priority(_cand(state="busy", element="h:#a"))


def test_pop_returns_highest_priority_first():
    f = Frontier(CoverageMap())
    f.push(_cand(element="h:#low", curiosity=0.0))
    f.push(_cand(element="h:#high", curiosity=5.0))
    assert f.pop().element_identity == "h:#high"


def test_pop_can_be_restricted_to_the_current_state():
    """With no checkpoints we can only act where we already are."""
    f = Frontier(CoverageMap())
    f.push(_cand(state="elsewhere", element="h:#x", curiosity=9.0))
    f.push(_cand(state="here", element="h:#y"))
    got = f.pop(state="here")
    assert got.element_identity == "h:#y"
    # The higher-priority out-of-state candidate is kept, not discarded.
    assert len(f) == 1
    assert f.pop().element_identity == "h:#x"


def test_pop_skips_work_completed_since_it_was_queued():
    m = CoverageMap()
    f = Frontier(m)
    stale, live = _cand(element="h:#a"), _cand(element="h:#b")
    f.push(stale)
    f.push(live)
    m.record_transition("s1", stale, "s2")     # done by some other path
    assert f.pop().element_identity == "h:#b"


def test_pop_returns_none_when_exhausted():
    f = Frontier(CoverageMap())
    assert f.pop() is None


def test_open_states_reports_where_work_remains():
    f = Frontier(CoverageMap())
    f.push(_cand(state="a"))
    f.push(_cand(state="b"))
    assert f.open_states() == {"a", "b"}


def test_has_work_is_state_aware():
    f = Frontier(CoverageMap())
    f.push(_cand(state="a"))
    assert f.has_work() and f.has_work("a") and not f.has_work("b")


def test_drop_state_forgets_unreachable_work():
    f = Frontier(CoverageMap())
    f.push(_cand(state="a", element="h:#1"))
    f.push(_cand(state="a", element="h:#2"))
    f.push(_cand(state="b", element="h:#3"))
    assert f.drop_state("a") == 2
    assert f.open_states() == {"b"}
    # And the state can be re-queued afterwards.
    assert f.push(_cand(state="a", element="h:#1"))


def test_iteration_is_in_priority_order_and_non_destructive():
    f = Frontier(CoverageMap())
    f.push(_cand(element="h:#low", curiosity=0.0))
    f.push(_cand(element="h:#high", curiosity=5.0))
    assert [c.element_identity for c in f] == ["h:#high", "h:#low"]
    assert len(f) == 2


def test_weights_are_tunable():
    greedy = Frontier(CoverageMap(), PriorityWeights(curiosity=100.0))
    assert greedy.priority(_cand(curiosity=1.0)) > 50


# --- Candidate binding -----------------------------------------------------

def test_candidate_binds_to_the_current_element_id():
    """Ids are per-observation; identity is what persists. Binding late is how
    a candidate queued three states ago still works."""
    c = _cand(element="h:#go", kind="click")
    obs = _obs(Element(id="e99", role="button", label="Go", handle="#go"))
    action = c.to_action(obs)
    assert action.target == "e99" and action.kind == "click"


def test_candidate_binding_fails_softly_when_the_element_is_gone():
    assert _cand(element="h:#vanished").to_action(_obs(_btn("a"))) is None


def test_candidate_reason_reaches_the_action():
    c = _cand(reason="pressed state (U1)", kind="press_hold")
    action = c.to_action(_obs(Element(id="e1", role="button", handle="#a")))
    assert action.reason == "pressed state (U1)"


# --- default_candidates ----------------------------------------------------

def test_buttons_get_the_rulebook_captures():
    obs = _obs(_btn("go"))
    kinds = {c.kind for c in default_candidates(obs, "fp")}
    assert kinds == {"click", "press_hold", "hover"}   # U1 and U2, plus explore


def test_focusable_controls_also_get_a_focus_probe():
    obs = _obs(_btn("go", focusable=True))
    assert "focus" in {c.kind for c in default_candidates(obs, "fp")}


def test_text_fields_get_a_typed_probe():
    obs = _obs(Element(id="e1", role="textbox", handle="#name"))
    cands = default_candidates(obs, "fp", probe_text="hello")
    typed = [c for c in cands if c.kind == "type"]
    assert typed and typed[0].value == "hello"
    assert "U6" in typed[0].reason


def test_unsupported_kinds_are_simply_absent():
    """A touch-only target has no hover, so U2 is inapplicable, not failing."""
    obs = _obs(_btn("go"))
    cands = default_candidates(obs, "fp", supported={"click"})
    assert {c.kind for c in cands} == {"click"}


def test_no_candidates_for_a_blind_observation():
    assert default_candidates(Observation(), "fp") == []


# --- Integration against the fake driver -----------------------------------

def test_exploration_reaches_every_state_and_then_runs_dry():
    """The whole point, end to end: drive a small app until there is nothing
    left to try, and confirm the map found both screens and the dead control."""
    home = FakeScreen("home", [
        Element(id="e1", role="button", label="Open", handle="#open"),
        Element(id="e2", role="button", label="Dead", handle="#dead"),
    ])
    panel = FakeScreen("panel", [Element(id="e1", role="button", label="Back", handle="#back")])
    home.transitions[("h:#open", "click")] = "panel"
    panel.transitions[("h:#back", "click")] = "home"

    driver = FakeDriver({"home": home, "panel": panel}, entry="home",
                        actions=frozenset({"click"}))
    coverage = CoverageMap()
    frontier = Frontier(coverage)

    for _ in range(50):
        obs = driver.observe()
        fp = obs.compute_fingerprint()
        node, is_new = coverage.observe(obs, fp)
        if is_new:
            frontier.extend(default_candidates(obs, fp, supported={"click"}))

        candidate = frontier.pop(state=fp)
        if candidate is None:
            if not frontier.has_work():
                break
            route = coverage.route_to_open_work(fp, frontier.open_states())
            if not route:
                driver.relaunch()
                continue
            # Walk one edge toward open work.
            edge = route[0]
            el = obs.by_identity(edge.element_identity)
            driver.act(Candidate(state=fp, element_identity=edge.element_identity,
                                 kind=edge.kind).to_action(obs))
            continue

        action = candidate.to_action(obs)
        driver.act(action)
        after = driver.observe().compute_fingerprint()
        coverage.record_transition(fp, candidate, after)

    assert not frontier.has_work(), "exploration should run to exhaustion"
    assert len(coverage.states) == 2, "both screens should be discovered"
    assert {n.screen_id for n in coverage.states.values()} == {"home", "panel"}
    # The dead control was tried and observably did nothing.
    assert any(e.is_self_loop and e.element_identity == "h:#dead"
               for e in coverage.edges)
