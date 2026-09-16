"""Malformed / truncated tool-call arguments must not take a bot down.

Regression: PandaBot-Devops answered "Error processing request: Unterminated
string starting at: line 1 column 13 (char 12)" and dropped the request. The
model had hit max_tokens part-way through a long shell command, leaving
`{"command": "` in the tool-call arguments, and complete() called json.loads on
it bare.

Built with object.__new__ and a fake client so nothing imports the openai SDK.
"""

import types

import pytest

from pandabot_core.llm import provider as prov
from pandabot_core.llm.loop import run_claude_loop


# --------------------------------------------------------------------------
# parse_tool_arguments
# --------------------------------------------------------------------------

class TestParseToolArguments:
    def test_valid_object(self):
        assert prov.parse_tool_arguments('{"command": "uptime"}', "run") == ({"command": "uptime"}, None)

    def test_empty_means_no_arguments(self):
        assert prov.parse_tool_arguments("", "run") == ({}, None)
        assert prov.parse_tool_arguments(None, "run") == ({}, None)

    def test_truncated_string_does_not_raise(self):
        args, error = prov.parse_tool_arguments('{"command": "', "run_command", "length")
        assert args == {}
        assert "max_tokens" in error
        assert "Nothing ran" in error

    def test_error_names_the_tool_and_suggests_a_retry(self):
        _, error = prov.parse_tool_arguments('{"command": "', "run_command", "length")
        assert "run_command" in error
        assert "shorter" in error

    def test_invalid_json_without_truncation(self):
        args, error = prov.parse_tool_arguments("not json at all", "run")
        assert args == {}
        assert "not valid JSON" in error

    def test_non_object_json_rejected(self):
        args, error = prov.parse_tool_arguments('["uptime"]', "run")
        assert args == {}
        assert "JSON object" in error


# --------------------------------------------------------------------------
# complete() - the path that crashed
# --------------------------------------------------------------------------

def _fake_choice(arguments, finish_reason="length", name="run_command"):
    tool_call = types.SimpleNamespace(
        id="call_1",
        function=types.SimpleNamespace(name=name, arguments=arguments),
    )
    return types.SimpleNamespace(
        finish_reason=finish_reason,
        message=types.SimpleNamespace(content=None, tool_calls=[tool_call]),
    )


def _openai_provider(choice):
    p = object.__new__(prov.OpenAICompatProvider)
    p.reasoning_effort = ""
    p.primary_model = "deepseek-v4-flash"
    p.upgrade_model = ""
    response = types.SimpleNamespace(
        choices=[choice],
        usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=20),
    )
    p.client = types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=lambda **kw: response),
        ),
    )
    return p


class TestCompleteWithTruncatedArgs:
    def _response(self, arguments, finish_reason="length"):
        p = _openai_provider(_fake_choice(arguments, finish_reason))
        return p.complete(
            system_prompt="sys",
            messages=[{"role": "user", "content": "reboot into the old kernel"}],
            formatted_tools=[],
            model="deepseek-v4-flash",
        )

    def test_truncated_arguments_no_longer_raise(self):
        resp = self._response('{"command": "')
        block = resp.content[0]
        assert block.type == "tool_use"
        assert block.input == {}
        assert block.error is not None

    def test_truncated_tool_call_is_still_a_tool_use_turn(self):
        """finish_reason is "length", not "tool_calls" - the loop still needs to
        answer the block so the model gets a chance to retry."""
        assert self._response('{"command": "').stop_reason == "tool_use"

    def test_well_formed_call_is_untouched(self):
        resp = self._response('{"command": "uptime"}', finish_reason="tool_calls")
        assert resp.content[0].input == {"command": "uptime"}
        assert resp.content[0].error is None
        assert resp.stop_reason == "tool_use"


# --------------------------------------------------------------------------
# loop integration
# --------------------------------------------------------------------------

class _ScriptedProvider:
    """Returns each queued NormalizedResponse in turn."""

    primary_model = "deepseek-v4-flash"
    upgrade_model = ""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.sent_messages = []

    def format_tool_definitions(self, tool_defs):
        return tool_defs

    def complete(self, **kwargs):
        self.calls += 1
        self.sent_messages = [dict(m) for m in kwargs["messages"]]
        return self._responses.pop(0)


def _truncated_response():
    return prov.NormalizedResponse(
        stop_reason="tool_use",
        content=[prov.ContentBlock(
            type="tool_use", id="call_1", name="run_command", input={},
            error="Tool call to run_command was discarded: the response hit the "
                  "max_tokens limit part-way through the arguments. Nothing ran. "
                  "Retry the call with shorter arguments.",
        )],
        model="deepseek-v4-flash",
        input_tokens=10,
        output_tokens=20,
    )


def _text_response(text):
    return prov.NormalizedResponse(
        stop_reason="end_turn",
        content=[prov.ContentBlock(type="text", text=text)],
        model="deepseek-v4-flash",
        input_tokens=5,
        output_tokens=5,
    )


@pytest.fixture
def scripted(monkeypatch):
    def _install(responses):
        p = _ScriptedProvider(responses)
        monkeypatch.setattr(prov, "get_provider", lambda: p)
        monkeypatch.setattr(prov, "get_provider_name", lambda: "openai_compat")
        return p
    return _install


class TestLoopHandlesUnusableArgs:
    def test_flagged_tool_is_not_executed(self, scripted):
        scripted([_truncated_response(), _text_response("Rebooted into 6.17.0-35.")])
        executed = []

        def _exec(name, args):
            executed.append((name, args))
            return "ran"

        reply = run_claude_loop("proceed as recommended", None, [], _exec, "sys")
        assert executed == []
        assert reply == "Rebooted into 6.17.0-35."

    def test_model_is_told_why_so_it_can_retry(self, scripted):
        p = scripted([_truncated_response(), _text_response("done")])
        run_claude_loop("proceed", None, [], lambda n, a: "ran", "sys")

        tool_result = p.sent_messages[-1]["content"][0]
        assert tool_result["type"] == "tool_result"
        assert tool_result["tool_use_id"] == "call_1"
        assert tool_result["is_error"] is True
        assert "max_tokens" in tool_result["content"]

    def test_repeated_truncation_gives_up_with_a_readable_message(self, scripted):
        p = scripted([_truncated_response(), _truncated_response()])
        reply = run_claude_loop("proceed", None, [], lambda n, a: "ran", "sys")

        assert p.calls == 2  # bailed out, did not spin to the 25-round limit
        assert "unusable" in reply
        assert "Unterminated" not in reply

    def test_a_good_round_clears_the_counter(self, scripted):
        good = prov.NormalizedResponse(
            stop_reason="tool_use",
            content=[prov.ContentBlock(type="tool_use", id="c2", name="run_command",
                                       input={"command": "uptime"})],
            model="deepseek-v4-flash", input_tokens=1, output_tokens=1,
        )
        scripted([_truncated_response(), good, _truncated_response(), _text_response("ok")])
        reply = run_claude_loop("proceed", None, [], lambda n, a: "ran", "sys")
        assert reply == "ok"
