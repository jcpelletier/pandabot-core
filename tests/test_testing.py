"""
Tests for pandabot_core.testing — the shared test harness.

Covers FakeProvider, FakeChannel, make_message, and stub_discord.
"""

import asyncio
import pytest

from pandabot_core.testing import (
    FakeChannel,
    FakeProvider,
    FakeMessage,
    ScriptedResponse,
    make_message,
    stub_discord,
)
from pandabot_core.llm.provider import NormalizedResponse


# ---------------------------------------------------------------------------
# FakeProvider
# ---------------------------------------------------------------------------

class TestFakeProvider:

    def test_default_returns_end_turn_text(self):
        fp = FakeProvider()
        resp = fp.complete("sys", [{"role": "user", "content": "hi"}], [], "m")
        assert resp.stop_reason == "end_turn"
        assert resp.content[0].type == "text"
        assert resp.content[0].text == "ok"

    def test_scripted_text_response(self):
        fp = FakeProvider([ScriptedResponse(text="hello world")])
        resp = fp.complete("sys", [], [], "m")
        assert resp.content[0].text == "hello world"

    def test_scripted_tool_use_response(self):
        fp = FakeProvider([
            ScriptedResponse(tool_calls=[{"name": "ping", "input": {"host": "panda"}}]),
        ])
        resp = fp.complete("sys", [], [], "m")
        assert resp.stop_reason == "tool_use"
        assert resp.content[0].type == "tool_use"
        assert resp.content[0].name == "ping"
        assert resp.content[0].input == {"host": "panda"}

    def test_tool_use_block_gets_fake_id(self):
        fp = FakeProvider([ScriptedResponse(tool_calls=[{"name": "x", "input": {}}])])
        resp = fp.complete("sys", [], [], "m")
        assert resp.content[0].id is not None
        assert resp.content[0].id.startswith("fake_")

    def test_sequence_replays_in_order(self):
        fp = FakeProvider([
            ScriptedResponse(tool_calls=[{"name": "t", "input": {}}]),
            ScriptedResponse(text="done"),
        ])
        r1 = fp.complete("sys", [], [], "m")
        r2 = fp.complete("sys", [], [], "m")
        assert r1.stop_reason == "tool_use"
        assert r2.stop_reason == "end_turn"
        assert r2.content[0].text == "done"

    def test_exhausted_script_repeats_last_entry(self):
        fp = FakeProvider([ScriptedResponse(text="final")])
        fp.complete("sys", [], [], "m")   # consume the one entry
        resp = fp.complete("sys", [], [], "m")  # should repeat
        assert resp.content[0].text == "final"

    def test_records_all_calls(self):
        fp = FakeProvider([ScriptedResponse(text="a"), ScriptedResponse(text="b")])
        fp.complete("sys1", [{"role": "user", "content": "x"}], [], "m1")
        fp.complete("sys2", [{"role": "user", "content": "y"}], [], "m2")
        assert len(fp.calls) == 2
        assert fp.calls[0]["system"] == "sys1"
        assert fp.calls[1]["model"] == "m2"

    def test_complete_simple_returns_configured_text(self):
        fp = FakeProvider()
        fp.simple_response = "summary text"
        text, in_tok, out_tok = fp.complete_simple([{"role": "user", "content": "x"}], "m")
        assert text == "summary text"
        assert isinstance(in_tok, int)
        assert isinstance(out_tok, int)

    def test_complete_simple_records_calls(self):
        fp = FakeProvider()
        fp.complete_simple([{"role": "user", "content": "q"}], "model-x")
        assert len(fp.simple_calls) == 1
        assert fp.simple_calls[0]["model"] == "model-x"

    def test_returning_constructor(self):
        fp = FakeProvider.returning("Server is fine.")
        resp = fp.complete("sys", [], [], "m")
        assert resp.content[0].text == "Server is fine."

    def test_calling_then_replying_constructor(self):
        fp = FakeProvider.calling_then_replying("get_status", {}, "All good.")
        r1 = fp.complete("sys", [], [], "m")
        r2 = fp.complete("sys", [], [], "m")
        assert r1.stop_reason == "tool_use"
        assert r1.content[0].name == "get_status"
        assert r2.stop_reason == "end_turn"
        assert r2.content[0].text == "All good."

    def test_format_tool_definitions_passthrough(self):
        fp = FakeProvider()
        defs = [{"name": "tool_a", "description": "desc"}]
        assert fp.format_tool_definitions(defs) is defs

    def test_token_counts_in_response(self):
        fp = FakeProvider([ScriptedResponse(text="hi", input_tokens=42, output_tokens=7)])
        resp = fp.complete("sys", [], [], "m")
        assert resp.input_tokens == 42
        assert resp.output_tokens == 7

    def test_response_is_normalized_response(self):
        fp = FakeProvider()
        resp = fp.complete("sys", [], [], "m")
        assert isinstance(resp, NormalizedResponse)


# ---------------------------------------------------------------------------
# FakeChannel
# ---------------------------------------------------------------------------

