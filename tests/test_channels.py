import asyncio

import pandabot_core.discord_comms as dc
from pandabot_core.channels import (
    BotChannelMap,
    make_message_bot_tool,
    send_to_bot,
)


def test_from_env_parses_pairs(monkeypatch):
    monkeypatch.setenv("BOT_CHANNELS", "pandabot-dev:123, PandaBot-Devops:456 ,junk,bad:x")
    m = BotChannelMap.from_env()
    assert m.get("pandabot-dev") == 123
    assert m.get("PANDABOT-DEVOPS") == 456  # case-insensitive
    assert "bad" not in m  # non-numeric id dropped
    assert set(m.names()) == {"pandabot-dev", "pandabot-devops"}
    assert bool(m) is True


def test_empty_map_is_falsey():
    assert bool(BotChannelMap()) is False
    assert BotChannelMap().get("anything") is None


def test_make_tool_lists_targets():
    tool = make_message_bot_tool(BotChannelMap({"pandabot-dev": 1, "pandabot-qa": 2}))
    assert tool["name"] == "message_bot"
    assert tool["input_schema"]["required"] == ["target", "request"]
    enum = tool["input_schema"]["properties"]["target"]["enum"]
    assert set(enum) == {"pandabot-dev", "pandabot-qa"}


def test_make_tool_no_targets_omits_enum():
    tool = make_message_bot_tool(BotChannelMap())
    assert "enum" not in tool["input_schema"]["properties"]["target"]


def test_send_unknown_target_reports_known():
    m = BotChannelMap({"pandabot-dev": 1})
    out = asyncio.run(send_to_bot(client=object(), channel_map=m, target="nope", text="hi"))
    assert "Unknown target" in out
    assert "pandabot-dev" in out


def test_send_channel_not_visible():
    class _Client:
        def get_channel(self, cid):
            return None

    m = BotChannelMap({"pandabot-dev": 999})
    out = asyncio.run(send_to_bot(client=_Client(), channel_map=m, target="pandabot-dev", text="hi"))
    assert "not visible" in out


def test_send_happy_path_addresses_target(monkeypatch):
    sent: list[str] = []

    async def _fake_send(channel, content, retries=3):
        sent.append(content)

    monkeypatch.setattr(dc, "send_with_retry", _fake_send)

    class _Channel:
        name = "pandabot-dev"

    class _Client:
        def get_channel(self, cid):
            return _Channel()

    m = BotChannelMap({"pandabot-dev": 42})
    out = asyncio.run(
        send_to_bot(
            client=_Client(),
            channel_map=m,
            target="pandabot-dev",
            text="please add requests to requirements.txt",
            sender="PandabotDevops",
        )
    )
    assert "Delivered" in out
    assert len(sent) == 1
    # The posted message must name the target (so its addressing gate matches)
    # and attribute the sender.
    assert "pandabot-dev" in sent[0]
    assert "PandabotDevops" in sent[0]
    assert "requirements.txt" in sent[0]
