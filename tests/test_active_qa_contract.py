"""Tests for the active-QA driver contract."""

from __future__ import annotations

import pytest

from pandabot_core.active_qa import (
    DEFAULT_FINGERPRINT_POLICY,
    STRICT_FINGERPRINT_POLICY,
    Action,
    Driver,
    Element,
    FakeDriver,
    FakeScreen,
    FingerprintPolicy,
    Health,
    Observation,
    ambient_noise,
    supported_actions,
)


# --- Action ---------------------------------------------------------------

def test_action_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown action kind"):
        Action(kind="teleport", target="e1")


@pytest.mark.parametrize("kind", ["click", "type", "press_hold", "hover", "focus"])
def test_action_requires_target_where_meaningful(kind):
    value = "x" if kind == "type" else None
    with pytest.raises(ValueError, match="requires a target"):
        Action(kind=kind, value=value)


def test_type_action_requires_a_value():
    with pytest.raises(ValueError, match="requires a value"):
        Action(kind="type", target="e1")


def test_back_and_wait_need_no_target():
    assert Action(kind="back").target is None
    assert Action(kind="wait", value=250).value == 250


def test_action_round_trips_through_dict():
    a = Action(kind="type", target="e3", value="hello", reason="boundary values")
    assert Action.from_dict(a.to_dict()) == a


def test_action_describe_truncates_long_values():
    a = Action(kind="type", target="e1", value="x" * 100)
    desc = a.describe()
    assert desc.startswith("type on e1 with")
    assert "..." in desc
    assert len(desc) < 80


# --- Element --------------------------------------------------------------

def test_identity_prefers_handle_and_is_stable_across_observations():
    # The same control, seen twice, given different per-observation ids.
    first = Element(id="e1", role="button", label="Go", handle="#go")
    second = Element(id="e42", role="button", label="Go", handle="#go")
    assert first.identity == second.identity


def test_identity_falls_back_to_role_and_label_without_a_handle():
    el = Element(id="e1", role="button", label="Confirm")
    assert el.identity == "r:button|l:Confirm"


def test_identity_distinguishes_different_controls():
    a = Element(id="e1", role="button", label="Save")
    b = Element(id="e2", role="button", label="Delete")
    assert a.identity != b.identity


def test_interactive_covers_roles_and_focusables():
    assert Element(id="e1", role="button").interactive
    assert Element(id="e2", role="textbox").interactive
    assert not Element(id="e3", role="text").interactive
    # A custom role that is nonetheless focusable still counts.
    assert Element(id="e4", role="widget", focusable=True).interactive


def test_element_round_trips_with_bounds():
    el = Element(id="e1", role="button", label="Go", bounds=(1, 2, 3, 4))
    assert Element.from_dict(el.to_dict()) == el


# --- Observation and fingerprinting ---------------------------------------

def _obs(**kw) -> Observation:
    els = kw.pop("elements", (Element(id="e1", role="button", label="Go", handle="#go"),))
    return Observation(elements=els, **kw)


def test_same_state_hashes_the_same():
    assert _obs().compute_fingerprint() == _obs().compute_fingerprint()


def test_fingerprint_is_order_independent():
    a = Element(id="e1", role="button", label="A", handle="#a")
    b = Element(id="e2", role="button", label="B", handle="#b")
    assert (_obs(elements=(a, b)).compute_fingerprint()
            == _obs(elements=(b, a)).compute_fingerprint())


def test_state_change_moves_the_fingerprint():
    enabled = Element(id="e1", role="button", label="Go", handle="#go", enabled=True)
    disabled = Element(id="e1", role="button", label="Go", handle="#go", enabled=False)
    assert (_obs(elements=(enabled,)).compute_fingerprint()
            != _obs(elements=(disabled,)).compute_fingerprint())


def test_selected_state_moves_the_fingerprint():
    off = Element(id="e1", role="tab", label="One", handle="#t1", selected=False)
    on = Element(id="e1", role="tab", label="One", handle="#t1", selected=True)
    assert (_obs(elements=(off,)).compute_fingerprint()
            != _obs(elements=(on,)).compute_fingerprint())