class TestFakeChannel:

    @pytest.mark.asyncio
    async def test_send_records_message(self):
        ch = FakeChannel()
        await ch.send("hello")
        assert ch.messages == ["hello"]

    @pytest.mark.asyncio
    async def test_send_multiple_messages(self):
        ch = FakeChannel()
        await ch.send("first")
        await ch.send("second")
        assert ch.messages == ["first", "second"]

    def test_last_message_none_when_empty(self):
        ch = FakeChannel()
        assert ch.last_message is None

    @pytest.mark.asyncio
    async def test_last_message_returns_most_recent(self):
        ch = FakeChannel()
        await ch.send("a")
        await ch.send("b")
        assert ch.last_message == "b"

    def test_channel_id(self):
        ch = FakeChannel(channel_id=777)
        assert ch.id == 777

    @pytest.mark.asyncio
    async def test_state_stub_does_not_raise(self):
        """keep_typing() calls channel._state.http.send_typing — must not crash."""
        ch = FakeChannel()
        await ch._state.http.send_typing(ch.id)

    @pytest.mark.asyncio
    async def test_default_history_is_empty(self):
        ch = FakeChannel()
        msgs = [m async for m in ch.history(limit=10)]
        assert msgs == []

    @pytest.mark.asyncio
    async def test_inject_history_provides_messages(self):
        ch = FakeChannel()
        ch.inject_history([
            {"role": "user", "content": "what's up?"},
            {"role": "bot",  "content": "Not much."},
        ])
        msgs = [m async for m in ch.history(limit=10)]
        assert len(msgs) == 2
        # inject_history reverses so oldest comes last in iteration
        assert msgs[0].content == "Not much."
        assert msgs[1].content == "what's up?"

    @pytest.mark.asyncio
    async def test_injected_history_bot_flag(self):
        ch = FakeChannel()
        ch.inject_history([{"role": "bot", "content": "hi"}])
        msgs = [m async for m in ch.history()]
        assert msgs[0].author.bot is True

    @pytest.mark.asyncio
    async def test_injected_history_user_flag(self):
        ch = FakeChannel()
        ch.inject_history([{"role": "user", "content": "hi"}])
        msgs = [m async for m in ch.history()]
        assert msgs[0].author.bot is False


# ---------------------------------------------------------------------------
# make_message / FakeMessage
# ---------------------------------------------------------------------------

class TestMakeMessage:

    def test_basic_content(self):
        msg = make_message("hello")
        assert msg.content == "hello"

    def test_creates_channel_if_not_supplied(self):
        msg = make_message("hi")
        assert isinstance(msg.channel, FakeChannel)

    def test_uses_provided_channel(self):
        ch = FakeChannel(channel_id=42)
        msg = make_message("hi", channel=ch)
        assert msg.channel.id == 42

    def test_author_not_bot_by_default(self):
        msg = make_message("hi")
        assert msg.author.bot is False

    def test_author_bot_flag(self):
        msg = make_message("hi", author_bot=True)
        assert msg.author.bot is True

    def test_author_id(self):
        msg = make_message("hi", author_id=99999)
        assert msg.author.id == 99999

    def test_guild_none_by_default(self):
        msg = make_message("hi")
        assert msg.guild is None


# ---------------------------------------------------------------------------
# stub_discord
# ---------------------------------------------------------------------------

class TestStubDiscord:

    def test_stub_discord_adds_to_sys_modules(self):
        import sys
        # Remove stubs if already present to start fresh
        for mod in ["discord", "aiohttp", "anthropic"]:
            sys.modules.pop(mod, None)
        stub_discord()
        assert "discord" in sys.modules
        assert "aiohttp" in sys.modules
        assert "anthropic" in sys.modules

    def test_stub_discord_is_idempotent(self):
        """Calling twice should not raise or replace an already-present real module."""
        import sys
        from unittest.mock import MagicMock
        sentinel = MagicMock()
        sys.modules["discord"] = sentinel
        stub_discord()
        assert sys.modules["discord"] is sentinel  # not replaced


# ---------------------------------------------------------------------------
# Integration: FakeProvider + FakeChannel drive run_claude_loop
# ---------------------------------------------------------------------------

class TestFakeProviderWithLoop:
    """Verify the fakes work end-to-end with run_claude_loop from pandabot_core."""

    def test_simple_text_reply(self, monkeypatch):
        from pandabot_core.llm import provider as prov_mod
        from pandabot_core.llm.loop import run_claude_loop

        fake = FakeProvider.returning("Panda is healthy.")
        monkeypatch.setattr(prov_mod, "_provider", fake)

        result = run_claude_loop(
            user_message="How is the server?",
            history=None,
            tool_definitions=[],
            execute_tool=lambda name, args: "ok",
            system_prompt="You are a helpful bot.",
        )
        assert result == "Panda is healthy."
        assert len(fake.calls) == 1

    def test_one_tool_call_then_reply(self, monkeypatch):
        from pandabot_core.llm import provider as prov_mod
        from pandabot_core.llm.loop import run_claude_loop

        fake = FakeProvider.calling_then_replying("ping", {}, "Ping returned pong.")
        monkeypatch.setattr(prov_mod, "_provider", fake)

        tool_calls_seen = []

        def fake_execute(name, args):
            tool_calls_seen.append(name)
            return "pong"

        result = run_claude_loop(
            user_message="Ping the server.",
            history=None,
            tool_definitions=[{
                "name": "ping",
                "description": "ping",
                "input_schema": {"type": "object", "properties": {}},
            }],
            execute_tool=fake_execute,
            system_prompt="You are a bot.",
        )
        assert result == "Ping returned pong."
        assert tool_calls_seen == ["ping"]
        assert len(fake.calls) == 2  # tool_use round + end_turn round
