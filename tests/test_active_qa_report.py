"""Tests for the markdown report and its evidence bundle.

Half of these check that the report says what the run established. The other
half check that it says what the run did *not* establish, which is the part a
reader will otherwise assume — a rule that was never implemented, a rule the
project waived, a target the driver could barely address.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pandabot_core.active_qa import (
    Budgets, Deck, Element, ExplorationLoop, FakeDriver, FakeScreen, Journal,
)
from pandabot_core.active_qa.report import SessionInfo, render_report, write_report
from pandabot_core.active_qa.rules import BLOCKER, MAJOR, MINOR, Finding
from pandabot_core.active_qa.triage import TriagedFinding, TriageReport, triage


def _t(rule="U7", severity=MAJOR, summary="A button does nothing",
       element="h:#dead", confirmed=True, **kw) -> TriagedFinding:
    f = Finding(rule=rule, summary=summary,
                observed="Acting on the control produced no observable change. "
                         "Nothing else happened.",
                expected="A control must produce a state change.",
                element_identity=element, state="s1", screen_id="home",
                severity=severity)
    return TriagedFinding(finding=f, confirmed=confirmed,
                          attempts=kw.pop("attempts", 3),
                          confirmations=kw.pop("confirmations", 3), **kw)


def _report(confirmed=(), unconfirmed=()) -> TriageReport:
    r = TriageReport()
    r.confirmed = list(confirmed)
    r.unconfirmed = list(unconfirmed)
    return r


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


# --- The required fields --------------------------------------------------

def test_every_finding_carries_the_six_required_fields():
    """Title, Description, Repro steps, Observed, Expected, Screenshot."""
    md = render_report(SessionInfo(repo="owner/repo"), _report([_t()]))
    assert "### AQ-1 · A button does nothing" in md
    for field in ("**Description**", "**Repro steps**", "**Observed**",
                  "**Expected**", "**Screenshot**"):
        assert field in md, field


def test_findings_are_numbered_and_ordered_worst_first():
    md = render_report(SessionInfo(), _report([
        _t(severity=MINOR, summary="minor thing", element="h:#a"),
        _t(severity=BLOCKER, summary="fatal thing", element="h:#b"),
    ]))
    assert md.index("fatal thing") < md.index("minor thing")
    assert "### AQ-1 · fatal thing" in md


def test_confirmation_count_is_shown():
    md = render_report(SessionInfo(), _report([_t(attempts=3, confirmations=2)]))
    assert "**Confirmed:** yes (2/3 replays)" in md


def test_repeat_sightings_are_noted():
    md = render_report(SessionInfo(), _report([_t(occurrences=7)]))
    assert "7 times" in md


def test_summary_table_counts_by_severity():
    md = render_report(SessionInfo(), _report([
        _t(severity=BLOCKER, element="h:#a"), _t(severity=BLOCKER, element="h:#b"),
        _t(severity=MINOR, element="h:#c"),
    ]))
    assert "| blocker | 2 |" in md and "| minor | 1 |" in md


def test_a_clean_run_says_so():
    md = render_report(SessionInfo(), _report([]))
    assert "No confirmed findings." in md


# --- What the run did NOT establish ---------------------------------------

def test_unimplemented_rules_are_declared_not_hidden():
    """Absence must never read as compliance."""
    md = render_report(SessionInfo(), _report([]))
    assert "**Not yet implemented:**" in md
    assert "their absence from the findings above means nothing" in md
    for rule_id in ("U4", "U8", "U9", "U11"):
        assert rule_id in md


def test_waivers_are_echoed_with_their_reasons():
    """A project can waive rules in PANDABOTQA.md, and suppression that is
    invisible is how a build waives its way to a clean report."""
    md = render_report(
        SessionInfo(waivers={"U2": "touch-only build, no pointer device"}),
        _report([]))
    assert "**Waived by the project:**" in md
    assert "touch-only build, no pointer device" in md
    # And a waived rule must not appear as in force.
    in_force = md.split("**Rules in force:**")[1].split("\n")[0]
    assert "U2" not in in_force


def test_inapplicable_rules_are_separate_from_waived_ones():
    """A platform that cannot hover is not the same as a project excusing it."""
    md = render_report(
        SessionInfo(inapplicable={"U1": "input tap is atomic on Android"}),
        _report([]))
    assert "**Inapplicable to this target:**" in md
    assert "input tap is atomic" in md
    assert "**Waived by the project:**" not in md


def test_rules_the_harness_could_not_see_are_listed_separately():
    """A vision run cannot read whether a control is disabled. That is neither a
    pass nor "does not apply": it is "could not check"."""
    from pandabot_core.active_qa.vision import WEAKENED_RULES
    md = render_report(SessionInfo(unavailable=dict(WEAKENED_RULES)), _report([]))
    assert "**Could not be checked on this target:**" in md
    assert "cannot be read from pixels" in md
    in_force = md.split("**Rules in force:**")[1].split(chr(10))[0]
    for rule_id in WEAKENED_RULES:
        assert rule_id not in in_force, f"{rule_id} could not be checked"


def test_degraded_identity_is_called_out(tmp_path):
    md = render_report(
        SessionInfo(degraded_identity=True,
                    degraded_note="Compose without test tags: 0 of 4 controls addressable."),
        _report([_t()]))
    assert "Degraded run" in md
    assert "0 of 4 controls addressable" in md


def test_out_of_scope_items_are_listed():
    md = render_report(SessionInfo(out_of_scope=["the debug console itself"]),
                       _report([]))
    assert "the debug console itself" in md


def test_known_issues_are_shown_as_suppressed():
    """Suppresses reporting, not detection, and the reader is told."""
    md = render_report(SessionInfo(known_issues=["GAM-41: freighter sprite clips"]),
                       _report([]))
    assert "Known issues suppressed" in md
    assert "GAM-41" in md


def test_not_reached_is_honest_when_nothing_was_recorded():
    md = render_report(SessionInfo(), _report([]))
    assert "## Not reached" in md
    assert "not the same as full coverage" in md


def test_not_reached_lists_what_was_missed():
    md = render_report(SessionInfo(not_reached=["the settings screen"]),
                       _report([]))
    assert "the settings screen" in md


def test_unconfirmed_candidates_are_separated_and_labelled():
    md = render_report(SessionInfo(), _report(
        confirmed=[],
        unconfirmed=[_t(confirmed=False, confirmations=0, attempts=3),
                     _t(confirmed=False, element="h:#x", diverged=True,
                        confirmations=0, attempts=0)]))
    assert "## Unconfirmed candidates (2)" in md
    assert "not as defects" in md
    assert "did not reproduce" in md
    assert "unverifiable" in md


def test_a_candidate_that_could_not_be_replayed_says_so_not_that_it_failed():
    md = render_report(SessionInfo(), _report([
        _t(confirmed=False, diverged=True, attempts=0, confirmations=0)]))
    assert "could not be replayed" in md


# --- Header ---------------------------------------------------------------

def test_header_carries_the_session_facts():
    md = render_report(
        SessionInfo(session_id="aq-1", repo="jcpelletier/Space-Trader",
                    target="android", runner="panda", model="qwen3.8:27b",
                    duration_seconds=3661, stop_reason="max time reached",
                    brief="focus on the station build screen"),
        _report([]), stats={"actions": 2318}, coverage={"states": 47})
    assert "jcpelletier/Space-Trader" in md
    assert "aq-1" in md and "android" in md and "qwen3.8:27b" in md
    assert "1h01m" in md and "2318 actions" in md and "47 states" in md
    assert "focus on the station build screen" in md
    assert "max time reached" in md


def test_coverage_section_reports_the_numbers():
    md = render_report(SessionInfo(), _report([]),
                       stats={"actions": 100, "model_calls": 8, "cards_played": 3},
                       coverage={"states": 12, "transitions": 40, "dead_ends": 1})
    assert "| Actions | 100 |" in md
    assert "| Model calls | 8 |" in md
    assert "| States visited | 12 |" in md


def test_journal_is_linked_when_present():
    md = render_report(SessionInfo(), _report([]), journal_path="journal/journal.html")
    assert "journal/journal.html" in md


# --- Writing to disk ------------------------------------------------------

def test_write_report_produces_a_bundle(tmp_path):
    out = write_report(tmp_path, SessionInfo(repo="o/r"), _report([_t()]))
    assert out.name == "report.md" and out.exists()
    assert (tmp_path / "evidence").is_dir()
    assert (tmp_path / "findings.json").exists()


def test_findings_json_is_machine_readable(tmp_path):
    write_report(tmp_path, SessionInfo(repo="o/r", session_id="aq-1"),
                 _report([_t()], [_t(confirmed=False)]))
    data = json.loads((tmp_path / "findings.json").read_text(encoding="utf-8"))
    assert data["session"]["session_id"] == "aq-1"
    assert len(data["confirmed"]) == 1 and len(data["unconfirmed"]) == 1
    assert data["confirmed"][0]["confidence"] == "confirmed"


def test_evidence_is_copied_not_referenced(tmp_path):
    """The harness's screenshot directory is temporary; a report whose evidence
    has been cleaned up is not evidence."""
    driver = _app()
    result = ExplorationLoop(driver, budgets=Budgets(min_seconds=0, max_actions=20),
                             deck=Deck(cards=()), clock=lambda: 0.0).run()
    report = triage(result.findings, driver=driver, recorder=result.recorder)
    assert report.confirmed

    out = write_report(tmp_path / "r", SessionInfo(repo="o/r"), report)
    md = out.read_text(encoding="utf-8")
    assert "evidence/AQ-1-trace.json" in md
    trace_file = tmp_path / "r" / "evidence" / "AQ-1-trace.json"
    assert trace_file.exists()
    trace = json.loads(trace_file.read_text(encoding="utf-8"))
    assert trace["steps"], "the trace must carry the actions to reproduce"


def test_screenshots_are_copied_when_the_driver_captured_them(tmp_path):
    shot = tmp_path / "s.png"
    shot.write_bytes(b"\x89PNG fake")
    driver = _app()
    result = ExplorationLoop(driver, budgets=Budgets(min_seconds=0, max_actions=20),
                             deck=Deck(cards=()), clock=lambda: 0.0).run()
    rep = triage(result.findings, driver=driver, recorder=result.recorder)
    # Point the confirmed finding's trace at a real file.
    from pandabot_core.active_qa.recorder import Step, Trace
    t = rep.confirmed[0]
    steps = list(t.trace.steps)
    steps[-1] = Step(**{**steps[-1].to_dict(), "action": steps[-1].action,
                        "result": steps[-1].result, "logs": steps[-1].logs,
                        "after_screenshot": str(shot)})
    t.trace = Trace(epoch=t.trace.epoch, steps=tuple(steps),
                    target_index=t.trace.target_index)

    write_report(tmp_path / "r2", SessionInfo(), rep)
    copied = list((tmp_path / "r2" / "evidence").glob("AQ-1-after.*"))
    assert copied and copied[0].read_bytes().startswith(b"\x89PNG")


# --- End to end -----------------------------------------------------------

def test_a_full_run_produces_a_readable_report(tmp_path):
    driver = _app()
    journal = Journal(tmp_path / "journal", session_id="aq-e2e")
    result = ExplorationLoop(driver, budgets=Budgets(min_seconds=0, max_actions=30),
                             deck=Deck(cards=()), journal=journal,
                             clock=lambda: 0.0).run()
    report = triage(result.findings, driver=driver, recorder=result.recorder)

    info = SessionInfo(
        session_id="aq-e2e", repo="jcpelletier/fixture", target="fake",
        runner="panda", stop_reason=result.stop_reason,
        inapplicable={"U1": "the fake driver supports only click"},
        not_reached=["nothing: the frontier was exhausted"])
    out = write_report(tmp_path / "report", info, report,
                       stats=result.stats.to_dict(),
                       coverage=result.coverage.stats(),
                       journal_path="../journal/journal.html")

    md = out.read_text(encoding="utf-8")
    assert md.startswith("# Active QA report — jcpelletier/fixture")
    assert "Dead" in md                      # the planted defect, by its label
    assert "**Repro steps**" in md
    assert "1. click " in md
    assert "## Coverage" in md
    assert "## Not reached" in md
    assert "the fake driver supports only click" in md


# --- Shortest-path repro --------------------------------------------------

def test_repro_prefers_the_shortest_observed_route(tmp_path):
    """The recorded trace is the whole wandering epoch, which is right for
    machine replay and unusable for a person. The map knows a shorter way."""
    driver = _app()
    result = ExplorationLoop(driver, budgets=Budgets(min_seconds=0, max_actions=60),
                             deck=Deck(cards=()), clock=lambda: 0.0).run()
    rep = triage(result.findings, driver=driver, recorder=result.recorder)
    assert rep.confirmed

    entry = result.recorder.steps[0].before_fingerprint
    long_md = render_report(SessionInfo(), rep)
    short_md = render_report(SessionInfo(), rep,
                             coverage_map=result.coverage, entry_state=entry)

    # Count numbered repro lines without embedding newline escapes.
    def numbered(md):
        return sum(1 for line in md.splitlines()
                   if line[:2].rstrip('.').isdigit() and '. ' in line)

    assert numbered(short_md) < numbered(long_md)
    assert "Shortest route the run observed" in short_md


def test_shortest_route_falls_back_when_the_map_knows_no_path():
    """No route means show the full trace rather than nothing."""
    driver = _app()
    result = ExplorationLoop(driver, budgets=Budgets(min_seconds=0, max_actions=40),
                             deck=Deck(cards=()), clock=lambda: 0.0).run()
    rep = triage(result.findings, driver=driver, recorder=result.recorder)
    md = render_report(SessionInfo(), rep, coverage_map=result.coverage,
                       entry_state="sha1:nosuchstate")
    assert "**Repro steps**" in md
    assert "Shortest route the run observed" not in md


def test_path_between_finds_the_short_way():
    from pandabot_core.active_qa.coverage import Candidate, CoverageMap
    from pandabot_core.active_qa import Observation
    m = CoverageMap()
    for fp in ("a", "b", "c"):
        m.observe(Observation(elements=(Element(id="e1", role="button", handle="#x"),)), fp)
    m.record_transition("a", Candidate(state="a", element_identity="h:#ab", kind="click"), "b")
    m.record_transition("b", Candidate(state="b", element_identity="h:#bc", kind="click"), "c")
    m.record_transition("a", Candidate(state="a", element_identity="h:#ac", kind="click"), "c")
    path = m.path_between("a", "c")
    assert len(path) == 1 and path[0].element_identity == "h:#ac"
    assert m.path_between("a", "a") == []
    assert m.path_between("c", "a") is None
