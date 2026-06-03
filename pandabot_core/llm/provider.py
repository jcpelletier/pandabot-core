"""LLM provider abstraction — Anthropic and OpenAI-compatible backends.

Providers expose three methods:
  format_tool_definitions(tool_defs)       -> formatted tool list (call once, cache result)
  complete(system, messages, tools, model) -> NormalizedResponse
  complete_simple(messages, model)         -> (text, input_tokens, output_tokens)

Tool definitions stay in canonical Anthropic format (input_schema) as the single
source of truth. The OpenAI-compat provider wraps them at format_tool_definitions time.
Messages are always in canonical Anthropic format; providers translate internally.

Model profiles
--------------
Multiple named profiles can be defined via env vars:

  PANDABOT_PROFILE_HAIKU_TYPE=anthropic
  PANDABOT_PROFILE_HAIKU_PRIMARY=claude-haiku-4-5
  PANDABOT_PROFILE_HAIKU_UPGRADE=claude-sonnet-4-6

  PANDABOT_PROFILE_DEEPSEEK_TYPE=openai_compat
  PANDABOT_PROFILE_DEEPSEEK_URL=https://api.deepseek.com/v1
  PANDABOT_PROFILE_DEEPSEEK_KEY=<key>
  PANDABOT_PROFILE_DEEPSEEK_PRIMARY=deepseek-chat
  PANDABOT_PROFILE_DEEPSEEK_UPGRADE=deepseek-reasoner

  PANDABOT_PROFILE_GEMMA_TYPE=openai_compat
  PANDABOT_PROFILE_GEMMA_URL=http://127.0.0.1:8081/v1
  PANDABOT_PROFILE_GEMMA_KEY=no-key
  PANDABOT_PROFILE_GEMMA_PRIMARY=gemma-4-2b-it

  PANDABOT_DEFAULT_PROFILE=haiku   # which profile is active at startup

The active profile is persisted to $PANDABOT_DATA_DIR/active_model.txt so it
survives bot restarts. set_active_profile() / get_active_profile_name() are the
public API for runtime switching.

Backward compatibility: if no PANDABOT_PROFILE_*_TYPE vars are set, a single
"default" profile is synthesised from the old LLM_PROVIDER / OPENAI_COMPAT_*
env vars so existing deployments keep working without changes.
"""

import json
import logging
import os
import re as _re
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("panda-bot")


@dataclass
class ContentBlock:
    type: str           # "text", "tool_use", or "thinking"
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict | None = None
    thinking: str | None = None
    signature: str | None = None


@dataclass
class NormalizedResponse:
    stop_reason: str        # "end_turn" or "tool_use"
    content: list[ContentBlock]
    model: str
    input_tokens: int
    output_tokens: int


# ---------------------------------------------------------------------------
# Anthropic provider
# ---------------------------------------------------------------------------

class AnthropicProvider:
    def __init__(
        self,
        primary_model: str | None = None,
        upgrade_model: str | None = None,
    ) -> None:
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.primary_model = primary_model or os.environ.get("ANTHROPIC_PRIMARY_MODEL", "claude-haiku-4-5")
        self.upgrade_model = upgrade_model or os.environ.get("ANTHROPIC_UPGRADE_MODEL", "claude-sonnet-4-5")

    def format_tool_definitions(self, tool_defs: list[dict]) -> list[dict]:
        return tool_defs  # Anthropic format is the canonical source of truth

    def complete(
        self,
        system_prompt: str,
        messages: list[dict],
        formatted_tools: list[dict],
        model: str,
        max_tokens: int = 4096,
    ) -> NormalizedResponse:
        response = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            tools=formatted_tools,
            messages=messages,
        )
        content: list[ContentBlock] = []
        for block in response.content:
            if block.type == "text":
                content.append(ContentBlock(type="text", text=block.text))
            elif block.type == "tool_use":
                content.append(ContentBlock(
                    type="tool_use",
                    id=block.id,
                    name=block.name,
                    input=block.input,
                ))
            elif block.type == "thinking":
                content.append(ContentBlock(
                    type="thinking",
                    thinking=block.thinking,
                    signature=block.signature,
                ))
        return NormalizedResponse(
            stop_reason="end_turn" if response.stop_reason == "end_turn" else "tool_use",
            content=content,
            model=model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def complete_simple(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int = 800,
    ) -> tuple[str, int, int]:
        response = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
        )
        return (
            response.content[0].text,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )


