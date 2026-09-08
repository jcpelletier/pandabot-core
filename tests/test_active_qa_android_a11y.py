"""Tests for the Android accessibility pack.

The last test runs against the real handset when it is reachable, because a
tree-shaped check is exactly the kind that passes its own fixtures and then
meets a real Compose dump.
"""

from __future__ import annotations

import pytest

from pandabot_core.active_qa import Element, Observation
from pandabot_core.active_qa.android_a11y import (
    JUDGED_CHECKS, MIN_TOUCH_TARGET_DP, android_a11y_findings, coverage_note,
    dp_to_px,
)
from pandabot_core.active_qa.rules import MAJOR, MINOR


def _btn(name="Go", *, box=(0, 0, 200, 200), **kw) -> Element:
    kw.setdefault("role", "button")
    return Element(id=f"e_{name or 'x'}", label=name, bounds=box, **kw)


def _obs(*elements: Element) -> Observation:
    return Observation(elements=elements)


def _rules(findings) -> set[str]:
    return {f.rule for f in findings}


# --- Density --------------------------------------------------------------

def test_dp_converts_against_density():
    """48dp is 48px at 160dpi and 192px on the 640dpi handset."""
    assert dp_to_px(48, 160) == 48
    assert dp_to_px(48, 640) == 192


def test_touch_targets_are_judged_physically_not_in_pixels():
    """A 100px control passes on a low-density screen and fails on a dense one.
    Comparing raw pixels would get both wrong."""
    obs = _obs(_btn(box=(0, 0, 100, 100)))
    assert "a11y/touch-target" not in _rules(
        android_a11y_findings(obs, density_dpi=160))
    assert "a11y/touch-target" in _rules(
        android_a11y_findings(obs, density_dpi=640))


# --- Unlabelled controls --------------------------------------------------

def test_an_unlabelled_control_is_reported():
    """TalkBack announces "button" and nothing else."""
    findings = android_a11y_findings(_obs(_btn(""), _btn("Save")))
    unlabelled = [f for f in findings if f.rule == "a11y/unlabelled-control"]
    assert len(unlabelled) == 1
    assert unlabelled[0].severity == MAJOR
    assert unlabelled[0].details["count"] == 1


def test_labelled_controls_are_quiet():
    assert "a11y/unlabelled-control" not in _rules(
        android_a11y_findings(_obs(_btn("Save"), _btn("Cancel"))))


def test_whitespace_is_not_a_label():
    assert "a11y/unlabelled-control" in _rules(
        android_a11y_findings(_obs(_btn("   "))))


def test_many_unlabelled_controls_are_one_finding():
    findings = android_a11y_findings(_obs(*[_btn("") for _ in range(9)]))
    unlabelled = [f for f in findings if f.rule == "a11y/unlabelled-control"]
    assert len(unlabelled) == 1
    assert unlabelled[0].details["count"] == 9
    assert "and 4 more" in unlabelled[0].observed


# --- Touch targets --------------------------------------------------------

def test_a_small_target_is_reported_with_the_arithmetic_shown():
    f = [f for f in android_a11y_findings(
        _obs(_btn("Tiny", box=(0, 0, 40, 40))), density_dpi=160)
        if f.rule == "a11y/touch-target"][0]
    assert f.severity == MINOR
    assert "48dp is 48px" in f.observed
    assert "40x40px" in f.observed


def test_a_target_short_in_only_one_dimension_still_fails():
    """A wide, thin control is as hard to hit as a small square one."""
    assert "a11y/touch-target" in _rules(android_a11y_findings(
        _obs(_btn("Thin", box=(0, 0, 400, 20))), density_dpi=160))


def test_a_control_with_no_bounds_is_skipped_not_failed():
    assert "a11y/touch-target" not in _rules(
        android_a11y_findings(_obs(_btn("No box", box=None))))


def test_the_minimum_is_configurable():
    obs = _obs(_btn("Small", box=(0, 0, 40, 40)))
    assert "a11y/touch-target" not in _rules(
        android_a11y_findings(obs, density_dpi=160, min_dp=24))


