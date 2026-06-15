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
