"""
pandabot_core.scheduler_runtime
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Runtime helpers wrapping `pandabot_core.scheduler` for use by bots that want
the manage_schedule tool without rebuilding the CRUD shell and polling loop.

Two consumers today: PandaQA and PandabotDevops. Pandabot has its own richer
fire_scheduled_task in discord-bot/bot.py (generative_prompt, condition_check,
llm_usage logging) and does not use this module.

What this module exposes:

  TOOL_DEFINITION
      Anthropic-format tool schema for `manage_schedule`. Drop into the bot's
      TOOL_DEFINITIONS list.

  handle_manage_schedule(action, default_channel_id, **kwargs) -> str
      CRUD handler for the tool. Bots call this from their dispatcher when the
      LLM picks `manage_schedule`.

  run_polling_loop(*, is_running, fire_callback, poll_seconds=60)
      Async loop. Polls scheduler.get_due_tasks() and schedules fire_callback
      for each due task. Bots start this as a background task at on_ready.

Supported task shapes (subset of the underlying scheduler):

  one_shot    — fire once at fire_at, then done.
  recurring   — fire at fire_at, then schedule the next occurrence using
                recurrence_rule ('weekly:W' or 'monthly:D').

  Payload at fire time is either `static_message` (post verbatim) or `prompt`
  (a natural-language instruction the bot routes through its own LLM loop).
  Bots see the difference via task["static_message"] vs task["generative_prompt"]
  — we re-use the existing schema column `generative_prompt` to carry `prompt`
  rather than adding a new column, so older bots stay compatible.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any, Awaitable, Callable

from pandabot_core import scheduler

__all__ = [
    "TOOL_DEFINITION",
    "handle_manage_schedule",
    "run_polling_loop",
    "fire_one_shot_or_recurring",
]

log = logging.getLogger("pandabot.scheduler_runtime")


TOOL_DEFINITION: dict[str, Any] = {
    "name": "manage_schedule",
    "description": (
        "Create, list, or cancel scheduled tasks. Use this whenever the user "
        "asks for something at a future time or on a recurring schedule "
        "(e.g. 'every Monday at 9am run the release tests', 'each morning "
        "tail the discord-bot logs'). At fire time, the task either posts a "
        "fixed message or hands a natural-language prompt back to you so you "
        "can carry out the work using your normal tools.\n"
        "action='create': required: fire_at (local ISO datetime, e.g. "
        "'2026-06-15T09:00:00'), description. Provide exactly one of "
        "static_message (posted verbatim, no LLM call) or prompt (run as a "
        "scheduled instruction in this channel). task_type defaults to "
        "'one_shot'; use 'recurring' with recurrence_rule for repeats.\n"
        "action='list': show all pending scheduled tasks with their fire time "
        "and recurrence rule.\n"
        "action='cancel': cancel a task by id (use list to find ids)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "cancel"],
                "description": "Operation to perform.",
            },
            "id": {
                "type": "integer",
                "description": "Task id — required for cancel.",
            },
            "description": {
                "type": "string",
                "description": "Human-readable summary of the task. Shown in list output.",
            },
            "fire_at": {
                "type": "string",
                "description": (
                    "Local ISO datetime for first/only fire, e.g. "
                    "'2026-06-15T09:00:00'. For recurring tasks this also sets "
                    "the time-of-day for every subsequent fire."
                ),
            },
            "task_type": {
                "type": "string",
                "enum": ["one_shot", "recurring"],
                "description": "Default 'one_shot'.",
            },
            "recurrence_rule": {
                "type": "string",
                "description": (
                    "Required when task_type='recurring'. "
                    "'weekly:W' fires once a week on weekday W (0=Monday, 6=Sunday). "
                    "'monthly:D' fires on day-of-month D each month."
                ),
            },
            "static_message": {
                "type": "string",
                "description": (
                    "Posted to the channel as-is at fire time. No LLM call, no "
                    "tools. Use for plain reminders ('time to back up the NAS')."
                ),
            },
            "prompt": {
                "type": "string",
                "description": (
                    "Natural-language instruction the bot runs through its own "
                    "LLM loop at fire time, in the same channel. Use this when "
                    "the scheduled work needs tool calls — e.g. 'run the release "
                    "test suite against production' or 'check disk usage on panda "
                    "and warn if any partition is over 90%%'."
                ),
            },
        },
        "required": ["action"],
    },
}


def handle_manage_schedule(
    action: str,
    default_channel_id: int,
    **kwargs: Any,
) -> str:
    """CRUD entrypoint for the manage_schedule tool.

    `default_channel_id` is the channel the bot considers its "home" — where
    scheduled output is posted. Each bot supplies its own (QA channel for
    PandaQA, devops channel for PandabotDevops).
    """
    scheduler.init_db()

    if action == "list":
        tasks = scheduler.list_pending()
        if not tasks:
            return "No scheduled tasks pending."
        lines = ["Pending scheduled tasks:"]
        for t in tasks:
            try:
                fire_local = (
                    datetime.datetime.fromisoformat(t["fire_at"])
                    .astimezone()
                    .strftime("%a %b %d %I:%M %p %Z")
                )
            except Exception:
                fire_local = t["fire_at"]
            recurr = f" [repeats {t['recurrence_rule']}]" if t["recurrence_rule"] else ""
            lines.append(
                f"  #{t['id']}  {fire_local}  — {t['description']} ({t['task_type']}){recurr}"
            )
        return "\n".join(lines)

    if action == "cancel":
        task_id = kwargs.get("id")
        if task_id is None:
            return "cancel requires an id (run list to find it)."
        cancelled = scheduler.cancel_task(int(task_id))
        return (
            f"Task #{task_id} cancelled."
            if cancelled
            else f"Task #{task_id} not found or already done."
        )

    if action == "create":
        fire_at = kwargs.get("fire_at")
        if not fire_at:
            return "create requires fire_at (local ISO datetime, e.g. '2026-06-15T09:00:00')."
        description = (kwargs.get("description") or "").strip() or "Scheduled task"
        task_type = kwargs.get("task_type", "one_shot")
        if task_type not in ("one_shot", "recurring"):
            return f"task_type must be one_shot or recurring (got {task_type!r})."

        recurrence_rule = kwargs.get("recurrence_rule")
        if task_type == "recurring" and not recurrence_rule:
            return "recurring tasks require recurrence_rule (e.g. 'weekly:0' for Mondays)."
        if recurrence_rule and not (
            recurrence_rule.startswith("weekly:") or recurrence_rule.startswith("monthly:")
        ):
            return (
                "recurrence_rule must be 'weekly:W' (W=0..6, Mon..Sun) or "
                "'monthly:D' (D=1..28)."
            )

        static_message = kwargs.get("static_message")
        prompt = kwargs.get("prompt")
        if not static_message and not prompt:
            return (
                "create requires exactly one of static_message (verbatim post) or "
                "prompt (natural-language instruction the bot runs at fire time)."
            )
        if static_message and prompt:
            return "Provide static_message OR prompt, not both."

        try:
            task_id = scheduler.add_task(
                fire_at_local=fire_at,
                channel_id=default_channel_id,
                description=description,
                task_type=task_type,
                static_message=static_message,
                # Re-use the generative_prompt column to carry the natural-language
                # prompt — the underlying scheduler stays unchanged.
                generative_prompt=prompt,
                recurrence_rule=recurrence_rule,
            )
        except Exception as exc:
            log.exception("scheduler.add_task failed")
            return f"Failed to schedule task: {exc}"

        try:
            local_dt = datetime.datetime.fromisoformat(fire_at)
            time_str = local_dt.strftime("%A %b %d at %I:%M %p")
        except Exception:
            time_str = fire_at
        type_note = (
            f"repeats {recurrence_rule}" if task_type == "recurring" else "fires once"
        )
        return f'Scheduled #{task_id}: "{description}" — {time_str} ({type_note}).'

    return f"Unknown action '{action}'. Use create, list, or cancel."


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

FireCallback = Callable[[dict], Awaitable[None]]


async def run_polling_loop(
    *,
    is_running: Callable[[], bool],
    fire_callback: FireCallback,
    poll_seconds: int = 60,
) -> None:
    """Poll the scheduler DB and dispatch due tasks.

    Bots call this as a background task once their Discord client is ready.

    Args:
        is_running: callable returning False when the bot is shutting down
                    (e.g. ``lambda: not bot.is_closed()``). The loop exits
                    once this returns False.
        fire_callback: async callable invoked with a dict of the task row for
                       each due task. The callback is responsible for posting
                       the result, marking the task done, and scheduling the
                       next occurrence for recurring tasks (use
                       fire_one_shot_or_recurring below as a helper).
        poll_seconds: how often to poll the DB (default 60).
    """
    scheduler.init_db()
    log.info("scheduler_runtime poll loop started (every %ds)", poll_seconds)

    while is_running():
        try:
            loop = asyncio.get_running_loop()
            due = await loop.run_in_executor(None, scheduler.get_due_tasks)
            for task in due:
                asyncio.create_task(fire_callback(dict(task)))
        except Exception:
            log.exception("scheduler_runtime poll error")
        await asyncio.sleep(poll_seconds)


async def fire_one_shot_or_recurring(
    task: dict,
    *,
    post_static: Callable[[int, str], Awaitable[None]],
    run_prompt: Callable[[int, str, str], Awaitable[None]],
) -> None:
    """Default fire handler covering static_message and prompt tasks.

    Bots usually want this; they only need to supply the two posting callbacks.

    Args:
        task: dict-row from scheduler.get_due_tasks().
        post_static: async (channel_id, message) -> None. Used when
                     task["static_message"] is set.
        run_prompt: async (channel_id, description, prompt) -> None. Called
                    when task["generative_prompt"] (carrying the natural-language
                    prompt) is set. Implementation routes the prompt through the
                    bot's LLM loop and posts the result.
    """
    task_id = task["id"]
    channel_id = task["channel_id"]
    description = task.get("description") or "Scheduled task"
    static_message = task.get("static_message")
    prompt = task.get("generative_prompt")
    loop = asyncio.get_running_loop()

    try:
        if static_message:
            await post_static(channel_id, static_message)
        elif prompt:
            await run_prompt(channel_id, description, prompt)
        else:
            # Nothing to do — surface so the bug is visible instead of silent.
            await post_static(
                channel_id,
                f"Scheduled task #{task_id} ({description}) had no static_message or prompt — skipped.",
            )
    except Exception:
        log.exception("fire_one_shot_or_recurring failed for task #%d", task_id)
        try:
            await post_static(
                channel_id,
                f"Scheduled task #{task_id} ({description}) failed — check bot logs.",
            )
        except Exception:
            log.exception("failed to post failure notice for task #%d", task_id)
    finally:
        if task.get("task_type") == "recurring" and task.get("recurrence_rule"):
            try:
                await loop.run_in_executor(None, scheduler.schedule_next_recurring, task)
            except Exception:
                log.exception("schedule_next_recurring failed for task #%d", task_id)
        try:
            await loop.run_in_executor(None, scheduler.mark_done, task_id)
        except Exception:
            log.exception("mark_done failed for task #%d", task_id)
