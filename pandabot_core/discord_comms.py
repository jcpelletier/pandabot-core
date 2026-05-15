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
import datetime
import logging
import os
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    import discord

log = logging.getLogger("pandabot.discord_comms")

__all__ = [
    "keep_typing",
    "split_message",
    "send_with_retry",
    "build_history",
    "announce_startup",
    "ConfirmationManager",
    "make_confirmation_view",
    "WebhookServer",
    "model_switch_banner",
    "make_model_switch_cog",
    "make_help_cog",
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
# Startup announcement
# ---------------------------------------------------------------------------

def _read_version(bot_dir: str) -> int | str:
    path = os.path.join(bot_dir, "VERSION")
    try:
        return int(open(path).read().strip())
    except (FileNotFoundError, ValueError):
        return ""


def _read_changelog_entry(bot_dir: str, version: int | str) -> str:
    """Return bullet lines for *version* from CHANGELOG.md in bot_dir, or ''."""
    if not version:
        return ""
    path = os.path.join(bot_dir, "CHANGELOG.md")
    try:
        lines = open(path).readlines()
    except FileNotFoundError:
        return ""
    collecting = False
    out: list[str] = []
    for line in lines:
        if line.startswith(f"## v{version}"):
            collecting = True
            continue
        if collecting:
            if line.startswith("## "):
                break
            stripped = line.rstrip()
            if stripped.startswith("- "):
                stripped = "• " + stripped[2:]
            if stripped:
                out.append(stripped)
    return "\n".join(out)


async def announce_startup(
    channel: "discord.abc.Messageable",
    bot_dir: str,
    extra: str = "",
) -> None:
    """Post startup message (version + changelog entry + extra) to channel."""
    from pandabot_core.identity import startup_message
    from pandabot_core.llm.provider import get_active_model_label
    version = _read_version(bot_dir)
    msg = startup_message(version)
    label = get_active_model_label()
    if label:
        msg += f" — powered by {label}"
    entry = _read_changelog_entry(bot_dir, version)
    if entry:
        msg += f"\n{entry}"
    if extra:
        msg += f"\n{extra}"
    await send_with_retry(channel, msg)


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

    def peek(self, channel_id: int) -> dict | None:
        """Return the pending confirmation for this channel without consuming it, or None."""
        return self._pending.get(channel_id)

    def force_consume(self, channel_id: int) -> dict | None:
        """Remove and return the pending confirmation without requiring an affirmative word."""
        return self._pending.pop(channel_id, None)

    def clear(self, channel_id: int) -> None:
        self._pending.pop(channel_id, None)


# ---------------------------------------------------------------------------
# Interactive confirmation view
# ---------------------------------------------------------------------------

def make_confirmation_view(
    execute: "Callable[[], Any]",
    on_cancel: "Callable[[], None] | None" = None,
    timeout: float = 300.0,
) -> "Any":
    """
    Return a discord.ui.View with green Confirm and red Cancel buttons.

    Parameters
    ----------
    execute   : async callable () -> str   — called when Confirm is clicked;
                its return value is sent as a follow-up message.
    on_cancel : sync callable () -> None  — called when Cancel is clicked
                (e.g. to clear a ConfirmationManager entry). Optional.
    timeout   : seconds before the buttons auto-disable (default 5 minutes).

    Usage
    -----
        view = make_confirmation_view(execute=my_coroutine, on_cancel=clear_fn)
        msg = await channel.send(view=view)
        view.message = msg   # lets on_timeout disable buttons via message.edit
    """
    import discord as _discord

    class _ConfirmationView(_discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=timeout)
            self.message: "_discord.Message | None" = None

        def _disable_all(self) -> None:
            for child in self.children:
                child.disabled = True  # type: ignore[union-attr]

        @_discord.ui.button(label="Confirm", style=_discord.ButtonStyle.green, emoji="✅")
        async def confirm_button(
            self,
            interaction: "_discord.Interaction",
            button: "_discord.ui.Button",
        ) -> None:
            self._disable_all()
            await interaction.response.edit_message(view=self)
            self.stop()
            try:
                result = await execute()
            except Exception as exc:
                result = f"❌ Error: {exc}"
            for chunk in split_message(str(result)):
                await interaction.followup.send(chunk)

        @_discord.ui.button(label="Cancel", style=_discord.ButtonStyle.red, emoji="✖️")
        async def cancel_button(
            self,
            interaction: "_discord.Interaction",
            button: "_discord.ui.Button",
        ) -> None:
            if on_cancel is not None:
                on_cancel()
            self._disable_all()
            await interaction.response.edit_message(content="❌ Cancelled.", view=self)
            self.stop()

        async def on_timeout(self) -> None:
            self._disable_all()
            if self.message is not None:
                try:
                    await self.message.edit(view=self)
                except Exception:
                    pass

    return _ConfirmationView()


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


# ---------------------------------------------------------------------------
# LLM model switching — !commands + confirmation banner
# ---------------------------------------------------------------------------

def model_switch_banner(profile: str, model: str) -> str:
    """Return the fixed-format confirmation block shown after a successful model switch.

    The code-block format with the exact configured model ID and a current timestamp
    is unlikely to be reproduced by a hallucinating LLM, making it easy to distinguish
    a real switch from a model that only claims to have switched.
    """
    now = datetime.datetime.now().strftime("%H:%M:%S")
    return f"```\n[MODEL SWITCH]\nprofile : {profile}\nmodel   : {model}\ntime    : {now}\n```"


def make_model_switch_cog(aliases: dict[str, str] | None = None):
    """Create a discord.ext.commands.Cog that provides !model switching commands.

    Commands registered: !deepseek (alias !ds), !haiku (alias !claude),
    !gemma (alias !local), !model? (status).

    aliases maps user-facing command names to profile names. Defaults to
    {"deepseek": "deepseek", "haiku": "haiku", "gemma": "gemma"} but can be
    overridden via env vars (DEEPSEEK_PROFILE_NAME, HAIKU_PROFILE_NAME,
    LOCAL_LLM_PROFILE_NAME) or by passing an explicit dict.

    The cog is imported lazily so this module can be imported without discord
    installed (tests still work).
    """
    from discord.ext import commands as _commands
    from pandabot_core.llm.provider import (
        get_available_profiles, set_active_profile,
        get_active_profile_name, get_provider,
    )

    _aliases: dict[str, str] = aliases or {
        "deepseek": os.environ.get("DEEPSEEK_PROFILE_NAME", "deepseek"),
        "haiku":    os.environ.get("HAIKU_PROFILE_NAME",    "haiku"),
        "gemma":    os.environ.get("LOCAL_LLM_PROFILE_NAME", "gemma"),
    }

    class _ModelSwitchCog(_commands.Cog, name="model_switch"):

        async def _switch(self, ctx: _commands.Context, alias: str) -> None:
            target = _aliases.get(alias.lower().strip(), alias.lower().strip())
            available = get_available_profiles()
            if target not in available:
                await ctx.send(
                    f"Unknown profile `{alias}`. Available: {', '.join(available)}"
                )
                return
            set_active_profile(target)
            provider = get_provider()
            await ctx.send(model_switch_banner(target, provider.primary_model))

        @_commands.command(name="deepseek", aliases=["ds"])
        async def cmd_deepseek(self, ctx: _commands.Context) -> None:
            """Switch the LLM to DeepSeek."""
            await self._switch(ctx, "deepseek")

        @_commands.command(name="haiku", aliases=["claude"])
        async def cmd_haiku(self, ctx: _commands.Context) -> None:
            """Switch the LLM to Claude Haiku."""
            await self._switch(ctx, "haiku")

        @_commands.command(name="gemma", aliases=["local"])
        async def cmd_gemma(self, ctx: _commands.Context) -> None:
            """Switch the LLM to local Gemma."""
            await self._switch(ctx, "gemma")

        @_commands.command(name="model?")
        async def cmd_model(self, ctx: _commands.Context) -> None:
            """Show the currently active LLM profile and available options."""
            name = get_active_profile_name()
            provider = get_provider()
            available = get_available_profiles()
            await ctx.send(
                f"```\n[MODEL STATUS]\nprofile : {name}\n"
                f"model   : {provider.primary_model}\n"
                f"available: {', '.join(available)}\n```"
            )

    return _ModelSwitchCog()


def make_help_cog():
    """Create a discord.ext.commands.Cog that provides !commands (list all !commands).

    Introspects ctx.bot.commands at call time, so it automatically includes both
    core commands (model switching) and any bot-specific commands added by the caller.
    """
    from discord.ext import commands as _commands

    class _HelpCog(_commands.Cog, name="help"):

        @_commands.command(name="commands?", aliases=["help?"])
        async def cmd_commands(self, ctx: _commands.Context) -> None:
            """List all available !commands."""
            lines = []
            for cmd in sorted(ctx.bot.commands, key=lambda c: c.name):
                aliases = f"  (also: {', '.join('!' + a for a in sorted(cmd.aliases))})" if cmd.aliases else ""
                desc = cmd.help or cmd.brief or ""
                lines.append(f"!{cmd.name}{aliases}" + (f" — {desc}" if desc else ""))
            await ctx.send("```\n" + "\n".join(lines) + "\n```")

    return _HelpCog()
