# CLAUDE.md — pandabot-core

Shared infrastructure package for all PandaBot deployments.
GitHub: `jcpelletier/pandabot-core` (private)
Local path: `C:\Users\genes\GitHub\PandaEcosystem\pandabot-core`
Server path: `/opt/pandabot-core/` (production, tracks `main`)

## What this repo is

A pure Python package — no Discord logic, no bot-specific business logic. It provides
the common infrastructure that every bot deployment needs: config loading, scheduling,
LLM calls, Discord helpers, telemetry, and a project-management adapter.

All the bots import from it — **Pandabot**, **Pandabot-QA**, **Pandabot-Dev**,
**Pandabot-Plan**, and **Pandabot-Devops** (on the Pi). The server reads it directly from
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
| `pandabot_core.pm.github` | functions | GitHub Issues adapter (list/get/search/create/update issues, sub-issues, milestones; `add_sub_issue` links an existing issue under a parent across repos — the tier-3 task ↔ product story link; `add_comment`, `list_comments`, `set_status_label`, `list_children_with_status` for the goal workflow) |
| `pandabot_core.goals` | functions, `STATUS_*` | Persistent long-term-goal store (epics + story runs in `scheduler.db`) and the shared status-label vocabulary the goal driver, QA, and devops all use |
| `pandabot_core.channels` | `BotChannelMap`, `make_message_bot_tool`, `send_to_bot`, `attach_message_bot` | Channel-as-inbox inter-bot messaging mechanism (deployment-agnostic; map via `BOT_CHANNELS`) |

## Coding conventions

See `AGENTS.md` — it is the authoritative source for coding rules (lazy imports,
backwards compatibility, no bot-specific logic, test requirements). Claude Code and
all other agents read that file.

## Key design decisions

**DB path:** `cfg.db_path("scheduler.db")` resolves to `$PANDABOT_DATA_DIR/scheduler.db`.
Pandabot sets `PANDABOT_DATA_DIR=/opt/discord-bot` in its systemd unit so it reuses the
existing `scheduler.db` with no data migration.

**Packaging:** Shared PYTHONPATH — the server clones this repo and each bot's systemd
unit sets `PYTHONPATH` to the clone directory. No pip install or build step required.

**System prompt is static for caching:** `identity.build_system_prompt()` must return
byte-identical output across consecutive calls (given the same env and `extra_sections`),
so providers' automatic prompt caching can hit on the full prefix. Do not add any
per-call dynamic content (timestamps, random seeds, conversation IDs, etc.) into this
function or the strings it composes — that content belongs in the user message,
injected by the calling bot. Pandabot does this in `_run_claude_loop` via
`_build_turn_context_prefix()`. See `discord-bot/CLAUDE.md` "Caching strategy" for the
full rationale.

## Branch strategy (trunk-based, since 2026-07-05)

`main` is the only branch. **Jules PRs target `main`**; every PR passes CI, the
DeepSeek pre-filter, and the PandaQA acceptance gate *before* merge — a merged PR
is the release. The old `staging` branch, `/opt/pandabot-core-staging/` clone, and
the promotion step are retired (see PandaEcosystem epic #9).

## Deploying changes

Deployment is handled automatically by GitHub Actions on push to `main` (server
pull + restart of `discord-bot` and `pandaqa`, then a health check). See the
parent CLAUDE.md for SSH commands
and server paths.

## Running tests

```bash
cd "C:\Users\genes\GitHub\PandaEcosystem\pandabot-core"
python -m pytest tests/ -v
```

~120 tests covering: config, scheduler(+runtime), discord_comms, identity, tool_registry,
channels, goals, pm.github, testing fakes.
Tests require no discord.py, anthropic, or aiohttp — all heavy deps are either
absent from core or stubbed by the lazy import pattern.

## Env vars consumed by core

| Var | Module | Effect |
|---|---|---|
| `PANDABOT_DATA_DIR` | `config` | Base directory for `scheduler.db` and other state files |
| `LLM_PROVIDER` | `llm.provider` | `anthropic` (default) or `deepseek` |
| `LLM_UPGRADE_MODEL` | `llm.provider` | Optional higher-capability model for complex tool calls |
| `OPENAI_COMPAT_REASONING_EFFORT` | `llm.provider` | DeepSeek thinking effort: `none`/`low`/`high`/`max`. Unset sends nothing (keeps the backend default and keeps llama.cpp/Ollama working). Per-profile equivalent: `PANDABOT_PROFILE_<NAME>_EFFORT`. Overridable per call via `reasoning_effort=` on complete/complete_stream/complete_simple and on `run_claude_loop`. |
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
| `BOT_ENVIRONMENT` | `config`, `identity`, `telemetry` | `staging` or `production` (default). Adds `[staging]` to startup message, staging note to system prompt, and tags all telemetry events with the environment. |
| `GITHUB_TOKEN`, `GITHUB_OWNER`, `GITHUB_REPOS` | `pm.github` | GitHub Issues connection (token with Issues read/write; owner defaults to `jcpelletier`) |
| `ENABLE_GITHUB_PM` | `pm.github` | Feature flag — enables the GitHub Issues adapter |
