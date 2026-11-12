"""
GitHub App installation-token auth, shared by every bot.

Each bot has its own GitHub App (pandabot-dev, PandaQA, pandabot-plan, pandabot-design),
so everything it does on GitHub is attributed to that bot, with no personal token to
expire. This module mints the App's short-lived installation token and exports it as
GH_TOKEN / GITHUB_TOKEN, so the ``gh`` CLI, git and ``pandabot_core.pm.github`` all
authenticate as the App.

Config (env, in the bot's .env):
    GITHUB_APP_ID                 the App's id (its settings page)
    GITHUB_APP_INSTALLATION_ID    its installation on the account (/settings/installations/<id>)
    GITHUB_APP_PRIVATE_KEY_PATH   the App's .pem on the server, readable only by the bot

Installation tokens last about an hour; get_token() caches one and refreshes it about
five minutes before expiry. With the App not configured, get_token() returns "" and the
caller falls back to whatever it used before (a static GITHUB_TOKEN, or gh's keyring).
PyJWT is imported only when a token is minted, so bots without an App do not need it.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("pandabot_core.github_app")

_lock = threading.Lock()
_token: str = ""
_expires: float = 0.0


def configured() -> bool:
    return bool(
        os.environ.get("GITHUB_APP_ID")
        and os.environ.get("GITHUB_APP_INSTALLATION_ID")
        and os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
    )


# Backwards-compatible name used by the bots' former copies of this module.
_configured = configured


def _mint() -> tuple[str, float]:
    import jwt  # PyJWT, lazily: only bots with an App need it
    import requests
    app_id = os.environ["GITHUB_APP_ID"]
    installation = os.environ["GITHUB_APP_INSTALLATION_ID"]
    with open(os.environ["GITHUB_APP_PRIVATE_KEY_PATH"], "rb") as fh:
        private_key = fh.read()
    now = int(time.time())
    assertion = jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": app_id},
        private_key, algorithm="RS256",
    )
    r = requests.post(
        f"https://api.github.com/app/installations/{installation}/access_tokens",
        headers={
            "Authorization": f"Bearer {assertion}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    exp = (
        datetime.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )
    return data["token"], exp


def get_token() -> str:
    """A valid App installation token, refreshed when needed and exported as GH_TOKEN /
    GITHUB_TOKEN. "" when the App is not configured. Never raises: a failed refresh
    keeps a still-valid cached token, else returns ""."""
    global _token, _expires
    if not configured():
        return ""
    with _lock:
        if _token and time.time() < _expires - 300:
            return _token
        try:
            _token, _expires = _mint()
        except Exception as exc:
            log.error("GitHub App token mint failed: %s", exc)
            return _token if (_token and time.time() < _expires) else ""
        os.environ["GH_TOKEN"] = _token
        os.environ["GITHUB_TOKEN"] = _token
        return _token


def _reset_for_tests() -> None:
    global _token, _expires
    with _lock:
        _token, _expires = "", 0.0
