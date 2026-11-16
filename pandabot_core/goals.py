"""
pandabot_core.goals
~~~~~~~~~~~~~~~~~~~~
Persistent store for **long-term goals** — a GitHub epic that the bot team works
to completion incrementally, one story (sub-issue) at a time.

A goal survives a bot restart: unlike pandabot-dev's in-memory ``_active_sessions``
dict (which tracks single in-flight builds and is lost on restart), the goal
driver records its progress here so it can resume mid-goal. GitHub remains the source
of truth for the work items themselves (epic + story sub-issues + their ``status:``
labels); this table is the driver's local bookkeeping — which story is in flight, the
builder and build driving it, feedback rounds spent, and the last QA verdict.

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
    "DESIGN_IN_REVIEW", "DESIGN_APPROVED", "DESIGN_BLOCKED", "ALL_DESIGN_LABELS",
    "ART_IN_QA", "ART_APPROVED", "ART_CHANGES_REQUESTED", "ART_BLOCKED",
    "ALL_ART_LABELS",
    "GOAL_INTAKE", "GOAL_ACTIVE", "GOAL_PAUSED", "GOAL_COMPLETE",
    "init_db", "create_goal", "get_goal", "active_goals",
    "set_goal_state", "complete_goal",
    "upsert_story_run", "get_story_run", "story_runs_for_goal",
    "set_story_status", "record_dispatch", "record_qa_verdict",
    "in_flight_story", "next_story_number",
    "story_builder_ref",
    "record_escalation", "escalations_for_story", "escalations_for_goal",
    "SOURCE_CI", "SOURCE_PREFILTER", "SOURCE_QA", "SOURCE_DRIVER", "ALL_SOURCES",
    "ATTR_BUILD", "ATTR_GATE", "ATTR_INFRA", "ATTR_SPEC", "ALL_ATTRIBUTIONS",
    "RESULT_PASS", "RESULT_FAIL", "RESULT_ERROR",
    "record_verdict", "verdicts_for_story", "verdicts_for_goal", "has_verdict_ref",
    "trailing_verdicts",
]

# --- Builders ---------------------------------------------------------------
# The builder that writes a story's code (pandabot-dev ``docs/builder-tiers.md``):
# ``harness:deepseek`` today, ``harness:desktop`` after its phase 5. Only the name
# is stored here: the builder protocol itself lives in pandabot-dev, which is the
# only bot that dispatches builds. Old rows may name the retired ``jules``.

# --- Verdicts and failure attribution ---------------------------------------
# Every verdict on a story round (CI, the DeepSeek pre-filter, PandaQA, or the driver
# itself noticing a stall) is kept with its attribution: what the failure is about.
# Only ``build`` failures spend a builder's feedback rounds; ``gate``, ``infrastructure``
# and ``spec`` failures retry or block instead (pandabot-dev docs/builder-tiers.md 4.5).
SOURCE_CI = "ci"
SOURCE_PREFILTER = "prefilter"
SOURCE_QA = "qa"
SOURCE_DRIVER = "driver"          # the goal driver's own findings (a stalled hand-off)
ALL_SOURCES = [SOURCE_CI, SOURCE_PREFILTER, SOURCE_QA, SOURCE_DRIVER]

ATTR_BUILD = "build"              # the builder's code: spends a feedback round
ATTR_GATE = "gate"                # the QA gate cannot judge it: block with a reason
ATTR_INFRA = "infrastructure"     # tooling failed: retry with backoff, then block
ATTR_SPEC = "spec"                # the story itself is wrong: block, route to Plan
ALL_ATTRIBUTIONS = [ATTR_BUILD, ATTR_GATE, ATTR_INFRA, ATTR_SPEC]

RESULT_PASS = "pass"
RESULT_FAIL = "fail"
RESULT_ERROR = "error"

# A verdict's text is kept as an excerpt: enough to audit the attribution, not a log.
VERDICT_EXCERPT_CHARS = 2000

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

# --- Art-loop sub-state (parallel ``art:`` label) ---------------------------
# A story that needs assets goes through an art phase BEFORE its build.
# The art sub-state rides a separate ``art:`` label so it never clobbers the
# story's ``status:`` label (which stays ``ready`` until the assets are in and
# the build is dispatched). PandaQA writes the verdict; PandaBot-Dev reads it.
ART_IN_QA = "art: in-qa"                      # asset generated, awaiting QA verdict
ART_APPROVED = "art: approved"                # QA approved the current asset
ART_CHANGES_REQUESTED = "art: changes-requested"  # QA wants a regeneration
ART_BLOCKED = "art: blocked"                  # too many failed art rounds

ALL_ART_LABELS = [ART_IN_QA, ART_APPROVED, ART_CHANGES_REQUESTED, ART_BLOCKED]

# --- Design-phase sub-state (parallel ``design:`` label) --------------------
# A story goes through a design pass BEFORE its art phase: PandaBot-Design reads
# the acceptance criteria, adds the design materials the build will need (an
# ``art`` block for any implied assets, concrete design ACs), and approves.
# Design runs first because the art phase consumes the ``art`` block it writes.
# Like ``art:``, this rides its own label so it never clobbers ``status:``.
DESIGN_IN_REVIEW = "design: in-review"   # handed to PandaBot-Design, awaiting its pass
DESIGN_APPROVED = "design: approved"     # design materials are in place; proceed to art
DESIGN_BLOCKED = "design: blocked"       # design could not be resolved autonomously

ALL_DESIGN_LABELS = [DESIGN_IN_REVIEW, DESIGN_APPROVED, DESIGN_BLOCKED]

# --- Goal-level state (epic) ------------------------------------------------
GOAL_INTAKE = "intake"      # awaiting QA capability review / skill
GOAL_ACTIVE = "active"      # driver is working stories
GOAL_PAUSED = "paused"      # held by the operator: the driver and the build poller leave it alone
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
                base_branch  TEXT    NOT NULL DEFAULT 'main',
                channel_id   INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT    NOT NULL,
                updated_at   TEXT    NOT NULL,
                UNIQUE(repo, epic_number)
            )
        """)
        # Defensive migration: add base_branch to a goals table created before it existed.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(goals)")}
        if "base_branch" not in cols:
            conn.execute("ALTER TABLE goals ADD COLUMN base_branch TEXT NOT NULL DEFAULT 'main'")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS goal_story_runs (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id           INTEGER NOT NULL,
                story_number      INTEGER NOT NULL,
                builder           TEXT    NOT NULL DEFAULT '',
                builder_ref       TEXT    NOT NULL DEFAULT '',
                pr_url            TEXT    NOT NULL DEFAULT '',
                status            TEXT    NOT NULL DEFAULT 'status: ready',
                feedback_rounds   INTEGER NOT NULL DEFAULT 0,
                qa_verdict        TEXT    NOT NULL DEFAULT '',
                updated_at        TEXT    NOT NULL,
                UNIQUE(goal_id, story_number)
            )
        """)
        _migrate_story_runs_builder(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS goal_escalations (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id       INTEGER NOT NULL,
                story_number  INTEGER NOT NULL,
                from_builder  TEXT    NOT NULL DEFAULT '',
                to_builder    TEXT    NOT NULL DEFAULT '',
                reason        TEXT    NOT NULL DEFAULT '',
                created_at    TEXT    NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_goal_escalations_story "
            "ON goal_escalations (goal_id, story_number)"
        )
        # Additive (phase G): a new table only, so it is safe on a live scheduler.db and
        # an older pandabot-dev that never reads it is unaffected.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS goal_verdicts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id       INTEGER NOT NULL,
                story_number  INTEGER NOT NULL,
                source        TEXT    NOT NULL,
                result        TEXT    NOT NULL DEFAULT 'fail',
                attribution   TEXT    NOT NULL DEFAULT '',
                signature     TEXT    NOT NULL DEFAULT '',
                excerpt       TEXT    NOT NULL DEFAULT '',
                ref           TEXT    NOT NULL DEFAULT '',
                builder       TEXT    NOT NULL DEFAULT '',
                feedback_round INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT    NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_goal_verdicts_story "
            "ON goal_verdicts (goal_id, story_number)"
        )


def _migrate_story_runs_builder(conn: sqlite3.Connection) -> None:
    """Bring an older ``goal_story_runs`` up to date. Safe on every ``init_db``.

    1. Add ``builder`` and ``builder_ref`` to a table created before builders existed.
    2. Retire ``jules_session_id`` (builder-tiers phase 7): copy any id it still holds
       into ``builder_ref`` (those rows were Jules builds), then drop the column. On a
       SQLite too old to drop a column (before 3.35) it stays, unread and unwritten."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(goal_story_runs)")}
    if "builder" not in cols:
        conn.execute(
            "ALTER TABLE goal_story_runs ADD COLUMN builder TEXT NOT NULL DEFAULT ''"
        )
    if "builder_ref" not in cols:
        conn.execute(
            "ALTER TABLE goal_story_runs ADD COLUMN builder_ref TEXT NOT NULL DEFAULT ''"
        )
    if "jules_session_id" in cols:
        conn.execute(
            "UPDATE goal_story_runs SET builder_ref = jules_session_id, builder = 'jules' "
            "WHERE builder_ref = '' AND jules_session_id != ''"
        )
        try:
            conn.execute("ALTER TABLE goal_story_runs DROP COLUMN jules_session_id")
        except sqlite3.OperationalError:
            pass


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db())
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------

