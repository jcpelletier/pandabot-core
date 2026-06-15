"""
pandabot_core.goals
~~~~~~~~~~~~~~~~~~~~
Persistent store for **long-term goals** — a GitHub epic that the bot team works
to completion incrementally, one story (sub-issue) at a time.

A goal survives a bot restart: unlike pandabot-dev's in-memory ``_active_sessions``
dict (which tracks single in-flight Jules sessions and is lost on restart), the goal
driver records its progress here so it can resume mid-goal. GitHub remains the source
of truth for the work items themselves (epic + story sub-issues + their ``status:``
labels); this table is the driver's local bookkeeping — which story is in flight, the
Jules session driving it, feedback rounds spent, and the last QA verdict.

DB: ``cfg.db_path("scheduler.db")`` — the same SQLite file the scheduler uses, so set
``PANDABOT_DATA_DIR`` to control its location.

The ``STATUS_*`` constants are the single source of truth for the status-label
vocabulary shared by pandabot-dev, pandabot-qa, and pandabot-devops. They are full
GitHub label strings (e.g. ``status: in-qa``) so they can be passed straight to
:func:`pandabot_core.pm.github.set_status_label`.
"""

from __future__ import annotations

import datetime
import sqlite3
from typing import Any

from pandabot_core.config import cfg

__all__ = [
    "STATUS_READY", "STATUS_IN_PROGRESS", "STATUS_IN_QA",
    "STATUS_QA_PASSED", "STATUS_CHANGES_REQUESTED", "STATUS_DONE",
    "STATUS_BLOCKED", "ALL_STATUSES",
    "GOAL_INTAKE", "GOAL_ACTIVE", "GOAL_COMPLETE",
    "init_db", "create_goal", "get_goal", "active_goals",
    "set_goal_state", "complete_goal",
    "upsert_story_run", "get_story_run", "story_runs_for_goal",
    "set_story_status", "record_dispatch", "record_qa_verdict",
    "in_flight_story", "next_story_number",
]

# --- Story status-label vocabulary (full GitHub label strings) --------------
STATUS_READY = "status: ready"
STATUS_IN_PROGRESS = "status: in-progress"
STATUS_IN_QA = "status: in-qa"
STATUS_QA_PASSED = "status: qa-passed"
STATUS_CHANGES_REQUESTED = "status: changes-requested"
STATUS_DONE = "status: done"
STATUS_BLOCKED = "status: blocked"

ALL_STATUSES = [
    STATUS_READY, STATUS_IN_PROGRESS, STATUS_IN_QA, STATUS_QA_PASSED,
    STATUS_CHANGES_REQUESTED, STATUS_DONE, STATUS_BLOCKED,
]

# --- Goal-level state (epic) ------------------------------------------------
GOAL_INTAKE = "intake"      # awaiting QA capability review / skill
GOAL_ACTIVE = "active"      # driver is working stories
GOAL_COMPLETE = "complete"  # every story done


def _db() -> str:
    return cfg.db_path("scheduler.db")


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db() -> None:
    with sqlite3.connect(_db()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS goals (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                repo         TEXT    NOT NULL,
                epic_number  INTEGER NOT NULL,
                title        TEXT    NOT NULL DEFAULT '',
                state        TEXT    NOT NULL DEFAULT 'intake',
                channel_id   INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT    NOT NULL,
                updated_at   TEXT    NOT NULL,
                UNIQUE(repo, epic_number)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS goal_story_runs (
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


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db())
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------

def create_goal(repo: str, epic_number: int, title: str = "",
                channel_id: int = 0, state: str = GOAL_ACTIVE) -> int:
    """Register a goal (or return the existing id for the same repo+epic)."""
    init_db()
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            "SELECT id FROM goals WHERE repo = ? AND epic_number = ?",
            (repo, epic_number),
        )
        row = cur.fetchone()
        if row:
            conn.execute(
                "UPDATE goals SET title = ?, channel_id = ?, state = ?, updated_at = ? WHERE id = ?",
                (title, channel_id, state, now, row["id"]),
            )
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO goals (repo, epic_number, title, state, channel_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (repo, epic_number, title, state, channel_id, now, now),
        )
        return int(cur.lastrowid)


def get_goal(goal_id: int) -> dict[str, Any] | None:
    init_db()
    with _conn() as conn:
        row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        return dict(row) if row else None


def active_goals() -> list[dict[str, Any]]:
    """Every goal the driver should still be working (not complete)."""
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM goals WHERE state != ? ORDER BY id", (GOAL_COMPLETE,)
        ).fetchall()
        return [dict(r) for r in rows]


