"""
pandabot_core.active_qa.drivers.android
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The Android driver: adb, uiautomator and logcat against a real device.

A physical handset attached to panda over USB, rather than an emulator. That is
not a compromise: panda has no KVM (no ``/dev/kvm``, no ``vmx`` flag on its
i7-4790K), so an emulator there would be software-emulated and useless for an
hour-long session, and a real device is more faithful anyway. It also keeps the
Android runner on the same machine as the orchestrator, so no remote-runner
protocol is needed for this target.

Three things differ from the web driver, and each is a genuine constraint rather
than a gap to fill in later:

**Rules U1 to U3 are not checkable here.** ``input tap`` is atomic: the shell
command presses and releases, and there is no moment in between at which the
harness can screenshot a control mid-press. Hover does not exist on touch at all,
and focus is not a meaningful visual state on most Android UI. So this driver
declares a narrower action vocabulary, the loop never queues those probes, and
the rules report as *inapplicable* rather than passing or failing. That is the
same mechanism a project uses to waive a rule, but decided by the platform.

**Compose is largely invisible to ``uiautomator dump``.** Semantics nodes are
merged and carry no resource ids unless the app sets ``Modifier.testTag`` plus
``testTagsAsResourceId``. Where a project has not done that, element identity
falls back to role plus label, which is weaker. :meth:`PlaywrightDriver` gets
``data-testid`` for free; here it has to be earned, and
:meth:`AndroidDriver.identity_quality` reports which case we are in so the run
can say so out loud.

**Text nodes are captured, not just controls.** On the web, content changes are
caught by hashing the flattened page text. Here every ``TextView`` is a node in
the dump with its own ``text`` attribute, so including them as elements catches
content changes through element identity instead -- which is strictly better,
because it survives a HUD counter ticking somewhere else on screen.
"""

from __future__ import annotations

import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Protocol, Sequence

from pandabot_core.active_qa.contract import (
    DEFAULT_FINGERPRINT_POLICY,
    Action, ActResult, DriverError, Element, Health, Observation,
)

__all__ = ["AndroidDriver", "AdbTransport", "LocalAdb", "RemoteAdb", "ANDROID_ACTIONS"]


#: What a touch device can actually do. press_hold, hover and focus are absent,
#: which is what makes rules U1 to U3 inapplicable rather than failing.
ANDROID_ACTIONS = frozenset({
    "click", "type", "key", "scroll", "drag", "back", "wait",
})

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# android.widget.Foo -> our role vocabulary.
_ROLES = {
    "Button": "button", "ImageButton": "button", "AppCompatButton": "button",
    "EditText": "textbox", "AutoCompleteTextView": "textbox",
    "CheckBox": "checkbox", "RadioButton": "radio", "Switch": "switch",
    "ToggleButton": "switch", "SeekBar": "slider", "Spinner": "combobox",
    "TextView": "text", "ImageView": "image", "CheckedTextView": "option",
}


# --- Transport --------------------------------------------------------------


class AdbTransport(Protocol):
    """How adb commands are actually run.

    Exists so the driver can be developed and exercised from a workstation
    against a device attached to another machine, without the driver itself
    knowing anything about SSH.
    """

    def run(self, args: Sequence[str], *, timeout: float = 30.0) -> tuple[int, bytes, str]:
        """Run ``adb <args>``. Returns (returncode, stdout bytes, stderr text)."""
        ...


