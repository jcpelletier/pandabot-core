"""Tests for replay-based triage.

The property that matters: a defect that will not reproduce must not reach the
report, and a replay that could not get far enough to try must be reported as
proving nothing rather than as a failure to reproduce. Those are different
answers and conflating them is how a gate quietly stops being a gate.
"""

from __future__ import annotations

import pytest

from pandabot_core.active_qa import (
    Action, Budgets, Deck, Element, ExplorationLoop, FakeDriver, FakeScreen,
    SessionRecorder,
)
from pandabot_core.active_qa.rules import BLOCKER, MAJOR, MINOR, Finding
from pandabot_core.active_qa.triage import (
    FindingCluster, TriagedFinding, cluster_findings, replay_once, triage,
)


def _f(rule="U7", element="h:#dead", state="s1", severity=MAJOR,
       step=0, summary="x") -> Finding:
    return Finding(rule=rule, summary=summary, observed="", expected="",
                   element_identity=element, state=state, step_index=step,
                   severity=severity)


def _app(dead=True) -> FakeDriver:
    """Two screens, optionally with a planted dead control."""
    home = FakeScreen("home", [
        Element(id="e1", role="button", label="Open", handle="#open"),
        Element(id="e2", role="button", label="Dead", handle="#dead"),
    ])
    panel = FakeScreen("panel", [Element(id="e1", role="button", label="Back", handle="#back")])
    home.transitions[("h:#open", "click")] = "panel"
    panel.transitions[("h:#back", "click")] = "home"
    if not dead:
        home.transitions[("h:#dead", "click")] = "panel"
    return FakeDriver({"home": home, "panel": panel}, entry="home",
                      actions=frozenset({"click"}))


def _run(driver, **kw):
    loop = ExplorationLoop(driver, budgets=Budgets(min_seconds=0, max_actions=40, **kw),
                           deck=Deck(cards=()), clock=lambda: 0.0)
    return loop.run()


# --- Clustering -----------------------------------------------------------

def test_repeated_sightings_become_one_defect():
    clusters = cluster_findings([_f(step=1), _f(step=5), _f(step=9)])
    assert len(clusters) == 1 and clusters[0].occurrences == 3


def test_different_controls_stay_separate():
    clusters = cluster_findings([_f(element="h:#a"), _f(element="h:#b")])
    assert len(clusters) == 2


def test_same_control_in_different_states_stays_separate():
    clusters = cluster_findings([_f(state="s1"), _f(state="s2")])
    assert len(clusters) == 2


def test_representative_is_the_earliest_sighting():
    """The first meeting had done least to the app, so its repro is shortest."""
    c = cluster_findings([_f(step=9, summary="late"), _f(step=2, summary="early")])[0]
    assert c.representative.summary == "early"


def test_severity_beats_recency_when_choosing_a_representative():
    c = cluster_findings([
        _f(step=1, severity=MINOR, summary="minor"),
        _f(step=8, severity=BLOCKER, summary="blocker"),
    ])[0]
    assert c.representative.summary == "blocker"


def test_clusters_are_ordered_worst_first():
    clusters = cluster_findings([
        _f(element="h:#a", severity=MINOR),
        _f(element="h:#b", severity=BLOCKER),
        _f(element="h:#c", severity=MAJOR),
    ])
    assert [c.representative.severity for c in clusters] == [BLOCKER, MAJOR, MINOR]


# --- Without a driver -----------------------------------------------------

def test_clustering_only_reports_everything_as_unverified():
    """What a paused or crashed session can still produce, stated honestly."""
    report = triage([_f(), _f(element="h:#b")])
    assert report.confirmed == []
    assert len(report.unconfirmed) == 2
    assert all(t.confidence == "unverified" for t in report.unconfirmed)
    assert report.replays == 0


# --- Replay ---------------------------------------------------------------

def test_replay_rebinds_elements_by_identity():
    """Recorded ids are per-observation; replay must re-resolve them or it
    targets whatever happens to be e2 this time."""
    driver = _app()
    result = _run(driver)
    trace = result.recorder.trace_for(result.recorder.steps[-1].index)
    fired, completed = replay_once(driver, trace)
    assert completed == len(trace)


def test_replay_reaches_the_end_of_a_clean_trace():
    driver = _app()
    result = _run(driver)
    dead = [f for f in result.findings if f.rule == "U7"]
    assert dead, "the fixture should produce a U7"
    trace = result.recorder.trace_for(dead[0].step_index)
    fired, completed = replay_once(driver, trace)
    assert completed == len(trace)
    assert dead[0].cluster_key in fired


def test_replay_reports_divergence_rather_than_guessing():
    """An element that is gone means the attempt proved nothing."""
    driver = _app()
    result = _run(driver)
    trace = result.recorder.trace_for(result.recorder.steps[-1].index)
    # Strip the identity so rebinding cannot resolve, as a vanished element would.
    from pandabot_core.active_qa.recorder import Step, Trace
    broken = Trace(
        epoch=trace.epoch,
        steps=tuple(Step(**{**s.to_dict(), "action": s.action,
                            "result": s.result, "logs": s.logs,
                            "element_identity": ""}) for s in trace.steps),
        target_index=trace.target_index)
    fired, completed = replay_once(driver, broken)
    assert completed < len(broken)