def test_typed_text_moves_the_fingerprint():
    """Rule U6 needs a field's own contents to count as state."""
    empty = Element(id="e1", role="textbox", handle="#name", text="")
    filled = Element(id="e1", role="textbox", handle="#name", text="abc")
    assert (_obs(elements=(empty,)).compute_fingerprint()
            != _obs(elements=(filled,)).compute_fingerprint())


def test_evidence_never_affects_identity():
    """Screenshots, logs and network traffic are evidence, not state.

    A log line arriving must not make a state look new, or the coverage map
    fragments and no-op detection stops firing.
    """
    plain = _obs()
    noisy = _obs(
        screenshot="/tmp/shot.png",
        logs=("ERROR something happened",),
        net=({"status": 500, "url": "/api"},),
    )
    assert plain.compute_fingerprint() == noisy.compute_fingerprint()


def test_ambient_motion_does_not_change_state_by_default():
    """The risk-2 guard: a world that is never still must still fingerprint stably.

    Swaying bosses, patrolling villagers and a ticking clock all land in the
    flattened screen text and in element bounds. If those counted, every
    observation would be a new state and the no-op detector would silently never
    fire -- a failure whose symptom is silence, not noise.
    """
    still = _obs(text="Goober Ranch", elements=(
        Element(id="e1", role="button", label="Go", handle="#go", bounds=(0, 0, 10, 10)),
    ))
    moving = _obs(text="Goober Ranch clock 03:47 boss sway 12", elements=(
        Element(id="e1", role="button", label="Go", handle="#go", bounds=(3, 1, 13, 11)),
    ))
    assert still.compute_fingerprint() == moving.compute_fingerprint()
    # And the strict policy, which is right for a static target, does notice.
    assert (still.compute_fingerprint(STRICT_FINGERPRINT_POLICY)
            != moving.compute_fingerprint(STRICT_FINGERPRINT_POLICY))


def test_volatile_patterns_are_redacted_under_strict_policy():
    """Even when hashing screen text, a bare clock should not fragment states."""
    a = _obs(text="Session started 10:31")
    b = _obs(text="Session started 10:32")
    assert (a.compute_fingerprint(STRICT_FINGERPRINT_POLICY)
            == b.compute_fingerprint(STRICT_FINGERPRINT_POLICY))


def test_custom_policy_can_drop_element_text():
    policy = FingerprintPolicy(include_element_text=False)
    empty = _obs(elements=(Element(id="e1", role="textbox", handle="#n", text=""),))
    filled = _obs(elements=(Element(id="e1", role="textbox", handle="#n", text="x"),))
    assert empty.compute_fingerprint(policy) == filled.compute_fingerprint(policy)


def test_screen_id_participates_in_identity():
    assert (_obs(screen_id="home").compute_fingerprint()
            != _obs(screen_id="settings").compute_fingerprint())


def test_lookup_helpers():
    el = Element(id="e1", role="button", label="Go", handle="#go")
    obs = _obs(elements=(el, Element(id="e2", role="text", label="hi")))
    assert obs.element("e1") is el
    assert obs.element("nope") is None
    assert obs.by_identity("h:#go") is el
    assert [e.id for e in obs.interactive()] == ["e1"]


def test_blind_observation_is_flagged():
    assert Observation().is_blind
    assert not _obs().is_blind


def test_observation_round_trips():
    obs = _obs(screen_id="home", text="hi", screenshot="/tmp/a.png",
               logs=("l1",), net=({"status": 200},), meta={"url": "/"})
    assert Observation.from_dict(obs.to_dict()).compute_fingerprint() == obs.compute_fingerprint()


# --- Driver protocol ------------------------------------------------------

def _two_screen_app() -> FakeDriver:
    home = FakeScreen("home", [
        Element(id="e1", role="button", label="Go", handle="#go"),
        Element(id="e2", role="button", label="Dead", handle="#dead"),
    ])
    nxt = FakeScreen("next", [Element(id="e1", role="button", label="Back", handle="#back")])
    home.transitions[("h:#go", "click")] = "next"
    return FakeDriver({"home": home, "next": nxt}, entry="home")


def test_fake_driver_satisfies_the_protocol():
    assert isinstance(_two_screen_app(), Driver)


