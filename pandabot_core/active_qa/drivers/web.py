"""
pandabot_core.active_qa.drivers.web
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The Playwright driver: the first real target kind, and the one the walking
skeleton measures against.

Playwright is **not** a dependency of pandabot-core and must not become one --
the package is imported by every bot on the fleet, most of which have no browser.
The import is therefore lazy and confined to :meth:`PlaywrightDriver.start`, so
``import pandabot_core.active_qa`` stays free.

Three things this driver has to get right, all of them consequences of decisions
taken earlier in the design:

**One DOM pass, not many.** The naive implementation calls into Playwright once
per element per property, which is a round-trip each and the fastest way to make
the mechanical half of the loop dominate the wall-clock (ACTIVE_QA.md 16, risk
1). Everything the engine needs is gathered in a single ``evaluate`` and returned
as one structure.

**Render hashes for U1 to U3.** "Does this button look different while pressed"
is a pixel question, so the driver answers it: it screenshots the element's own
box and hashes the bytes. That is why :class:`~pandabot_core.active_qa.contract.Element`
carries ``render_hash``, and why the probe actions are stateful here.

**Relaunch means a genuinely cold start.** With no checkpoints, replay-confirming
a finding depends on a fresh context reproducing the original conditions
exactly, so relaunch drops the browser context entirely -- storage, cookies,
service workers and all -- rather than merely navigating home.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any, Sequence

from pandabot_core.active_qa.contract import (
    DOCUMENT_FINGERPRINT_POLICY,
    Action, ActResult, DriverError, Element, Health, Observation,
)

__all__ = ["PlaywrightDriver", "WEB_ACTIONS"]

log = logging.getLogger("active_qa.web")

#: Everything a browser can do. Notably includes hover and focus, so rules U2
#: and U3 are checkable here -- unlike a touch-only mobile target.
WEB_ACTIONS = frozenset({
    "click", "type", "key", "scroll", "drag",
    "press_hold", "hover", "focus", "back", "wait",
})

# One pass over the DOM, returning everything the engine needs. Kept as a single
# evaluate so a thirty-control page costs one round-trip rather than ninety.
_COLLECT_JS = r"""
() => {
  const SEL = 'a[href],button,input,select,textarea,summary,[role],[tabindex],' +
              '[onclick],[contenteditable=""],[contenteditable="true"]';
  const ROLE = (el) => {
    const r = el.getAttribute('role');
    if (r) return r.toLowerCase();
    const t = el.tagName.toLowerCase();
    if (t === 'a') return 'link';
    if (t === 'button' || t === 'summary') return 'button';
    if (t === 'select') return 'combobox';
    if (t === 'textarea') return 'textbox';
    if (t === 'input') {
      const it = (el.type || 'text').toLowerCase();
      if (it === 'checkbox') return 'checkbox';
      if (it === 'radio') return 'radio';
      if (it === 'range') return 'slider';
      if (['button', 'submit', 'reset', 'image'].includes(it)) return 'button';
      if (['hidden'].includes(it)) return 'hidden';
      return 'textbox';
    }
    return t;
  };
  const NAME = (el) => (
    el.getAttribute('aria-label') ||
    (el.getAttribute('aria-labelledby') &&
      (document.getElementById(el.getAttribute('aria-labelledby'))||{}).innerText) ||
    el.getAttribute('title') ||
    (el.labels && el.labels[0] && el.labels[0].innerText) ||
    el.getAttribute('placeholder') ||
    (el.value && el.type && ['button','submit','reset'].includes(el.type) ? el.value : '') ||
    (el.innerText || '').trim().slice(0, 80) ||
    el.getAttribute('name') || ''
  ).trim().replace(/\s+/g, ' ');
  // A stable-ish handle. data-testid first (a project that has adopted test ids
  // gives us real identity), then id, then a structural path.
  const HANDLE = (el) => {
    const tid = el.getAttribute('data-testid') || el.getAttribute('data-test-id');
    if (tid) return '[data-testid=' + JSON.stringify(tid) + ']';
    if (el.id) return '#' + el.id;
    const parts = [];
    let n = el;
    while (n && n.nodeType === 1 && parts.length < 6) {
      let s = n.tagName.toLowerCase();
      const p = n.parentElement;
      if (p) {
        const same = [...p.children].filter(c => c.tagName === n.tagName);
        if (same.length > 1) s += ':nth-of-type(' + (same.indexOf(n) + 1) + ')';
      }
      parts.unshift(s);
      n = n.parentElement;
    }
    return parts.join('>');
  };

  const out = [];
  let i = 0;
  for (const el of document.querySelectorAll(SEL)) {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    if (r.width === 0 && r.height === 0) continue;
    const role = ROLE(el);
    if (role === 'hidden') continue;
    const disabled = el.disabled === true ||
                     el.getAttribute('aria-disabled') === 'true';
    const selected = el.getAttribute('aria-selected') === 'true' ||
                     el.getAttribute('aria-current') !== null ||
                     el.checked === true ||
                     el.classList.contains('active') ||
                     el.classList.contains('selected');
    let text = null;
    if (['textbox', 'combobox'].includes(role)) text = el.value ?? '';
    out.push({
      id: 'e' + (++i),
      role: role,
      label: NAME(el),
      bounds: [Math.round(r.left), Math.round(r.top),
               Math.round(r.right), Math.round(r.bottom)],
      enabled: !disabled,
      selected: !!selected,
      focusable: el.tabIndex >= 0,
      text: text,
      handle: HANDLE(el),
    });
  }
  return {
    elements: out,
    text: (document.body ? document.body.innerText : '').slice(0, 20000),
    title: document.title,
    url: location.href,
  };
}
"""


_FOCUSED_JS = r"""
() => {
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  const tid = el.getAttribute('data-testid') || el.getAttribute('data-test-id');
  if (tid) return 'h:[data-testid=' + JSON.stringify(tid) + ']';
  if (el.id) return 'h:#' + el.id;
  const parts = [];
  let n = el;
  while (n && n.nodeType === 1 && parts.length < 6) {
    let s = n.tagName.toLowerCase();
    const p = n.parentElement;
    if (p) {
      const same = [...p.children].filter(c => c.tagName === n.tagName);
      if (same.length > 1) s += ':nth-of-type(' + (same.indexOf(n) + 1) + ')';
    }
    parts.unshift(s);
    n = n.parentElement;
  }
  return 'h:' + parts.join('>');
}
"""


class PlaywrightDriver:
    """Drives a web page under Playwright.

    >>> d = PlaywrightDriver("https://example.com")   # doctest: +SKIP
    >>> d.start()                                     # doctest: +SKIP
    >>> obs = d.observe()                             # doctest: +SKIP
    """

    kind = "web"
    actions = WEB_ACTIONS
    #: Text counts as state here. On the web most real changes are text-only --
    #: a validation message, an error, a panel that adds copy but no controls --
    #: so the ambient-motion default would report a working button as dead.
    fingerprint_policy = DOCUMENT_FINGERPRINT_POLICY

    def __init__(
        self,
        url: str,
        *,
        headless: bool = True,
        viewport: tuple[int, int] = (1280, 800),
        screenshot_dir: str | Path | None = None,
        capture_screenshots: bool = True,
        render_hashes: bool = True,
        settle_ms: int = 120,
        nav_timeout_ms: int = 15_000,
        action_timeout_ms: int = 5_000,
    ) -> None:
        self.url = url
        self.headless = headless
        self.viewport = viewport
        self.screenshot_dir = Path(screenshot_dir) if screenshot_dir else None
        if self.screenshot_dir:
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.capture_screenshots = capture_screenshots and bool(self.screenshot_dir)
        self.render_hashes = render_hashes
        self.settle_ms = settle_ms
        self.nav_timeout_ms = nav_timeout_ms
        self.action_timeout_ms = action_timeout_ms

        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._shot_seq = 0
        # Console errors and failed responses accumulate here and are drained by
        # observe(), so each observation carries only its own delta. A log line
        # that persisted across observations would look like a state change.
        self._logs: list[str] = []
        self._net: list[dict[str, Any]] = []
        self._crashed = False
        #: Element handle currently held down by press_hold, released on the next
        #: action so a probe cannot leak into the following step.
        self._held: str | None = None
        #: Element.id -> selector, rebuilt on every observation. Ids are only
        #: meaningful within the observation that produced them.
        self._id_to_handle: dict[str, str] = {}

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - environment-specific
            raise DriverError(
                "Playwright is not installed. It is deliberately not a "
                "pandabot-core dependency; install it in the bot's venv "
                "(pip install playwright && playwright install chromium)."
            ) from exc
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self._open_context()

    def _open_context(self) -> None:
        assert self._browser is not None
        self._context = self._browser.new_context(
            viewport={"width": self.viewport[0], "height": self.viewport[1]})
        self._page = self._context.new_page()
        self._page.set_default_timeout(self.action_timeout_ms)
        self._page.on("console", self._on_console)
        self._page.on("pageerror", self._on_page_error)
        self._page.on("response", self._on_response)
        self._page.on("crash", self._on_crash)
        self._logs.clear()
        self._net.clear()
        self._crashed = False
        self._held = None
        self._page.goto(self.url, timeout=self.nav_timeout_ms,
                        wait_until="domcontentloaded")

    def close(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # pragma: no cover - teardown is best-effort
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # pragma: no cover
            pass
        self._pw = self._browser = self._context = self._page = None

    def __enter__(self) -> "PlaywrightDriver":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def relaunch(self) -> None:
        """A genuinely cold start: the whole context goes, not just the page.

        Navigating home would leave localStorage, cookies, service workers and
        in-memory state behind, and a replay that starts from a dirty context is
        not a replay of the original conditions. With no checkpoints, this is the
        only reset there is, so it has to be a real one.
        """
        if self._browser is None:
            raise DriverError("driver was never started")
        try:
            if self._context is not None:
                self._context.close()
        except Exception:  # pragma: no cover
            pass
        self._open_context()

    # -- event plumbing ----------------------------------------------------
    def _on_console(self, msg: Any) -> None:
        if msg.type in ("error", "warning"):
            self._logs.append(f"console.{msg.type}: {msg.text}"[:500])

    def _on_page_error(self, err: Any) -> None:
        self._logs.append(f"pageerror: {err}"[:500])

    def _on_response(self, resp: Any) -> None:
        try:
            if resp.status >= 400:
                self._net.append({"status": resp.status, "url": resp.url[:300]})
        except Exception:  # pragma: no cover
            pass

    def _on_crash(self, _page: Any) -> None:  # pragma: no cover - hard to force
        self._crashed = True
        self._logs.append("FATAL: the page crashed")

    # -- observation -------------------------------------------------------
    def observe(self) -> Observation:
        page = self._require_page()
        self._release_held()
        try:
            page.wait_for_timeout(self.settle_ms)
            data = page.evaluate(_COLLECT_JS)
        except Exception as exc:
            self._logs.append(f"observe failed: {exc}"[:300])
            return Observation(logs=tuple(self._drain_logs()), meta={"url": self.url})

        elements = [Element.from_dict(d) for d in data["elements"]]
        self._id_to_handle = {el.id: el.handle for el in elements if el.handle}
        if self.render_hashes:
            elements = self._attach_render_hashes(elements)

        return Observation(
            elements=tuple(elements),
            screen_id=self._screen_id(data),
            text=data.get("text", ""),
            screenshot=self._screenshot(),
            logs=tuple(self._drain_logs()),
            net=tuple(self._drain_net()),
            meta={"url": data.get("url", ""), "title": data.get("title", "")},
        )

    def _screen_id(self, data: dict[str, Any]) -> str:
        """A readable name for the state, for the report and the journal.

        Path plus title rather than the full URL: query strings and fragments
        change constantly and would make every state look like a new screen in
        the human-facing labels. Actual state identity is the fingerprint's job,
        not this.
        """
        url = data.get("url", "")
        path = url.split("://", 1)[-1].split("?", 1)[0].split("#", 1)[0]
        if url.startswith("file:"):
            # A local fixture's absolute path is noise in every heading.
            path = path.rsplit("/", 1)[-1] or path
        elif "/" in path:
            path = "/" + path.split("/", 1)[1]
        title = (data.get("title") or "").strip()
        return f"{path} {title}".strip() or url

    def _attach_render_hashes(self, elements: list[Element]) -> list[Element]:
        """Hash each interactive control's own pixels, for rules U1 to U3.

        Only interactive elements are shot: hashing every node would multiply the
        cost of an observation by the size of the DOM for no gain, since the
        rules only ever ask about controls.
        """
        page = self._require_page()
        out: list[Element] = []
        for el in elements:
            if not el.interactive or not el.handle:
                out.append(el)
                continue
            try:
                locator = page.locator(el.handle).first
                data = locator.screenshot(timeout=1500)
                digest = hashlib.sha1(data).hexdigest()
                out.append(Element(**{**el.to_dict(), "bounds": el.bounds,
                                      "render_hash": digest}))
            except Exception:
                out.append(el)
        return out

    def _screenshot(self) -> str | None:
        if not self.capture_screenshots or self.screenshot_dir is None:
            return None
        page = self._require_page()
        self._shot_seq += 1
        path = self.screenshot_dir / f"s{self._shot_seq:06d}.png"
        try:
            page.screenshot(path=str(path))
            return str(path)
        except Exception:
            return None

    def _drain_logs(self) -> list[str]:
        logs, self._logs = self._logs[:], []
        return logs

    def _drain_net(self) -> list[dict[str, Any]]:
        net, self._net = self._net[:], []
        return net

    # -- acting ------------------------------------------------------------
    def act(self, action: Action) -> ActResult:
        page = self._require_page()
        started = time.monotonic()
        if action.kind != "press_hold":
            self._release_held()

        try:
            self._dispatch(page, action)
            ok, error = True, ""
        except Exception as exc:
            ok, error = False, f"{type(exc).__name__}: {exc}"[:300]
        return ActResult(ok=ok, error=error,
                         duration_ms=int((time.monotonic() - started) * 1000))

    def _dispatch(self, page: Any, action: Action) -> None:
        kind = action.kind
        if kind == "wait":
            page.wait_for_timeout(int(action.value or 0))
            return
        if kind == "back":
            page.go_back(timeout=self.nav_timeout_ms)
            return

        locator = page.locator(self._selector_for(action)).first

        if kind == "click":
            locator.click(timeout=self.action_timeout_ms)
        elif kind == "type":
            locator.fill(str(action.value), timeout=self.action_timeout_ms)
        elif kind == "key":
            locator.press(str(action.value or "Enter"), timeout=self.action_timeout_ms)
        elif kind == "hover":
            locator.hover(timeout=self.action_timeout_ms)
        elif kind == "focus":
            locator.focus(timeout=self.action_timeout_ms)
        elif kind == "press_hold":
            box = locator.bounding_box(timeout=self.action_timeout_ms)
            if not box:
                raise RuntimeError("element has no box to press")
            page.mouse.move(box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2)
            page.mouse.down()
            self._held = "held"
        elif kind == "scroll":
            amount = int(action.value or 400)
            page.mouse.wheel(0, amount)
        elif kind == "drag":
            locator.hover(timeout=self.action_timeout_ms)
            page.mouse.down()
            page.mouse.move(200, 200)
            page.mouse.up()
        else:  # pragma: no cover - ACTION_KINDS is closed
            raise RuntimeError(f"unsupported action kind {kind!r}")

    def _selector_for(self, action: Action) -> str:
        """Resolve an element id to a selector.

        The engine addresses elements by :attr:`Element.id`, which is only
        meaningful within one observation, so the driver keeps the id-to-handle
        mapping from its most recent observation.
        """
        handle = self._id_to_handle.get(action.target or "")
        if not handle:
            raise RuntimeError(
                f"unknown element {action.target!r}; ids are only valid for the "
                "observation they came from")
        return handle

    def _release_held(self) -> None:
        """Release a press_hold WITHOUT activating the control.

        Mouse-down followed by mouse-up at the same coordinates is a click, so
        naively releasing turns every U1 probe into a real activation. That made
        the probe sweep silently drive the app: by the time the loop performed
        its own click the page had already moved, and the genuine click looked
        like a no-op -- U7 false positives on controls that worked perfectly.

        Moving the pointer off the element first means the mouseup lands
        elsewhere and no click event is dispatched.
        """
        if self._held and self._page is not None:
            try:
                self._page.mouse.move(0, 0)
                self._page.mouse.up()
            except Exception:  # pragma: no cover
                pass
            self._held = None

    # -- accessibility -----------------------------------------------------
    def run_axe(self, axe_source: str, *, tags: Sequence[str] = ()) -> dict[str, Any]:
        """Run axe-core against the current page and return its raw results.

        The source is injected per call rather than cached on the page: a
        navigation wipes it, and silently auditing a page that never got axe
        would report a clean result for a check that did not run.
        """
        page = self._require_page()
        try:
            page.evaluate(axe_source)
            options = {"runOnly": {"type": "tag", "values": list(tags)}} if tags else {}
            return page.evaluate(
                """(opts) => axe.run(document, Object.assign(
                       {resultTypes: ['violations']}, opts))""",
                options)
        except Exception as exc:
            self._logs.append(f"axe run failed: {exc}"[:300])
            return {}

    def tab_order(self, *, max_stops: int = 80) -> tuple[list[str], str | None]:
        """Tab through the page, returning (identities reached, trapped-at).

        Focus is read back as the element's own identity so it lines up with the
        rest of the engine. A control that focus will not leave is a keyboard
        trap (WCAG 2.1.2), and is reported rather than looped on forever.
        """
        page = self._require_page()
        reached: list[str] = []
        last: str | None = None
        repeats = 0
        try:
            page.evaluate("() => { if (document.activeElement) "
                          "document.activeElement.blur(); }")
            for _ in range(max_stops):
                page.keyboard.press("Tab")
                identity = page.evaluate(_FOCUSED_JS)
                if not identity:
                    break
                if identity == last:
                    repeats += 1
                    # Three presses with focus unmoved is a trap, not a slow page.
                    if repeats >= 2:
                        return reached, identity
                    continue
                repeats = 0
                last = identity
                if identity in reached:
                    break          # wrapped around: the cycle is complete
                reached.append(identity)
        except Exception as exc:
            self._logs.append(f"tab traversal failed: {exc}"[:300])
        return reached, None

    # -- health ------------------------------------------------------------
    def health(self) -> Health:
        if self._crashed:
            return Health(ok=False, crashed=True, message="the page crashed")
        if self._page is None:
            return Health(ok=False, crashed=True, message="no page")
        try:
            if self._page.is_closed():
                return Health(ok=False, crashed=True, message="the page was closed")
        except Exception as exc:  # pragma: no cover
            return Health(ok=False, crashed=True, message=str(exc))
        return Health(ok=True)

    def _require_page(self) -> Any:
        if self._page is None:
            raise DriverError("driver is not started; call start() first")
        return self._page
