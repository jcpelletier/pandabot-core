"""Tests for the WCAG 2.2 AA pack.

The unit half runs anywhere. The last block runs axe-core against a real page
with deliberate accessibility failures, and skips cleanly when Playwright or
axe-core cannot be obtained -- a run without axe is a smaller rule pack, not a
failed run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pandabot_core.active_qa import Element, Observation
from pandabot_core.active_qa.rules import BLOCKER, MAJOR, MINOR, POLISH
from pandabot_core.active_qa.wcag import (
    AXE_VERSION, JUDGED_CRITERIA, WCAG_TAGS, axe_findings, coverage_note,
    keyboard_findings, resolve_axe_source,
)

_LOCAL_AXE = Path(r"C:\Users\genes\GitHub\WebQATest\node_modules\axe-core\axe.min.js")


def _violation(rule="color-contrast", impact="serious", nodes=1, **kw):
    return {
        "id": rule, "impact": impact,
        "help": kw.get("help", "Elements must have sufficient colour contrast"),
        "description": kw.get("description", "Ensures contrast meets AA"),
        "helpUrl": "https://example/rule",
        "tags": kw.get("tags", ["cat.color", "wcag2aa", "wcag143"]),
        "nodes": [{"target": [f"#n{i}"]} for i in range(nodes)],
    }


def _obs(*elements: Element) -> Observation:
    return Observation(elements=elements or (
        Element(id="e1", role="button", label="Go", handle="#go"),
        Element(id="e2", role="link", label="Home", handle="#home"),
    ))


# --- Turning axe results into findings ------------------------------------

def test_a_violation_becomes_one_finding():
    findings = axe_findings({"violations": [_violation()]}, state="s1",
                            screen_id="home")
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "wcag/color-contrast"
    assert f.severity == MAJOR
    assert f.state == "s1" and f.screen_id == "home"


def test_many_offending_nodes_are_one_defect_not_many():
    """Colour contrast on forty list items is one defect with forty instances.
    Forty findings would bury everything else."""
    findings = axe_findings({"violations": [_violation(nodes=40)]})
    assert len(findings) == 1
    assert "40 element(s)" in findings[0].observed
    assert findings[0].details["nodes"] == 40


def test_only_a_few_selectors_are_shown():
    observed = axe_findings({"violations": [_violation(nodes=10)]})[0].observed
    assert "and 7 more" in observed


def test_impact_maps_to_severity_without_softening():
    """The requirement is that web UX follows WCAG, so a critical violation is
    a blocker rather than a polish item."""
    for impact, severity in (("critical", BLOCKER), ("serious", MAJOR),
                             ("moderate", MINOR), ("minor", POLISH)):
        f = axe_findings({"violations": [_violation(impact=impact)]})[0]
        assert f.severity == severity, impact


def test_an_unknown_impact_does_not_crash():
    f = axe_findings({"violations": [_violation(impact="bananas")]})[0]
    assert f.severity == MINOR


def test_success_criteria_are_recorded():
    f = axe_findings({"violations": [_violation()]})[0]
    assert "wcag143" in f.details["criteria"]
    assert f.details["help_url"]


def test_findings_cluster_by_rule_and_first_node():
    a = axe_findings({"violations": [_violation()]}, state="s1")[0]
    b = axe_findings({"violations": [_violation()]}, state="s1")[0]
    assert a.cluster_key == b.cluster_key


def test_an_empty_result_produces_nothing():
    assert axe_findings({}) == []
    assert axe_findings({"violations": []}) == []


# --- Keyboard -------------------------------------------------------------

def test_unreachable_controls_are_reported():
    obs = _obs()
    findings = keyboard_findings(["h:#go"], obs, state="s1")
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "wcag/keyboard-reachable" and f.severity == MAJOR
    assert "Home" in f.observed
    assert f.details["reached"] == 1 and f.details["total"] == 2


def test_a_fully_reachable_screen_is_quiet():
    assert keyboard_findings(["h:#go", "h:#home"], _obs()) == []


def test_a_keyboard_trap_is_a_blocker_and_stops_the_check():
    """Focus that cannot move on makes the page unusable by keyboard."""
    findings = keyboard_findings(["h:#go"], _obs(), trapped_at="h:#go")
    assert len(findings) == 1
    assert findings[0].rule == "wcag/keyboard-trap"
    assert findings[0].severity == BLOCKER
    assert "2.1.2" in findings[0].details["criteria"]


def test_a_traversal_that_reached_nothing_reports_nothing():
    """Reaching zero controls means the traversal failed, not that the page is
    inaccessible -- claiming otherwise would be a false blocker on every state."""
    assert keyboard_findings([], _obs()) == []


# --- Honesty about coverage -----------------------------------------------

def test_the_coverage_note_states_the_limit():
    note = coverage_note()
    assert "roughly a third" in note
    assert "not a statement that the page conforms" in note
    assert AXE_VERSION in note


def test_judged_criteria_are_declared_so_silence_is_not_a_pass():
    assert "1.1.1" in JUDGED_CRITERIA and "2.4.3" in JUDGED_CRITERIA
    assert all(isinstance(v, str) and v for v in JUDGED_CRITERIA.values())


def test_the_ruleset_is_pinned():
    """An unpinned audit tool silently changes what conformant means."""
    assert AXE_VERSION.count(".") == 2
    assert "wcag22aa" in WCAG_TAGS


# --- Obtaining axe --------------------------------------------------------

def test_an_explicit_path_is_used(tmp_path):
    js = tmp_path / "axe.js"
    js.write_text("/* fake axe */", encoding="utf-8")
    assert resolve_axe_source(js) == "/* fake axe */"


def test_a_missing_path_returns_none_rather_than_raising():
    assert resolve_axe_source("/no/such/axe.js", allow_download=False) is None


def test_the_cache_is_used_when_present(tmp_path):
    cached = tmp_path / f"axe-{AXE_VERSION}.min.js"
    cached.write_text("/* cached */", encoding="utf-8")
    assert resolve_axe_source(cache_dir=tmp_path) == "/* cached */"


def test_no_axe_and_no_download_is_none_not_an_exception(tmp_path):
    """A run without axe is a smaller rule pack, not a failed run."""
    assert resolve_axe_source(cache_dir=tmp_path, allow_download=False) is None


# --- Against a real browser -----------------------------------------------

playwright = pytest.importorskip("playwright.sync_api",
                                 reason="Playwright is not installed")
from pandabot_core.active_qa.drivers.web import PlaywrightDriver  # noqa: E402

INACCESSIBLE = """<!doctype html><html><head><meta charset="utf-8"><title>A11y fixture</title>
<style>
 body{background:#fff;font:15px system-ui;padding:20px}
 .faint{color:#c8c8c8;background:#fff}          /* fails colour contrast */
 button{font:inherit;padding:8px 14px;margin:4px}
</style></head><body>
  <h1>Accessibility fixture</h1>
  <p class="faint">This text has deliberately poor contrast against the background.</p>

  <!-- An image with no alt text at all. -->
  <img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" width="40" height="40">

  <!-- An input with no label of any kind. -->
  <input id="unlabelled" type="text">

  <!-- Reachable and labelled: these must NOT be reported. -->
  <label for="named">Your name</label>
  <input id="named" type="text">
  <button id="ok" data-testid="ok">Continue</button>

  <!-- Removed from the tab order, so keyboard cannot reach it. -->
  <button id="unreachable" data-testid="unreachable" tabindex="-1">Hidden from keyboard</button>
</body></html>"""


@pytest.fixture(scope="module")
def a11y_url(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("a11y") / "fixture.html"
    path.write_text(INACCESSIBLE, encoding="utf-8")
    return path.as_uri()


@pytest.fixture
def driver(a11y_url):
    try:
        d = PlaywrightDriver(a11y_url, capture_screenshots=False, render_hashes=False)
        d.start()
    except Exception as exc:
        pytest.skip(f"cannot launch a browser: {exc}")
    yield d
    d.close()


@pytest.fixture(scope="module")
def axe_js() -> str:
    js = resolve_axe_source(_LOCAL_AXE if _LOCAL_AXE.exists() else None)
    if not js:
        pytest.skip("axe-core could not be obtained")
    return js


def test_axe_finds_the_planted_violations(driver, axe_js):
    results = driver.run_axe(axe_js, tags=WCAG_TAGS)
    assert results, "axe should have run"
    findings = axe_findings(results, state="s1", screen_id="fixture")
    rules = {f.rule for f in findings}
    assert "wcag/image-alt" in rules, f"missing alt text should be caught: {rules}"
    assert "wcag/color-contrast" in rules, f"poor contrast should be caught: {rules}"
    assert any("label" in r for r in rules), f"unlabelled input: {rules}"


def test_axe_findings_carry_usable_evidence(driver, axe_js):
    findings = axe_findings(driver.run_axe(axe_js, tags=WCAG_TAGS), state="s1")
    assert findings
    for f in findings:
        assert f.summary and f.observed and f.expected
        assert f.details["axe_rule"] and f.details["nodes"] >= 1


def test_tab_order_reaches_the_focusable_controls(driver):
    reached, trapped = driver.tab_order()
    assert trapped is None
    assert 'h:[data-testid="ok"]' in reached, reached
    assert "h:#named" in reached


def test_a_control_removed_from_the_tab_order_is_reported(driver):
    """tabindex=-1 on a real button: the mouse can use it, a keyboard cannot."""
    reached, trapped = driver.tab_order()
    findings = keyboard_findings(reached, driver.observe(), state="s1")
    assert findings, "the unreachable button should be reported"
    assert findings[0].rule == "wcag/keyboard-reachable"
    assert "Hidden from keyboard" in findings[0].observed


def test_a_clean_page_produces_no_keyboard_findings(tmp_path):
    path = tmp_path / "clean.html"
    path.write_text('<!doctype html><title>Clean</title>'
                    '<button id="a">One</button><button id="b">Two</button>',
                    encoding="utf-8")
    d = PlaywrightDriver(path.as_uri(), capture_screenshots=False, render_hashes=False)
    try:
        d.start()
    except Exception as exc:
        pytest.skip(f"cannot launch a browser: {exc}")
    try:
        reached, trapped = d.tab_order()
        assert trapped is None
        assert keyboard_findings(reached, d.observe()) == []
    finally:
        d.close()
