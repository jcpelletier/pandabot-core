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
| `pandabot_core.llm.usage` | `log_call()`, `query_usage()`, `cost_usd()`, `story_usage()` | Token/cost logging to `scheduler.db`; pricing table in `_PRICING` dict. Rows carry an optional `builder` and goal `story` (builder-tiers phase 6); `story_usage("goal:<id>")` totals a goal's spend per story |
| `pandabot_core.llm.desktop` | `client()`, `DesktopClient`, `DesktopUnavailable` | Batch calls on the desktop's local model (Qwen3.8) through the PandaLocal-LLM arbiter's LAN listener: takes, renews and releases a lease around a burst of calls, and raises `DesktopUnavailable` on any refusal so the caller falls back. Batch clients on one model share it and take turns. Off unless `DESKTOP_LLM_URL` and `PANDA_LAN_KEY` are set. Standard library only |
| `pandabot_core.discord_comms` | `keep_typing()`, `split_message()`, `send_with_retry()`, `build_history()`, `ConfirmationManager`, `WebhookServer` | Discord utilities; discord.py import is lazy (TYPE_CHECKING only) so tests work without discord installed |
| `pandabot_core.identity` | `build_system_prompt()`, `startup_message()`, `bot_name()`, `bot_emoji()` | System prompt assembly from env vars and feature flags |
| `pandabot_core.telemetry` | `ai_event()`, `ai_trace()` | Fire-and-forget App Insights via daemon threads; silently disabled if `APPINSIGHTS_IKEY` unset |
| `pandabot_core.tool_registry` | `ToolRegistry`, `registry` | Feature-flag-gated tool registration and dispatch |
| `pandabot_core.pm.github` | functions | GitHub Issues adapter (list/get/search/create/update issues, sub-issues, milestones; `add_sub_issue` links an existing issue under a parent across repos — the tier-3 task ↔ product story link; `add_comment`, `list_comments`, `set_status_label`, `list_children_with_status` for the goal workflow) |
| `pandabot_core.github_app` | `get_token()`, `configured()` | Per-bot GitHub App auth: mints the bot's installation token (1 hour, refreshed 5 minutes early), exports it as `GH_TOKEN`/`GITHUB_TOKEN`; `pm.github` uses it automatically. Every bot has its own App (pandabot-dev, PandaQA, pandabot-plan, pandabot-design) so GitHub attributes each action to the bot, and nothing expires. PyJWT is imported only when minting |
| `pandabot_core.goals` | functions, `STATUS_*` | Persistent long-term-goal store (epics + story runs in `scheduler.db`) and the shared status-label vocabulary the goal driver, QA, and devops all use. Each story run records its `builder` + `builder_ref` (the harness job id; the legacy `jules_session_id` column is dropped in builder-tiers phase 7), `goal_escalations` logs builder routing moves, and `goal_verdicts` keeps every verdict per round (source ci/prefilter/qa/driver, result, attribution build/gate/infrastructure/spec, signature, excerpt, source ref) so only build failures spend feedback rounds (pandabot-dev `docs/builder-tiers.md` 4.5, 9) |
| `pandabot_core.channels` | `BotChannelMap`, `make_message_bot_tool`, `send_to_bot`, `attach_message_bot` | Channel-as-inbox inter-bot messaging mechanism (deployment-agnostic; map via `BOT_CHANNELS`) |
| `pandabot_core.active_qa` | `Driver`, `Observation`, `Action`, `SessionRecorder`, `CoverageMap`, `Frontier`, `RULES`, `ExplorationLoop`, `FakeDriver` | Exploratory (active) QA engine: the uniform driver contract, full-session traces, the state graph, the frontier policy, the universal UX rulebook (U1-U12), the strategy deck, the exploration loop, the HTML session journal, replay-based triage (cold-start confirmation before anything is reported), the markdown report and evidence bundle, the per-project `PANDABOTQA.md` reader, the model advisor (expectation records + finding judgement), the WCAG 2.2 AA pack, the Android accessibility pack, vision-only mode for targets with no UI tree, and drivers for web (Playwright) and Android (adb/uiautomator against a physical device on panda). Note: the journal compresses frames with Pillow, which is NOT in `/opt/pandaqa/venv` -- without it the journal still works but stores full-size PNGs (~500 MB/hour instead of ~50 MB). Design: `ACTIVE_QA.md` in PandaEcosystem |

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

`main` is the only branch. **Builder PRs target `main`**; every PR passes CI, the
DeepSeek pre-filter, and the PandaQA acceptance gate *before* merge — a merged PR
is the release. The old `staging` branch, `/opt/pandabot-core-staging/` clone, and
the promotion step are retired (see PandaEcosystem epic #9).

## Deploying changes

Deployed by **panda-deployer** on panda (PandaEcosystem `deployer/README.md`): it
pulls `main` every 10 minutes with its own read-only token, restarts every bot that imports core,
health-checks, and rolls back on failure. GitHub Actions never connects to panda
(no Tailscale or SSH secrets). To ship now instead of waiting:

```bash
git push origin main
bash ../deployer/deploy-now.sh pandabot-core     # or ask PandaBot-Dev: "deploy pandabot-core now"
```

The deployer only deploys a core commit once its `Test` workflow has passed on that
commit (`test.yml` runs on pushes to `main` as well as PRs).

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
| `PANDABOT_MAX_TOKENS` | `llm.loop` | Per-round output ceiling in tokens (default 8192). Not the context window. Too low and a tool call carrying a long shell command gets cut off mid-JSON; lower it only for a small local model whose own context is a few thousand tokens. |
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
| `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY_PATH` | `github_app`, `pm.github` | The bot's GitHub App; when set, `pm.github` authenticates as the App |
| `GITHUB_TOKEN`, `GITHUB_OWNER`, `GITHUB_REPOS` | `pm.github` | Fallback static token when no App is set; owner defaults to `jcpelletier`; `GITHUB_REPOS` is optional (unset: `list_repos` lists what the App's installation can reach) |
| `ENABLE_GITHUB_PM` | `pm.github` | Feature flag — enables the GitHub Issues adapter |
| `DESKTOP_LLM_URL`, `PANDA_LAN_KEY`, `DESKTOP_LLM_MODEL`, `ENABLE_DESKTOP_LLM` | `llm.desktop` | The desktop's LAN listener, the shared LAN key, the profile (default `qwen38q3-100k`), and an off switch |
