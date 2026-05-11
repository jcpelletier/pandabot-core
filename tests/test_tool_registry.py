import os
from pandabot_core.tool_registry import ToolRegistry


def _make_registry():
    reg = ToolRegistry()
    reg.register(
        name="always_on",
        fn=lambda: "always",
        schema={"name": "always_on", "description": "x", "input_schema": {"type": "object", "properties": {}}},
        flags=[],
    )
    reg.register(
        name="flagged_tool",
        fn=lambda: "flagged",
        schema={"name": "flagged_tool", "description": "y", "input_schema": {"type": "object", "properties": {}}},
        flags=["ENABLE_FEATURE_X"],
    )
    return reg


def test_all_tools_without_flag():
    os.environ.pop("ENABLE_FEATURE_X", None)
    reg = _make_registry()
    defs = reg.build_tool_definitions()
    names = [d["name"] for d in defs]
    assert "always_on" in names
    assert "flagged_tool" not in names


def test_flagged_tool_appears_when_enabled():
    os.environ["ENABLE_FEATURE_X"] = "true"
    reg = _make_registry()
    defs = reg.build_tool_definitions()
    names = [d["name"] for d in defs]
    assert "flagged_tool" in names
    del os.environ["ENABLE_FEATURE_X"]


def test_execute_tool_dispatches():
    reg = _make_registry()
    result = reg.execute_tool("always_on", {})
    assert result == "always"


def test_execute_unknown_tool_returns_error():
    reg = _make_registry()
    result = reg.execute_tool("nonexistent", {})
    assert "Unknown tool" in result


def test_execute_tool_catches_exceptions():
    reg = ToolRegistry()
    reg.register(
        name="exploder",
        fn=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        schema={"name": "exploder", "description": "", "input_schema": {"type": "object", "properties": {}}},
    )
    result = reg.execute_tool("exploder", {})
    assert "Tool error" in result