class LocalAdb:
    """adb on this machine. What PandaQA uses on panda."""

    def __init__(self, adb: str = "adb", serial: str | None = None) -> None:
        self.adb = adb
        self.serial = serial

    def _argv(self, args: Sequence[str]) -> list[str]:
        argv = [self.adb]
        if self.serial:
            argv += ["-s", self.serial]
        return argv + list(args)

    def run(self, args: Sequence[str], *, timeout: float = 30.0) -> tuple[int, bytes, str]:
        try:
            p = subprocess.run(self._argv(args), capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, b"", f"timed out after {timeout}s"
        except FileNotFoundError as exc:
            raise DriverError(f"adb not found: {exc}") from exc
        return p.returncode, p.stdout, p.stderr.decode("utf-8", "replace")


class RemoteAdb:
    """adb on another host, over SSH. For development only.

    Production runs on panda where adb is local; this exists so the driver can be
    written and tested from a workstation against the device on panda.
    """

    def __init__(self, host: str, *, adb: str = "adb", serial: str | None = None,
                 ssh: Sequence[str] = ("ssh",)) -> None:
        self.host = host
        self.adb = adb
        self.serial = serial
        self.ssh = list(ssh)

    def run(self, args: Sequence[str], *, timeout: float = 30.0) -> tuple[int, bytes, str]:
        import shlex
        remote = [self.adb] + (["-s", self.serial] if self.serial else []) + list(args)
        cmd = self.ssh + [self.host, " ".join(shlex.quote(a) for a in remote)]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, b"", f"timed out after {timeout}s"
        return p.returncode, p.stdout, p.stderr.decode("utf-8", "replace")


# --- The driver -------------------------------------------------------------


class AndroidDriver:
    """Drives an Android app on a real device through adb."""

    kind = "android"
    actions = ANDROID_ACTIONS
    #: Element text is captured per node (including TextViews), so content
    #: changes are caught through element identity. Flattened screen text stays
    #: out, which keeps a ticking HUD counter from making every frame a new
    #: state -- the same guarantee that matters for a game.
    fingerprint_policy = DEFAULT_FINGERPRINT_POLICY

    def __init__(
        self,
        package: str,
        *,
        activity: str | None = None,
        transport: AdbTransport | None = None,
        screenshot_dir: str | Path | None = None,
        capture_screenshots: bool = True,
        clear_data_on_relaunch: bool = True,
        grant_permissions: bool = True,
        settle_ms: int = 400,
        include_text_nodes: bool = True,
        dump_path: str = "/sdcard/pandaqa-ui.xml",
    ) -> None:
        self.package = package
        self.activity = activity
        self.adb = transport or LocalAdb()
        self.screenshot_dir = Path(screenshot_dir) if screenshot_dir else None
        if self.screenshot_dir:
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.capture_screenshots = capture_screenshots and bool(self.screenshot_dir)
        self.clear_data_on_relaunch = clear_data_on_relaunch
        self.grant_permissions = grant_permissions
        self.settle_ms = settle_ms
        self.include_text_nodes = include_text_nodes
        self.dump_path = dump_path

        self._shot_seq = 0
        self._id_to_element: dict[str, Element] = {}
        self._logcat_cursor = ""
        self._pending_logs: list[str] = []
        self._started = False

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        rc, out, err = self.adb.run(["get-state"], timeout=15)
        state = out.decode("utf-8", "replace").strip()
        if rc != 0 or state != "device":
            raise DriverError(
                f"no usable device: adb get-state returned {state or err.strip()!r}. "
                "Check the USB cable and that the device is authorised.")
        self.adb.run(["shell", "input", "keyevent", "KEYCODE_WAKEUP"], timeout=15)
        self.adb.run(["logcat", "-c"], timeout=15)
        if self.grant_permissions:
            self._grant_permissions()
        self._launch()
        self._started = True

    def _grant_permissions(self) -> list[str]:
        """Pre-grant every runtime permission the app declares.

        Otherwise the app launches behind a system permission dialog, which is
        not part of the product under test. Measured on the first real-device
        run: the loop spent all twelve of its actions exploring
        ``com.google.android.packageinstaller`` and then reported a crash
        against the app, which was neither true nor useful.
        """
        rc, out, _err = self.adb.run(
            ["shell", "dumpsys", "package", self.package], timeout=30)
        if rc != 0:
            return []
        text = out.decode("utf-8", "replace")
        wanted = sorted(set(re.findall(r"(android\.permission\.[A-Z_]+)", text)))
        granted = []
        for perm in wanted:
            rc, _o, _e = self.adb.run(
                ["shell", "pm", "grant", self.package, perm], timeout=20)
            if rc == 0:
                granted.append(perm)
        return granted

    def _launch(self) -> None:
        if self.activity:
            target = f"{self.package}/{self.activity}"
            rc, _out, err = self.adb.run(
                ["shell", "am", "start", "-W", "-n", target], timeout=60)
        else:
            rc, _out, err = self.adb.run(
                ["shell", "monkey", "-p", self.package,
                 "-c", "android.intent.category.LAUNCHER", "1"], timeout=60)
        if rc != 0:
            raise DriverError(f"could not launch {self.package}: {err.strip()}")
        time.sleep(self.settle_ms / 1000.0)

    def close(self) -> None:
        if self._started:
            self.adb.run(["shell", "am", "force-stop", self.package], timeout=30)
            self._started = False

    def __enter__(self) -> "AndroidDriver":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def relaunch(self) -> None:
        """A genuinely cold start.

        Force-stop plus ``pm clear`` wipes files, databases and shared prefs, so
        the app comes back as if freshly installed. With no checkpoints, this is
        the only reset there is, and a replay that begins from a dirty state is
        not a replay of the original conditions.
        """
        self.adb.run(["shell", "am", "force-stop", self.package], timeout=30)
        if self.clear_data_on_relaunch:
            self.adb.run(["shell", "pm", "clear", self.package], timeout=60)
            if self.grant_permissions:
                # pm clear revokes runtime grants, so a relaunch would land back
                # on the permission dialog without this.
                self._grant_permissions()
        self.adb.run(["logcat", "-c"], timeout=15)
        self._pending_logs.clear()
        self._launch()

    # -- observation -------------------------------------------------------
    def observe(self) -> Observation:
        elements = self._dump_elements()
        self._id_to_element = {el.id: el for el in elements}
        foreground = self._foreground_package()
        in_app = foreground == self.package
        return Observation(
            elements=tuple(elements),
            screen_id=self._current_activity(),
            text="\n".join(e.text for e in elements if e.text),
            screenshot=self._screenshot(),
            logs=tuple(self._drain_logcat()),
            meta={"package": self.package, "foreground_package": foreground,
                  "in_app": in_app},
        )

    def _foreground_package(self, elements: Sequence[Element] = ()) -> str:
        """Which app is actually on screen.

        Android puts things in front of the app under test that are not the app
        under test: a runtime permission dialog, a share sheet, an ANR box, the
        launcher after a crash. Acting on those is not testing the product, and a
        run that does not notice will file findings against Android itself. Every
        node in the dump names its owning package, so the majority owner is the
        honest answer.
        """
        rc, out, _err = self.adb.run(["shell", "cat", self.dump_path], timeout=20)
        if rc == 0 and out:
            packages = re.findall(rb'package="([^"]+)"', out)
            if packages:
                counts: dict[bytes, int] = {}
                for pkg in packages:
                    counts[pkg] = counts.get(pkg, 0) + 1
                return max(counts, key=counts.__getitem__).decode("utf-8", "replace")
        return self.package

    def return_to_app(self) -> bool:
        """Get back to the app under test. Returns whether it worked.

        Back first, since it dismisses most dialogs, then a relaunch. The loop
        calls this rather than exploring a system dialog it cannot fix.
        """
        for _ in range(3):
            self.adb.run(["shell", "input", "keyevent", "KEYCODE_BACK"], timeout=45)
            time.sleep(self.settle_ms / 1000.0)
            if self._foreground_package() == self.package:
                return True
        try:
            self._launch()
        except DriverError:
            return False
        return self._foreground_package() == self.package

    def _dump_elements(self) -> list[Element]:
        rc, _out, err = self.adb.run(
            ["shell", "uiautomator", "dump", self.dump_path], timeout=30)
        if rc != 0:
            self._pending_logs.append(f"uiautomator dump failed: {err.strip()}"[:300])
            return []
        rc, raw, err = self.adb.run(["shell", "cat", self.dump_path], timeout=30)
        if rc != 0 or not raw.strip():
            return []
        try:
            root = ET.fromstring(raw.decode("utf-8", "replace"))
        except ET.ParseError as exc:
            self._pending_logs.append(f"could not parse the UI dump: {exc}"[:300])
            return []

        out: list[Element] = []
        seq = 0
        for node in root.iter("node"):
            el = self._element_from(node, seq + 1)
            if el is None:
                continue
            seq += 1
            out.append(el)
        return out

    def _element_from(self, node: ET.Element, seq: int) -> Element | None:
        a = node.attrib
        cls = a.get("class", "").rsplit(".", 1)[-1]
        role = _ROLES.get(cls, "")
        clickable = a.get("clickable") == "true"
        long_clickable = a.get("long-clickable") == "true"
        checkable = a.get("checkable") == "true"
        text = a.get("text", "") or ""
        desc = a.get("content-desc", "") or ""

        if not role:
            role = "button" if clickable else "unknown"
        # A clickable container is a control whatever its class says.
        if clickable and role in ("text", "image", "unknown"):
            role = "button"

        interactive = clickable or long_clickable or checkable or role == "textbox"
        if not interactive:
            if not self.include_text_nodes or role != "text" or not text:
                return None

        m = _BOUNDS.match(a.get("bounds", ""))
        bounds = tuple(int(g) for g in m.groups()) if m else None
        if bounds and (bounds[2] - bounds[0] <= 0 or bounds[3] - bounds[1] <= 0):
            return None

        rid = a.get("resource-id", "") or ""
        handle = rid or None
        if handle and handle.startswith(f"{self.package}:id/"):
            handle = handle.split(":id/", 1)[1]

        return Element(
            id=f"e{seq}",
            role=role,
            label=" ".join((desc or text or "").split())[:80],
            bounds=bounds,  # type: ignore[arg-type]
            enabled=a.get("enabled") == "true",
            selected=a.get("selected") == "true" or a.get("checked") == "true",
            focusable=a.get("focusable") == "true",
            # Every node reports its text, so a TextView changing content moves
            # the fingerprint without needing the flattened-screen-text policy.
            text=text if (role in ("textbox", "text") or text) else None,
            handle=handle,
        )

    def _current_activity(self) -> str:
        rc, out, _err = self.adb.run(
            ["shell", "dumpsys", "activity", "activities"], timeout=20)
        if rc != 0:
            return self.package
        text = out.decode("utf-8", "replace")
        for pattern in (r"mResumedActivity[^\n]*?([\w.]+)/([\w.$]+)",
                        r"topResumedActivity[^\n]*?([\w.]+)/([\w.$]+)"):
            m = re.search(pattern, text)
            if m:
                return f"{m.group(1)}/{m.group(2)}".replace(self.package, "").lstrip("/") \
                    or m.group(2)
        return self.package

    def _screenshot(self) -> str | None:
        if not self.capture_screenshots or self.screenshot_dir is None:
            return None
        # exec-out streams the PNG straight down the wire. The obvious
        # alternative -- screencap to /sdcard then pull -- measured about 3s per
        # frame against 1.4s here, and at a frame per action that difference is
        # most of the session.
        rc, data, _err = self.adb.run(["exec-out", "screencap", "-p"], timeout=45)
        if rc != 0 or not data.startswith(b"\x89PNG"):
            return None
        self._shot_seq += 1
        path = self.screenshot_dir / f"a{self._shot_seq:06d}.png"
        path.write_bytes(data)
        return str(path)

    def _drain_logcat(self) -> list[str]:
        rc, out, _err = self.adb.run(
            ["logcat", "-d", "-v", "brief", "*:W"], timeout=30)
        logs = self._pending_logs[:]
        self._pending_logs.clear()
        if rc == 0:
            text = out.decode("utf-8", "replace")
            for line in text.splitlines():
                if self.package in line or "FATAL" in line or line.startswith("E/"):
                    logs.append(line.strip()[:400])
        self.adb.run(["logcat", "-c"], timeout=15)
        return logs[:200]

    # -- acting ------------------------------------------------------------
    def act(self, action: Action) -> ActResult:
        started = time.monotonic()
        try:
            self._dispatch(action)
            ok, error = True, ""
        except Exception as exc:
            ok, error = False, f"{type(exc).__name__}: {exc}"[:300]
        if self.settle_ms:
            time.sleep(self.settle_ms / 1000.0)
        return ActResult(ok=ok, error=error,
                         duration_ms=int((time.monotonic() - started) * 1000))

    def _dispatch(self, action: Action) -> None:
        kind = action.kind
        if kind == "wait":
            time.sleep(float(action.value or 0) / 1000.0)
            return
        if kind == "back":
            self._shell("input", "keyevent", "KEYCODE_BACK")
            return

        el = self._id_to_element.get(action.target or "")
        if el is None:
            raise RuntimeError(
                f"unknown element {action.target!r}; ids are only valid for the "
                "observation they came from")
        if el.bounds is None:
            raise RuntimeError(f"element {action.target!r} has no bounds to touch")
        x = (el.bounds[0] + el.bounds[2]) // 2
        y = (el.bounds[1] + el.bounds[3]) // 2

        if kind == "click":
            self._shell("input", "tap", str(x), str(y))
        elif kind == "type":
            self._shell("input", "tap", str(x), str(y))
            # Clear whatever is there, so `type` means "the field now contains
            # this" on Android as it does on the web. Without it, rule U6 would
            # compare against text the harness appended to rather than wrote.
            self._shell("input", "keyevent", "KEYCODE_MOVE_END")
            for _ in range(80):
                self._shell("input", "keyevent", "KEYCODE_DEL")
            value = str(action.value)
            if value:
                self._shell("input", "text", value.replace(" ", "%s"))
        elif kind == "key":
            self._shell("input", "keyevent", _keycode(str(action.value or "Enter")))
        elif kind == "scroll":
            amount = int(action.value or 600)
            self._shell("input", "swipe", str(x), str(y), str(x),
                        str(max(y - amount, 10)), "300")
        elif kind == "drag":
            self._shell("input", "swipe", str(x), str(y), str(x + 300),
                        str(y), "400")
        else:  # pragma: no cover - the vocabulary is closed
            raise RuntimeError(f"unsupported action kind {kind!r}")

    def _shell(self, *args: str) -> None:
        rc, _out, err = self.adb.run(["shell", *args], timeout=45)
        if rc != 0:
            raise RuntimeError(err.strip() or f"adb shell {' '.join(args)} failed")

    # -- health ------------------------------------------------------------
    def health(self) -> Health:
        rc, out, err = self.adb.run(["get-state"], timeout=15)
        if rc != 0 or out.decode("utf-8", "replace").strip() != "device":
            # A USB device can drop mid-run. That is a harness problem, not an
            # app defect, so it is reported as unhealthy rather than as a crash
            # finding against the product.
            return Health(ok=False, crashed=False,
                          message="the device is no longer attached",
                          details={"adb": err.strip()})
        foreground = self._foreground_package()
        if foreground == self.package:
            # It is on screen, so it is alive. Ask this before pidof: pidof is
            # unreliable across Android versions and was reporting "the app
            # crashed" as a blocker while the app was plainly in the foreground.
            return Health(ok=True, details={"in_app": True})
        rc, out, _err = self.adb.run(["shell", "pidof", self.package], timeout=20)
        if rc != 0 or not out.strip():
            return Health(ok=False, crashed=True,
                          message=f"{self.package} is not running")
        if foreground != self.package:
            # Alive, just not in front: a permission dialog or the launcher
            # is covering it. Recoverable, and emphatically not a crash
            # finding against the product.
            return Health(ok=True, crashed=False,
                          message=f"{foreground} is in the foreground",
                          details={"in_app": False, "foreground": foreground})
        return Health(ok=True, details={"in_app": True})

    # -- recovery ----------------------------------------------------------
    def recover(self) -> bool:
        """Get back into the app under test. The loop's escape hatch."""
        return self.return_to_app()

    # -- reporting ---------------------------------------------------------
    def identity_quality(self, observation: Observation) -> dict[str, Any]:
        """How well this app can be addressed, for the report to state plainly.

        Compose without test tags gives merged nodes and no resource ids, so
        element identity degrades to role plus label. A run in that state is
        weaker and should say so rather than presenting itself as equivalent.
        """
        interactive = observation.interactive()
        with_ids = [e for e in interactive if e.handle]
        total = len(interactive)
        return {
            "interactive_elements": total,
            "with_resource_id": len(with_ids),
            "fraction_addressable": round(len(with_ids) / total, 2) if total else 0.0,
            "degraded": total > 0 and len(with_ids) / total < 0.5,
        }


_KEYCODES = {
    "enter": "KEYCODE_ENTER", "back": "KEYCODE_BACK", "home": "KEYCODE_HOME",
    "tab": "KEYCODE_TAB", "escape": "KEYCODE_ESCAPE", "delete": "KEYCODE_DEL",
    "space": "KEYCODE_SPACE", "menu": "KEYCODE_MENU",
}


def _keycode(name: str) -> str:
    if name.upper().startswith("KEYCODE_"):
        return name.upper()
    return _KEYCODES.get(name.lower(), "KEYCODE_ENTER")
