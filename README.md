# pandabot-core

Shared infrastructure package for the Pandabot family of Discord bots. Provides config
loading, scheduling, LLM abstraction, Discord helpers, telemetry, and a project-management
adapter — with no bot-specific or Discord-event logic.

## Modules

| Module | What it provides |
|---|---|
| `pandabot_core.config` | `cfg` singleton — env-var reading, feature flags, DB path via `PANDABOT_DATA_DIR` |
| `pandabot_core.scheduler` | SQLite-backed task scheduler (one-shot, condition-check, recurring) |
| `pandabot_core.llm.provider` | Anthropic + OpenAI-compatible provider abstraction (`get_provider`) |
| `pandabot_core.llm.loop` | `run_claude_loop` — synchronous agentic loop with tool-use rounds and confirmation callbacks |
| `pandabot_core.llm.usage` | Token/cost logging to SQLite |
| `pandabot_core.discord_comms` | `keep_typing`, `split_message`, `send_with_retry`, `build_history`, `ConfirmationManager`, `WebhookServer` |
| `pandabot_core.identity` | `build_system_prompt`, `startup_message` — assembles bot identity from env vars and feature flags |
| `pandabot_core.telemetry` | `ai_event`, `ai_trace` — fire-and-forget App Insights via daemon threads; silently disabled if key is unset |
| `pandabot_core.tool_registry` | `ToolRegistry` — feature-flag-gated tool registration and dispatch |
| `pandabot_core.pm.openproject` | OpenProject REST adapter (list/get/create/update projects and work packages) |

## Usage

This package is consumed via `PYTHONPATH` rather than pip install:

```bash
export PYTHONPATH=/path/to/pandabot-core
```

Then import normally:

```python
from pandabot_core.llm.loop import run_claude_loop
from pandabot_core.discord_comms import keep_typing, split_message
```

## Environment variables

| Variable | Module | Description |
|---|---|---|
| `PANDABOT_DATA_DIR` | `config` | Directory for `scheduler.db` and other state files |
| `LLM_PROVIDER` | `llm.provider` | `anthropic` (default) or `openai_compat` |
| `LLM_UPGRADE_MODEL` | `llm.provider` | Optional higher-capability model for complex tool calls |
| `ANTHROPIC_API_KEY` | `llm.provider` | Required when provider is `anthropic` |
| `OPENAI_COMPAT_API_KEY` | `llm.provider` | Required when provider is `openai_compat` |
| `OPENAI_COMPAT_BASE_URL` | `llm.provider` | Base URL for OpenAI-compatible provider |
| `OPENAI_COMPAT_PRIMARY_MODEL` | `llm.provider` | Model name for the compatible provider |
| `APPINSIGHTS_IKEY` | `telemetry` | App Insights instrumentation key; omit to disable telemetry |
| `APPINSIGHTS_ENDPOINT` | `telemetry` | Custom ingestion endpoint (optional) |
| `BOT_NAME`, `BOT_EMOJI` | `identity` | Bot identity in prompts and startup messages |
| `BOT_ENVIRONMENT` | `config`, `identity`, `telemetry` | `staging` or `production` (default) |
| `OPENPROJECT_URL`, `OPENPROJECT_API_KEY` | `pm.openproject` | OpenProject connection |

## Running tests

```bash
pip install pytest pytest-asyncio
python -m pytest tests/ -v
```

Tests have no dependency on discord.py, anthropic, or aiohttp — heavy runtime deps are
either absent from core or covered by the lazy import pattern in `discord_comms.py`.

## Design notes

**No pip install required** — the server clones this repo and sets `PYTHONPATH` to the
clone directory. This keeps the dependency surface minimal while the API stabilises.

**discord.py lazy import** — `discord_comms.py` uses `TYPE_CHECKING` guards for all
discord types so the module can be imported in test environments without discord installed.

**DB path** — `cfg.db_path("scheduler.db")` resolves to `$PANDABOT_DATA_DIR/scheduler.db`,
letting each bot point to its own data directory without code changes.
