"""Tests for the full-session action recorder.

The property under test throughout is the one the no-checkpoint decision
creates (ACTIVE_QA.md 5.2): **every trace must begin at a cold start**, because
a trace that does not cannot be replayed at all, and an unreplayable trace means
an unconfirmable finding.
"""

from __future__ import annotations

import pytest

from pandabot_core.active_qa import (
    Action, ActResult, Element, FakeDriver, FakeScreen, SessionRecorder, Trace,
)


def _rec(session_id: str = "aq-test") -> SessionRecorder:
    r = SessionRecorder(session_id)
    r.mark_launch()
    return r


def _click(n: int = 1) -> Action:
    return Action(kind="click", target=f"e{n}")


# --- Recording ------------------------------------------------------------

def test_recording_before_launch_is_refused():
    """A step with no cold start behind it is unreplayable, so it is a bug."""
    r = SessionRecorder("aq-test")
    with pytest.raises(RuntimeError, match="must be called before the first record"):
        r.record(_click())


def test_steps_are_indexed_from_zero_and_in_order():
    r = _rec()
    steps = [r.record(_click(i)) for i in range(1, 4)]
    assert [s.index for s in steps] == [0, 1, 2]
    assert len(r) == 3


def test_step_carries_the_evidence_it_was_given():
    r = _rec()
    step = r.record(
        _click(),
        before_fingerprint="sha1:aaa",
        after_fingerprint="sha1:bbb",
        result=ActResult(ok=True, duration_ms=12),
        before_screenshot="/tmp/b.png",
        after_screenshot="/tmp/a.png",
        logs=["ERROR boom"],
        screen_id="home",
        duration_ms=12,
    )
    assert step.before_screenshot == "/tmp/b.png"
    assert step.logs == ("ERROR boom",)
    assert step.screen_id == "home"
    assert step.changed


def test_changed_is_false_when_the_fingerprint_holds():
    r = _rec()
    step = r.record(_click(), before_fingerprint="sha1:x", after_fingerprint="sha1:x")
    assert not step.changed


# --- Epochs ---------------------------------------------------------------

def test_first_launch_is_epoch_zero():
    r = _rec()
    assert r.epoch == 0
    assert r.record(_click()).epoch == 0


def test_relaunch_starts_a_new_epoch():
    r = _rec()
    r.record(_click(1))
    r.mark_launch()
    second = r.record(_click(2))
    assert second.epoch == 1
    assert [s.index for s in r.epoch_steps(0)] == [0]
    assert [s.index for s in r.epoch_steps(1)] == [1]


def test_indices_are_never_reused_across_epochs():
    r = _rec()
    r.record(_click(1))
    r.mark_launch()
    r.record(_click(2))
    assert [s.index for s in r.steps] == [0, 1]


# --- Traces ---------------------------------------------------------------

def test_trace_starts_at_the_cold_start_and_ends_at_the_target():
    r = _rec()
    for i in range(1, 5):
        r.record(_click(i))
    trace = r.trace_for(2)
    assert len(trace) == 3
    assert [a.target for a in trace.actions] == ["e1", "e2", "e3"]
    assert trace.target.index == 2


def test_trace_excludes_steps_from_earlier_epochs():
    """The heart of it: replaying must not include actions from before the
    relaunch, or the replay diverges from what actually happened."""
    r = _rec()
    r.record(_click(1))
    r.record(_click(2))
    r.mark_launch()
    r.record(_click(3))
    target = r.record(_click(4))

    trace = r.trace_for_step(target)
    assert trace.epoch == 1
    assert [a.target for a in trace.actions] == ["e3", "e4"]


def test_trace_for_the_first_step_of_an_epoch_is_just_that_step():
    r = _rec()
    r.record(_click(1))
    r.mark_launch()
    step = r.record(_click(2))
    trace = r.trace_for_step(step)
    assert len(trace) == 1 and trace.target_index == 0


def test_trace_for_rejects_an_unknown_index():
    r = _rec()
    r.record(_click())
    with pytest.raises(IndexError):
        r.trace_for(7)


def test_trace_round_trips_through_json():
    r = _rec()
    r.record(_click(1), before_fingerprint="sha1:a", after_fingerprint="sha1:a")
    r.record(Action(kind="type", target="e2", value="hi"))
    trace = r.trace_for(1)
    restored = Trace.from_dict(trace.to_dict())
    assert restored.actions == trace.actions
    assert restored.epoch == trace.epoch
    assert restored.target_index == trace.target_index


# --- Repro steps ----------------------------------------------------------

def test_repro_steps_are_numbered_from_one():
    r = _rec()
    r.record(Action(kind="click", target="e1", reason="explore"))
    r.record(Action(kind="type", target="e2", value="abc"))
    lines = r.trace_for(1).repro_steps()
    assert lines[0].startswith("1. click on e1")
    assert lines[1].startswith("2. type on e2 with 'abc'")


