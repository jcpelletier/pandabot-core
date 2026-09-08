"""Tests for vision-only mode.

The behaviour that matters most here is what the mode refuses to claim. A vision
run is genuinely weaker than a tree-backed one, and presenting the two as
equivalent would be the most consequential dishonesty this harness could commit:
it would report a clean run on a target it could barely see.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pandabot_core.active_qa import (
    Budgets, Deck, Element, ExplorationLoop, FakeDriver, FakeScreen, Observation,
)
from pandabot_core.active_qa.vision import (
    WEAKENED_RULES, degraded_note, encode_image, propose_elements,
    vision_observation,
)


class FakeVision:
    def __init__(self, *answers: str, fail: Exception | None = None) -> None:
        self.answers = list(answers)
        self.fail = fail
        self.calls: list[tuple[str, str, str]] = []

    def complete_with_image(self, system: str, user: str, image: str) -> str:
        self.calls.append((system, user, image))
        if self.fail is not None:
            raise self.fail
        return self.answers.pop(0) if self.answers else "{}"


def _controls(*items) -> str:
    return json.dumps({"controls": list(items)})


def _shot(tmp_path: Path, name="s.png") -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n fake pixels")
    return p


# --- Proposing controls ---------------------------------------------------

def test_percentages_become_pixels(tmp_path):
    chat = FakeVision(_controls(
        {"label": "Start", "role": "button", "box": [10, 20, 30, 25]}))
    els = propose_elements(chat, _shot(tmp_path), viewport=(1000, 800))
    assert len(els) == 1
    assert els[0].bounds == (100, 160, 300, 200)
    assert els[0].label == "Start" and els[0].role == "button"


def test_a_model_answering_in_zero_to_one_is_rescaled(tmp_path):
    """Otherwise every control lands in the top-left corner and clicks miss."""
    chat = FakeVision(_controls(
        {"label": "Go", "role": "button", "box": [0.1, 0.2, 0.3, 0.25]}))
    els = propose_elements(chat, _shot(tmp_path), viewport=(1000, 800))
    assert els[0].bounds == (100, 160, 300, 200)


def test_boxes_are_clamped_to_the_viewport(tmp_path):
    chat = FakeVision(_controls(
        {"label": "Off screen", "role": "button", "box": [-20, -10, 400, 300]}))
    left, top, right, bottom = propose_elements(
        chat, _shot(tmp_path), viewport=(1000, 800))[0].bounds
    assert left == 0 and top == 0
    assert right <= 1000 and bottom <= 800


def test_a_degenerate_box_is_dropped(tmp_path):
    """A zero-width box cannot be aimed at."""
    chat = FakeVision(_controls(
        {"label": "Sliver", "role": "button", "box": [10, 10, 10, 10]}))
    assert propose_elements(chat, _shot(tmp_path), viewport=(1000, 800)) == []


def test_an_unknown_role_falls_back_rather_than_being_dropped(tmp_path):
    chat = FakeVision(_controls(
        {"label": "Thing", "role": "hologram", "box": [1, 1, 20, 10]}))
    assert propose_elements(chat, _shot(tmp_path), viewport=(1000, 800))[0].role == "button"


def test_malformed_entries_are_skipped_not_fatal(tmp_path):
    chat = FakeVision(json.dumps({"controls": [
        {"label": "No box"},
        {"label": "Bad box", "box": "nonsense"},
        {"label": "Good", "role": "button", "box": [1, 1, 20, 10]},
    ]}))
    els = propose_elements(chat, _shot(tmp_path), viewport=(1000, 800))
    assert [e.label for e in els] == ["Good"]


def test_the_count_is_capped(tmp_path):
    many = [{"label": f"b{i}", "role": "button", "box": [1, 1, 20, 10]}
            for i in range(100)]
    chat = FakeVision(json.dumps({"controls": many}))
    assert len(propose_elements(chat, _shot(tmp_path), viewport=(1000, 800),
                                max_controls=12)) == 12


def test_no_controls_is_an_honest_empty(tmp_path):
    chat = FakeVision(json.dumps({"controls": []}))
    assert propose_elements(chat, _shot(tmp_path), viewport=(1000, 800)) == []


def test_a_failed_call_returns_nothing_rather_than_raising(tmp_path):
    """A vision run that cannot see has nothing to do, which the loop already
    handles. An exception would end the session."""
    chat = FakeVision(fail=TimeoutError("model gone"))
    assert propose_elements(chat, _shot(tmp_path), viewport=(1000, 800)) == []


def test_an_unreadable_screenshot_is_not_fatal(tmp_path):
    chat = FakeVision(_controls({"label": "x", "box": [1, 1, 9, 9]}))
    assert propose_elements(chat, tmp_path / "missing.png", viewport=(100, 100)) == []
    assert chat.calls == [], "no point calling the model with no image"


def test_prose_and_thinking_around_the_json_survive(tmp_path):
    chat = FakeVision('<think>maybe</think> Sure:\n```json\n'
                      '{"controls": [{"label":"Go","role":"button","box":[1,1,20,10]}]}\n```')
    assert propose_elements(chat, _shot(tmp_path), viewport=(1000, 800))[0].label == "Go"


def test_the_image_is_sent_as_a_data_uri(tmp_path):
    chat = FakeVision("{}")
    propose_elements(chat, _shot(tmp_path), viewport=(100, 100))
    assert chat.calls[0][2].startswith("data:image/png;base64,")


def test_project_notes_are_passed_through(tmp_path):
    chat = FakeVision("{}")
    propose_elements(chat, _shot(tmp_path), viewport=(100, 100),
                     project_context="The HUD is not interactive.")
    assert "HUD is not interactive" in chat.calls[0][1]


def test_encode_image_handles_a_missing_file(tmp_path):
    assert encode_image(tmp_path / "nope.png") is None


# --- What the mode refuses to claim ---------------------------------------

def test_proposed_controls_carry_no_handle():
    """There is nothing stable to hang one on, and inventing one would claim an
    identity strength this mode does not have."""
    obs = vision_observation(Observation(), [
        Element(id="v1", role="button", label="Go", bounds=(0, 0, 10, 10))])
    assert all(e.handle is None for e in obs.elements)


def test_identity_is_positional_when_there_is_no_label():
    a = Element(id="v1", role="button", label="", bounds=(0, 96, 400, 176))
    b = Element(id="v2", role="button", label="", bounds=(0, 400, 400, 480))
    assert a.identity != b.identity


def test_the_weakened_rules_are_named_with_reasons():
    """Silence about a rule that could not run reads as a pass."""
    assert {"U4", "U5", "U6"} <= set(WEAKENED_RULES)
    assert all(isinstance(v, str) and v for v in WEAKENED_RULES.values())
    assert "cannot be read" in WEAKENED_RULES["U5"]


def test_the_degraded_note_says_an_absence_of_findings_means_less():
    note = degraded_note(7)
    assert "7 proposed" in note
    assert "positional" in note
    assert "absence of findings says much less" in note


def test_a_vision_observation_is_marked_as_such():
    obs = vision_observation(
        Observation(screen_id="canvas", screenshot="/tmp/x.png"),
        [Element(id="v1", role="button", bounds=(0, 0, 10, 10))])
    assert obs.meta["vision"] is True
    assert obs.meta["vision_controls"] == 1
    assert obs.screen_id == "canvas" and obs.screenshot == "/tmp/x.png"


# --- In the loop ----------------------------------------------------------

class BlindDriver(FakeDriver):
    """A target with no tree: one SurfaceView and a screenshot."""

    kind = "blind"

    def __init__(self, shot: str, **kw):
        home = FakeScreen("canvas", [])
        super().__init__({"canvas": home}, entry="canvas", **kw)
        self._shot = shot
        self.accepted: list[Element] = []

    def observe(self):
        obs = super().observe()
        return Observation(elements=(), screen_id="canvas", text="",
                           screenshot=self._shot, logs=obs.logs, meta={})

    def accept_vision_elements(self, elements):
        self.accepted = list(elements)


class BlindDriverNoCoords(BlindDriver):
    """A blind target whose driver cannot aim at a coordinate."""
    accept_vision_elements = None  # type: ignore[assignment]


def test_a_blind_target_has_nothing_to_try_without_vision(tmp_path):
    """The gap this story fills: measured, a blind observation yields no work."""
    driver = BlindDriver(str(_shot(tmp_path)))
    result = ExplorationLoop(driver, budgets=Budgets(min_seconds=0, max_actions=20),
                             deck=Deck(cards=()), clock=lambda: 0.0).run()
    assert result.stats.actions == 0


def test_vision_gives_a_blind_target_something_to_do(tmp_path):
    chat = FakeVision(*[_controls(
        {"label": "Play", "role": "button", "box": [10, 10, 40, 20]},
        {"label": "Options", "role": "button", "box": [10, 30, 40, 40]},
    )] * 6)
    driver = BlindDriver(str(_shot(tmp_path)))
    loop = ExplorationLoop(driver, budgets=Budgets(min_seconds=0, max_actions=20),
                           deck=Deck(cards=()), vision=chat, viewport=(1000, 800),
                           clock=lambda: 0.0)
    result = loop.run()
    assert result.stats.actions > 0, "vision should produce work"
    assert loop.used_vision is True
    assert [e.label for e in driver.accepted] == ["Play", "Options"]


def test_the_run_is_marked_as_vision_in_the_journal(tmp_path):
    from pandabot_core.active_qa import Journal
    chat = FakeVision(*[_controls(
        {"label": "Play", "role": "button", "box": [10, 10, 40, 20]})] * 6)
    journal = Journal(tmp_path / "j", session_id="aq-vision")
    ExplorationLoop(BlindDriver(str(_shot(tmp_path))),
                    budgets=Budgets(min_seconds=0, max_actions=10),
                    deck=Deck(cards=()), vision=chat, journal=journal,
                    clock=lambda: 0.0).run()
    text = " ".join(e.text for e in journal.events())
    assert "no ui tree" in text.lower()
    assert "weaker" in text.lower()


def test_a_driver_that_cannot_aim_stays_blind(tmp_path):
    """Generating work that cannot be performed would report every failure as a
    defect. Better to do nothing and say why."""
    from pandabot_core.active_qa import Journal
    chat = FakeVision(*[_controls(
        {"label": "Play", "role": "button", "box": [10, 10, 40, 20]})] * 4)
    journal = Journal(tmp_path / "j2", session_id="aq-nocoord")
    result = ExplorationLoop(BlindDriverNoCoords(str(_shot(tmp_path))),
                             budgets=Budgets(min_seconds=0, max_actions=10),
                             deck=Deck(cards=()), vision=chat, journal=journal,
                             clock=lambda: 0.0).run()
    assert result.stats.actions == 0
    text = " ".join(e.text for e in journal.events())
    assert "cannot act by coordinate" in text


def test_a_dead_vision_model_leaves_the_run_blind_not_broken(tmp_path):
    driver = BlindDriver(str(_shot(tmp_path)))
    loop = ExplorationLoop(driver, budgets=Budgets(min_seconds=0, max_actions=10),
                           deck=Deck(cards=()),
                           vision=FakeVision(fail=ConnectionError("gone")),
                           clock=lambda: 0.0)
    result = loop.run()
    assert result.stats.actions == 0
    assert loop.used_vision is False


def test_a_sighted_target_never_pays_for_vision(tmp_path):
    """The mode must not cost anything on a target that has a tree."""
    home = FakeScreen("home", [Element(id="e1", role="button", label="Go", handle="#go")])
    chat = FakeVision("{}")
    ExplorationLoop(FakeDriver({"home": home}, entry="home",
                               actions=frozenset({"click"})),
                    budgets=Budgets(min_seconds=0, max_actions=10),
                    deck=Deck(cards=()), vision=chat, clock=lambda: 0.0).run()
    assert chat.calls == [], "vision should not be consulted when a tree exists"
