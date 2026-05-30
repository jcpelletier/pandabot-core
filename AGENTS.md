# AGENTS.md — pandabot-core

AI coding agent instructions for this repository.

## What this repo is

Shared infrastructure package imported by all PandaBot deployments. Pure Python — no
Discord logic, no bot-specific business logic.

**Both Pandabot and Pandabot-QA depend on this package.** A change here affects both bots.

## Critical coding rules

**Lazy discord.py imports** — `discord_comms.py` uses `TYPE_CHECKING` for all discord
types. Runtime discord usage goes inside functions with `import discord`. Never add a
top-level `import discord` to any `pandabot_core` module — tests run without discord.py
installed.

**No bot-specific logic** — this package must not reference Pandabot tools, Jenkins,
Jellyfin, or any bot-specific concept. Generic infrastructure only.

**Backwards compatibility** — both bots restart when this package changes. Never remove
or rename a public function or parameter without checking all callers across
`jcpelletier/Pandabot` and `jcpelletier/PandabotQA`. Add new parameters with defaults.

**No version file** — this package has no `VERSION` file or pre-commit hook.

## Tests

```bash
python -m pytest tests/ -v
```

Tests must pass before submitting any PR. The suite requires no discord.py, anthropic,
or aiohttp — all heavy deps are stubbed or lazily imported.

Every PR must include at least one test that exercises the change.

## Adding a new module

1. Create `pandabot_core/<module>.py` with a module docstring and `__all__`.
2. Add tests in `tests/test_<module>.py`.
3. Document the module in `README.md`.