def test_repro_steps_collapse_repeated_actions():
    """A spam-submit finding otherwise produces forty identical lines."""
    r = _rec()
    for _ in range(5):
        r.record(Action(kind="click", target="e1"))
    r.record(Action(kind="click", target="e2"))
    lines = r.trace_for(5).repro_steps()
    assert lines == ["1. click on e1 (x5)", "2. click on e2"]


def test_repro_steps_do_not_collapse_across_a_different_action():
    r = _rec()
    r.record(Action(kind="click", target="e1"))
    r.record(Action(kind="click", target="e2"))
    r.record(Action(kind="click", target="e1"))
    assert len(r.trace_for(2).repro_steps()) == 3


# --- Stats and persistence ------------------------------------------------

def test_stats_summarise_the_session():
    r = _rec()
    r.record(_click(1), before_fingerprint="a", after_fingerprint="b",
             screen_id="home", duration_ms=10)
    r.record(_click(2), before_fingerprint="b", after_fingerprint="b",
             screen_id="home", duration_ms=5)
    r.record(_click(3), result=ActResult(ok=False, error="nope"),
             screen_id="settings", duration_ms=1)
    r.mark_launch()

    s = r.stats()
    assert s["session_id"] == "aq-test"
    assert s["actions"] == 3
    assert s["screens"] == 2
    assert s["no_change_actions"] == 2   # step 2 held, step 3 had no fingerprints
    assert s["failed_actions"] == 1
    assert s["duration_ms"] == 16
    assert s["epochs"] == 2 and s["relaunches"] == 1


def test_recorder_round_trips_through_json():
    """A run can be paused when the model is unloaded, so it must reload."""
    r = _rec()
    r.record(_click(1), before_fingerprint="a", after_fingerprint="b", screen_id="home")
    r.mark_launch()
    r.record(Action(kind="type", target="e2", value="x"))

    restored = SessionRecorder.from_json(r.to_json())
    assert restored.session_id == r.session_id
    assert restored.epoch == r.epoch
    assert [s.to_dict() for s in restored.steps] == [s.to_dict() for s in r.steps]
    # And it can keep recording where it left off.
    nxt = restored.record(_click(3))
    assert nxt.index == 2 and nxt.epoch == 1


# --- Against the fake driver ----------------------------------------------

def test_a_recorded_trace_replays_to_the_same_state():
    """The end-to-end property. Record a session against the fake, then replay
    the trace from a cold start and land in the same place."""
    home = FakeScreen("home", [
        Element(id="e1", role="button", label="Open", handle="#open"),
        Element(id="e2", role="button", label="Dead", handle="#dead"),
    ])
    panel = FakeScreen("panel", [
        Element(id="e1", role="textbox", label="Name", handle="#name", text=""),
    ])
    home.transitions[("h:#open", "click")] = "panel"

    driver = FakeDriver({"home": home, "panel": panel}, entry="home")
    rec = SessionRecorder("aq-replay")
    rec.mark_launch()

    plan = [
        Action(kind="click", target="e2"),                    # the dead control
        Action(kind="click", target="e1"),                    # into the panel
        Action(kind="type", target="e1", value="abc"),        # type a name
    ]
    for action in plan:
        before = driver.observe()
        result = driver.act(action)
        after = driver.observe()
        rec.record(
            action,
            before_fingerprint=before.compute_fingerprint(),
            after_fingerprint=after.compute_fingerprint(),
            result=result,
            screen_id=before.screen_id,
        )

    end_state = driver.observe().compute_fingerprint()

    # The dead control changed nothing; the other two did.
    assert [s.changed for s in rec.steps] == [False, True, True]

    # Replay from a cold start reproduces the final state exactly.
    driver.relaunch()
    replay = rec.trace_for(2)
    for action in replay.actions:
        driver.act(action)
    assert driver.observe().compute_fingerprint() == end_state


def test_replaying_a_partial_trace_lands_mid_session():
    home = FakeScreen("home", [Element(id="e1", role="button", handle="#open")])
    panel = FakeScreen("panel", [Element(id="e1", role="button", handle="#close")])
    home.transitions[("h:#open", "click")] = "panel"
    panel.transitions[("h:#close", "click")] = "home"

    driver = FakeDriver({"home": home, "panel": panel}, entry="home")
    rec = SessionRecorder()
    rec.mark_launch()
    for _ in range(3):
        rec.record(Action(kind="click", target="e1"))
        driver.act(Action(kind="click", target="e1"))

    # After three alternating clicks we are on `panel`; replaying only the
    # first click should leave us on `panel` too, and the first two on `home`.
    driver.relaunch()
    for action in rec.trace_for(0).actions:
        driver.act(action)
    assert driver.observe().screen_id == "panel"

    driver.relaunch()
    for action in rec.trace_for(1).actions:
        driver.act(action)
    assert driver.observe().screen_id == "home"