def create_goal(repo: str, epic_number: int, title: str = "",
                channel_id: int = 0, state: str = GOAL_ACTIVE,
                base_branch: str = "main") -> int:
    """Register a goal (or return the existing id for the same repo+epic).

    base_branch is the branch builds target for this goal's stories (the repo's
    default branch — resolved by the caller), not assumed to be 'staging'."""
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
                "UPDATE goals SET title = ?, channel_id = ?, state = ?, base_branch = ?, updated_at = ? WHERE id = ?",
                (title, channel_id, state, base_branch, now, row["id"]),
            )
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO goals (repo, epic_number, title, state, base_branch, channel_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (repo, epic_number, title, state, base_branch, channel_id, now, now),
        )
        return int(cur.lastrowid)


def get_goal(goal_id: int) -> dict[str, Any] | None:
    init_db()
    with _conn() as conn:
        row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        return dict(row) if row else None


def active_goals(include_paused: bool = False) -> list[dict[str, Any]]:
    """Every goal the driver should still be working: not complete, and not paused
    unless ``include_paused`` (status listings want paused goals too)."""
    init_db()
    skip = (GOAL_COMPLETE,) if include_paused else (GOAL_COMPLETE, GOAL_PAUSED)
    marks = ",".join("?" for _ in skip)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM goals WHERE state NOT IN ({marks}) ORDER BY id", skip
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
    allowed = {"pr_url", "status", "feedback_rounds", "qa_verdict", "builder", "builder_ref"}
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
        # A new row names no builder until it is dispatched (an older table still
        # carries the column default 'jules', so say so explicitly).
        sets.setdefault("builder", "")
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