def test_supported_actions_defaults_to_everything():
    assert "hover" in supported_actions(_two_screen_app())


def test_supported_actions_respects_a_narrower_declaration():
    """A touch-only target has no hover, so U2 is inapplicable rather than failing."""
    home = FakeScreen("home", [Element(id="e1", role="button", handle="#a")])
    d = FakeDriver({"home": home}, entry="home", actions=frozenset({"click", "type"}))
    assert supported_actions(d) == frozenset({"click", "type"})


def test_acting_moves_state_and_observing_does_not():
    d = _two_screen_app()
    before = d.observe().compute_fingerprint()
    assert d.observe().compute_fingerprint() == before  # observe is side-effect free
    assert d.act(Action(kind="click", target="e1")).ok
    assert d.observe().screen_id == "next"


def test_dead_control_produces_no_state_change():
    """The primitive rule U7 is built on."""
    d = _two_screen_app()
    before = d.observe().compute_fingerprint()
    assert d.act(Action(kind="click", target="e2")).ok
    assert d.observe().compute_fingerprint() == before


def test_typing_is_echoed_back():
    home = FakeScreen("home", [Element(id="e1", role="textbox", handle="#name", text="")])
    d = FakeDriver({"home": home}, entry="home")
    d.act(Action(kind="type", target="e1", value="hello"))
    assert d.observe().element("e1").text == "hello"


def test_a_field_that_swallows_input_is_representable():
    """Rule U6's failure case has to be constructible, or it cannot be tested."""
    home = FakeScreen("home", [Element(id="e1", role="textbox", handle="#name", text="")])
    d = FakeDriver({"home": home}, entry="home")
    d.echo_typing = False
    d.act(Action(kind="type", target="e1", value="hello"))
    assert d.observe().element("e1").text == ""


def test_disabled_elements_refuse_input():
    home = FakeScreen("home", [
        Element(id="e1", role="button", label="Go", handle="#go", enabled=False),
    ])
    d = FakeDriver({"home": home}, entry="home")
    res = d.act(Action(kind="click", target="e1"))
    assert not res.ok and "disabled" in res.error


def test_acting_on_a_missing_element_fails_without_raising():
    d = _two_screen_app()
    res = d.act(Action(kind="click", target="nope"))
    assert not res.ok and "no such element" in res.error


def test_relaunch_returns_to_a_cold_start():
    d = _two_screen_app()
    d.act(Action(kind="click", target="e1"))
    assert d.observe().screen_id == "next"
    d.relaunch()
    assert d.observe().screen_id == "home"
    assert d.relaunches == 1


def test_crash_is_reported_by_health():
    home = FakeScreen("home", [Element(id="e1", role="button", handle="#a")])
    d = FakeDriver({"home": home}, entry="home", crash_after=1)
    assert d.health().ok
    d.act(Action(kind="click", target="e1"))
    d.act(Action(kind="click", target="e1"))
    h = d.health()
    assert not h.ok and h.crashed


def test_crash_surfaces_in_the_log_delta():
    home = FakeScreen("home", [Element(id="e1", role="button", handle="#a")])
    d = FakeDriver({"home": home}, entry="home", crash_after=0)
    d.act(Action(kind="click", target="e1"))
    assert any("FATAL" in line for line in d.observe().logs)


def test_ambient_driver_is_stable_under_the_default_policy():
    """End to end: a never-still app still produces one stable state."""
    home = FakeScreen("home", [Element(id="e1", role="button", handle="#a")])
    d = FakeDriver({"home": home}, entry="home", ambient=ambient_noise)
    prints = {d.observe().compute_fingerprint() for _ in range(20)}
    assert len(prints) == 1
    strict = {d.observe().compute_fingerprint(STRICT_FINGERPRINT_POLICY) for _ in range(20)}
    assert len(strict) == 20


def test_logs_are_drained_not_repeated():
    home = FakeScreen("home", [Element(id="e1", role="button", handle="#a")],
                      logs=["boot line"])
    d = FakeDriver({"home": home}, entry="home")
    assert d.observe().logs == ("boot line",)
    assert d.observe().logs == ()


def test_health_reports_ok_for_a_live_app():
    assert _two_screen_app().health() == Health(ok=True)