def set_goal_state(goal_id: int, state: str) -> None:
    init_db()
    with _conn() as conn:
        conn.execute("UPDATE goals SET state = ?, updated_at = ? WHERE id = ?",
                     (state, _now(), goal_id))


def complete_goal(goal_id: int) -> None:
    set_goal_state(goal_id, GOAL_COMPLETE)


# ---------------------------------------------------------------------------
# Story runs
# ---------------------------------------------------------------------------

def upsert_story_run(goal_id: int, story_number: int, **fields: Any) -> int:
    """Create or update the run record for one story. Unknown fields are ignored."""
    init_db()
    allowed = {"jules_session_id", "pr_url", "status", "feedback_rounds", "qa_verdict"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    now = _now()
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM goal_story_runs WHERE goal_id = ? AND story_number = ?",
            (goal_id, story_number),
        ).fetchone()
        if row:
            if sets:
                cols = ", ".join(f"{k} = ?" for k in sets)
                conn.execute(
                    f"UPDATE goal_story_runs SET {cols}, updated_at = ? WHERE id = ?",
                    (*sets.values(), now, row["id"]),
                )
            return int(row["id"])
        cols = ["goal_id", "story_number", "updated_at", *sets.keys()]
        vals = [goal_id, story_number, now, *sets.values()]
        placeholders = ", ".join("?" for _ in cols)
        cur = conn.execute(
            f"INSERT INTO goal_story_runs ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        return int(cur.lastrowid)


def get_story_run(goal_id: int, story_number: int) -> dict[str, Any] | None:
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM goal_story_runs WHERE goal_id = ? AND story_number = ?",
            (goal_id, story_number),
        ).fetchone()
        return dict(row) if row else None


def story_runs_for_goal(goal_id: int) -> list[dict[str, Any]]:
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM goal_story_runs WHERE goal_id = ? ORDER BY story_number",
            (goal_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def set_story_status(goal_id: int, story_number: int, status: str) -> None:
    upsert_story_run(goal_id, story_number, status=status)


def record_dispatch(goal_id: int, story_number: int, jules_session_id: str) -> None:
    """Mark a story as dispatched to Jules and in progress."""
    upsert_story_run(goal_id, story_number,
                     jules_session_id=jules_session_id, status=STATUS_IN_PROGRESS)


def record_qa_verdict(goal_id: int, story_number: int, verdict: str, status: str) -> None:
    upsert_story_run(goal_id, story_number, qa_verdict=verdict, status=status)


def in_flight_story(goal_id: int) -> dict[str, Any] | None:
    """The story the driver is currently working, if any (one at a time per goal).

    A story is in flight while it is being built, validated, or fixed — i.e. any
    status that is neither terminal (``done``) nor waiting-to-start (``ready``,
    ``blocked``)."""
    terminal = {STATUS_DONE, STATUS_READY, STATUS_BLOCKED}
    for run in story_runs_for_goal(goal_id):
        if run["status"] not in terminal:
            return run
    return None


def next_story_number(goal_id: int) -> int | None:
    """The lowest story number recorded as ready to start, or None. Story
    *discovery* (reading the epic's children from GitHub) lives in the bot; this
    only reflects what the driver has persisted as ``ready``."""
    for run in story_runs_for_goal(goal_id):
        if run["status"] == STATUS_READY:
            return int(run["story_number"])
    return None
