"""
pandabot_core.discord_comms
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Discord client utilities: typing indicator, message splitting, send helpers,
channel history builder, webhook server framework, and confirmation manager.

The typing indicator implementation is carefully designed to avoid two known
anti-patterns in discord.py:

  BAD: async with channel.typing()
       Crashes the entire handler if Discord returns 429/500 on __aenter__.

  BAD: await channel.typing()
       Calls __aenter__ via __await__, spawning an internal do_typing background
       task that loops every 5 seconds and NEVER gets cancelled. Each message
       leaks another task, eventually hammering the typing endpoint into 429s.

  CORRECT: asyncio.create_task(_keep_typing()) with explicit cancel on reply.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    import discord

log = logging.getLogger("pandabot.discord_comms")

__all__ = [
    "keep_typing",
    "split_message",
    "send_with_retry",
    "build_history",
    "ConfirmationManager",
    "WebhookServer",
]

DISCORD_MSG_LIMIT = 1900  # leave headroom below Discord's 2000-char limit


# ---------------------------------------------------------------------------
# Typing indicator
# ---------------------------------------------------------------------------

def keep_typing(channel: "discord.abc.Messageable") -> asyncio.Task:
    """
    Return a running asyncio Task that re-triggers the typing indicator every 8 s.
    Cancel the returned task once the reply is ready to send.

    Usage:
        typing_task = keep_typing(channel)
        try:
            reply = await do_work()
        finally:
            typing_task.cancel()
    """
    async def _loop() -> None:
        try:
            while True:
                try:
                    await channel._state.http.send_typing(channel.id)  # type: ignore[attr-defined]
                except Exception as exc:
                    log.warning("Typing indicator failed (will retry): %s", exc)
                await asyncio.sleep(8)
        except asyncio.CancelledError:
            pass

    return asyncio.create_task(_loop())


# ---------------------------------------------------------------------------
# Message helpers
# ---------------------------------------------------------------------------

def split_message(text: str, limit: int = DISCORD_MSG_LIMIT) -> list[str]:
    """Split a long response into <=limit-char chunks on line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > limit and current:
            chunks.append("".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


async def send_with_retry(
    channel: "discord.abc.Messageable",
    content: str,
    retries: int = 3,
) -> None:
    """Send a message, retrying on transient Discord 5xx or network errors."""
    import aiohttp
    import discord as _discord
    delay = 1.0
    for attempt in range(retries):
        try:
            await channel.send(content)
            return
        except (_discord.errors.DiscordServerError, aiohttp.ClientConnectorError, OSError) as e:
            if attempt == retries - 1:
                raise
            log.warning(
                "Transient send error (%s), retrying in %.0fs (attempt %d/%d)",
                type(e).__name__, delay, attempt + 1, retries,
            )
            await asyncio.sleep(delay)
            delay *= 2


# ---------------------------------------------------------------------------
# Channel history
# ---------------------------------------------------------------------------

async def build_history(
    channel: "discord.abc.Messageable",
    before: "discord.Message",
    limit: int = 15,
    bot_user_id: int | None = None,
) -> list[dict]:
    """
    Return up to `limit` messages before `before` as Claude-formatted turns.

    Bot messages -> assistant role. All other messages -> user role.
    Consecutive same-role messages are merged; leading assistant turns dropped.

    Parameters
    ----------
    bot_user_id : if supplied, messages from this user ID are tagged as assistant.
                  If None, uses msg.author.bot flag.
    """
    raw: list[tuple[str, str]] = []
    async for msg in channel.history(limit=limit, before=before):
        if not msg.content:
            continue
        if bot_user_id is not None:
            role = "assistant" if msg.author.id == bot_user_id else "user"
        else:
            role = "assistant" if msg.author.bot else "user"
        raw.append((role, msg.content))
    raw.reverse()  # oldest first

    merged: list[dict] = []
    for role, content in raw:
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] += "\n" + content
        else:
            merged.append({"role": role, "content": content})

    while merged and merged[0]["role"] == "assistant":
        merged.pop(0)

    return merged


# ---------------------------------------------------------------------------
# Pending confirmation manager
# ---------------------------------------------------------------------------

class ConfirmationManager:
    """
    Stores pending destructive-action previews per channel.
    When the user replies with an affirmative word, the bot executes the
    confirmed action directly (bypassing Claude, which is unreliable here).
    """

    AFFIRMATIVES = {"yes", "y", "yep", "yeah", "yup", "confirm", "ok", "okay", "sure", "do it"}

    def __init__(self) -> None:
        # channel_id -> {"name": tool_name, "inputs": dict}
        self._pending: dict[int, dict] = {}

    def save(self, channel_id: int, tool_name: str, confirmed_inputs: dict) -> None:
        self._pending[channel_id] = {"name": tool_name, "inputs": confirmed_inputs}
        log.info("Pending confirmation saved for channel %s: %s", channel_id, tool_name)

    def consume(self, channel_id: int, text: str) -> dict | None:
        """
        If `text` is an affirmative and a confirmation is pending for this channel,
        remove and return it. Otherwise return None.
        """
        if text.lower().strip() not in self.AFFIRMATIVES:
            return None
        return self._pending.pop(channel_id, None)

    def clear(self, channel_id: int) -> None:
        self._pending.pop(channel_id, None)


# ---------------------------------------------------------------------------
# Webhook server
# ---------------------------------------------------------------------------

class WebhookServer:
    """
    Thin aiohttp wrapper. Bots add routes, then call start().

    Usage:
        webhook = WebhookServer(port=8765, secret="...")
        webhook.add_route("POST", "/notify", my_handler)
        await webhook.start()
    """

    def __init__(self, port: int = 8765, secret: str = "") -> None:
        self.port = port
        self.secret = secret
        self._routes: list[tuple[str, str, Any]] = []

    def add_route(self, method: str, path: str, handler: Callable) -> None:
        self._routes.append((method, path, handler))

    async def start(self) -> None:
        from aiohttp import web
        app = web.Application()
        for method, path, handler in self._routes:
            app.router.add_route(method, path, handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        log.info("Webhook server listening on 0.0.0.0:%d", self.port)

    def check_secret(self, received: str) -> bool:
        """Return True if no secret is configured, or if received matches."""
        if not self.secret:
            return True
        return received == self.secret
