"""
pandabot_core.channels
~~~~~~~~~~~~~~~~~~~~~~~~
Cross-bot Discord messaging.

Convention — every bot owns one Discord channel that acts as its **inbox**. To
ask bot X to do something, post the request in X's channel, where X is listening.
A reply happens in that same channel (one-way from the requester's point of view);
humans observe the exchange there.

This module is the shared *mechanism* only. The concrete name -> channel-id map is
supplied at runtime via the ``BOT_CHANNELS`` env var (or passed explicitly), never
hardcoded here, so core stays deployment-agnostic.

``BOT_CHANNELS`` format — comma-separated ``name:channel_id`` pairs::

    BOT_CHANNELS=pandabot-dev:123456,pandabot-devops:456789

Invariant for the convention to work: each map *name* must be a token the target
bot recognises as itself (i.e. one of its addressing aliases), because
:func:`send_to_bot` puts that name in the message header so the target's
addressing gate matches.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    import discord

log = logging.getLogger("pandabot.channels")

__all__ = [
    "BotChannelMap",
    "send_to_bot",
    "send_to_bot_threadsafe",
    "make_message_bot_tool",
    "MessageBotDispatcher",
    "attach_message_bot",
]


class BotChannelMap:
    """A name -> Discord-channel-id registry for sibling bots."""

    def __init__(self, mapping: "dict[str, int] | None" = None) -> None:
        self._map: dict[str, int] = {}
        for name, cid in (mapping or {}).items():
            self._map[str(name).strip().lower()] = int(cid)

    @classmethod
    def from_env(cls, var: str = "BOT_CHANNELS") -> "BotChannelMap":
        """Parse ``name:id,name:id`` pairs from an environment variable."""
        mapping: dict[str, int] = {}
        for pair in os.environ.get(var, "").split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            name, _, cid = pair.partition(":")
            name, cid = name.strip().lower(), cid.strip()
            if name and cid.isdigit():
                mapping[name] = int(cid)
        return cls(mapping)

    def get(self, name: str) -> "int | None":
        return self._map.get(str(name).strip().lower())

    def names(self) -> "list[str]":
        return sorted(self._map)

    def __contains__(self, name: str) -> bool:
        return str(name).strip().lower() in self._map

    def __bool__(self) -> bool:
        return bool(self._map)


def _format_request(target: str, text: str, sender: "str | None") -> str:
    """Build the posted message — header names the target so its gate matches."""
    who = f" from {sender}" if sender else ""
    return f"📨 **Request{who} → {target}**\n{text}"


async def send_to_bot(
    client: "discord.Client",
    channel_map: BotChannelMap,
    target: str,
    text: str,
    *,
    sender: "str | None" = None,
) -> str:
    """Post a request into ``target``'s channel. Returns a human-readable status."""
    cid = channel_map.get(target)
    if cid is None:
        known = ", ".join(channel_map.names()) or "(none configured)"
        return f"Unknown target bot '{target}'. Known targets: {known}."
    channel = client.get_channel(cid)
    if channel is None:
        return (
            f"Channel {cid} for '{target}' is not visible to this bot "
            f"(not a member, or the id is wrong)."
        )

    from .discord_comms import send_with_retry, split_message

    body = _format_request(target, text, sender)
    try:
        for chunk in split_message(body):
            if chunk.strip():
                await send_with_retry(channel, chunk)
    except Exception as exc:  # noqa: BLE001 - report, don't crash the loop
        log.exception("send_to_bot failed for target %s", target)
        return f"Failed to post to '{target}' channel: {exc}"
    name = getattr(channel, "name", target)
    return f"Delivered to #{name} — request posted in {target}'s channel (one-way)."


def send_to_bot_threadsafe(
    client: "discord.Client",
    loop: "asyncio.AbstractEventLoop",
    channel_map: BotChannelMap,
    target: str,
    text: str,
    *,
    sender: "str | None" = None,
    timeout: float = 30.0,
) -> str:
    """Call :func:`send_to_bot` from a sync tool running off the event loop.

    Tool functions run in ``run_in_executor`` (a worker thread), so they cannot
    await Discord directly. This bridges back to the bot's main loop.
    """
    fut = asyncio.run_coroutine_threadsafe(
        send_to_bot(client, channel_map, target, text, sender=sender), loop
    )
    try:
        return fut.result(timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        log.exception("send_to_bot_threadsafe failed for target %s", target)
        return f"Failed to deliver to '{target}': {exc}"


class MessageBotDispatcher:
    """One-call wiring for the ``message_bot`` inter-bot tool.

    Bots run their tool functions in a worker thread (``run_in_executor``), so the
    tool cannot await Discord directly. This bundles the channel map, the tool
    schema, and a thread-safe ``execute`` that bridges back to the bot's event loop
    — so a bot opts in with roughly::

        dispatcher = attach_message_bot(client, loop, sender="pandabot-dev")
        TOOL_DEFINITIONS.append(dispatcher.tool)
        # in execute_tool(): if name == "message_bot": return dispatcher.execute(args)

    Replies are NOT routed back here — the convention is one-way (see module
    docstring); the target writes results to the shared work item (a GitHub issue)
    or replies in its own channel where a human/poller picks them up.
    """

    def __init__(self, client, loop, channel_map: BotChannelMap,
                 sender: "str | None" = None) -> None:
        self._client = client
        self._loop = loop
        self._map = channel_map
        self._sender = sender
        self.tool = make_message_bot_tool(channel_map)

    def execute(self, args: dict) -> str:
        target = (args or {}).get("target", "")
        request = (args or {}).get("request", "")
        if not target or not request:
            return "message_bot requires both 'target' and 'request'."
        return send_to_bot_threadsafe(
            self._client, self._loop, self._map, target, request, sender=self._sender,
        )


def attach_message_bot(client, loop, *, sender: "str | None" = None,
                       channel_map: "BotChannelMap | None" = None) -> MessageBotDispatcher:
    """Build a :class:`MessageBotDispatcher` from the ``BOT_CHANNELS`` env var.

    ``loop`` is the bot's running asyncio event loop (capture it in ``on_ready`` via
    ``asyncio.get_running_loop()``). ``sender`` is this bot's own map name, used in the
    request header so the target's addressing gate matches.
    """
    cmap = channel_map if channel_map is not None else BotChannelMap.from_env()
    return MessageBotDispatcher(client, loop, cmap, sender=sender)


def make_message_bot_tool(channel_map: BotChannelMap) -> dict:
    """Build the ``message_bot`` tool schema, enumerating known targets."""
    targets = channel_map.names()
    listed = ", ".join(targets) if targets else "none configured"
    target_prop: dict = {
        "type": "string",
        "description": f"Name of the bot to send the request to. Known targets: {listed}.",
    }
    if targets:
        target_prop["enum"] = targets
    return {
        "name": "message_bot",
        "description": (
            "Send a request to another bot by posting it in THAT bot's Discord channel "
            "(its inbox), where it is listening. Use this whenever you need another agent "
            "to do something outside your own tool set. The target does the work and "
            "replies in its own channel; this is one-way (you will not receive its reply "
            f"back here). Known targets: {listed}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": target_prop,
                "request": {
                    "type": "string",
                    "description": "The natural-language request to post in the target bot's channel.",
                },
            },
            "required": ["target", "request"],
        },
    }
