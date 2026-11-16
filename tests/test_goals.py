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

    # Dispatch story 10 to the builder.
    goals.record_dispatch(gid, 10, "job-abc", builder="harness:deepseek")
    run = goals.get_story_run(gid, 10)
    assert (run["builder"], run["builder_ref"]) == ("harness:deepseek", "job-abc")
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
    goals.record_dispatch(gid, 10, "job-abc", builder="harness:deepseek")
    goals.upsert_story_run(gid, 10, feedback_rounds=1,
                           status=goals.STATUS_CHANGES_REQUESTED)
    run = goals.get_story_run(gid, 10)
    assert run["feedback_rounds"] == 1
    assert run["status"] == goals.STATUS_CHANGES_REQUESTED
    # builder ref preserved across the partial update
    assert run["builder_ref"] == "job-abc"


def test_status_constants_are_full_labels():
    for s in goals.ALL_STATUSES:
        assert s.startswith("status: ")


# --- builder columns, the Jules column retirement, escalations ---------------

def _old_schema_db(path):
    """A scheduler.db from before the builder columns: the old goal_story_runs shape
    with real Jules rows already in it."""
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


def _panda_schema_db(path):
    """scheduler.db as it is on panda before phase 7: the builder columns were added
    by ALTER (``builder`` defaulting to 'jules'), next to ``jules_session_id``."""
    import sqlite3
    _old_schema_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE goal_story_runs ADD COLUMN builder TEXT NOT NULL DEFAULT 'jules'")
        conn.execute("ALTER TABLE goal_story_runs ADD COLUMN builder_ref TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE goal_story_runs SET builder_ref = jules_session_id "
                     "WHERE jules_session_id != ''")
        conn.execute(
            "INSERT INTO goal_story_runs (goal_id, story_number, builder, builder_ref, status, "
            "updated_at) VALUES (1, 12, 'harness:deepseek', 'job-12', 'status: done', "
            "'2026-11-12T00:00:00')")


def _columns(path):
    import sqlite3
    with sqlite3.connect(path) as conn:
        return {r[1] for r in conn.execute("PRAGMA table_info(goal_story_runs)")}


@pytest.mark.parametrize("make_db", [_old_schema_db, _panda_schema_db])
def test_migration_retires_the_jules_column_and_keeps_the_data(tmp_path, monkeypatch, make_db):
    sub = tmp_path / "db"
    sub.mkdir()
    monkeypatch.setenv("PANDABOT_DATA_DIR", str(sub))
    db = str(sub / "scheduler.db")
    make_db(db)

    goals.init_db()
    goals.init_db()  # idempotent: a second run must not fail or change anything

    assert "jules_session_id" not in _columns(db)
    assert {"builder", "builder_ref"} <= _columns(db)
    run = goals.get_story_run(1, 10)
    assert (run["builder"], run["builder_ref"]) == ("jules", "sess-old")   # history kept
    assert run["pr_url"] == "http://pr/10"
    assert run["status"] == goals.STATUS_IN_QA
    assert run["feedback_rounds"] == 1
    assert goals.get_story_run(1, 11)["builder_ref"] == ""
    assert goals.escalations_for_goal(1) == []


def test_new_rows_name_no_builder_until_dispatched(tmp_path, monkeypatch):
    """On panda's table the column default is still 'jules'; a story seeded as ready
    must not read as a Jules build."""
    sub = tmp_path / "panda"
    sub.mkdir()
    monkeypatch.setenv("PANDABOT_DATA_DIR", str(sub))
    _panda_schema_db(str(sub / "scheduler.db"))
    goals.set_story_status(1, 20, goals.STATUS_READY)
    assert goals.get_story_run(1, 20)["builder"] == ""
    assert goals.get_story_run(1, 12)["builder"] == "harness:deepseek"


def test_record_dispatch_writes_builder_and_ref():
    gid = goals.create_goal("o/game", 1)
    goals.record_dispatch(gid, 10, "job-7", builder="harness:deepseek")
    run = goals.get_story_run(gid, 10)
    assert (run["builder"], run["builder_ref"]) == ("harness:deepseek", "job-7")
    assert run["status"] == goals.STATUS_IN_PROGRESS
    assert goals.story_builder_ref(run) == "job-7"
    assert "jules_session_id" not in run


def test_record_dispatch_needs_a_builder():
    gid = goals.create_goal("o/game", 1)
    with pytest.raises(TypeError):
        goals.record_dispatch(gid, 10, "job-7")


def test_story_builder_ref():
    assert goals.story_builder_ref({"builder_ref": "job-x"}) == "job-x"
    assert goals.story_builder_ref({}) == ""