def test_strict_replay_stops_when_the_state_stops_matching():
    driver = _app()
    result = _run(driver)
    trace = result.recorder.trace_for(result.recorder.steps[-1].index)
    from pandabot_core.active_qa.recorder import Step, Trace
    wrong = Trace(
        epoch=trace.epoch,
        steps=tuple(Step(**{**s.to_dict(), "action": s.action, "result": s.result,
                            "logs": s.logs, "before_fingerprint": "sha1:nonsense"})
                    for s in trace.steps),
        target_index=trace.target_index)
    _fired, completed = replay_once(driver, wrong, strict=True)
    assert completed == 0


# --- Confirmation ---------------------------------------------------------

def test_a_real_defect_is_confirmed():
    driver = _app()
    result = _run(driver)
    report = triage(result.findings, driver=driver, recorder=result.recorder)
    u7 = [t for t in report.confirmed if t.rule == "U7"]
    assert u7, f"the planted dead control should confirm; got {report.stats()}"
    assert u7[0].confirmed and u7[0].confidence == "confirmed"
    assert u7[0].confirmations >= 2


def test_a_defect_that_will_not_reproduce_is_held_back():
    """The whole point: a finding that does not survive replay must not reach
    the report as though it did."""
    driver = _app()
    result = _run(driver)
    # A finding nothing will ever re-fire.
    phantom = Finding(rule="U7", summary="phantom", observed="", expected="",
                      element_identity="h:#never", state="s1",
                      step_index=result.recorder.steps[-1].index)
    report = triage([phantom], driver=driver, recorder=result.recorder)
    assert report.confirmed == []
    assert report.unconfirmed[0].confidence == "did not reproduce"


def test_confirmation_is_batched_by_epoch():
    """One relaunch per epoch, not one per finding. On Roblox the difference is
    a whole world rebuild each time."""
    driver = _app()
    result = _run(driver)
    findings = [
        Finding(rule="U7", summary=f"f{i}", observed="", expected="",
                element_identity=f"h:#x{i}", state="s1", step_index=s.index)
        for i, s in enumerate(result.recorder.steps[:4])
    ]
    report = triage(findings, driver=driver, recorder=result.recorder, attempts=3)
    # Four candidates, all in epoch 0, so at most `attempts` replays total.
    assert report.replays <= 3
    assert report.relaunches == report.replays


def test_required_confirmations_are_configurable():
    driver = _app()
    result = _run(driver)
    dead = [f for f in result.findings if f.rule == "U7"][0]
    strictest = triage([dead], driver=driver, recorder=result.recorder,
                       attempts=3, required=3)
    assert strictest.confirmed and strictest.confirmed[0].confirmations == 3


def test_occurrences_survive_into_the_triaged_finding():
    driver = _app()
    result = _run(driver)
    dead = [f for f in result.findings if f.rule == "U7"][0]
    report = triage([dead, dead, dead], driver=driver, recorder=result.recorder)
    assert report.all[0].occurrences == 3


def test_a_clean_app_confirms_nothing():
    """The precision half, through the whole pipeline."""
    driver = _app(dead=False)
    result = _run(driver)
    report = triage(result.findings, driver=driver, recorder=result.recorder)
    assert [t for t in report.confirmed if t.rule in ("U6", "U7")] == []


# --- Reporting ------------------------------------------------------------

def test_report_stats_summarise_the_pass():
    driver = _app()
    result = _run(driver)
    report = triage(result.findings, driver=driver, recorder=result.recorder)
    s = report.stats()
    assert set(s) >= {"confirmed", "unconfirmed", "diverged",
                      "confirmed_by_rule", "confirmed_by_severity", "replays"}
    assert s["confirmed"] == len(report.confirmed)


def test_triaged_finding_carries_repro_steps():
    driver = _app()
    result = _run(driver)
    report = triage(result.findings, driver=driver, recorder=result.recorder)
    assert report.all
    d = report.all[0].to_dict()
    assert d["repro_steps"] and d["repro_steps"][0].startswith("1.")
    assert "confidence" in d and "occurrences" in d


def test_confirmed_findings_are_ordered_worst_first():
    report = triage([
        _f(element="h:#a", severity=MINOR),
        _f(element="h:#b", severity=BLOCKER),
    ])
    assert [t.severity for t in report.unconfirmed] == [BLOCKER, MINOR]


def test_progress_is_reported_for_a_long_confirmation_pass():
    driver = _app()
    result = _run(driver)
    seen: list[str] = []
    triage(result.findings, driver=driver, recorder=result.recorder,
           on_progress=seen.append)
    assert seen and "confirming" in seen[0]
