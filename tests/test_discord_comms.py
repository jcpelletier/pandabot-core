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
