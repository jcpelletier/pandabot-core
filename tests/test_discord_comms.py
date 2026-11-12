from pandabot_core.discord_comms import split_message, ConfirmationManager


def test_split_message_short():
    chunks = split_message("hello", limit=1900)
    assert chunks == ["hello"]


def test_split_message_splits_on_newline():
    # Three 800-char lines: lines 1+2 fit in 1900 (1602 chars), line 3 spills to chunk 2
    line = "x" * 800
    text = f"{line}\n{line}\n{line}"
    chunks = split_message(text, limit=1900)
    assert len(chunks) == 2
    for chunk in chunks:
        assert len(chunk) <= 1900


def test_confirmation_manager_affirmative():
    cm = ConfirmationManager()
    cm.save(channel_id=1, actions=[{"name": "manage_files", "inputs": {"confirmed": True}}])
    result = cm.consume(channel_id=1, text="yes")
    assert result is not None
    assert result[0]["name"] == "manage_files"


def test_confirmation_manager_non_affirmative():
    cm = ConfirmationManager()
    cm.save(channel_id=1, actions=[{"name": "manage_files", "inputs": {"confirmed": True}}])
    result = cm.consume(channel_id=1, text="no thanks")
    assert result is None
    # pending should still be there
    result2 = cm.consume(channel_id=1, text="yes")
    assert result2 is not None


def test_confirmation_manager_consumed_once():
    cm = ConfirmationManager()
    cm.save(channel_id=1, actions=[{"name": "manage_files", "inputs": {"confirmed": True}}])
    cm.consume(channel_id=1, text="yes")
    result = cm.consume(channel_id=1, text="yes")
    assert result is None


def test_confirmation_manager_different_channels():
    cm = ConfirmationManager()
    cm.save(channel_id=1, actions=[{"name": "tool_a", "inputs": {}}])
    result = cm.consume(channel_id=2, text="yes")
    assert result is None


def test_confirmation_manager_peek_does_not_consume():
    cm = ConfirmationManager()
    cm.save(channel_id=1, actions=[{"name": "tool_a", "inputs": {"x": 1}}])
    peeked = cm.peek(channel_id=1)
    assert peeked is not None
    assert peeked[0]["name"] == "tool_a"
    # still consumable after peek
    result = cm.consume(channel_id=1, text="yes")
    assert result is not None


def test_confirmation_manager_peek_missing():
    cm = ConfirmationManager()
    assert cm.peek(channel_id=99) is None


def test_confirmation_manager_force_consume():
    cm = ConfirmationManager()
    cm.save(channel_id=1, actions=[{"name": "tool_a", "inputs": {}}])
    result = cm.force_consume(channel_id=1)
    assert result is not None
    assert result[0]["name"] == "tool_a"
    # gone after force_consume
    assert cm.force_consume(channel_id=1) is None


def test_confirmation_manager_force_consume_prevents_text_consume():
    cm = ConfirmationManager()
    cm.save(channel_id=1, actions=[{"name": "tool_a", "inputs": {}}])
    cm.force_consume(channel_id=1)
    # text "yes" should find nothing
    result = cm.consume(channel_id=1, text="yes")
    assert result is None


def test_confirmation_manager_batches_multiple_actions():
    # A single model turn previewing four file moves must not collapse to one.
    cm = ConfirmationManager()
    actions = [
        {"name": "manage_files", "inputs": {"source": f"f{i}.mkv", "confirmed": True}}
        for i in range(4)
    ]
    cm.save(channel_id=1, actions=actions)
    result = cm.consume(channel_id=1, text="yes")
    assert result is not None
    assert len(result) == 4
    assert [a["inputs"]["source"] for a in result] == ["f0.mkv", "f1.mkv", "f2.mkv", "f3.mkv"]


def test_confirmation_manager_save_replaces_prior_batch():
    # A newer preview round fully replaces an older, unconfirmed one rather
    # than appending to it — avoids resurrecting stale actions from an
    # unrelated earlier turn when the user later says "yes".
    cm = ConfirmationManager()
    cm.save(channel_id=1, actions=[{"name": "mkdir", "inputs": {}}])
    cm.save(channel_id=1, actions=[{"name": "move", "inputs": {}}, {"name": "move", "inputs": {}}])
    result = cm.consume(channel_id=1, text="yes")
    assert result is not None
    assert len(result) == 2
    assert all(a["name"] == "move" for a in result)


def _run_announce(tmp_path, entry, channel, monkeypatch):
    import asyncio
    from pandabot_core import discord_comms

    (tmp_path / "VERSION").write_text("7\n")
    (tmp_path / "CHANGELOG.md").write_text(f"# Changelog\n\n## v7\n\n{entry}\n\n## v6\n\n- old\n")

    async def fake_send(ch, content, retries=3):
        await ch.send(content)

    monkeypatch.setattr(discord_comms, "send_with_retry", fake_send)
    asyncio.run(discord_comms.announce_startup(channel, str(tmp_path), extra="Backend: x"))


class _Channel:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send(self, content):
        if self.fail:
            raise RuntimeError("400 Bad Request")
        assert len(content) <= 2000
        self.sent.append(content)


def test_announce_startup_splits_long_changelog(tmp_path, monkeypatch):
    entry = "\n".join(f"- item {i}: " + "x" * 300 for i in range(10))
    ch = _Channel()
    _run_announce(tmp_path, entry, ch, monkeypatch)
    assert len(ch.sent) >= 2
    assert "Backend: x" in ch.sent[-1]


def test_announce_startup_cuts_a_single_overlong_line(tmp_path, monkeypatch):
    ch = _Channel()
    _run_announce(tmp_path, "- " + "y" * 4500, ch, monkeypatch)
    assert len(ch.sent) >= 3


def test_announce_startup_never_raises(tmp_path, monkeypatch):
    _run_announce(tmp_path, "- short", _Channel(fail=True), monkeypatch)


def test_announce_startup_uses_a_given_model_label(tmp_path, monkeypatch):
    import asyncio
    from pandabot_core import discord_comms
    from pandabot_core.llm import provider

    (tmp_path / "VERSION").write_text("7\n")
    sent = []

    async def fake_send(ch, content, retries=3):
        sent.append(content)

    monkeypatch.setattr(discord_comms, "send_with_retry", fake_send)
    monkeypatch.setattr(provider, "get_active_model_label", lambda: "claude-haiku-4-5")
    asyncio.run(discord_comms.announce_startup(object(), str(tmp_path), model_label="deepseek-v4-flash"))
    assert "powered by deepseek-v4-flash" in sent[0] and "haiku" not in sent[0]
    sent.clear()
    asyncio.run(discord_comms.announce_startup(object(), str(tmp_path)))
    assert "powered by claude-haiku-4-5" in sent[0]
