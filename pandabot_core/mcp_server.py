"""
pandabot_core.mcp_server
------------------------
MCP stdio server that exposes pandabot_core.pm.openproject as tools for
Claude Code sessions.

Usage
-----
Run from a Python environment that has `mcp` and `requests` installed:

    python -m pandabot_core.mcp_server

Credentials
-----------
The server reads OpenProject credentials from ~/.pandabot.env (one KEY=VALUE
per line, # comments allowed) before falling back to shell environment
variables.  Minimum required entries:

    OPENPROJECT_URL=https://plan.jpelletier.com
    OPENPROJECT_API_KEY=your-key

Claude Code registration (~/.claude/settings.json)
----------------------------------------------------
    "mcpServers": {
        "openproject": {
            "command": "python",
            "args": ["-m", "pandabot_core.mcp_server"],
            "env": {
                "PYTHONPATH": "C:\\\\Users\\\\genes\\\\GitHub\\\\PandaEcosystem\\\\pandabot-core"
            }
        }
    }

Install dependency (once, in whichever Python environment Claude Code uses):
    pip install mcp requests
"""

from __future__ import annotations

import asyncio
import os
import pathlib

# ---------------------------------------------------------------------------
# Load ~/.pandabot.env before anything else so credentials are available
# ---------------------------------------------------------------------------

_env_file = pathlib.Path.home() / ".pandabot.env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# Always enable the OpenProject feature flag in this context
os.environ.setdefault("ENABLE_OPENPROJECT", "true")

# ---------------------------------------------------------------------------
# Imports (mcp must be installed; openproject uses requests which is in core)
# ---------------------------------------------------------------------------

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    import mcp.types as types
except ImportError as _e:
    raise SystemExit(
        "The 'mcp' package is required to run the MCP server.\n"
        "Install it with:  pip install mcp\n"
        f"(original error: {_e})"
    ) from _e

from pandabot_core.pm import openproject as op

# ---------------------------------------------------------------------------
# Server definition
# ---------------------------------------------------------------------------

_server = Server("openproject")

_TOOLS: list[types.Tool] = [
    types.Tool(
        name="list_projects",
        description="List all OpenProject projects.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    types.Tool(
        name="get_project",
        description="Get details for a specific OpenProject project.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project identifier slug, e.g. 'pandabot'."},
            },
            "required": ["project"],
        },
    ),
    types.Tool(
        name="list_work_packages",
        description="List work packages for a project. Returns id, subject, type, status, assignee, description.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project identifier slug."},
                "status": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "description": "Filter by status. Defaults to 'open'.",
                },
                "limit": {"type": "integer", "description": "Max results. Defaults to 25."},
            },
            "required": ["project"],
        },
    ),
    types.Tool(
        name="get_work_package",
        description="Get full details of a single work package by numeric ID.",
        inputSchema={
            "type": "object",
            "properties": {
                "wp_id": {"type": "integer", "description": "Work package numeric ID."},
            },
            "required": ["wp_id"],
        },
    ),
    types.Tool(
        name="list_children",
        description="List all child work packages of an epic or parent WP.",
        inputSchema={
            "type": "object",
            "properties": {
                "parent_wp_id": {"type": "integer", "description": "Parent work package numeric ID."},
            },
            "required": ["parent_wp_id"],
        },
    ),
    types.Tool(
        name="search_work_packages",
        description="Full-text search across work packages. Optionally scope to a project.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search text."},
                "project": {"type": "string", "description": "Optional project slug to scope the search."},
                "limit": {"type": "integer", "description": "Max results. Defaults to 25."},
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="list_versions",
        description="List versions (milestones/sprints) for a project.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project identifier slug."},
            },
            "required": ["project"],
        },
    ),
    types.Tool(
        name="list_version_tickets",
        description="List all work packages assigned to a specific version.",
        inputSchema={
            "type": "object",
            "properties": {
                "version_id": {"type": "integer", "description": "Version numeric ID."},
            },
            "required": ["version_id"],
        },
    ),
    types.Tool(
        name="create_work_package",
        description=(
            "Create a new work package. type_id 1=Task, 2=Milestone, 3=Phase, 4=Feature, "
            "5=Epic, 6=User Story, 7=Bug — use list_types to confirm IDs for the target project."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project identifier slug."},
                "subject": {"type": "string", "description": "Title of the work package."},
                "type_id": {"type": "integer", "description": "Work package type ID. Defaults to 1 (Task)."},
                "description": {"type": "string", "description": "Markdown body."},
                "assignee": {"type": "string", "description": "User login or email to assign."},
                "start_date": {"type": "string", "description": "ISO date, e.g. '2026-06-01'."},
                "due_date": {"type": "string", "description": "ISO date, e.g. '2026-06-30'."},
                "parent_wp_id": {"type": "integer", "description": "Parent work package ID (to nest under an epic)."},
            },
            "required": ["project", "subject"],
        },
    ),
    types.Tool(
        name="update_work_package",
        description=(
            "Update fields on an existing work package. Only supplied fields are changed. "
            "Pass parent_wp_id=-1 to remove the parent relationship."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "wp_id": {"type": "integer", "description": "Work package numeric ID."},
                "subject": {"type": "string", "description": "New title."},
                "type_id": {"type": "integer", "description": "New type ID."},
                "description": {"type": "string", "description": "New markdown body."},
                "assignee": {"type": "string", "description": "User login or email."},
                "status": {"type": "string", "description": "Status name, e.g. 'In progress', 'Closed'."},
                "start_date": {"type": "string", "description": "ISO date."},
                "due_date": {"type": "string", "description": "ISO date."},
                "parent_wp_id": {"type": "integer", "description": "New parent WP ID, or -1 to remove."},
            },
            "required": ["wp_id"],
        },
    ),
]


@_server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return _TOOLS


@_server.call_tool()
async def _call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    args = arguments or {}

    if name == "list_projects":
        result = op.list_projects()
    elif name == "get_project":
        result = op.get_project(args["project"])
    elif name == "list_work_packages":
        result = op.list_work_packages(
            args["project"],
            status=args.get("status", "open"),
            limit=args.get("limit", 25),
        )
    elif name == "get_work_package":
        result = op.get_work_package(int(args["wp_id"]))
    elif name == "list_children":
        result = op.list_children(int(args["parent_wp_id"]))
    elif name == "search_work_packages":
        result = op.search_work_packages(
            args["query"],
            project=args.get("project", ""),
            limit=args.get("limit", 25),
        )
    elif name == "list_versions":
        result = op.list_versions(args["project"])
    elif name == "list_version_tickets":
        result = op.list_version_tickets(int(args["version_id"]))
    elif name == "create_work_package":
        result = op.create_work_package(
            project=args["project"],
            subject=args["subject"],
            type_id=args.get("type_id", 1),
            description=args.get("description", ""),
            assignee=args.get("assignee", ""),
            start_date=args.get("start_date", ""),
            due_date=args.get("due_date", ""),
            parent_wp_id=args.get("parent_wp_id", 0),
        )
    elif name == "update_work_package":
        result = op.update_work_package(
            wp_id=int(args["wp_id"]),
            subject=args.get("subject", ""),
            type_id=args.get("type_id", 0),
            description=args.get("description", ""),
            assignee=args.get("assignee", ""),
            status=args.get("status", ""),
            start_date=args.get("start_date", ""),
            due_date=args.get("due_date", ""),
            parent_wp_id=args.get("parent_wp_id", 0),
        )
    else:
        result = f"Unknown tool: {name!r}"

    return [types.TextContent(type="text", text=result)]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await _server.run(
            read_stream,
            write_stream,
            _server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(_run())
