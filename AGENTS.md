# AGENTS.md — pandabot-core

AI coding agent instructions for this repository.

## What this repo is

Shared infrastructure package (`jcpelletier/pandabot-core`) imported by all PandaBot
deployments. Pure Python — no Discord logic, no bot-specific business logic.

**Both Pandabot and Pandabot-QA depend on this repo.** A change here affects both bots.
Think carefully about backwards compatibility before changing any public function signature.

## Architecture

| Module | What it provides |
|---|---|
| `pandabot_core.config` | `cfg` singleton — env-var reading, feature flags, DB path |
| `pandabot_core.scheduler` | SQLite-backed task scheduler |
| `pandabot_core.llm.provider` | Anthropic + DeepSeek provider abstraction |
| `pandabot_core.llm.loop` | `run_claude_loop` — synchronous agentic loop |
| `pandabot_core.llm.usage` | Token/cost logging |
| `pandabot_core.discord_comms` | `keep_typing`, `split_message`, `send_with_retry`, `build_history`, `ConfirmationManager` |
| `pandabot_core.identity` | System prompt assembly from env vars |
| `pandabot_core.telemetry` | App Insights fire-and-forget |
| `pandabot_core.tool_registry` | Feature-flag-gated tool registration |
| `pandabot_core.pm.openproject` | OpenProject REST adapter |

## Running tests

```bash
python -m pytest tests/ -v
```

Tests must pass before submitting any PR. The test suite requires no discord.py,
anthropic, or aiohttp — all heavy deps are stubbed.

## Critical coding rules

**Lazy discord.py imports** — `discord_comms.py` uses `TYPE_CHECKING` for all discord
types. Runtime discord usage goes inside functions with `import discord`. This lets tests
run without installing discord.py. Never add a top-level `import discord` to any
pandabot_core module.

**No bot-specific logic** — this package must not reference Pandabot tools, Jenkins,
Jellyfin, or any bot-specific concept. Keep it generic infrastructure only.

**Backwards compatibility** — both bots restart when this package changes. If you rename
or remove a public function, the bots will fail to start. Add new parameters with defaults;
never remove parameters without checking all callers.

**No version file** — this package has no `VERSION` file. Deployment is `git pull` on the
server followed by bot restarts.

## Files never to modify

- `tests/conftest.py` — test infrastructure
- `.env` / any credential files (not in repo)

## Adding a new module

1. Create `pandabot_core/<module>.py` with a module docstring and `__all__`.
2. Add tests in `tests/test_<module>.py`.
3. Document the module in the table in `CLAUDE.md`.
4. Push to `main` — bots restart to pick it up.

Full context and deployment: see `CLAUDE.md` in this repo.
