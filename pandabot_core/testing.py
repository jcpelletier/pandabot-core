"""
pandabot_core.testing
~~~~~~~~~~~~~~~~~~~~~
Reusable pytest fixtures and fakes for unit-testing bots that depend on
pandabot_core.  Import from here instead of re-implementing in each bot.

Typical usage in a bot's conftest.py or test file::

    from pandabot_core.testing import FakeProvider, FakeChannel, make_message

Public API
----------
FakeProvider        — scripted LLM provider; drives run_claude_loop without hitting the API
FakeChannel         — async Discord channel that records sent messages
make_message        — factory for lightweight discord.Message-shaped objects
stub_discord        — call once at import time to stub discord/aiohttp in sys.modules
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

from pandabot_core.llm.provider import ContentBlock, NormalizedResponse

__all__ = [
    "FakeProvider",
    "FakeChannel",
    "FakeMessage",
    "make_message",
    "stub_discord",
]


# ---------------------------------------------------------------------------
# Module stubbing helper
# ---------------------------------------------------------------------------

def stub_discord() -> None:
    """
    Insert MagicMock stubs for discord, aiohttp, and anthropic into sys.modules
    so that bot.py (and any module that imports them at the top level) can be
    imported in a test environment without installing the real packages.

    Call this at the top of a bot's conftest.py, before any bot imports::

        from pandabot_core.testing import stub_discord
        stub_discord()
    """
    _STUBS = [
        "discord", "discord.ext", "discord.ext.commands", "discord.opus",
        "aiohttp", "aiohttp.web",
        "anthropic",
    ]
    for mod in _STUBS:
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock()


# ---------------------------------------------------------------------------
# FakeProvider
# ---------------------------------------------------------------------------

@dataclass
class ScriptedResponse:
    """One scripted turn in a FakeProvider sequence.

    Parameters
    ----------
    text        : reply text (stop_reason="end_turn").  Pass None when the
                  response is tool_use only.
    tool_calls  : list of dicts with keys "name" and "input".  When non-empty,
                  stop_reason is "tool_use" and a tool_use ContentBlock is
                  added for each entry.
    model       : model name reported in the NormalizedResponse.
    input_tokens / output_tokens : fake usage numbers for cost tracking tests.
    """
    text: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    model: str = "fake-model"
    input_tokens: int = 10
    output_tokens: int = 5


class FakeProvider:
    """Drop-in replacement for AnthropicProvider / OpenAICompatProvider.

    Accepts a scripted list of responses and replays them in order each time
    ``complete()`` is called.  When the script is exhausted it loops back to
    the last entry so tests don't need to predict exact call counts.

    ``complete_simple()`` always returns ``(simple_response, 5, 3)`` unless
    overridden by setting ``fake.simple_response``.

    Usage::

        fake = FakeProvider([
            ScriptedResponse(tool_calls=[{"name": "get_status", "input": {}}]),
            ScriptedResponse(text="The server is healthy."),
        ])
        monkeypatch.setattr(llm_provider, "_provider", fake)

        # After driving run_claude_loop:
        assert len(fake.calls) == 2
        assert fake.calls[0]["messages"][-1]["role"] == "user"

    Attributes
    ----------
    calls       : list of dicts recorded for each ``complete()`` call.
                  Each dict has: ``messages``, ``system``, ``model``.
    simple_calls: list of dicts recorded for each ``complete_simple()`` call.
    simple_response : text returned by ``complete_simple()`` (default "ok").
    """

    primary_model: str = "fake-model"
    upgrade_model: str = ""

    def __init__(self, script: list[ScriptedResponse] | None = None) -> None:
        # Default: single end_turn text response
        self._script: list[ScriptedResponse] = script or [ScriptedResponse(text="ok")]
        self._index: int = 0
        self.calls: list[dict] = []
        self.simple_calls: list[dict] = []
        self.simple_response: str = "ok"

    # --- Provider interface -------------------------------------------------

    def format_tool_definitions(self, tool_defs: list[dict]) -> list[dict]:
        """Pass-through — tests use canonical tool defs directly."""
        return tool_defs

    def complete(
        self,
        system_prompt: str,
        messages: list[dict],
        formatted_tools: list[dict],
        model: str,
        max_tokens: int = 4096,
    ) -> NormalizedResponse:
        scripted = self._next()
        self.calls.append({
            "system": system_prompt,
            "messages": messages,
            "model": model,
        })
        return self._to_response(scripted)

    def complete_simple(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int = 800,
    ) -> tuple[str, int, int]:
        self.simple_calls.append({"messages": messages, "model": model})
        return (self.simple_response, 5, 3)

    # --- Helpers ------------------------------------------------------------

    def _next(self) -> ScriptedResponse:
        entry = self._script[self._index]
        # Advance, but clamp at the last entry so exhausted scripts don't crash
        self._index = min(self._index + 1, len(self._script) - 1)
        return entry

    @staticmethod
    def _to_response(scripted: ScriptedResponse) -> NormalizedResponse:
        content: list[ContentBlock] = []
        if scripted.text is not None:
            content.append(ContentBlock(type="text", text=scripted.text))
        for tc in scripted.tool_calls:
            content.append(ContentBlock(
                type="tool_use",
                id=f"fake_{uuid.uuid4().hex[:8]}",
                name=tc["name"],
                input=tc.get("input", {}),
            ))
        stop_reason = "tool_use" if scripted.tool_calls else "end_turn"
        return NormalizedResponse(
            stop_reason=stop_reason,
            content=content,
            model=scripted.model,
            input_tokens=scripted.input_tokens,
            output_tokens=scripted.output_tokens,
        )

    # --- Convenience constructors -------------------------------------------

    @classmethod
    def returning(cls, text: str) -> "FakeProvider":
        """Shorthand for a provider that always replies with a single text."""
        return cls([ScriptedResponse(text=text)])

    @classmethod
    def calling_then_replying(
        cls,
        tool_name: str,
        tool_input: dict,
        reply_text: str,
    ) -> "FakeProvider":
        """Shorthand: one tool_use turn followed by one end_turn text reply."""
        return cls([
            ScriptedResponse(tool_calls=[{"name": tool_name, "input": tool_input}]),
            ScriptedResponse(text=reply_text),
        ])


# ---------------------------------------------------------------------------
# FakeChannel
# ---------------------------------------------------------------------------

class FakeChannel:
    """Minimal async Discord channel that records sent messages.

    Implements only what tests need: ``.send()``, ``.id``, and ``.messages``.
    ``send_with_retry`` and ``split_message`` work against this directly.

    Usage::

        channel = FakeChannel(channel_id=999)
        await channel.send("hello")
        assert channel.messages == ["hello"]

    Also provides a ``_state`` stub so ``keep_typing(channel)`` doesn't crash
    (it calls ``channel._state.http.send_typing``).
    """

    def __init__(self, channel_id: int = 1) -> None:
        self.id = channel_id
        self.messages: list[str] = []
        # Stub _state so keep_typing() doesn't raise AttributeError
        self._state = MagicMock()
        self._state.http.send_typing = AsyncMock()

    async def send(self, content: str, **kwargs) -> None:  # noqa: ARG002
        self.messages.append(content)

    async def history(self, limit: int = 15, before: Any = None):  # noqa: ARG002
        """Yield nothing by default — tests override with inject_history()."""
        return
        yield  # make this an async generator

    def inject_history(self, messages: list[dict]) -> None:
        """Replace the history generator with a fixed list.

        Each dict should have ``role`` ("user" or "bot") and ``content`` keys::

            channel.inject_history([
                {"role": "user", "content": "what time is it?"},
                {"role": "bot",  "content": "It is 3pm."},
            ])
        """
        fake_msgs = [_FakeHistoryMessage(m) for m in reversed(messages)]

        async def _gen(limit=15, before=None):  # noqa: ARG001
            for msg in fake_msgs:
                yield msg

        self.history = _gen

    @property
    def last_message(self) -> str | None:
        """Convenience: the most recent message sent to this channel."""
        return self.messages[-1] if self.messages else None


class _FakeHistoryMessage:
    """Minimal discord.Message shape for build_history()."""
    def __init__(self, spec: dict) -> None:
        self.content = spec["content"]
        self.author = MagicMock()
        self.author.bot = spec.get("role") == "bot"
        self.author.id = spec.get("author_id", 0)


# Async mock helper (available in Python 3.8+, but define locally for clarity)
try:
    from unittest.mock import AsyncMock
except ImportError:  # pragma: no cover
    class AsyncMock(MagicMock):  # type: ignore[no-redef]
        async def __call__(self, *args, **kwargs):
            return super().__call__(*args, **kwargs)


# ---------------------------------------------------------------------------
# make_message / FakeMessage
# ---------------------------------------------------------------------------

@dataclass
class FakeMessage:
    """Lightweight stand-in for discord.Message.

    Contains the fields that bot.py's ``on_message`` handler and
    ``build_history`` actually access.

    Parameters
    ----------
    content     : raw message text
    channel     : FakeChannel (created automatically if not supplied)
    author_bot  : whether the author is a bot (affects history role)
    author_id   : discord user/bot ID
    guild       : fake guild object (None means DM)
    """
    content: str
    channel: FakeChannel = field(default_factory=FakeChannel)
    author_bot: bool = False
    author_id: int = 12345
    guild: Any = None

    def __post_init__(self) -> None:
        self.author = MagicMock()
        self.author.bot = self.author_bot
        self.author.id = self.author_id


def make_message(
    content: str,
    channel: FakeChannel | None = None,
    author_bot: bool = False,
    author_id: int = 12345,
    guild: Any = None,
) -> FakeMessage:
    """Factory for FakeMessage — preferred over direct instantiation.

    Creates a new FakeChannel if none is provided, so the returned message's
    channel is always a usable FakeChannel::

        msg = make_message("hello")
        assert isinstance(msg.channel, FakeChannel)
    """
    return FakeMessage(
        content=content,
        channel=channel or FakeChannel(),
        author_bot=author_bot,
        author_id=author_id,
        guild=guild,
    )