def record_dispatch(goal_id: int, story_number: int, builder_ref: str, *,
                    builder: str) -> None:
    """Mark a story as dispatched to ``builder`` and in progress. ``builder_ref`` is
    the builder's handle on the build (the harness job id)."""
    upsert_story_run(goal_id, story_number, builder=builder, builder_ref=builder_ref,
                     status=STATUS_IN_PROGRESS)


def story_builder_ref(run: dict[str, Any]) -> str:
    """The builder ref of a story run ('' when it was never dispatched)."""
    return run.get("builder_ref") or ""


def record_qa_verdict(goal_id: int, story_number: int, verdict: str, status: str) -> None:
    upsert_story_run(goal_id, story_number, qa_verdict=verdict, status=status)


# ---------------------------------------------------------------------------
# Escalations (the builder routing log)
# ---------------------------------------------------------------------------

def record_escalation(goal_id: int, story_number: int, from_builder: str,
                      to_builder: str, reason: str) -> int:
    """Log one routing move for a story: which builder it left, which it went to,
    and why. ``from_builder`` is '' for the first routing decision."""
    init_db()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO goal_escalations "
            "(goal_id, story_number, from_builder, to_builder, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (goal_id, story_number, from_builder, to_builder, reason, _now()),
        )
        return int(cur.lastrowid)


