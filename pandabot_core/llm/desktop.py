"""
pandabot_core.llm.desktop
~~~~~~~~~~~~~~~~~~~~~~~~~
Batch model calls on the desktop's local model (Qwen3.8 by default), through the
PandaLocal-LLM arbiter's LAN listener (PandaLocal-LLM ``docs/remote-access.md``).

The desktop is the free tier: a bot uses it when the desktop allows batch work and
falls back to its cloud or local model on any "no". The person at the desktop always
wins, so every call can be refused or cut short, and callers must always have a
fallback.

    from pandabot_core.llm import desktop

    client = desktop.client("pandaqa")            # None when not configured
    try:
        text = client.chat(messages, purpose="grade test X")
    except desktop.DesktopUnavailable as e:
        text = cloud_call(...)                    # the fallback, with e.reason logged

**Leases.** Batch calls need a lease from the arbiter. The client takes one on its first
call, renews it in the background while calls keep coming, and releases it after
``idle_release_s`` without a call. So a burst of calls (one test run, one validation)
shares one lease and one desktop notification. Batch clients on the same model share
it (the arbiter's ``batch_priority`` decides who goes first when both wait), so PandaQA
and PandaBot-Dev take turns instead of refusing each other.

**Configuration** (environment):

| Variable | Default | Meaning |
|---|---|---|
| ``DESKTOP_LLM_URL`` | (empty: off) | The listener, e.g. ``http://192.168.1.124:8761`` |
| ``PANDA_LAN_KEY`` | (empty: off) | The shared LAN key |
| ``DESKTOP_LLM_MODEL`` | ``qwen38q3-100k`` | Profile id or model name (Dev's builder uses the same, so no swap) |
| ``ENABLE_DESKTOP_LLM`` | ``true`` | ``false`` turns it off without removing the URL |

Standard library only, so command-line tools with a trimmed environment can import it.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

__all__ = ["DesktopUnavailable", "DesktopClient", "client", "configured"]

log = logging.getLogger("pandabot_core.llm.desktop")

PROTOCOL = "1"
DEFAULT_MODEL = "qwen38q3-100k"
LEASE_TTL_S = 300.0
IDLE_RELEASE_S = 90.0
# A batch call waits behind the person at the desktop, the laptop and any batch client
# ahead of it in the arbiter's queue, so it can take minutes before it even starts.
CHAT_TIMEOUT_S = 900.0
_TRUE = ("1", "true", "yes", "on")


class DesktopUnavailable(RuntimeError):
    """The desktop said no, or stopped answering: use the fallback."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def configured() -> bool:
    return bool(os.environ.get("DESKTOP_LLM_URL", "").strip()
                and os.environ.get("PANDA_LAN_KEY", "").strip()
                and os.environ.get("ENABLE_DESKTOP_LLM", "true").strip().lower() in _TRUE)


