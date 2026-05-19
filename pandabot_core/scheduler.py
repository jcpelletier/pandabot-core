"""
pandabot_core.scheduler
~~~~~~~~~~~~~~~~~~~~~~~
Persistent task scheduler backed by SQLite.

The LLM decides what to do at schedule time (which tools to call, what message
to post, regex condition to evaluate).  Fire time is pure mechanical execution
— no LLM call for most tasks.

Task types:
  one_shot        — run tools once, post results, done.
  condition_check — run tools, regex-test output; retry up to max_attempts.
  recurring       — run tools, post results, schedule next occurrence.

DB path: cfg.db_path("scheduler.db") — set PANDABOT_DATA_DIR to control location.
"""

from __future__ import annotations

import calendar
import datetime
import json
import sqlite3
from typing import Any

from pandabot_core.config import cfg

__all__ = [
    "init_db", "add_task", "get_due_tasks", "mark_done",
    "reschedule", "cancel_task", "list_pending", "schedule_next_recurring",
]


def _db() -> str:
    return cfg.db_path("scheduler.db")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db() -> None:
    with sqlite3.connect(_db()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at             TEXT    NOT NULL,
                fire_at                TEXT    NOT NULL,
                channel_id             INTEGER NOT NULL,
                description            TEXT    NOT NULL,
                task_type              TEXT    NOT NULL DEFAULT 'one_shot',
                tool_calls             TEXT    NOT NULL DEFAULT '[]',
                intro_message          TEXT,
                static_message         TEXT,
                generative_prompt      TEXT,
                condition_pattern      TEXT,
                met_message            TEXT,
                not_met_message        TEXT,
                attempt                INTEGER NOT NULL DEFAULT 0,
                max_attempts           INTEGER NOT NULL DEFAULT 5,
                check_interval_minutes INTEGER NOT NULL DEFAULT 30,
                recurrence_rule        TEXT,
                done                   INTEGER NOT NULL DEFAULT 0
            )
        """)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def add_task(
    fire_at_local: str,
    channel_id: int,
    description: str,
    task_type: str = "one_shot",
    tool_calls: list | None = None,
    intro_message: str | None = None,
    static_message: str | None = None,
    generative_prompt: str | None = None,
    condition_pattern: str | None = None,
    met_message: str | None = None,
    not_met_message: str | None = None,
    max_attempts: int = 5,
    check_interval_minutes: int = 30,
    recurrence_rule: str | None = None,
) -> int:
    """Insert a task and return its id. fire_at_local is naive ISO in server local time."""
    local_dt = datetime.datetime.fromisoformat(fire_at_local)
    utc_dt = local_dt.astimezone(datetime.timezone.utc)
    with sqlite3.connect(_db()) as conn:
        cur = conn.execute(
            """
            INSERT INTO scheduled_tasks
              (created_at, fire_at, channel_id, description, task_type, tool_calls,
               intro_message, static_message, generative_prompt, condition_pattern,
               met_message, not_met_message, max_attempts, check_interval_minutes,
               recurrence_rule)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
                utc_dt.isoformat(),
                channel_id,
                description,
                task_type,
                json.dumps(tool_calls or []),
                intro_message,
                static_message,
                generative_prompt,
                condition_pattern,
                met_message,
                not_met_message,
                max_attempts,
                check_interval_minutes,
                recurrence_rule,
            ),
        )
        return cur.lastrowid  # type: ignore[return-value]


def get_due_tasks() -> list[sqlite3.Row]:
    """Return all tasks whose fire_at has passed and are not done."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM scheduled_tasks WHERE fire_at <= ? AND done = 0 ORDER BY fire_at",
            (now,),
        ).fetchall()


def mark_done(task_id: int) -> None:
    with sqlite3.connect(_db()) as conn:
        conn.execute("UPDATE scheduled_tasks SET done=1 WHERE id=?", (task_id,))


def reschedule(task_id: int, new_fire_at_utc: str, new_attempt: int) -> None:
    with sqlite3.connect(_db()) as conn:
        conn.execute(
            "UPDATE scheduled_tasks SET fire_at=?, attempt=? WHERE id=?",
            (new_fire_at_utc, new_attempt, task_id),
        )


def cancel_task(task_id: int) -> bool:
    """Mark a task done (cancelled). Returns True if found and not already done."""
    with sqlite3.connect(_db()) as conn:
        cur = conn.execute(
            "UPDATE scheduled_tasks SET done=1 WHERE id=? AND done=0", (task_id,)
        )
        return cur.rowcount > 0


def list_pending() -> list[sqlite3.Row]:
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """SELECT id, fire_at, description, task_type, attempt, max_attempts,
                      recurrence_rule
               FROM scheduled_tasks WHERE done=0 ORDER BY fire_at"""
        ).fetchall()


# ---------------------------------------------------------------------------
# Recurrence helpers
# ---------------------------------------------------------------------------

def schedule_next_recurring(task: Any) -> None:
    """Insert a fresh row for the next recurrence after the task that just fired."""
    rule = task["recurrence_rule"]
    if not rule:
        return

    fired_utc = datetime.datetime.fromisoformat(task["fire_at"])
    fired_local = fired_utc.astimezone()
    now_local = datetime.datetime.now().astimezone()

    if rule.startswith("monthly:"):
        day = int(rule.split(":")[1])
        next_local = fired_local
        while next_local <= now_local:
            m = next_local.month + 1
            y = next_local.year + (1 if m > 12 else 0)
            m = m if m <= 12 else 1
            _, last_day = calendar.monthrange(y, m)
            next_local = next_local.replace(year=y, month=m, day=min(day, last_day))

    elif rule.startswith("weekly:"):
        next_local = fired_local + datetime.timedelta(weeks=1)
        if next_local <= now_local:
            weeks_behind = int((now_local - next_local).days / 7) + 1
            next_local += datetime.timedelta(weeks=weeks_behind)
    else:
        return

    next_utc = next_local.astimezone(datetime.timezone.utc).isoformat()
    with sqlite3.connect(_db()) as conn:
        conn.execute(
            """
            INSERT INTO scheduled_tasks
              (created_at, fire_at, channel_id, description, task_type, tool_calls,
               intro_message, static_message, generative_prompt,
               max_attempts, check_interval_minutes, recurrence_rule)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
                next_utc,
                task["channel_id"],
                task["description"],
                task["task_type"],
                task["tool_calls"],
                task["intro_message"],
                task["static_message"],
                task["generative_prompt"],
                task["max_attempts"],
                task["check_interval_minutes"],
                rule,
            ),
        )
