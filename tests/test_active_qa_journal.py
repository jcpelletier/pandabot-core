"""Tests for the session journal.

Two properties carry the weight. Frames must be content-addressed, or an hour of
clicking dead controls on one screen writes hundreds of identical images. And
the page must distinguish what the model said from what the priority queue
decided, because presenting a queue's arithmetic as reasoning would be a
comfortable read and a false one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pandabot_core.active_qa import Element, FakeDriver, FakeScreen
from pandabot_core.active_qa.journal import (
    EventKind, FrameStore, Journal, JournalEvent, render_html,
)
from pandabot_core.active_qa.loop import Budgets, ExplorationLoop, Orientation
from pandabot_core.active_qa.strategies import Deck


def _fake_compressor(src: Path, dst: Path, **kw) -> bool:
    """Stand in for Pillow: copies bytes, reports success."""
    dst.write_bytes(Path(src).read_bytes())
    return True


def _shot(tmp_path: Path, name: str, content: bytes) -> str:
    p = tmp_path / name
    p.write_bytes(content)
    return str(p)


def _journal(tmp_path: Path, **kw) -> Journal:
    return Journal(tmp_path / "journal", session_id="aq-test",
                   compressor=_fake_compressor, **kw)


# --- FrameStore -----------------------------------------------------------

def test_identical_screens_are_stored_once(tmp_path):
    store = FrameStore(tmp_path / "frames", compressor=_fake_compressor)
    a = _shot(tmp_path, "a.png", b"same pixels")
    b = _shot(tmp_path, "b.png", b"same pixels")

    first, repeat_a = store.add(a)
    second, repeat_b = store.add(b)

    assert first == second
    assert not repeat_a and repeat_b
    assert store.frames_stored == 1 and store.frames_offered == 2
    assert store.stats()["duplicates_collapsed"] == 1


def test_different_screens_get_different_frames(tmp_path):
    store = FrameStore(tmp_path / "frames", compressor=_fake_compressor)
    first, _ = store.add(_shot(tmp_path, "a.png", b"screen one"))
    second, _ = store.add(_shot(tmp_path, "b.png", b"screen two"))
    assert first != second and store.frames_stored == 2


def test_frames_are_content_addressed(tmp_path):
    store = FrameStore(tmp_path / "frames", compressor=_fake_compressor)
    rel, _ = store.add(_shot(tmp_path, "a.png", b"x"))
    assert rel.endswith(".webp")
    assert (tmp_path / "frames" / rel).exists()


def test_missing_and_absent_screenshots_are_tolerated(tmp_path):
    store = FrameStore(tmp_path / "frames", compressor=_fake_compressor)
    assert store.add(None) == (None, False)
    assert store.add(tmp_path / "nope.png") == (None, False)
    assert store.frames_offered == 0


def test_no_temp_files_are_left_behind(tmp_path):
    store = FrameStore(tmp_path / "frames", compressor=_fake_compressor)
    store.add(_shot(tmp_path, "a.png", b"same"))
    store.add(_shot(tmp_path, "b.png", b"same"))
    assert [p.name for p in (tmp_path / "frames").iterdir() if p.name.startswith(".tmp")] == []


def test_a_failing_compressor_degrades_rather_than_raising(tmp_path):
    """No Pillow must mean a bigger journal, never a crashed run."""
    def refuses(src, dst, **kw):
        dst.write_bytes(Path(src).read_bytes())
        return False
    store = FrameStore(tmp_path / "frames", compressor=refuses)
    rel, _ = store.add(_shot(tmp_path, "a.png", b"x"))
    assert rel is not None
    assert store.stats()["compressed"] is False


# --- Journal --------------------------------------------------------------

def test_events_are_appended_as_they_happen(tmp_path):
    """Written during the run, so a crash cannot lose the narrative."""
    j = _journal(tmp_path)
    j.record(EventKind.SESSION_START, "starting")
    assert j.events_path.exists()
    lines = j.events_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    j.record(EventKind.ACTION, "clicked")
    assert len(j.events_path.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_events_round_trip(tmp_path):
    j = _journal(tmp_path)
    j.record(EventKind.FINDING, "a dead button", rule="U7", severity="major",
             screen="home", step=4, detail={"why": "explore"})
    ev = list(j.events())[0]
    assert ev.rule == "U7" and ev.severity == "major"
    assert ev.screen == "home" and ev.step == 4
    assert ev.detail["why"] == "explore"


def test_journal_lines_are_valid_jsonl(tmp_path):
    j = _journal(tmp_path)
    for i in range(3):
        j.record(EventKind.ACTION, f"action {i}")
    for line in j.events_path.read_text(encoding="utf-8").strip().splitlines():
        assert json.loads(line)["kind"] == EventKind.ACTION


def test_frames_attach_to_events(tmp_path):
    j = _journal(tmp_path)
    j.record(EventKind.ACTION, "one", screenshot=_shot(tmp_path, "a.png", b"one"))
    j.record(EventKind.ACTION, "two", screenshot=_shot(tmp_path, "b.png", b"one"))
    events = list(j.events())
    assert events[0].frame == events[1].frame
    assert events[1].frame_repeat is True


def test_model_turns_are_identifiable(tmp_path):
    j = _journal(tmp_path)
    j.record(EventKind.ACTION, "clicked")
    j.record(EventKind.MODEL_ORIENT, "this looks like a settings screen")
    kinds = [(e.kind, e.from_model) for e in j.events()]
    assert kinds == [(EventKind.ACTION, False), (EventKind.MODEL_ORIENT, True)]


# --- Rendering ------------------------------------------------------------

def test_render_produces_a_standalone_page(tmp_path):
    j = _journal(tmp_path)
    j.record(EventKind.SESSION_START, "starting")
    out = j.render()
    page = out.read_text(encoding="utf-8")
    assert out.name == "journal.html"
    assert page.startswith("<!doctype html>")
    assert "aq-test" in page


def test_render_is_safe_to_call_mid_run(tmp_path):
    j = _journal(tmp_path)
    j.record(EventKind.ACTION, "first")
    assert "first" in j.render().read_text(encoding="utf-8")
    j.record(EventKind.ACTION, "second")
    page = j.render().read_text(encoding="utf-8")
    assert "first" in page and "second" in page


def test_frames_are_referenced_not_inlined(tmp_path):
    """A few thousand base64 frames would be a page no browser opens."""
    j = _journal(tmp_path)
    j.record(EventKind.ACTION, "one", screenshot=_shot(tmp_path, "a.png", b"one"))
    page = j.render().read_text(encoding="utf-8")
    assert "data:image" not in page
    assert 'loading="lazy"' in page
    assert "frames/" not in page  # paths are relative to the journal dir
    assert ".webp" in page


def test_repeated_frames_render_as_a_marker(tmp_path):
    j = _journal(tmp_path)
    j.record(EventKind.ACTION, "one", screenshot=_shot(tmp_path, "a.png", b"same"))
    j.record(EventKind.ACTION, "two", screenshot=_shot(tmp_path, "b.png", b"same"))
    page = j.render().read_text(encoding="utf-8")
    assert page.count("<img") == 1
    assert "screen unchanged" in page


def test_model_turns_are_visually_distinguished(tmp_path):
    j = _journal(tmp_path)
    j.record(EventKind.MODEL_ORIENT, "a settings screen")
    j.record(EventKind.ACTION, "clicked Save")
    page = j.render().read_text(encoding="utf-8")
    assert 'class="model"' in page
    # And the page says plainly that the rest was not the model.
    assert "without a model call" in page


def test_findings_carry_their_severity_into_the_markup(tmp_path):
    j = _journal(tmp_path)
    j.record(EventKind.FINDING, "field eats input", rule="U6", severity="blocker")
    page = j.render().read_text(encoding="utf-8")
    assert "sev-blocker" in page and "U6" in page


def test_content_is_escaped(tmp_path):
    j = _journal(tmp_path)
    j.record(EventKind.ACTION, "typed <script>alert(1)</script>")
    page = j.render().read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_page_is_theme_aware_and_responsive(tmp_path):
    j = _journal(tmp_path)
    j.record(EventKind.ACTION, "x")
    page = j.render().read_text(encoding="utf-8")
    assert "prefers-color-scheme" in page
    assert "viewport" in page


def test_render_handles_an_empty_journal(tmp_path):
    assert render_html([]).startswith("<!doctype html>")


def test_stats_summarise_the_journal(tmp_path):
    j = _journal(tmp_path)
    j.record(EventKind.ACTION, "one", screenshot=_shot(tmp_path, "a.png", b"same"))
    j.record(EventKind.ACTION, "two", screenshot=_shot(tmp_path, "b.png", b"same"))
    s = j.stats()
    assert s["events"] == 2 and s["frames_stored"] == 1
    assert s["duplicates_collapsed"] == 1


# --- Against a real loop --------------------------------------------------

def _app(tmp_path):
    """A two-screen app whose driver hands out real screenshot files."""
    home = FakeScreen("home", [
        Element(id="e1", role="button", label="Open", handle="#open"),
        Element(id="e2", role="button", label="Dead", handle="#dead"),
    ])
    panel = FakeScreen("panel", [Element(id="e1", role="button", label="Back", handle="#back")])
    home.transitions[("h:#open", "click")] = "panel"
    panel.transitions[("h:#back", "click")] = "home"

    shots = {}
    for name, content in (("home", b"home pixels"), ("panel", b"panel pixels")):
        shots[name] = _shot(tmp_path, f"{name}.png", content)

    class Shooting(FakeDriver):
        def observe(self):
            obs = super().observe()
            return type(obs)(**{**obs.to_dict(), "elements": obs.elements,
                                "screenshot": shots[self.current],
                                "logs": obs.logs, "net": obs.net})
    return Shooting({"home": home, "panel": panel}, entry="home",
                    actions=frozenset({"click"}))


def test_a_real_run_produces_a_readable_journal(tmp_path):
    j = _journal(tmp_path)
    loop = ExplorationLoop(_app(tmp_path), budgets=Budgets(min_seconds=0, max_actions=30),
                           deck=Deck(cards=()), journal=j, clock=lambda: 0.0)
    result = loop.run()

    kinds = [e.kind for e in j.events()]
    assert kinds[0] == EventKind.SESSION_START
    assert kinds[-1] == EventKind.SESSION_END
    assert EventKind.STATE_NEW in kinds
    assert EventKind.ACTION in kinds
    assert EventKind.FINDING in kinds

    page = (tmp_path / "journal" / "journal.html").read_text(encoding="utf-8")
    assert "Dead" in page                       # the action narrative
    assert "does nothing when used" in page     # the finding
    assert str(result.stats.actions) in page    # the closing summary


def test_the_journal_narrates_why_each_action_was_chosen(tmp_path):
    j = _journal(tmp_path)
    ExplorationLoop(_app(tmp_path), budgets=Budgets(min_seconds=0, max_actions=20),
                    deck=Deck(cards=()), journal=j, clock=lambda: 0.0).run()
    actions = [e for e in j.events() if e.kind == EventKind.ACTION]
    assert actions and all("nothing changed" in e.text or "state changed" in e.text
                           for e in actions)
    assert any(e.detail.get("why") for e in actions)


def test_unchanged_screens_collapse_across_a_real_run(tmp_path):
    """The volume guarantee: an hour on one screen must not be an hour of images."""
    j = _journal(tmp_path)
    ExplorationLoop(_app(tmp_path), budgets=Budgets(min_seconds=0, max_actions=40),
                    deck=Deck(cards=()), journal=j, clock=lambda: 0.0).run()
    s = j.stats()
    assert s["frames_stored"] == 2, "only two distinct screens exist"
    assert s["frames_offered"] > s["frames_stored"]


def test_a_model_run_marks_its_turns(tmp_path):
    class Advisor:
        def orient(self, observation, fingerprint):
            return Orientation(expectations={"h:#dead": "should open settings"},
                               notes="A home screen with two buttons.")
        def judge(self, finding, context):
            return True

    j = _journal(tmp_path)
    ExplorationLoop(_app(tmp_path), budgets=Budgets(min_seconds=0, max_actions=30),
                    deck=Deck(cards=()), advisor=Advisor(), journal=j,
                    clock=lambda: 0.0).run()
    model_events = [e for e in j.events() if e.from_model]
    assert model_events and all(e.kind == EventKind.MODEL_ORIENT for e in model_events)
    page = (tmp_path / "journal" / "journal.html").read_text(encoding="utf-8")
    assert "A home screen with two buttons." in page
