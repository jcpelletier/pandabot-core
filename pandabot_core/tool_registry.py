"""
pandabot_core.tool_registry
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Register tools, gate them behind feature flags, build the definition list
Claude receives, and dispatch tool calls.

Usage in a bot's tools.py:
    from pandabot_core.tool_registry import registry

    registry.register(
        name="get_docker_status",
        fn=get_docker_status,
        schema={
            "name": "get_docker_status",
            "description": "...",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        flags=["ENABLE_JELLYFIN"],   # tool hidden unless all listed flags are True
    )

In the bot entrypoint:
    from pandabot_core.tool_registry import registry
    TOOL_DEFINITIONS = registry.build_tool_definitions()
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("pandabot.tool_registry")

__all__ = ["ToolRegistry", "registry"]


@dataclass
class _ToolEntry:
    name: str
    fn: Callable[..., str]
    schema: dict
    flags: list[str] = field(default_factory=list)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, _ToolEntry] = {}

    def register(
        self,
        name: str,
        fn: Callable[..., str],
        schema: dict,
        flags: list[str] | None = None,
    ) -> None:
        """Register a tool function with its Claude schema and optional feature flags."""
        self._tools[name] = _ToolEntry(name=name, fn=fn, schema=schema, flags=flags or [])

    def build_tool_definitions(self) -> list[dict]:
        """
        Return the list of tool schemas to pass to Claude.
        Tools are excluded when any of their required flags resolve to False.
        """
        result = []
        for entry in self._tools.values():
            if all(os.environ.get(flag, "false").lower() == "true" for flag in entry.flags):
                result.append(entry.schema)
        log.debug("build_tool_definitions: %d/%d tools enabled", len(result), len(self._tools))
        return result

    def execute_tool(self, name: str, args: dict[str, Any]) -> str:
        """
        Dispatch a tool call by name. Always handles all registered names regardless
        of flag state — safe for saved scheduled tasks that reference disabled tools.
        Returns a readable error string rather than raising.
        """
        entry = self._tools.get(name)
        if entry is None:
            return f"Unknown tool: {name!r}"
        try:
            return entry.fn(**args)
        except Exception as exc:
            log.exception("Tool %s raised an exception", name)
            return f"Tool error ({name}): {exc}"

    def names(self) -> list[str]:
        return list(self._tools.keys())


# Module-level singleton — bots import this and call .register()
registry = ToolRegistry()
