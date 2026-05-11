import os
from pandabot_core import identity


def test_bot_name_default():
    os.environ.pop("BOT_NAME", None)
    assert identity.bot_name() == "PandaBot"


def test_bot_name_custom():
    os.environ["BOT_NAME"] = "MyBot"
    assert identity.bot_name() == "MyBot"
    del os.environ["BOT_NAME"]


def test_startup_message_with_version():
    os.environ["BOT_NAME"] = "Panda"
    os.environ["BOT_EMOJI"] = "X"
    msg = identity.startup_message(version=42)
    assert "Panda" in msg
    assert "v42" in msg
    del os.environ["BOT_NAME"]
    del os.environ["BOT_EMOJI"]


def test_system_prompt_contains_name():
    os.environ["BOT_NAME"] = "TestBot"
    prompt = identity.build_system_prompt()
    assert "TestBot" in prompt
    del os.environ["BOT_NAME"]


def test_system_prompt_server_description_override():
    os.environ["SERVER_DESCRIPTION"] = "Runs only FooService"
    prompt = identity.build_system_prompt()
    assert "FooService" in prompt
    assert "Jellyfin" not in prompt
    del os.environ["SERVER_DESCRIPTION"]


def test_system_prompt_extra_sections():
    prompt = identity.build_system_prompt(extra_sections=["MY_CUSTOM_SECTION"])
    assert "MY_CUSTOM_SECTION" in prompt
