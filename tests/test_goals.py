"""Tests for pandabot_core.goals — the persistent long-term-goal store."""
import os

import pytest

from pandabot_core import goals


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PANDABOT_DATA_DIR", str(tmp_path))
    goals.init_db()


def test_create_goal_is_idempotent_per_epic():
    gid1 = goals.create_goal("jcpelletier/game", 1, title="Build the game", channel_id=42)
    gid2 = goals.create_goal("jcpelletier/game", 1, title="Build the game (again)")
    assert gid1 == gid2
    g = goals.get_goal(gid1)
    assert g["repo"] == "jcpelletier/game"
    assert g["epic_number"] == 1
    assert g["title"] == "Build the game (again)"  # latest title wins


def test_base_branch_defaults_and_round_trips():
    g_default = goals.create_goal("o/d", 1)
    assert goals.get_goal(g_default)["base_branch"] == "main"   # default, not "staging"
    g_custom = goals.create_goal("o/c", 2, base_branch="trunk")
    assert goals.get_goal(g_custom)["base_branch"] == "trunk"
    # Re-registering the same epic updates the base branch.
    goals.create_goal("o/c", 2, base_branch="develop")
    assert goals.get_goal(g_custom)["base_branch"] == "develop"


def test_active_goals_excludes_complete():
    a = goals.create_goal("o/a", 1)
    b = goals.create_goal("o/b", 2)
    goals.complete_goal(b)
    active_ids = {g["id"] for g in goals.active_goals()}
    assert a in active_ids
    assert b not in active_ids


def test_story_run_lifecycle_and_in_flight():
    gid = goals.create_goal("o/game", 1)
    # Seed two ready stories (what the bot does after reading epic children).
    goals.set_story_status(gid, 10, goals.STATUS_READY)
    goals.set_story_status(gid, 11, goals.STATUS_READY)

    # Nothing in flight yet; next ready is the lowest number.
    assert goals.in_flight_story(gid) is None
    assert goals.next_story_number(gid) == 10

    # Dispatch story 10 to Jules.
    goals.record_dispatch(gid, 10, "sess-abc")
    run = goals.get_story_run(gid, 10)
    assert run["jules_session_id"] == "sess-abc"
    assert run["status"] == goals.STATUS_IN_PROGRESS
    assert goals.in_flight_story(gid)["story_number"] == 10
    # 11 is still the next ready story.
    assert goals.next_story_number(gid) == 11

    # QA passes -> done -> no longer in flight.
    goals.record_qa_verdict(gid, 10, "looks good", goals.STATUS_DONE)
    assert goals.in_flight_story(gid) is None
    assert goals.get_story_run(gid, 10)["qa_verdict"] == "looks good"


def test_feedback_rounds_increment_via_upsert():
    gid = goals.create_goal("o/game", 1)
    goals.record_dispatch(gid, 10, "sess-abc")
    goals.upsert_story_run(gid, 10, feedback_rounds=1,
                           status=goals.STATUS_CHANGES_REQUESTED)
    run = goals.get_story_run(gid, 10)
    assert run["feedback_rounds"] == 1
    assert run["status"] == goals.STATUS_CHANGES_REQUESTED
    # session id preserved across the partial update
    assert run["jules_session_id"] == "sess-abc"


def test_status_constants_are_full_labels():
    for s in goals.ALL_STATUSES:
        assert s.startswith("status: ")


# --- builder columns + escalations (builder tiers, phase 1) -----------------

