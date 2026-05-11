# CLAUDE.md — pandabot-core

Shared infrastructure package for all PandaBot deployments.
GitHub: `jcpelletier/pandabot-core` (private)
Local path: `C:\Users\genes\Downloads\PandaMigration\pandabot-core`
Server path: `/opt/pandabot-core/` (bare git clone, on `PYTHONPATH` for both bots)

## What this repo is

A pure Python package — no Discord logic, no bot-specific business logic. It provides
the common infrastructure that every bot deployment needs: config loading, scheduling,
LLM calls, Discord helpers, telemetry, and a project-management adapter.

Both **Pandabot** and **Pandabot-QA** import from it. The server reads it directly from
`/opt/pandabot-core` via `PYTHONPATH` — no pip install or build step required.

## Module inventory

| Module | Singleton / export | What it does |
|---|---|---|
| `pandabot_core.config` | `cfg`, `ConfigError` | Env-var reading, feature flags, DB path resolution via `PANDABOT_DATA_DIR` |
| `pandabot_core.scheduler` | functions | SQLite-backed task scheduler: one_shot, condition_check, recurring |
| `pandabot_core.llm.provider` | `get_provider()`, `get_provider_name()` | Anthropic + DeepSeek abstraction; reads `LLM_PROVIDER`, `LLM_UPGRADE_MODEL` |
| `pandabot_core.llm.loop` | `run_claude_loop()` | Synchronous agentic loop — 25-round limit, upgrade model for `manage_schedule`, `on_confirm` callback for confirmation side-effects |
| `pandabot_core.llm.usage` | `log_call()`, `query_usage()`, `cost_usd()` | Token/cost logging to `scheduler.db`; pricing table in `_PRICING` dict |
| `pandabot_core.discord_comms` | `keep_typing()`, `split_message()`, `send_with_retry()`, `build_history()`, `ConfirmationManager`, `WebhookServer` | Discord utilities; discord.py import is lazy (TYPE_CHECKING only) so tests work without discord installed |
| `pandabot_core.identity` | `build_system_prompt()`, `startup_message()`, `bot_name()`, `bot_emoji()` | System prompt assembly from env vars and feature flags |
| `pandabot_core.telemetry` | `ai_event()`, `ai_trace()` | Fire-and-forget App Insights via daemon threads; silently disabled if `APPINSIGHTS_IKEY` unset |
| `pandabot_core.tool_registry` | `ToolRegistry`, `registry` | Feature-flag-gated tool registration and dispatch |
| `pandabot_core.pm.openproject` | functions | OpenProject REST adapter (list/get/create/update projects and work packages) |

## Key design decisions

**DB path:** `cfg.db_path("scheduler.db")` resolves to `$PANDABOT_DATA_DIR/scheduler.db`.
Pandabot sets `PANDABOT_DATA_DIR=/opt/discord-bot` in its systemd unit so it reuses the
existing `scheduler.db` with no data migration.

**discord.py lazy import:** `discord_comms.py` uses `TYPE_CHECKING` for all discord types.
Runtime discord usage is done via inline `import discord` inside functions. This lets tests
import the module without installing discord.py.

**No version file:** pandabot-core has no `VERSION` file or pre-commit hook. Changes are
deployed by `git pull` on the server — both bots restart to pick them up.

**Packaging:** Option C (shared PYTHONPATH) — chosen for simplicity while the API
stabilises. Future migration path: add `jcpelletier/pandabot-core` as a git dependency in
each bot's `requirements.txt` and switch to `pip install -e`.

## Deploying changes

```bash
# 1. Push from local repo
cd "C:\Users\genes\Downloads\PandaMigration\pandabot-core"
git push

# 2. Pull on server and restart whichever bots are affected
wsl ssh -i ~/.ssh/id_ed25519 genesis@192.168.1.100 \
  "sudo git -C /opt/pandabot-core pull origin main && sudo systemctl restart discord-bot pandaqa"
```

If only one bot is affected by the change, restart only that bot. Both bots share the same
library path so a pull always affects both at runtime.

## Running tests

```bash
cd "C:\Users\genes\Downloads\PandaMigration\pandabot-core"
python -m pytest tests/ -v
```

30 tests covering: config, scheduler, discord_comms, identity, tool_registry.
Tests require no discord.py, anthropic, or aiohttp — all heavy deps are either
absent from core or stubbed by the lazy import pattern.

## Adding a new module

1. Create `pandabot_core/<module>.py` with `__all__` and a module docstring.
2. Add tests in `tests/test_<module>.py`.
3. Import from it in the relevant bot(s) after confirming tests pass.
4. Document the module in the table above.

## Env vars consumed by core

| Var | Module | Effect |
|---|---|---|
| `PANDABOT_DATA_DIR` | `config` | Base directory for `scheduler.db` and other state files |
| `LLM_PROVIDER` | `llm.provider` | `anthropic` (default) or `deepseek` |
| `LLM_UPGRADE_MODEL` | `llm.provider` | Optional higher-capability model for complex tool calls |
| `ANTHROPIC_API_KEY` | `llm.provider` | Required when provider is anthropic |
| `DEEPSEEK_API_KEY` | `llm.provider` | Required when provider is deepseek |
| `APPINSIGHTS_IKEY` | `telemetry` | App Insights instrumentation key; omit to disable telemetry silently |
| `APPINSIGHTS_ENDPOINT` | `telemetry` | Custom ingestion endpoint (optional) |
| `BOT_NAME`, `BOT_EMOJI` | `identity` | Bot identity in prompts and startup messages |
| `HARDWARE_DESCRIPTION`, `TZ_NAME` | `identity` | Hardware and timezone lines in system prompt |
| `SERVER_DESCRIPTION` | `identity` | Free-form override for the services block; blank = auto-built from flags |
| `OPERATOR_SSH_CMD` | `identity` | SSH command shown to the operator in the system prompt |
| `TAILSCALE_IP` | `identity` | Shown in the auto-built services block |
| `ENABLE_JELLYFIN/JENKINS/RIPPING` | `identity` | Which services appear in the auto-built services block |
| `JENKINS_JOBS` | `identity` | Job names shown in Jenkins block |
| `SYSTEMD_SERVICES` | `identity` | Systemd units shown in services block |
| `OPENPROJECT_URL`, `OPENPROJECT_API_KEY` | `pm.openproject` | OpenProject connection |
