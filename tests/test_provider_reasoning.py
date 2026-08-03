"""Thinking-mode (reasoning_effort) plumbing on the OpenAI-compat provider.

Constructed with object.__new__ so the tests never import the openai SDK — the real
__init__ builds a client. Only the request-field construction is under test here.
"""

import pytest

from pandabot_core.llm import provider as prov


def _provider(effort: str = "") -> prov.OpenAICompatProvider:
    p = object.__new__(prov.OpenAICompatProvider)
    p.reasoning_effort = effort
    return p


class TestThinkingKwargs:
    def test_unset_sends_nothing(self):
        """Non-DeepSeek backends (llama.cpp, Ollama) reject the thinking field."""
        assert _provider()._thinking_kwargs(None) == {}

    @pytest.mark.parametrize("effort", ["low", "high", "max"])
    def test_enabled_efforts(self, effort):
        assert _provider()._thinking_kwargs(effort) == {
            "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": effort},
        }

    def test_none_disables_thinking(self):
        assert _provider()._thinking_kwargs("none") == {
            "extra_body": {"thinking": {"type": "disabled"}},
        }

    def test_unknown_effort_is_ignored_not_raised(self):
        """A typo must not take a bot down mid-conversation."""
        assert _provider()._thinking_kwargs("maximum") == {}

    def test_instance_default_applies_when_no_override(self):
        assert _provider("max")._thinking_kwargs(None)["extra_body"]["reasoning_effort"] == "max"

    def test_per_call_override_beats_instance_default(self):
        assert _provider("max")._thinking_kwargs("low")["extra_body"]["reasoning_effort"] == "low"

    def test_per_call_none_beats_enabled_default(self):
        assert _provider("max")._thinking_kwargs("none") == {
            "extra_body": {"thinking": {"type": "disabled"}},
        }

    def test_case_and_whitespace_tolerated(self):
        assert _provider()._thinking_kwargs("  MAX ")["extra_body"]["reasoning_effort"] == "max"


class TestProfileEffort:
    def test_named_profile_reads_effort(self, monkeypatch):
        monkeypatch.setenv("PANDABOT_PROFILE_DEEPSEEK_TYPE", "openai_compat")
        monkeypatch.setenv("PANDABOT_PROFILE_DEEPSEEK_PRIMARY", "deepseek-v4-flash")
        monkeypatch.setenv("PANDABOT_PROFILE_DEEPSEEK_EFFORT", "low")
        assert prov._load_profiles()["deepseek"].reasoning_effort == "low"

    def test_named_profile_effort_defaults_empty(self, monkeypatch):
        monkeypatch.setenv("PANDABOT_PROFILE_GEMMA_TYPE", "openai_compat")
        monkeypatch.setenv("PANDABOT_PROFILE_GEMMA_PRIMARY", "gemma-4-2b-it")
        monkeypatch.delenv("PANDABOT_PROFILE_GEMMA_EFFORT", raising=False)
        assert prov._load_profiles()["gemma"].reasoning_effort == ""

    def test_legacy_env_vars_read_effort(self, monkeypatch):
        for key in list(prov.os.environ):
            if key.startswith("PANDABOT_PROFILE_"):
                monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("LLM_PROVIDER", "openai_compat")
        monkeypatch.setenv("OPENAI_COMPAT_REASONING_EFFORT", "max")
        assert prov._load_profiles()["default"].reasoning_effort == "max"

    def test_anthropic_profile_has_no_effort(self, monkeypatch):
        for key in list(prov.os.environ):
            if key.startswith("PANDABOT_PROFILE_"):
                monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        assert prov._load_profiles()["default"].reasoning_effort == ""