def _old_schema_db(path):
    """A scheduler.db as it exists on panda before the builder columns: the old
    goal_story_runs shape with real Jules rows already in it."""
    import sqlite3
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE goal_story_runs (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id           INTEGER NOT NULL,
                story_number      INTEGER NOT NULL,
                jules_session_id  TEXT    NOT NULL DEFAULT '',
                pr_url            TEXT    NOT NULL DEFAULT '',
                status            TEXT    NOT NULL DEFAULT 'status: ready',
                feedback_rounds   INTEGER NOT NULL DEFAULT 0,
                qa_verdict        TEXT    NOT NULL DEFAULT '',
                updated_at        TEXT    NOT NULL,
                UNIQUE(goal_id, story_number)
            )
        """)
        conn.execute(
            "INSERT INTO goal_story_runs (goal_id, story_number, jules_session_id, pr_url, "
            "status, feedback_rounds, updated_at) VALUES "
            "(1, 10, 'sess-old', 'http://pr/10', 'status: in-qa', 1, '2026-11-01T00:00:00')"
        )
        conn.execute(
            "INSERT INTO goal_story_runs (goal_id, story_number, status, updated_at) VALUES "
            "(1, 11, 'status: ready', '2026-11-01T00:00:00')"
        )


def test_migration_backfills_existing_rows(tmp_path, monkeypatch):
    sub = tmp_path / "old"
    sub.mkdir()
    monkeypatch.setenv("PANDABOT_DATA_DIR", str(sub))
    _old_schema_db(str(sub / "scheduler.db"))

    goals.init_db()
    goals.init_db()  # idempotent: a second run must not fail or change anything

    run = goals.get_story_run(1, 10)
    assert run["builder"] == goals.BUILDER_JULES
    assert run["builder_ref"] == "sess-old"
    # Existing data is untouched and the old column is still readable.
    assert run["jules_session_id"] == "sess-old"
    assert run["pr_url"] == "http://pr/10"
    assert run["status"] == goals.STATUS_IN_QA
    assert run["feedback_rounds"] == 1

    never_dispatched = goals.get_story_run(1, 11)
    assert never_dispatched["builder"] == goals.BUILDER_JULES
    assert never_dispatched["builder_ref"] == ""
    assert goals.escalations_for_goal(1) == []


def test_record_dispatch_writes_builder_and_mirrors_jules_session():
    gid = goals.create_goal("o/game", 1)
    goals.record_dispatch(gid, 10, "sess-new", builder=goals.BUILDER_JULES)
    run = goals.get_story_run(gid, 10)
    assert run["builder"] == "jules"
    assert run["builder_ref"] == "sess-new"
    assert run["jules_session_id"] == "sess-new"
    assert run["status"] == goals.STATUS_IN_PROGRESS
    assert goals.story_builder_ref(run) == "sess-new"


def test_record_dispatch_pre_builder_call_still_records_jules():
    # Dev before the builder refactor calls record_dispatch(goal, story, session_id).
    gid = goals.create_goal("o/game", 1)
    goals.record_dispatch(gid, 10, "sess-legacy")
    run = goals.get_story_run(gid, 10)
    assert (run["builder"], run["builder_ref"], run["jules_session_id"]) == \
        ("jules", "sess-legacy", "sess-legacy")


def test_legacy_upsert_of_jules_session_id_sets_builder_ref():
    gid = goals.create_goal("o/game", 1)
    goals.upsert_story_run(gid, 10, jules_session_id="sess-up", status=goals.STATUS_IN_PROGRESS)
    run = goals.get_story_run(gid, 10)
    assert run["builder_ref"] == "sess-up"
    assert run["builder"] == "jules"


def test_non_jules_dispatch_clears_stale_jules_session():
    gid = goals.create_goal("o/game", 1)
    goals.record_dispatch(gid, 10, "sess-first")
    goals.record_dispatch(gid, 10, "run-7@v1.0.1", builder="harness:deepseek")
    run = goals.get_story_run(gid, 10)
    assert run["builder"] == "harness:deepseek"
    assert run["builder_ref"] == "run-7@v1.0.1"
    assert run["jules_session_id"] == ""


def test_story_builder_ref_falls_back_to_jules_session_id():
    # A row dict from a store without the builder columns (core deployed after Dev).
    assert goals.story_builder_ref({"jules_session_id": "sess-x"}) == "sess-x"
    assert goals.story_builder_ref({"builder_ref": "", "jules_session_id": "sess-y"}) == "sess-y"
    assert goals.story_builder_ref({}) == ""


def test_escalations_are_logged_in_order_per_story():
    gid = goals.create_goal("o/game", 1)
    goals.record_escalation(gid, 10, "", "jules", "tier 1: default")
    goals.record_escalation(gid, 10, "jules", "harness:deepseek", "feedback limit reached")
    goals.record_escalation(gid, 11, "", "jules", "tier 1: default")

    story = goals.escalations_for_story(gid, 10)
    assert [(e["from_builder"], e["to_builder"]) for e in story] == \
        [("", "jules"), ("jules", "harness:deepseek")]
    assert story[1]["reason"] == "feedback limit reached"
    assert story[0]["created_at"]
    assert len(goals.escalations_for_goal(gid)) == 3