# ---------------------------------------------------------------------------
# OpenAI-compatible provider
# ---------------------------------------------------------------------------

class OpenAICompatProvider:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        primary_model: str | None = None,
        upgrade_model: str | None = None,
    ) -> None:
        from openai import OpenAI
        _api_key = api_key or os.environ["OPENAI_COMPAT_API_KEY"]
        _base_url = base_url or os.environ["OPENAI_COMPAT_BASE_URL"]
        self.client = OpenAI(
            api_key=_api_key,
            base_url=_base_url,
            timeout=60.0,
        )
        self.primary_model = primary_model or os.environ["OPENAI_COMPAT_PRIMARY_MODEL"]
        self.upgrade_model = upgrade_model or os.environ.get("OPENAI_COMPAT_UPGRADE_MODEL", "")

    def format_tool_definitions(self, tool_defs: list[dict]) -> list[dict]:
        """Wrap Anthropic-canonical tool defs into OpenAI function-call format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": td["name"],
                    "description": td.get("description", ""),
                    "parameters": td["input_schema"],
                },
            }
            for td in tool_defs
        ]

    def _to_openai_messages(self, system_prompt: str, messages: list[dict]) -> list[dict]:
        """Translate canonical Anthropic-format message history to OpenAI format.

        Enforces strict ordering:
          [system] -> [user] -> [assistant(tool_calls)] -> [tool, tool, ...] -> [assistant] -> ...

        Three failure modes guarded against:
        1. Every tool_calls assistant turn is immediately followed by tool messages.
        2. Assistant messages with tool_calls always have content set (even if null).
        3. System prompt injected at index 0, not as a top-level API parameter.
        """
        result: list[dict] = [{"role": "system", "content": system_prompt}]

        for msg in messages:
            role = msg["role"]
            content = msg["content"]

            if role == "user":
                if isinstance(content, str):
                    result.append({"role": "user", "content": content})
                elif isinstance(content, list):
                    # May contain tool_result blocks (from prior tool calls) and/or text.
                    # tool_results become individual "tool" role messages (one per result).
                    tool_results = [
                        b for b in content
                        if isinstance(b, dict) and b.get("type") == "tool_result"
                    ]
                    text_parts = [
                        b for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    for tr in tool_results:
                        tr_content = tr["content"]
                        if not isinstance(tr_content, str):
                            tr_content = json.dumps(tr_content)
                        result.append({
                            "role": "tool",
                            "tool_call_id": tr["tool_use_id"],
                            "content": tr_content,
                        })
                    if text_parts:
                        combined = "\n".join(p.get("text", "") for p in text_parts)
                        result.append({"role": "user", "content": combined})

            elif role == "assistant":
                if isinstance(content, str):
                    result.append({"role": "assistant", "content": content})
                elif isinstance(content, list):
                    tool_calls = []
                    text_parts = []
                    reasoning_parts = []
                    for block in content:
                        # Support both plain dicts (canonical) and SDK objects (legacy)
                        bt = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
                        if bt == "tool_use":
                            tid = block["id"] if isinstance(block, dict) else block.id
                            tname = block["name"] if isinstance(block, dict) else block.name
                            tinput = block["input"] if isinstance(block, dict) else block.input
                            tool_calls.append({
                                "id": tid,
                                "type": "function",
                                "function": {
                                    "name": tname,
                                    "arguments": json.dumps(tinput),
                                },
                            })
                        elif bt == "text":
                            txt = block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")
                            text_parts.append(txt)
                        elif bt == "reasoning_content":
                            txt = block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")
                            reasoning_parts.append(txt)

                    combined_text: str | None = "\n".join(text_parts) if text_parts else None
                    oai_msg: dict = {
                        "role": "assistant",
                        "content": combined_text,  # null is valid and required for providers that check it
                    }
                    if tool_calls:
                        oai_msg["tool_calls"] = tool_calls
                    if reasoning_parts:
                        oai_msg["reasoning_content"] = reasoning_parts[0]
                    result.append(oai_msg)

        return result

    def complete(
        self,
        system_prompt: str,
        messages: list[dict],
        formatted_tools: list[dict],
        model: str,
        max_tokens: int = 4096,
    ) -> NormalizedResponse:
        oai_messages = self._to_openai_messages(system_prompt, messages)
        response = self.client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=oai_messages,
            tools=formatted_tools,
            tool_choice="auto",
        )

        choice = response.choices[0]
        msg = choice.message
        content: list[ContentBlock] = []

        # DeepSeek reasoning models return reasoning_content alongside content.
        # Must be echoed back in subsequent turns or the API returns 400.
        reasoning = getattr(msg, "reasoning_content", None)
        if reasoning:
            content.append(ContentBlock(type="reasoning_content", text=reasoning))

        if msg.content:
            content.append(ContentBlock(type="text", text=msg.content))

        if msg.tool_calls:
            for tc in msg.tool_calls:
                content.append(ContentBlock(
                    type="tool_use",
                    id=tc.id,
                    name=tc.function.name,
                    input=json.loads(tc.function.arguments),
                ))

        stop_reason = "tool_use" if choice.finish_reason == "tool_calls" else "end_turn"

        return NormalizedResponse(
            stop_reason=stop_reason,
            content=content,
            model=model,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
        )

    def complete_stream(
        self,
        system_prompt: str,
        messages: list[dict],
        formatted_tools: list[dict],
        model: str,
        max_tokens: int = 4096,
        on_delta: Callable[[str], None] | None = None,
    ) -> NormalizedResponse:
        """Like complete(), but streams the response and calls on_delta with each text chunk.

        on_delta is called only when the response is a plain text turn (no tool calls).
        Tool-call rounds are detected in the stream and on_delta is suppressed for that
        round; the returned NormalizedResponse is identical to what complete() would return.
        """
        oai_messages = self._to_openai_messages(system_prompt, messages)
        stream = self.client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=oai_messages,
            tools=formatted_tools or None,
            tool_choice="auto" if formatted_tools else None,
            stream=True,
            stream_options={"include_usage": True},
        )

        full_text = ""
        reasoning_text = ""
        # tool_calls_raw: index → {id, name, arguments}
        tool_calls_raw: dict[int, dict] = {}
        finish_reason: str | None = None
        input_tokens = 0
        output_tokens = 0
        tool_calls_detected = False

        for chunk in stream:
            if chunk.usage:
                input_tokens = chunk.usage.prompt_tokens
                output_tokens = chunk.usage.completion_tokens

            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta

            if delta.tool_calls:
                tool_calls_detected = True
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_raw:
                        tool_calls_raw[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.id:
                        tool_calls_raw[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            tool_calls_raw[idx]["name"] += tc.function.name
                        if tc.function.arguments:
                            tool_calls_raw[idx]["arguments"] += tc.function.arguments

            reasoning_delta = getattr(delta, "reasoning_content", None)
            if reasoning_delta:
                reasoning_text += reasoning_delta

            if delta.content:
                full_text += delta.content
                if not tool_calls_detected and on_delta:
                    on_delta(delta.content)

        content: list[ContentBlock] = []
        if reasoning_text:
            content.append(ContentBlock(type="reasoning_content", text=reasoning_text))
        if full_text:
            content.append(ContentBlock(type="text", text=full_text))
        for idx in sorted(tool_calls_raw):
            tc = tool_calls_raw[idx]
            try:
                input_dict = json.loads(tc["arguments"] or "{}")
            except json.JSONDecodeError:
                input_dict = {}
            content.append(ContentBlock(
                type="tool_use",
                id=tc["id"],
                name=tc["name"],
                input=input_dict,
            ))

        stop_reason = "tool_use" if finish_reason == "tool_calls" else "end_turn"
        return NormalizedResponse(
            stop_reason=stop_reason,
            content=content,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def complete_simple(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int = 800,
    ) -> tuple[str, int, int]:
        response = self.client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
        )
        msg = response.choices[0].message
        text = msg.content or ""
        if not text:
            # Some DeepSeek reasoning models return the answer only in reasoning_content
            # when content is empty. Fall back so callers don't silently receive "".
            reasoning = getattr(msg, "reasoning_content", None) or ""
            if reasoning:
                log.warning(
                    "complete_simple: content empty, falling back to reasoning_content "
                    "(%d chars). finish_reason=%s model=%s",
                    len(reasoning),
                    response.choices[0].finish_reason,
                    model,
                )
                text = reasoning
            else:
                log.warning(
                    "complete_simple: both content and reasoning_content empty. "
                    "finish_reason=%s model=%s input_tokens=%d output_tokens=%d",
                    response.choices[0].finish_reason,
                    model,
                    response.usage.prompt_tokens,
                    response.usage.completion_tokens,
                )
        return (text, response.usage.prompt_tokens, response.usage.completion_tokens)


# ---------------------------------------------------------------------------
# Multi-profile system
# ---------------------------------------------------------------------------

@dataclass
class ModelProfile:
    name: str
    provider_type: str          # "anthropic" or "openai_compat"
    primary_model: str = ""
    upgrade_model: str = ""
    base_url: str = ""          # openai_compat only
    api_key: str = ""           # openai_compat only


def _load_profiles() -> dict[str, ModelProfile]:
    """Build profile dict from PANDABOT_PROFILE_<NAME>_TYPE env vars.

    Falls back to a single "default" profile synthesised from the old
    LLM_PROVIDER / OPENAI_COMPAT_* vars when no PANDABOT_PROFILE_* vars exist.
    """
    profiles: dict[str, ModelProfile] = {}

    for key in os.environ:
        m = _re.match(r"^PANDABOT_PROFILE_([A-Z0-9_]+)_TYPE$", key)
        if not m:
            continue
        raw = m.group(1)
        name = raw.lower()
        prefix = f"PANDABOT_PROFILE_{raw}_"
        profiles[name] = ModelProfile(
            name=name,
            provider_type=os.environ[key].lower(),
            primary_model=os.environ.get(f"{prefix}PRIMARY", ""),
            upgrade_model=os.environ.get(f"{prefix}UPGRADE", ""),
            base_url=os.environ.get(f"{prefix}URL", ""),
            api_key=os.environ.get(f"{prefix}KEY", ""),
        )

    if not profiles:
        # Backward-compat: synthesise from old env vars
        old = os.environ.get("LLM_PROVIDER", "anthropic")
        if old == "openai_compat":
            profiles["default"] = ModelProfile(
                name="default",
                provider_type="openai_compat",
                base_url=os.environ.get("OPENAI_COMPAT_BASE_URL", ""),
                api_key=os.environ.get("OPENAI_COMPAT_API_KEY", ""),
                primary_model=os.environ.get("OPENAI_COMPAT_PRIMARY_MODEL", ""),
                upgrade_model=os.environ.get("OPENAI_COMPAT_UPGRADE_MODEL", ""),
            )
        else:
            profiles["default"] = ModelProfile(
                name="default",
                provider_type="anthropic",
                primary_model=os.environ.get("ANTHROPIC_PRIMARY_MODEL", "claude-haiku-4-5"),
                upgrade_model=os.environ.get("ANTHROPIC_UPGRADE_MODEL", "claude-sonnet-4-5"),
            )

    return profiles


def _active_profile_file() -> str:
    data_dir = os.environ.get("PANDABOT_DATA_DIR", "")
    return os.path.join(data_dir, "active_model.txt") if data_dir else ""


# Module-level state — lazily initialised
_profiles: dict[str, ModelProfile] = {}
_providers: dict[str, AnthropicProvider | OpenAICompatProvider] = {}
_active_profile_cache: str | None = None


def _ensure_profiles() -> None:
    global _profiles
    if not _profiles:
        _profiles = _load_profiles()


def get_available_profiles() -> list[str]:
    """Return all defined profile names."""
    _ensure_profiles()
    return list(_profiles.keys())


def get_active_profile_name() -> str:
    """Return the name of the currently active profile."""
    global _active_profile_cache
    _ensure_profiles()

    if _active_profile_cache and _active_profile_cache in _profiles:
        return _active_profile_cache

    # Try persisted file
    f = _active_profile_file()
    if f:
        try:
            with open(f) as fh:
                name = fh.read().strip()
                if name in _profiles:
                    _active_profile_cache = name
                    return name
        except FileNotFoundError:
            pass

    # Env default or first profile
    default = os.environ.get("PANDABOT_DEFAULT_PROFILE", "")
    if default and default in _profiles:
        _active_profile_cache = default
        return default

    first = next(iter(_profiles))
    _active_profile_cache = first
    return first


def set_active_profile(name: str) -> None:
    """Switch the active profile at runtime and persist the choice."""
    global _active_profile_cache
    _ensure_profiles()
    if name not in _profiles:
        raise ValueError(f"Unknown profile {name!r}. Available: {list(_profiles)}")
    _active_profile_cache = name
    f = _active_profile_file()
    if f:
        try:
            with open(f, "w") as fh:
                fh.write(name)
        except Exception as e:
            log.warning("Could not persist active profile to %s: %s", f, e)
    log.info("Active model profile → %r", name)


def _make_provider(profile: ModelProfile) -> AnthropicProvider | OpenAICompatProvider:
    if profile.provider_type == "openai_compat":
        return OpenAICompatProvider(
            base_url=profile.base_url or None,
            api_key=profile.api_key or None,
            primary_model=profile.primary_model or None,
            upgrade_model=profile.upgrade_model or None,
        )
    return AnthropicProvider(
        primary_model=profile.primary_model or None,
        upgrade_model=profile.upgrade_model or None,
    )


# ---------------------------------------------------------------------------
# Public factory — call get_provider() everywhere
# ---------------------------------------------------------------------------

def get_provider() -> AnthropicProvider | OpenAICompatProvider:
    """Return the provider for the currently active profile (cached per profile)."""
    _ensure_profiles()
    name = get_active_profile_name()
    if name not in _providers:
        profile = _profiles[name]
        _providers[name] = _make_provider(profile)
        log.info(
            "Created provider for profile %r  type=%s  primary=%s  upgrade=%s",
            name, profile.provider_type, profile.primary_model, profile.upgrade_model or "none",
        )
    return _providers[name]


def get_provider_name() -> str:
    """Return the provider type string for the active profile ('anthropic' or 'openai_compat')."""
    _ensure_profiles()
    name = get_active_profile_name()
    return _profiles[name].provider_type if name in _profiles else "anthropic"


def get_active_model_label() -> str:
    """Return a short display label for the active model, suitable for startup messages.

    Named profiles return the profile name (e.g. 'haiku', 'gemma').
    Backward-compat 'default' profiles return the primary model string instead.
    """
    _ensure_profiles()
    name = get_active_profile_name()
    profile = _profiles.get(name)
    if not profile:
        return name
    if name == "default":
        return profile.primary_model or name
    return name