def escalations_for_story(goal_id: int, story_number: int) -> list[dict[str, Any]]:
    """A story's routing history, oldest first."""
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM goal_escalations WHERE goal_id = ? AND story_number = ? "
            "ORDER BY id",
            (goal_id, story_number),
        ).fetchall()
        return [dict(r) for r in rows]


def escalations_for_goal(goal_id: int) -> list[dict[str, Any]]:
    """Every routing move for a goal's stories, oldest first."""
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM goal_escalations WHERE goal_id = ? ORDER BY id", (goal_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Verdicts (one row per verdict on a story round, with its attribution)
# ---------------------------------------------------------------------------

def record_verdict(goal_id: int, story_number: int, source: str, result: str,
                   attribution: str = "", excerpt: str = "", signature: str = "",
                   ref: str = "", builder: str = "", feedback_round: int = 0) -> int:
    """Keep one verdict: where it came from, pass/fail, what it was attributed to, the
    signature that decided the attribution ('deepseek' or '' when none matched), and an
    excerpt of its text. ``ref`` identifies the verdict at its source (a QA comment id,
    a PR head sha) so the driver acts on each verdict once. ``feedback_round`` is the
    story's build round count when the verdict arrived."""
    init_db()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO goal_verdicts (goal_id, story_number, source, result, attribution, "
            "signature, excerpt, ref, builder, feedback_round, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (goal_id, story_number, source, result, attribution, signature[:200],
             (excerpt or "")[:VERDICT_EXCERPT_CHARS], ref, builder, int(feedback_round),
             _now()),
        )
        return int(cur.lastrowid)


def verdicts_for_story(goal_id: int, story_number: int,
                       source: str | None = None) -> list[dict[str, Any]]:
    """A story's verdicts, oldest first, optionally from one source."""
    init_db()
    sql = "SELECT * FROM goal_verdicts WHERE goal_id = ? AND story_number = ?"
    args: list[Any] = [goal_id, story_number]
    if source:
        sql += " AND source = ?"
        args.append(source)
    with _conn() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id", args).fetchall()]


def verdicts_for_goal(goal_id: int) -> list[dict[str, Any]]:
    """Every verdict on a goal's stories, oldest first (for reports and tuning)."""
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM goal_verdicts WHERE goal_id = ? ORDER BY id", (goal_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def has_verdict_ref(goal_id: int, story_number: int, ref: str) -> bool:
    """True if a verdict with this source ref was already recorded for the story."""
    if not ref:
        return False
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM goal_verdicts WHERE goal_id = ? AND story_number = ? AND ref = ?",
            (goal_id, story_number, ref),
        ).fetchone()
        return row is not None


def trailing_verdicts(goal_id: int, story_number: int, attribution: str,
                      source: str | None = None) -> int:
    """How many of the story's most recent verdicts in a row carry ``attribution``
    (the run of consecutive infrastructure failures, for retry limits). A verdict with
    any other attribution or a pass ends the run."""
    n = 0
    for v in reversed(verdicts_for_story(goal_id, story_number, source)):
        if v["attribution"] != attribution or v["result"] == RESULT_PASS:
            break
        n += 1
    return n


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
