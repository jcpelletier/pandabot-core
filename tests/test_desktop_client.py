"""pandabot_core.llm.desktop against a fake arbiter listener (a real local HTTP server)."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from pandabot_core.llm import desktop


class Fake:
    def __init__(self):
        self.available = True
        self.reason = "idle"
        self.calls = []            # (method, path, headers, body)
        self.leases = {}
        self.chat_status = 200
        self.reply = "PASS: looks right"
        self.n = 0


def _handler(fake):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _do(self, method):
            body = self._body()
            fake.calls.append((method, self.path, dict(self.headers), body))
            if self.headers.get("Authorization") != "Bearer k":
                return self._send(401, {"error": "rejected_key", "reason": "wrong LAN key"})
            if self.path == "/remote/availability":
                return self._send(200, {"available_batch": fake.available,
                                        "reason": fake.reason})
            if self.path == "/remote/leases" and method == "POST":
                if not fake.available:
                    return self._send(503, {"error": "unavailable", "reason": fake.reason})
                fake.n += 1
                lid = f"L{fake.n}"
                fake.leases[lid] = "active"
                return self._send(200, {"lease_id": lid})
            if self.path.endswith("/renew"):
                lid = self.path.split("/")[3]
                ok = fake.leases.get(lid) == "active"
                return self._send(200 if ok else 409, {"lease_id": lid} if ok else
                                  {"error": "revoked", "reason": "lease revoked: busy"})
            if self.path.startswith("/remote/leases/") and method == "DELETE":
                fake.leases[self.path.split("/")[3]] = "released"
                return self._send(200, {})
            if self.path == "/remote/ollama/api/chat":
                lid = self.headers.get("X-Lease")
                if fake.leases.get(lid) != "active":
                    return self._send(409, {"error": "revoked", "reason": "lease revoked: busy"})
                if fake.chat_status != 200:
                    return self._send(fake.chat_status, {"error": "unavailable",
                                                         "reason": "user active 0 s ago"})
                return self._send(200, {"message": {"role": "assistant", "content": fake.reply}})
            return self._send(404, {"error": "not found"})

        def do_GET(self):
            self._do("GET")

        def do_POST(self):
            self._do("POST")

        def do_DELETE(self):
            self._do("DELETE")
    return H


@pytest.fixture
def server():
    fake = Fake()
    srv = HTTPServer(("127.0.0.1", 0), _handler(fake))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield fake, f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _client(url, **kw):
    return desktop.DesktopClient("pandaqa", url, "k", **kw)


def test_not_configured_without_url_or_key(monkeypatch):
    for v in ("DESKTOP_LLM_URL", "PANDA_LAN_KEY", "ENABLE_DESKTOP_LLM"):
        monkeypatch.delenv(v, raising=False)
    assert desktop.client("pandaqa") is None
    monkeypatch.setenv("DESKTOP_LLM_URL", "http://x:8761")
    assert desktop.client("pandaqa") is None
    monkeypatch.setenv("PANDA_LAN_KEY", "k")
    assert desktop.client("pandaqa") is not None
    monkeypatch.setenv("ENABLE_DESKTOP_LLM", "false")
    assert desktop.client("pandaqa") is None


def test_a_burst_of_calls_shares_one_lease_with_batch_headers(server):
    fake, url = server
    c = _client(url)
    assert c.chat([{"role": "user", "content": "grade"}], purpose="validate #7") == "PASS: looks right"
    c.chat([{"role": "user", "content": "grade again"}])
    takes = [x for x in fake.calls if x[0] == "POST" and x[1] == "/remote/leases"]
    assert len(takes) == 1 and takes[0][3]["model"] == desktop.DEFAULT_MODEL
    chat = [x for x in fake.calls if x[1] == "/remote/ollama/api/chat"][0]
    h = chat[2]
    assert h["X-Client"] == "pandaqa" and h["X-Client-Kind"] == "batch"
    assert h["X-Protocol"] == "1" and h["X-Lease"] == "L1"
    assert chat[3]["stream"] is False and chat[3]["think"] is False
    c.release()
    assert fake.leases["L1"] == "released"


def test_images_go_on_the_last_user_message(server):
    fake, url = server
    c = _client(url)
    c.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "look"}],
           images=[b"\x89PNG", "already-b64"])
    msgs = [x for x in fake.calls if x[1] == "/remote/ollama/api/chat"][0][3]["messages"]
    assert "images" not in msgs[0]
    assert msgs[1]["images"] == ["iVBORw==", "already-b64"]
    c.release()


def test_desktop_says_no_raises_unavailable_without_a_lease(server):
    fake, url = server
    fake.available, fake.reason = False, "user active 2 min ago"
    with pytest.raises(desktop.DesktopUnavailable, match="user active"):
        _client(url).chat([{"role": "user", "content": "x"}])
    assert not any(x[1] == "/remote/leases" for x in fake.calls)


def test_a_revoked_lease_is_dropped_and_the_next_call_takes_a_new_one(server):
    fake, url = server
    c = _client(url)
    c.chat([{"role": "user", "content": "x"}])
    fake.leases["L1"] = "revoked"
    with pytest.raises(desktop.DesktopUnavailable, match="409"):
        c.chat([{"role": "user", "content": "x"}])
    c.chat([{"role": "user", "content": "x"}])
    assert fake.leases.get("L2") == "active"
    c.release()


def test_unreachable_and_empty_replies_are_unavailable(server):
    fake, url = server
    with pytest.raises(desktop.DesktopUnavailable, match="unreachable"):
        desktop.DesktopClient("pandaqa", "http://127.0.0.1:1", "k").chat(
            [{"role": "user", "content": "x"}], timeout=2)
    fake.reply = ""
    c = _client(url)
    with pytest.raises(desktop.DesktopUnavailable, match="empty"):
        c.chat([{"role": "user", "content": "x"}])
    c.release()


def test_the_lease_is_released_after_idle(server):
    fake, url = server
    c = _client(url, idle_release_s=0.6, ttl_s=30)
    c.chat([{"role": "user", "content": "x"}])
    deadline = time.time() + 5
    while time.time() < deadline and fake.leases.get("L1") != "released":
        time.sleep(0.1)
    assert fake.leases["L1"] == "released" and c._lease is None
