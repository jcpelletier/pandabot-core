"""Tests for the Playwright web driver, against a real browser.

These are the first tests in this package that touch a real backend, so they
skip cleanly when Playwright or its browsers are absent -- the rest of the suite
must keep running on a machine that has neither.

The fixture page carries defects planted on purpose: a dead button, a field that
drops what you type, a disabled control that acts anyway, and a button with no
pressed state. Knowing the answer in advance is the only way to measure whether
the rulebook actually finds it, which is what the walking skeleton exists to do.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pandabot_core.active_qa import (
    Action, Budgets, Deck, ExplorationLoop, Journal,
)
from pandabot_core.active_qa.contract import fingerprint_policy_for
from pandabot_core.active_qa.rules import run_step_detectors, StepContext

playwright = pytest.importorskip("playwright.sync_api",
                                 reason="Playwright is not installed")
from pandabot_core.active_qa.drivers.web import PlaywrightDriver  # noqa: E402


FIXTURE = """<!doctype html><html><head><meta charset="utf-8"><title>Fixture</title>
<style>
  body{font:15px system-ui;margin:0;padding:24px;background:#f7f7f5}
  button{font:inherit;padding:10px 16px;margin:6px 0;border:1px solid #bbb;
         border-radius:6px;background:#fff;display:block;min-width:220px}
  /* Good button: has hover AND pressed states. */
  #good:hover{background:#eef}
  #good:active{background:#99f;transform:translateY(1px)}
  /* Bad button: no :active rule at all, so rule U1 should fire. */
  #nostates{background:#fff}
  input{font:inherit;padding:8px;min-width:220px;display:block;margin:6px 0}
  #panel{display:none;padding:12px;border:1px solid #ccc;background:#fff;margin-top:12px}
  #panel.open{display:block}
</style></head><body>
  <h1>Fixture app</h1>

  <button id="good" data-testid="good">Opens the panel</button>
  <button id="dead" data-testid="dead">Does nothing</button>
  <button id="nostates" data-testid="nostates">No pressed state</button>
  <button id="disabled-acts" data-testid="disabled-acts" disabled>Disabled but acts</button>

  <input id="echoes" data-testid="echoes" placeholder="Echoes what you type">
  <input id="swallows" data-testid="swallows" placeholder="Swallows what you type">

  <div id="panel">The panel is open.</div>

<script>
  document.getElementById('good').onclick = () => {
    document.getElementById('panel').classList.toggle('open');
  };
  // #dead has no handler at all -- the U7 case.
  // A disabled button still wired up: browsers suppress the click, so drive the
  // U5 case through a capture-phase listener on the document instead.
  document.addEventListener('click', (e) => {
    if (e.target && e.target.id === 'disabled-acts') {
      document.title = 'changed by a disabled control';
    }
  }, true);
  // A field that drops everything typed into it -- the U6 case.
  document.getElementById('swallows').addEventListener('input', (e) => {
    e.target.value = '';
  });
</script></body></html>"""


@pytest.fixture(scope="module")
def fixture_url(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("web") / "fixture.html"
    path.write_text(FIXTURE, encoding="utf-8")
    return path.as_uri()


@pytest.fixture
def driver(fixture_url, tmp_path):
    try:
        d = PlaywrightDriver(fixture_url, screenshot_dir=tmp_path / "shots")
        d.start()
    except Exception as exc:  # browsers not installed, or no display
        pytest.skip(f"cannot launch a browser: {exc}")
    yield d
    d.close()


def _fp(driver, obs):
    """Fingerprint the way the loop does: with the driver's own policy."""
    return obs.compute_fingerprint(fingerprint_policy_for(driver))


def _el(obs, testid):
    return obs.by_identity(f'h:[data-testid="{testid}"]')


# --- Observation ----------------------------------------------------------

def test_observe_finds_the_controls(driver):
    obs = driver.observe()
    labels = {e.label for e in obs.interactive()}
    assert "Opens the panel" in labels
    assert "Does nothing" in labels


def test_roles_are_normalised(driver):
    obs = driver.observe()
    assert _el(obs, "good").role == "button"
    assert _el(obs, "echoes").role == "textbox"


def test_test_ids_become_the_handle(driver):
    """A project that has adopted test ids gets real, stable element identity."""
    obs = driver.observe()
    assert _el(obs, "good") is not None
    assert _el(obs, "good").handle == '[data-testid="good"]'


def test_identity_is_stable_across_observations(driver):
    first, second = driver.observe(), driver.observe()
    assert {e.identity for e in first.interactive()} == \
           {e.identity for e in second.interactive()}


def test_disabled_state_is_reported(driver):
    obs = driver.observe()
    assert _el(obs, "disabled-acts").enabled is False
    assert _el(obs, "good").enabled is True


def test_placeholder_is_used_as_a_label(driver):
    assert _el(driver.observe(), "echoes").label == "Echoes what you type"


def test_field_contents_are_read_back(driver):
    driver.act(Action(kind="type", target=_el(driver.observe(), "echoes").id,
                      value="hello"))
    assert _el(driver.observe(), "echoes").text == "hello"


def test_hidden_elements_are_excluded(driver):
    """The closed panel holds no controls, and display:none must not surface."""
    obs = driver.observe()
    assert "The panel is open." not in obs.text


def test_screenshots_are_captured(driver, tmp_path):
    obs = driver.observe()
    assert obs.screenshot and Path(obs.screenshot).exists()


def test_render_hashes_are_attached(driver):
    """Rules U1 to U3 depend on these entirely."""
    obs = driver.observe()
    assert _el(obs, "good").render_hash


def test_screen_id_is_readable(driver):
    assert "Fixture" in driver.observe().screen_id


def test_fingerprint_is_stable_when_nothing_happens(driver):
    """If this is flaky, no-op detection is worthless."""
    prints = {_fp(driver, driver.observe()) for _ in range(5)}
    assert len(prints) == 1


# --- Acting ---------------------------------------------------------------

def test_click_changes_state(driver):
    before = driver.observe()
    assert driver.act(Action(kind="click", target=_el(before, "good").id)).ok
    after = driver.observe()
    assert _fp(driver, after) != _fp(driver, before)
    assert "The panel is open." in after.text


def test_a_dead_control_changes_nothing(driver):
    before = driver.observe()
    assert driver.act(Action(kind="click", target=_el(before, "dead").id)).ok
    assert _fp(driver, driver.observe()) == _fp(driver, before)


def test_typing_lands_in_the_field(driver):
    obs = driver.observe()
    driver.act(Action(kind="type", target=_el(obs, "echoes").id, value="abc"))
    assert _el(driver.observe(), "echoes").text == "abc"


def test_hover_and_focus_are_supported(driver):
    obs = driver.observe()
    assert driver.act(Action(kind="hover", target=_el(obs, "good").id)).ok
    assert driver.act(Action(kind="focus", target=_el(obs, "good").id)).ok


def test_press_hold_changes_the_render_hash_of_a_good_button(driver):
    """The mechanism rule U1 is built on, proved against a real :active style."""
    at_rest = _el(driver.observe(), "good").render_hash
    driver.act(Action(kind="press_hold", target=_el(driver.observe(), "good").id))
    during = _el(driver.observe(), "good").render_hash
    assert at_rest and during
    assert at_rest != during


def test_a_button_without_an_active_style_does_not_change(driver):
    """And the negative case, which is what U1 reports."""
    at_rest = _el(driver.observe(), "nostates").render_hash
    driver.act(Action(kind="press_hold", target=_el(driver.observe(), "nostates").id))
    during = _el(driver.observe(), "nostates").render_hash
    assert at_rest == during


def test_a_failed_action_is_reported_not_raised(driver):
    result = driver.act(Action(kind="click", target="e999"))
    assert not result.ok and "unknown element" in result.error


def test_stale_element_ids_are_rejected_clearly(driver):
    """Ids belong to the observation that produced them."""
    driver.observe()
    result = driver.act(Action(kind="click", target="e9999"))
    assert "only valid for the observation" in result.error


def test_console_errors_surface_in_the_log_delta(driver):
    driver._page.evaluate("console.error('boom')")
    assert any("boom" in line for line in driver.observe().logs)


def test_logs_are_drained_between_observations(driver):
    driver._page.evaluate("console.error('once')")
    assert any("once" in line for line in driver.observe().logs)
    assert driver.observe().logs == ()


# --- Relaunch -------------------------------------------------------------

def test_relaunch_is_a_genuinely_cold_start(driver):
    """Not just navigation home: storage has to go, or a replay starts dirty."""
    obs = driver.observe()
    driver.act(Action(kind="type", target=_el(obs, "echoes").id, value="typed"))
    driver.act(Action(kind="click", target=_el(obs, "good").id))
    driver._page.evaluate("localStorage.setItem('k', 'v')")

    driver.relaunch()

    after = driver.observe()
    assert _el(after, "echoes").text == ""
    assert "The panel is open." not in after.text
    assert driver._page.evaluate("localStorage.getItem('k')") is None


def test_health_is_ok_while_the_page_lives(driver):
    assert driver.health().ok


# --- The rulebook against a real page -------------------------------------

def test_the_planted_defects_are_found_and_the_good_controls_are_not(driver):
    """The precision measurement in miniature: four planted defects, and the
    correct controls must stay silent."""
    findings: list = []

    def step(action_kind, testid, value=None):
        before = driver.observe()
        el = _el(before, testid)
        action = Action(kind=action_kind, target=el.id, value=value)
        result = driver.act(action)
        after = driver.observe()
        findings.extend(run_step_detectors(StepContext(
            action=action, before=before, after=after,
            before_fingerprint=_fp(driver, before),
            after_fingerprint=_fp(driver, after),
            ok=result.ok)))

    step("click", "dead")                       # U7
    step("type", "swallows", "hello")           # U6
    step("click", "good")                       # nothing: it works
    step("type", "echoes", "hello")             # nothing: it echoes

    rules = {f.rule for f in findings}
    assert "U7" in rules, "the dead button should have been caught"
    assert "U6" in rules, "the swallowing field should have been caught"
    culprits = {f.element_identity for f in findings}
    assert 'h:[data-testid="good"]' not in culprits
    assert 'h:[data-testid="echoes"]' not in culprits


# --- End to end -----------------------------------------------------------

def test_a_full_loop_run_against_a_real_page(driver, tmp_path):
    """Everything at once: loop, rulebook, recorder, journal, real browser."""
    journal = Journal(tmp_path / "journal", session_id="aq-web-test")
    loop = ExplorationLoop(driver, budgets=Budgets(min_seconds=0, max_actions=40),
                           deck=Deck(cards=()), journal=journal)
    result = loop.run()

    assert result.stats.actions > 0
    assert result.coverage.states, "at least one state should be mapped"
    assert len(result.recorder) > 0

    rules = {f.rule for f in result.findings}
    assert "U7" in rules or "U6" in rules, f"expected a planted defect, got {rules}"

    # Every finding must carry a trace that starts at a cold start.
    for finding in result.findings:
        if finding.step_index >= 0:
            trace = result.recorder.trace_for(finding.step_index)
            assert trace.steps[0].epoch == trace.target.epoch
            assert trace.repro_steps()

    page = (tmp_path / "journal" / "journal.html")
    assert page.exists() and page.stat().st_size > 500
    assert journal.stats()["frames_stored"] > 0
