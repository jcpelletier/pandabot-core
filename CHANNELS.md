# Channel map — inter-bot messaging guide

How the Panda bots talk to each other on Discord. Backed by
`pandabot_core.channels`.

## The convention: each bot's channel is its inbox

Every bot owns **one** Discord channel and listens only there. To ask a bot to
do something, you post the request **in that bot's channel** — the channel *is*
the addressing. The target does the work and replies in its own channel; a human
watching that channel sees the whole exchange.

This is **one-way** from the requester's point of view: it drops the request in
the target's inbox and does not receive the reply back in its own channel. (A
human, or the target's own follow-up, carries things forward.)

Why this shape: it keeps every request and result visible in the channel that
owns the work, and it means a bot never has to listen across many channels or
juggle cross-channel reply routing.

## The map (this deployment)

| Bot | Channel | Channel ID | Role |
|---|---|---|---|
| `pandabot` | `#pandabot` | `1082362941191495700` | Main server assistant |
| `pandabot-qa` | `#pandabot-qa` | `1501713664040894505` | QA gate for staging → prod |
| `pandabot-dev` | `#pandabot-dev` | `1503589824752521216` | Coding agent (dispatches to Jules) |
| `pandabot-devops` | `#pandabot-devops` | `1511823781620879462` | Infra/CLI + CI troubleshooting (runs on the Pi) |

These IDs are **configuration, not code** — `pandabot_core` hardcodes none of
them (it must stay deployment-agnostic). Each bot receives the map via the
`BOT_CHANNELS` env var:

```
BOT_CHANNELS=pandabot:1082362941191495700,pandabot-qa:1501713664040894505,pandabot-dev:1503589824752521216,pandabot-devops:1511823781620879462
```

Format: comma-separated `name:channel_id` pairs. Names are matched
case-insensitively.

## Key invariant

A map **name** must be a token the target bot recognises as *itself*. When a bot
sends a request, `send_to_bot` puts the target name in the message header
(`📨 Request from X → pandabot-dev`), and the target's addressing gate matches on
that name. So the channel-map key (`pandabot-dev`) must appear in the target
bot's alias list (e.g. PandaBot-Dev's `BOT_ALIASES`). Keep the two in sync.

## Using it from a bot (opt-in wiring)

`pandabot_core.channels` is the shared mechanism; each bot opts in with a few
lines:

```python
from pandabot_core.channels import (
    BotChannelMap, make_message_bot_tool, send_to_bot_threadsafe,
)

CHANNEL_MAP = BotChannelMap.from_env()          # reads BOT_CHANNELS

# 1. expose the tool to the LLM
tool_definitions = [*MY_TOOLS, make_message_bot_tool(CHANNEL_MAP)]

# 2. capture the running loop once (e.g. in on_ready)
_MAIN_LOOP = asyncio.get_running_loop()

# 3. route the tool — it runs in a worker thread, so bridge to the loop
def execute_tool(name, args):
    if name == "message_bot":
        return send_to_bot_threadsafe(
            client, _MAIN_LOOP, CHANNEL_MAP,
            args["target"], args["request"], sender=BOT_NAME,
        )
    ...
```

For the target bot to accept the request, it must (a) allow-list the sender bot's
Discord user ID and (b) recognise its own map-name as an alias. The sending bot
must also be a **member of the target's channel with send permission**.

## API reference

| Symbol | Purpose |
|---|---|
| `BotChannelMap.from_env(var="BOT_CHANNELS")` | Parse the registry from env. |
| `BotChannelMap(mapping)` | Build from an explicit `{name: id}` dict. |
| `map.get(name)` / `name in map` / `map.names()` | Look up / test / list targets. |
| `await send_to_bot(client, map, target, text, sender=...)` | Post a request (async). |
| `send_to_bot_threadsafe(client, loop, map, target, text, sender=...)` | Sync bridge for tools running off the event loop. |
| `make_message_bot_tool(map)` | Anthropic tool schema, with known targets as an enum. |
