"""Tests for pandabot_core.scheduler_runtime."""

from __future__ import annotations

import asyncio
import datetime

from pandabot_core import scheduler, scheduler_runtime


CHANNEL = 99999


def _future_iso(minutes: int = 60) -> str:
    return (datetime.datetime.now() + datetime.timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


def _past_iso(minutes: int = 5) -> str:
    return (datetime.datetime.now() - datetime.timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


# ---------------------------------------------------------------------------
# Tool definition shape
# ---------------------------------------------------------------------------

def test_tool_definition_shape():
    td = scheduler_runtime.TOOL_DEFINITION
    assert td["name"] == "manage_schedule"
    assert "input_schema" in td
    props = td["input_schema"]["properties"]
    # The two payload modes a fire callback supports.
    assert "static_message" in props
    assert "prompt" in props
    # Recurrence support exposed.
    assert "recurrence_rule" in props
    assert td["input_schema"]["required"] == ["action"]


# ---------------------------------------------------------------------------
# handle_manage_schedule
# ---------------------------------------------------------------------------

def test_create_one_shot_with_static_message():
    out = scheduler_runtime.handle_manage_schedule(
        "create",
        CHANNEL,
        fire_at=_future_iso(),
        description="reminder",
        static_message="time to back up the NAS",
    )
    assert "Scheduled #" in out
    pending = scheduler.list_pending()
    assert len(pending) == 1
    assert pending[0]["description"] == "reminder"


def test_create_recurring_with_prompt():
    out = scheduler_runtime.handle_manage_schedule(
        "create",
        CHANNEL,
        fire_at=_future_iso(),
        description="weekly release run",
        prompt="run the release suite against production",
        task_type="recurring",
        recurrence_rule="weekly:0",
    )
    assert "Scheduled #" in out
    assert "repeats" in out.lower()


def test_create_rejects_missing_fire_at():
    out = scheduler_runtime.handle_manage_schedule(
        "create", CHANNEL, description="no time given", static_message="hi"
    )
    assert "fire_at" in out


def test_create_rejects_both_static_and_prompt():
    out = scheduler_runtime.handle_manage_schedule(
        "create",
        CHANNEL,
        fire_at=_future_iso(),
        description="both",
        static_message="x",
        prompt="y",
    )
    assert "not both" in out.lower()


def test_create_rejects_neither_payload():
    out = scheduler_runtime.handle_manage_schedule(
        "create",
        CHANNEL,
        fire_at=_future_iso(),
        description="empty payload",
    )
    assert "static_message" in out and "prompt" in out


def test_create_recurring_requires_rule():
    out = scheduler_runtime.handle_manage_schedule(
        "create",
        CHANNEL,
        fire_at=_future_iso(),
        description="recurring without rule",
        prompt="do thing",
        task_type="recurring",
    )
    assert "recurrence_rule" in out


def test_create_rejects_bad_recurrence_rule():
    out = scheduler_runtime.handle_manage_schedule(
        "create",
        CHANNEL,
        fire_at=_future_iso(),
        description="bad rule",
        prompt="do thing",
        task_type="recurring",
        recurrence_rule="every_blue_moon",
    )
    assert "weekly" in out and "monthly" in out


def test_list_empty():
    out = scheduler_runtime.handle_manage_schedule("list", CHANNEL)
    assert "No scheduled tasks" in out


def test_list_renders_tasks():
    scheduler_runtime.handle_manage_schedule(
        "create",
        CHANNEL,
        fire_at=_future_iso(),
        description="alpha task",
        static_message="ping",
    )
    out = scheduler_runtime.handle_manage_schedule("list", CHANNEL)
    assert "alpha task" in out
    assert "#" in out


def test_cancel_round_trip():
    create_out = scheduler_runtime.handle_manage_schedule(
        "create",
        CHANNEL,
        fire_at=_future_iso(),
        description="cancel me",
        static_message="x",
    )
    # Pull the id back out of "Scheduled #N:" prefix
    task_id = int(create_out.split("#")[1].split(":")[0])
    out = scheduler_runtime.handle_manage_schedule("cancel", CHANNEL, id=task_id)
    assert "cancelled" in out.lower()
    # Second cancel is a no-op
    out2 = scheduler_runtime.handle_manage_schedule("cancel", CHANNEL, id=task_id)
    assert "not found" in out2.lower() or "already" in out2.lower()


def test_cancel_requires_id():
    out = scheduler_runtime.handle_manage_schedule("cancel", CHANNEL)
    assert "id" in out.lower()


def test_unknown_action():
    out = scheduler_runtime.handle_manage_schedule("frobnicate", CHANNEL)
    assert "Unknown action" in out


# ---------------------------------------------------------------------------
# fire_one_shot_or_recurring
# ---------------------------------------------------------------------------

def test_fire_static_message_posts_and_marks_done():
    scheduler.init_db()
    task_id = scheduler.add_task(
        fire_at_local=_past_iso(),
        channel_id=CHANNEL,
        description="static fire",
        task_type="one_shot",
        static_message="hello world",
    )
    due = scheduler.get_due_tasks()
    assert any(t["id"] == task_id for t in due)

    posted: list[tuple[int, str]] = []
    prompts: list[tuple[int, str, str]] = []

    async def post_static(channel_id: int, msg: str) -> None:
        posted.append((channel_id, msg))

    async def run_prompt(channel_id: int, desc: str, prompt: str) -> None:
        prompts.append((channel_id, desc, prompt))

    task = dict(next(t for t in due if t["id"] == task_id))
    asyncio.run(
        scheduler_runtime.fire_one_shot_or_recurring(
            task, post_static=post_static, run_prompt=run_prompt
        )
    )

    assert posted == [(CHANNEL, "hello world")]
    assert prompts == []
    # marked done — no longer due
    assert not any(t["id"] == task_id for t in scheduler.get_due_tasks())


def test_fire_prompt_routes_through_run_prompt():
    scheduler.init_db()
    task_id = scheduler.add_task(
        fire_at_local=_past_iso(),
        channel_id=CHANNEL,
        description="weekly release run",
        task_type="one_shot",
        generative_prompt="run the release suite against production",
    )
    task = dict(next(t for t in scheduler.get_due_tasks() if t["id"] == task_id))

    posted: list[tuple[int, str]] = []
    prompts: list[tuple[int, str, str]] = []

    async def post_static(channel_id: int, msg: str) -> None:
        posted.append((channel_id, msg))

    async def run_prompt(channel_id: int, desc: str, prompt: str) -> None:
        prompts.append((channel_id, desc, prompt))

    asyncio.run(
        scheduler_runtime.fire_one_shot_or_recurring(
            task, post_static=post_static, run_prompt=run_prompt
        )
    )

    assert prompts == [
        (CHANNEL, "weekly release run", "run the release suite against production")
    ]
    assert posted == []


def test_fire_recurring_schedules_next_occurrence():
    scheduler.init_db()
    # Past fire so it's due now; weekly rule means a fresh row is added.
    task_id = scheduler.add_task(
        fire_at_local=_past_iso(),
        channel_id=CHANNEL,
        description="recurring",
        task_type="recurring",
        static_message="tick",
        recurrence_rule="weekly:0",
    )
    task = dict(next(t for t in scheduler.get_due_tasks() if t["id"] == task_id))

    async def post_static(channel_id: int, msg: str) -> None:
        pass

    async def run_prompt(channel_id: int, desc: str, prompt: str) -> None:
        pass

    asyncio.run(
        scheduler_runtime.fire_one_shot_or_recurring(
            task, post_static=post_static, run_prompt=run_prompt
        )
    )

    pending = scheduler.list_pending()
    # Original is done; a new recurring row exists with the same description.
    assert any(p["description"] == "recurring" for p in pending)
    assert all(p["id"] != task_id for p in pending)


def test_fire_callback_failure_posts_warning():
    scheduler.init_db()
    task_id = scheduler.add_task(
        fire_at_local=_past_iso(),
        channel_id=CHANNEL,
        description="will fail",
        task_type="one_shot",
        generative_prompt="something",
    )
    task = dict(next(t for t in scheduler.get_due_tasks() if t["id"] == task_id))

    posted: list[tuple[int, str]] = []

    async def post_static(channel_id: int, msg: str) -> None:
        posted.append((channel_id, msg))

    async def run_prompt(channel_id: int, desc: str, prompt: str) -> None:
        raise RuntimeError("boom")

    asyncio.run(
        scheduler_runtime.fire_one_shot_or_recurring(
            task, post_static=post_static, run_prompt=run_prompt
        )
    )

    assert posted, "expected a failure notice to be posted"
    assert "failed" in posted[0][1].lower()
    # Still marked done so it doesn't refire on the next poll.
    assert not any(t["id"] == task_id for t in scheduler.get_due_tasks())


# ---------------------------------------------------------------------------
# run_polling_loop
# ---------------------------------------------------------------------------

def test_polling_loop_invokes_fire_callback_for_due_tasks():
    scheduler.init_db()
    task_id = scheduler.add_task(
        fire_at_local=_past_iso(),
        channel_id=CHANNEL,
        description="due now",
        task_type="one_shot",
        static_message="x",
    )

    fired: list[int] = []

    async def driver():
        seen = asyncio.Event()
        ticks = {"n": 0}

        async def fire_callback(task: dict) -> None:
            fired.append(task["id"])
            scheduler.mark_done(task["id"])
            seen.set()

        def is_running() -> bool:
            ticks["n"] += 1
            # Allow two polls: first one finds the task and schedules the
            # callback; second poll returns False and exits cleanly.
            return ticks["n"] <= 1

        loop_task = asyncio.create_task(
            scheduler_runtime.run_polling_loop(
                is_running=is_running,
                fire_callback=fire_callback,
                poll_seconds=0,
            )
        )
        await asyncio.wait_for(seen.wait(), timeout=2.0)
        await asyncio.wait_for(loop_task, timeout=2.0)

    asyncio.run(driver())
    assert fired == [task_id]
