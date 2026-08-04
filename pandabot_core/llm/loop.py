"""
pandabot_core.llm.loop
~~~~~~~~~~~~~~~~~~~~~~
Synchronous Claude agentic loop — intended to run in a thread executor so the
asyncio event loop stays free.

Usage:
    from pandabot_core.llm.loop import run_claude_loop

    reply = await loop.run_in_executor(
        None, run_claude_loop,
        user_message,   # str
        history,        # list[dict] | None
        tool_defs,      # list[dict] from registry.build_tool_definitions()
        execute_tool,   # callable(name, args) -> str
        system_prompt,  # str
        channel_id,     # int | None  (for pending-confirmation side-effect)
        conv_id,        # str | None
        on_confirm,     # callable(channel_id, tool_name, confirmed_inputs) | None
    )
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from pandabot_core.llm import provider as _prov_mod
from pandabot_core.llm import usage as _usage
from pandabot_core.telemetry import ai_event

log = logging.getLogger("pandabot.llm.loop")

__all__ = ["run_claude_loop"]

# Tools that warrant upgrading to a more capable model when detected
_UPGRADE_TOOLS: set[str] = {"manage_schedule"}

# Tools whose unconfirmed preview results should trigger a pending-confirmation save
_CONFIRM_TOOLS: set[str] = {"manage_files", "set_jenkins_schedule"}


def run_claude_loop(
    user_message: str,
    history: list[dict] | None,
    tool_definitions: list[dict],
    execute_tool: Callable[[str, dict[str, Any]], str],
    system_prompt: str,
    channel_id: int | None = None,
    conversation_id: str | None = None,
    on_confirm: Callable[[int, list[dict]], None] | None = None,
    extra_confirm_tools: "set[str] | None" = None,
    on_text_delta: Callable[[str], None] | None = None,
    reasoning_effort: str | None = None,
) -> str:
    """
    Synchronous agentic loop. Runs LLM → tool → LLM until end_turn or tool limit.

    Parameters
    ----------
    user_message    : the current user query
    history         : prior conversation turns in Anthropic message format
    tool_definitions: output of registry.build_tool_definitions()
    execute_tool    : callable(name, args) -> str  (e.g. registry.execute_tool)
    system_prompt   : assembled system prompt string
    channel_id      : Discord channel ID — passed to on_confirm for state storage
    conversation_id : UUID string for usage logging; generated if None
    on_confirm      : called once per round (not per tool call) with every destructive
                      preview from that round batched together, so multi-action turns
                      (e.g. four file moves) aren't collapsed to just the last one.
                      Signature: (channel_id, actions) where actions is a list of
                      {"name": tool_name, "inputs": confirmed_inputs} dicts.
    on_text_delta   : if provided and the active provider supports streaming, called
                      with each text chunk on the final (non-tool-call) round. Tool-call
                      rounds never invoke this callback.
    reasoning_effort: DeepSeek thinking effort for this loop ("none"/"low"/"high"/"max").
                      None uses the provider's configured default. Raising it costs
                      latency and output tokens, so keep interactive loops low.
    """
    import time as _time

    conv_id = conversation_id or str(uuid.uuid4())
    provider = _prov_mod.get_provider()
    provider_name = _prov_mod.get_provider_name()
    formatted_tools = provider.format_tool_definitions(tool_definitions)

    messages = (history or []) + [{"role": "user", "content": user_message}]
    tools_called: list[str] = []
    t0 = _time.monotonic()

    _can_stream = on_text_delta is not None and hasattr(provider, "complete_stream")

    for _ in range(25):  # safety: max 25 tool-call rounds
        if _can_stream:
            response = provider.complete_stream(
                system_prompt=system_prompt,
                messages=messages,
                formatted_tools=formatted_tools,
                model=provider.primary_model,
                max_tokens=4096,
                on_delta=on_text_delta,
                reasoning_effort=reasoning_effort,
            )
        else:
            response = provider.complete(
                system_prompt=system_prompt,
                messages=messages,
                formatted_tools=formatted_tools,
                model=provider.primary_model,
                max_tokens=4096,
                reasoning_effort=reasoning_effort,
            )
        _usage.log_call(
            conversation_id=conv_id,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            user_message=user_message,
            context="main",
            provider=provider_name,
        )
        log.info(
            "LLM stop_reason=%s model=%s in=%d out=%d cost=$%.5f",
            response.stop_reason, response.model,
            response.input_tokens, response.output_tokens,
            _usage.cost_usd(response.model, response.input_tokens, response.output_tokens, provider=provider_name),
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if block.type == "text" and block.text:
                    ai_event(
                        "BotQuery",
                        message=user_message[:200],
                        tools=",".join(tools_called) or "none",
                        response_ms=str(int((_time.monotonic() - t0) * 1000)),
                    )
                    return block.text
            # Thinking models (e.g. Gemma) may return only reasoning_content with no
            # text block when the context window is tight. Fall back so callers don't
            # silently receive "(no text response)".
            for block in response.content:
                if block.type == "reasoning_content" and block.text:
                    log.warning(
                        "run_claude_loop: no text block — falling back to reasoning_content (%d chars)",
                        len(block.text),
                    )
                    return block.text
            return "(no text response)"

        if response.stop_reason == "tool_use":
            # Upgrade to higher-capability model for complex scheduling tool
            if (
                provider.upgrade_model
                and any(b.type == "tool_use" and b.name in _UPGRADE_TOOLS for b in response.content)
            ):
                upgrade_tool = next(
                    b.name for b in response.content
                    if b.type == "tool_use" and b.name in _UPGRADE_TOOLS
                )
                log.info("Upgrading to %s for %s", provider.upgrade_model, upgrade_tool)
                response = provider.complete(
                    system_prompt=system_prompt,
                    messages=messages,
                    formatted_tools=formatted_tools,
                    model=provider.upgrade_model,
                    max_tokens=4096,
                    reasoning_effort=reasoning_effort,
                )
                _usage.log_call(
                    conversation_id=conv_id,
                    model=response.model,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    user_message=user_message,
                    context="model_upgrade",
                    provider=provider_name,
                )

            # Build canonical assistant turn
            canonical_content = []
            for b in response.content:
                if b.type == "text":
                    canonical_content.append({"type": "text", "text": b.text})
                elif b.type == "tool_use":
                    canonical_content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
                elif b.type == "thinking":
                    canonical_content.append({"type": "thinking", "thinking": b.thinking, "signature": b.signature})
                elif b.type == "reasoning_content":
                    canonical_content.append({"type": "reasoning_content", "text": b.text})
            messages.append({"role": "assistant", "content": canonical_content})

            # Execute tools and gather results
            tool_results = []
            pending_actions: list[dict] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                log.info("Tool call: %s(%s)", block.name, block.input)
                tools_called.append(block.name)
                result = execute_tool(block.name, block.input)
                log.debug("Tool result (%s): %.300s", block.name, result)

                # Pending-confirmation side-effect. Collected across the whole
                # round (a single model turn can preview several destructive
                # calls, e.g. four file moves) and flushed once below — calling
                # on_confirm per-block here would let each call clobber the
                # last, silently dropping all but the final preview.
                _all_confirm = _CONFIRM_TOOLS | (extra_confirm_tools or set())
                if (
                    block.name in _all_confirm
                    and not block.input.get("confirmed", False)
                    and ("Reply **yes** to confirm" in result or "warning" in result.lower())
                ):
                    confirmed_inputs = {**block.input, "confirmed": True}
                    pending_actions.append({"name": block.name, "inputs": confirmed_inputs})

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
            messages.append({"role": "user", "content": tool_results})

            if on_confirm is not None and channel_id is not None and pending_actions:
                on_confirm(channel_id, pending_actions)

        else:
            return f"(unexpected stop_reason: {response.stop_reason})"

    return "Sorry, I hit the tool-call limit without finishing. Try a more specific question."