def test_escalations_are_logged_in_order_per_story():
    gid = goals.create_goal("o/game", 1)
    goals.record_escalation(gid, 10, "", "harness:desktop", "tier 1: default")
    goals.record_escalation(gid, 10, "harness:desktop", "harness:deepseek", "feedback limit reached")
    goals.record_escalation(gid, 11, "", "harness:desktop", "tier 1: default")

    story = goals.escalations_for_story(gid, 10)
    assert [(e["from_builder"], e["to_builder"]) for e in story] == \
        [("", "harness:desktop"), ("harness:desktop", "harness:deepseek")]
    assert story[1]["reason"] == "feedback limit reached"
    assert story[0]["created_at"]
    assert len(goals.escalations_for_goal(gid)) == 3


# --- verdict record (builder tiers, phase G) ----------------------------------

def test_verdict_table_is_added_to_a_live_db_without_touching_data(tmp_path, monkeypatch):
    """Panda's scheduler.db predates goal_verdicts: init_db adds the table and leaves
    every existing story row as it was (the migration is a CREATE TABLE only)."""
    sub = tmp_path / "live"
    sub.mkdir()
    monkeypatch.setenv("PANDABOT_DATA_DIR", str(sub))
    _old_schema_db(str(sub / "scheduler.db"))
    goals.init_db()
    goals.init_db()
    assert goals.verdicts_for_story(1, 10) == []
    run = goals.get_story_run(1, 10)
    assert (run["status"], run["feedback_rounds"], run["pr_url"]) == (
        goals.STATUS_IN_QA, 1, "http://pr/10")


def test_record_verdict_round_trips_and_truncates_excerpt():
    gid = goals.create_goal("o/game", 1)
    vid = goals.record_verdict(gid, 19, goals.SOURCE_QA, goals.RESULT_FAIL,
                               attribution=goals.ATTR_INFRA,
                               excerpt="You've hit your session limit " + "x" * 5000,
                               signature="hit your session limit", ref="comment:123",
                               builder="harness:deepseek", feedback_round=0)
    assert vid > 0
    [v] = goals.verdicts_for_story(gid, 19)
    assert v["source"] == "qa"
    assert v["attribution"] == "infrastructure"
    assert v["signature"] == "hit your session limit"
    assert len(v["excerpt"]) == goals.VERDICT_EXCERPT_CHARS
    assert v["created_at"]
    assert goals.has_verdict_ref(gid, 19, "comment:123")
    assert not goals.has_verdict_ref(gid, 19, "comment:124")
    assert not goals.has_verdict_ref(gid, 19, "")


def test_verdicts_filter_by_source_and_goal():
    gid = goals.create_goal("o/game", 1)
    goals.record_verdict(gid, 5, goals.SOURCE_CI, goals.RESULT_FAIL, goals.ATTR_BUILD)
    goals.record_verdict(gid, 5, goals.SOURCE_PREFILTER, goals.RESULT_FAIL, goals.ATTR_BUILD)
    goals.record_verdict(gid, 6, goals.SOURCE_QA, goals.RESULT_PASS, goals.ATTR_BUILD)
    assert [v["source"] for v in goals.verdicts_for_story(gid, 5)] == ["ci", "prefilter"]
    assert len(goals.verdicts_for_story(gid, 5, goals.SOURCE_CI)) == 1
    assert len(goals.verdicts_for_goal(gid)) == 3


def test_trailing_verdicts_counts_the_current_run_only():
    gid = goals.create_goal("o/game", 1)
    infra = goals.ATTR_INFRA
    goals.record_verdict(gid, 20, goals.SOURCE_QA, goals.RESULT_ERROR, infra)
    goals.record_verdict(gid, 20, goals.SOURCE_QA, goals.RESULT_FAIL, goals.ATTR_BUILD)
    goals.record_verdict(gid, 20, goals.SOURCE_QA, goals.RESULT_ERROR, infra)
    goals.record_verdict(gid, 20, goals.SOURCE_QA, goals.RESULT_ERROR, infra)
    assert goals.trailing_verdicts(gid, 20, infra) == 2
    assert goals.trailing_verdicts(gid, 20, infra, source=goals.SOURCE_CI) == 0
    goals.record_verdict(gid, 20, goals.SOURCE_QA, goals.RESULT_PASS, goals.ATTR_BUILD)
    assert goals.trailing_verdicts(gid, 20, infra) == 0


def test_paused_goals_are_left_out_of_the_drivers_list():
    from pandabot_core import goals as g
    a = g.create_goal("o/a", 1)
    b = g.create_goal("o/b", 2)
    g.set_goal_state(b, g.GOAL_PAUSED)
    assert [x["id"] for x in g.active_goals()] == [a]
    assert {x["id"] for x in g.active_goals(include_paused=True)} == {a, b}
    g.set_goal_state(b, g.GOAL_ACTIVE)
    assert {x["id"] for x in g.active_goals()} == {a, b}
    g.complete_goal(a)
    assert [x["id"] for x in g.active_goals(include_paused=True)] == [b]
