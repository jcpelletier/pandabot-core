"""
pandabot_core.telemetry
~~~~~~~~~~~~~~~~~~~~~~~
Azure Application Insights helpers — fire-and-forget, never raise.

Uses stdlib urllib only (no azure-monitor-opentelemetry SDK dependency).
Telemetry is silently disabled when APPINSIGHTS_IKEY / APPINSIGHTS_ENDPOINT
are not set.

Usage:
    from pandabot_core.telemetry import ai_event, ai_trace

    ai_event("BotQuery", message="...", tools="get_docker_status")
    ai_trace("Warning", "Something looked wrong", component="scheduler")
"""

from __future__ import annotations

import datetime
import logging
import os
import threading

log = logging.getLogger("pandabot.telemetry")

__all__ = ["ai_event", "ai_trace"]


def _ikey() -> str:
    return os.environ.get("APPINSIGHTS_IKEY", "")


def _endpoint() -> str:
    return os.environ.get("APPINSIGHTS_ENDPOINT", "")


def _role_name() -> str:
    return os.environ.get("BOT_NAME", "pandabot")


def ai_event(name: str, **props: str) -> None:
    """Send a custom event to App Insights in a daemon thread."""
    ikey = _ikey()
    endpoint = _endpoint()
    if not ikey or not endpoint:
        return

    import json
    import urllib.request

    payload = json.dumps([{
        "name": "Microsoft.ApplicationInsights.Event",
        "time": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "iKey": ikey,
        "tags": {"ai.cloud.roleName": _role_name(), "ai.device.type": "Other"},
        "data": {"baseType": "EventData", "baseData": {
            "ver": 2,
            "name": name,
            "properties": {k: str(v) for k, v in props.items()},
        }},
    }]).encode()

    def _send() -> None:
        try:
            urllib.request.urlopen(
                urllib.request.Request(endpoint, payload, {"Content-Type": "application/json"}),
                timeout=5,
            )
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


def ai_trace(severity: str, message: str, **props: str) -> None:
    """Send a trace to App Insights. severity: Verbose|Information|Warning|Error|Critical"""
    ikey = _ikey()
    endpoint = _endpoint()
    if not ikey or not endpoint:
        return

    import json
    import urllib.request

    level = {"verbose": 0, "information": 1, "warning": 2, "error": 3, "critical": 4}.get(
        severity.lower(), 1
    )
    payload = json.dumps([{
        "name": "Microsoft.ApplicationInsights.Message",
        "time": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "iKey": ikey,
        "tags": {"ai.cloud.roleName": _role_name(), "ai.device.type": "Other"},
        "data": {"baseType": "MessageData", "baseData": {
            "ver": 2,
            "message": message,
            "severityLevel": level,
            "properties": {k: str(v) for k, v in props.items()},
        }},
    }]).encode()

    def _send() -> None:
        try:
            urllib.request.urlopen(
                urllib.request.Request(endpoint, payload, {"Content-Type": "application/json"}),
                timeout=5,
            )
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()