# --- Duplicate labels -----------------------------------------------------

def test_duplicate_labels_are_reported():
    """Two controls announced identically cannot be told apart by ear."""
    f = [f for f in android_a11y_findings(
        _obs(_btn("Buy", box=(0, 0, 200, 200)),
             _btn("Buy", box=(0, 300, 200, 500)),
             _btn("Sell", box=(0, 600, 200, 800))))
        if f.rule == "a11y/duplicate-label"][0]
    assert f.details["duplicates"] == {"buy": 2}


def test_distinct_labels_are_quiet():
    assert "a11y/duplicate-label" not in _rules(android_a11y_findings(
        _obs(_btn("Buy"), _btn("Sell"))))


def test_duplicate_detection_ignores_case():
    assert "a11y/duplicate-label" in _rules(android_a11y_findings(
        _obs(_btn("Buy"), _btn("BUY"))))


# --- Scope ----------------------------------------------------------------

def test_disabled_controls_are_not_judged():
    """A disabled control is not something a user is trying to operate."""
    assert android_a11y_findings(
        _obs(_btn("", box=(0, 0, 10, 10), enabled=False))) == []


def test_non_interactive_text_is_not_judged():
    obs = Observation(elements=(Element(id="t", role="text", label="",
                                        bounds=(0, 0, 10, 10)),))
    assert android_a11y_findings(obs) == []


def test_foreign_ui_is_not_judged():
    """Run against the notification shade, this pack once reported "gmail" and a
    Cloud Print notification as duplicate labels. Those are Android's defects,
    not the app's."""
    obs = Observation(elements=(_btn(""), _btn("")),
                      meta={"in_app": False, "foreground_package": "com.android.systemui"})
    assert android_a11y_findings(obs) == []


def test_an_observation_that_cannot_say_is_judged_normally():
    """A driver that does not report in_app should not silently disable the pack."""
    assert android_a11y_findings(_obs(_btn(""))) != []


def test_an_empty_screen_produces_nothing():
    assert android_a11y_findings(Observation()) == []


def test_a_clean_screen_produces_nothing():
    assert android_a11y_findings(
        _obs(_btn("Buy", box=(0, 0, 200, 200)),
             _btn("Sell", box=(0, 300, 200, 500))), density_dpi=160) == []


# --- Honesty about coverage -----------------------------------------------

def test_contrast_is_declared_unchecked_rather_than_passed():
    """It needs the rendered pixels; guessing from the tree would be confident
    nonsense."""
    assert "text-contrast" in JUDGED_CHECKS
    note = coverage_note()
    assert "not checked" in note
    assert "not a statement that the app is accessible" in note
    assert str(MIN_TOUCH_TARGET_DP) in note


# --- Against the real handset ---------------------------------------------

def test_the_pack_runs_against_the_real_device():
    """A tree-shaped check passes its own fixtures and then meets a real
    Compose dump, where nothing has a resource id and labels live one node down."""
    from pandabot_core.active_qa.drivers.android import AndroidDriver, RemoteAdb

    adb = RemoteAdb("genesis@192.168.1.100", serial="192.168.1.172:5555",
                    ssh=("wsl", "ssh", "-o", "ConnectTimeout=15"))
    rc, out, _err = adb.run(["get-state"], timeout=25)
    if rc != 0 or out.decode("utf-8", "replace").strip() != "device":
        pytest.skip("the test handset is not reachable")

    driver = AndroidDriver("com.example.spacetrader", activity=".MainActivity",
                           transport=adb, capture_screenshots=False,
                           clear_data_on_relaunch=False)
    try:
        driver.start()
        observation = driver.observe()
    except Exception as exc:
        pytest.skip(f"could not drive the device: {exc}")

    findings = android_a11y_findings(observation, density_dpi=driver.density(),
                                     state="s1", screen_id=observation.screen_id)
    # Not asserting specific defects: the point is that it runs on a real tree
    # and produces well-formed findings rather than crashing on it.
    for f in findings:
        assert f.rule.startswith("a11y/")
        assert f.summary and f.observed and f.expected
        assert f.element_identity
    driver.close()