class DesktopClient:
    """One bot's batch access to the desktop. Thread-safe; one lease at a time."""

    def __init__(self, client_name: str, url: str, key: str, model: str = DEFAULT_MODEL,
                 idle_release_s: float = IDLE_RELEASE_S, ttl_s: float = LEASE_TTL_S):
        self.client_name = client_name
        self.url = url.rstrip("/")
        self.key = key
        self.model = model
        self.idle_release_s = idle_release_s
        self.ttl_s = ttl_s
        self._lock = threading.RLock()
        self._lease: str | None = None
        self._last_use = 0.0
        self._keeper: threading.Thread | None = None
        self._stop = threading.Event()

    # --- HTTP ------------------------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None = None,
                 timeout: float = 15.0, purpose: str = "") -> dict:
        headers = {"Authorization": f"Bearer {self.key}", "X-Client": self.client_name,
                   "X-Client-Kind": "batch", "X-Protocol": PROTOCOL,
                   "Content-Type": "application/json"}
        if purpose:
            headers["X-Purpose"] = purpose[:200]
        if self._lease and path.startswith("/remote/ollama"):
            headers["X-Lease"] = self._lease
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read() or b"{}")
            except ValueError:
                detail = {}
            why = detail.get("reason") or detail.get("error") or e.reason
            raise DesktopUnavailable(f"{e.code}: {why}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise DesktopUnavailable(f"desktop unreachable: {getattr(e, 'reason', e)}") from None
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise DesktopUnavailable("desktop sent an unreadable reply") from None

    # --- the lease -------------------------------------------------------------------

    def available(self) -> tuple[bool, str]:
        """Whether the desktop takes batch work now, and why."""
        try:
            a = self._request("GET", "/remote/availability", timeout=5.0)
        except DesktopUnavailable as e:
            return False, e.reason
        return bool(a.get("available_batch")), str(a.get("reason") or "")

    def _ensure_lease(self, purpose: str) -> None:
        with self._lock:
            if self._lease:
                return
            ok, why = self.available()
            if not ok:
                raise DesktopUnavailable(f"desktop not taking batch work: {why}")
            lease = self._request("POST", "/remote/leases",
                                  {"model": self.model, "purpose": purpose,
                                   "ttl_s": self.ttl_s}, purpose=purpose)
            self._lease = lease.get("lease_id")
            if not self._lease:
                raise DesktopUnavailable("desktop granted no lease")
            log.info("desktop lease %s taken for %s", self._lease, purpose or self.client_name)
            self._stop.clear()
            self._keeper = threading.Thread(target=self._keep, daemon=True,
                                            name=f"desktop-lease-{self.client_name}")
            self._keeper.start()

    def _keep(self) -> None:
        """Renew the lease while calls keep coming; release it once idle."""
        interval = max(10.0, self.ttl_s / 3)
        while not self._stop.wait(min(interval, self.idle_release_s / 3)):
            with self._lock:
                if not self._lease:
                    return
                if time.time() - self._last_use >= self.idle_release_s:
                    self._release_locked("idle")
                    return
                try:
                    self._request("POST", f"/remote/leases/{self._lease}/renew")
                except DesktopUnavailable as e:
                    log.info("desktop lease %s ended: %s", self._lease, e.reason)
                    self._lease = None
                    return

    def _release_locked(self, why: str) -> None:
        lease, self._lease = self._lease, None
        if lease:
            try:
                self._request("DELETE", f"/remote/leases/{lease}")
                log.info("desktop lease %s released (%s)", lease, why)
            except DesktopUnavailable:
                pass

    def release(self) -> None:
        """Release the lease now (a job finished). Safe to call at any time."""
        self._stop.set()
        with self._lock:
            self._release_locked("released")

    # --- calls -------------------------------------------------------------------------

    def chat(self, messages: list[dict], *, purpose: str = "", max_tokens: int = 2048,
             images: "list[bytes | str] | None" = None, think: bool = False,
             timeout: float = CHAT_TIMEOUT_S) -> str:
        """One chat on the desktop's model; returns the reply text.

        ``images`` (raw bytes or base64 strings) are attached to the last user message.
        ``think`` is off by default: grading wants a short answer. Raises
        :class:`DesktopUnavailable` on any refusal, revoke or empty reply."""
        self._ensure_lease(purpose)
        msgs = [dict(m) for m in messages]
        if images:
            last = max(i for i, m in enumerate(msgs) if m.get("role") == "user")
            msgs[last]["images"] = [base64.b64encode(i).decode() if isinstance(i, bytes)
                                    else i for i in images]
        body = {"model": self.model, "messages": msgs, "stream": False, "think": think,
                "options": {"num_predict": int(max_tokens)}}
        self._last_use = time.time()
        try:
            out = self._request("POST", "/remote/ollama/api/chat", body,
                                timeout=timeout, purpose=purpose)
        except DesktopUnavailable as e:
            if e.reason.startswith(("409", "410")):     # revoked or expired: drop it
                with self._lock:
                    self._lease = None
            raise
        finally:
            self._last_use = time.time()
        text = ((out.get("message") or {}).get("content") or "").strip()
        if not text:
            raise DesktopUnavailable("desktop returned an empty reply")
        return text


_clients: dict[str, DesktopClient] = {}
_clients_lock = threading.Lock()


def client(client_name: str) -> "DesktopClient | None":
    """The shared desktop client for this bot, or None when the desktop is not set up
    (``DESKTOP_LLM_URL`` and ``PANDA_LAN_KEY``) or ``ENABLE_DESKTOP_LLM`` is off."""
    if not configured():
        return None
    with _clients_lock:
        c = _clients.get(client_name)
        if c is None:
            c = _clients[client_name] = DesktopClient(
                client_name, os.environ["DESKTOP_LLM_URL"].strip(),
                os.environ["PANDA_LAN_KEY"].strip(),
                os.environ.get("DESKTOP_LLM_MODEL", "").strip() or DEFAULT_MODEL)
        return c
