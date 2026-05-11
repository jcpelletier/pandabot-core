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
    cm.save(channel_id=1, tool_name="manage_files", confirmed_inputs={"confirmed": True})
    result = cm.consume(channel_id=1, text="yes")
    assert result is not None
    assert result["name"] == "manage_files"


def test_confirmation_manager_non_affirmative():
    cm = ConfirmationManager()
    cm.save(channel_id=1, tool_name="manage_files", confirmed_inputs={"confirmed": True})
    result = cm.consume(channel_id=1, text="no thanks")
    assert result is None
    # pending should still be there
    result2 = cm.consume(channel_id=1, text="yes")
    assert result2 is not None


def test_confirmation_manager_consumed_once():
    cm = ConfirmationManager()
    cm.save(channel_id=1, tool_name="manage_files", confirmed_inputs={"confirmed": True})
    cm.consume(channel_id=1, text="yes")
    result = cm.consume(channel_id=1, text="yes")
    assert result is None


def test_confirmation_manager_different_channels():
    cm = ConfirmationManager()
    cm.save(channel_id=1, tool_name="tool_a", confirmed_inputs={})
    result = cm.consume(channel_id=2, text="yes")
    assert result is None
