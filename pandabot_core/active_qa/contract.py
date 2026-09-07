"""
pandabot_core.active_qa.contract
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The uniform driver contract for exploratory (active) QA.

Every target kind -- a web page under Playwright, an Android app under adb, a
Roblox place under the Studio MCP -- implements the same four operations and
normalises itself into the same :class:`Observation` shape. The exploration
loop, the rulebook detectors and the model never see Playwright vs uiautomator
vs Luau; they see this.

Design notes live in ``ACTIVE_QA.md`` in the PandaEcosystem repo (sections 4 and
5.2). Two decisions from there are load-bearing here:

**There is no checkpoint/restore.** A run is one continuous session from a cold
start, and :meth:`Driver.relaunch` is the only reset. Saved states an agent
resumes from are future functionality. The consequence is that every action
trace must be complete from launch -- see
:mod:`pandabot_core.active_qa.recorder`.

**Fingerprints exclude ambient motion by default.** The no-op detector ("this
control did nothing") compares state before and after an action, so it assumes a
UI at rest is actually at rest. In a game that is false: bosses sway, villagers
patrol, a day/night clock advances, timers tick. The default
:class:`FingerprintPolicy` therefore hashes only the *interactive UI tree*
(role, label, enabled, selected, focusable) and deliberately excludes element
bounds and flattened screen text, so a swaying boss does not make every
observation look different. See :meth:`Observation.compute_fingerprint`.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Iterable, Protocol, Sequence, runtime_checkable

__all__ = [
    "ActionKind", "ACTION_KINDS", "Action", "ActResult",
    "Element", "Observation", "Health",
    "FingerprintPolicy", "DEFAULT_FINGERPRINT_POLICY", "STRICT_FINGERPRINT_POLICY",
    "DOCUMENT_FINGERPRINT_POLICY",
    "Driver", "DriverError", "fingerprint_policy_for",
]


class DriverError(RuntimeError):
    """A driver could not carry out an operation (target gone, transport dead)."""


# --- Actions ----------------------------------------------------------------

# The action vocabulary. ``press_hold``, ``hover`` and ``focus`` exist solely so
# the rulebook can capture a control mid-interaction and diff it against rest --
# they are how rules U1 (pressed state), U2 (hover state) and U3 (focus
# indicator) are checked. Without them those rules are unenforceable.
ActionKind = str
ACTION_KINDS: frozenset[str] = frozenset({
    "click",       # primary activation
    "type",        # enter `value` into `target`
    "key",         # send a key or chord named by `value`
    "scroll",      # scroll `target` (or the viewport) by `value`
    "drag",        # drag `target` to `value` (an element id or a coordinate pair)
    "press_hold",  # hold the pointer down on `target` -- rule U1
    "hover",       # move the pointer over `target` without pressing -- rule U2
    "focus",       # give `target` input focus -- rule U3
    "back",        # platform back / escape
    "wait",        # do nothing for `value` milliseconds
})


@dataclass(frozen=True, slots=True)
class Action:
    """One thing the harness does to the app under test.

    ``target`` is an :attr:`Element.id` from the observation the action was
    chosen against. ``value`` carries the payload for kinds that need one (the
    text to type, the key to press, the milliseconds to wait).
    """

    kind: ActionKind
    target: str | None = None
    value: Any = None
    # Why this action was chosen. Carried into the report so a repro step reads
    # as intent ("boundary values: max int + 1") rather than as a bare click.
    reason: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ACTION_KINDS:
            raise ValueError(
                f"unknown action kind {self.kind!r}; expected one of "
                f"{', '.join(sorted(ACTION_KINDS))}"
            )
        if self.kind in ("click", "type", "press_hold", "hover", "focus") and not self.target:
            raise ValueError(f"action kind {self.kind!r} requires a target element id")
        if self.kind == "type" and self.value is None:
            raise ValueError("action kind 'type' requires a value")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Action":
        return cls(
            kind=d["kind"],
            target=d.get("target"),
            value=d.get("value"),
            reason=d.get("reason", ""),
        )

    def describe(self) -> str:
        """One-line human phrasing, used to build repro steps in the report."""
        bits = [self.kind.replace("_", " ")]
        if self.target:
            bits.append(f"on {self.target}")
        if self.value is not None and self.kind != "wait":
            v = str(self.value)
            if len(v) > 40:
                v = v[:37] + "..."
            bits.append(f"with {v!r}")
        elif self.kind == "wait":
            bits.append(f"{self.value}ms")
        return " ".join(bits)


@dataclass(frozen=True, slots=True)
class ActResult:
    """What happened when a driver carried out an :class:`Action`."""

    ok: bool = True
    error: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- Observations -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Element:
    """One addressable thing in the UI, normalised across every target kind."""

    id: str
    role: str = "unknown"
    label: str = ""
    bounds: tuple[int, int, int, int] | None = None
    enabled: bool = True
    selected: bool = False
    focusable: bool = False
    text: str | None = None
    # The driver's own handle for this element (a CSS selector, a uiautomator
    # resource-id, a Roblox instance path). Opaque to everything above the
    # driver, but it is the strongest available identity signal, so
    # `identity` prefers it.
    handle: str | None = None
    #: Hash of this element's *rendered pixels*, when the driver can supply one.
    #: Rules U1 to U3 are "does this control look different while pressed /
    #: hovered / focused", which is a pixel question, so the driver (which has
    #: the screenshot) answers it and the detectors compare hashes. Deliberately
    #: never part of the fingerprint: a render hash moves with every animation
    #: frame, and letting it count as state would undo the ambient-motion
    #: guarantee that makes no-op detection work at all.
    render_hash: str | None = None

    @property
    def identity(self) -> str:
        """A key that is stable for *the same control across observations*.

        :attr:`id` is only unique within one observation (``e12`` is whatever
        was twelfth this time), so it cannot be used to answer "have I already
        tried this button" or to cluster candidates in triage. This can: it
        prefers the driver's stable handle and falls back to role plus label.
        """
        if self.handle:
            return f"h:{self.handle}"
        if self.label:
            return f"r:{self.role}|l:{self.label}"
        # Neither a handle nor a label. Position is all that is left, and a
        # colliding identity is far worse than an unstable one: two controls
        # sharing an identity means the coverage map cannot tell them apart, the
        # frontier marks both tried when only one is, and actions bind to
        # whichever came first. Quantised to an 8px grid so small reflow does
        # not invent elements -- though any grid has boundaries, so a control
        # that straddles one can still change identity. That is a weaker
        # guarantee than a handle or a label, which is exactly why a driver's
        # identity_quality() reports when a run is leaning on it.
        if self.bounds:
            box = ",".join(str(v // 8 * 8) for v in self.bounds)
            return f"r:{self.role}|b:{box}"
        return f"r:{self.role}|i:{self.id}"

    @property
    def interactive(self) -> bool:
        """Whether the exploration loop should consider acting on this."""
        return self.role in _INTERACTIVE_ROLES or self.focusable

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.bounds is not None:
            d["bounds"] = list(self.bounds)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Element":
        bounds = d.get("bounds")
        return cls(
            id=d["id"],
            role=d.get("role", "unknown"),
            label=d.get("label", ""),
            bounds=tuple(bounds) if bounds else None,  # type: ignore[arg-type]
            enabled=d.get("enabled", True),
            selected=d.get("selected", False),
            focusable=d.get("focusable", False),
            text=d.get("text"),
            handle=d.get("handle"),
            render_hash=d.get("render_hash"),
        )


_INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "input", "checkbox", "radio", "switch",
    "slider", "menuitem", "tab", "combobox", "listitem", "option",
})


@dataclass(frozen=True, slots=True)
class FingerprintPolicy:
    """What counts as "the same state" when hashing an observation.

    The defaults are chosen for **ambient motion**, which is the thing most
    likely to break no-op detection. A Roblox world is never still, so hashing
    bounds or flattened screen text would make every observation unique and the
    no-op detector would silently never fire -- the failure mode is silence, not
    noise, which is why the default is conservative.
    """

    include_bounds: bool = False       # positions move under animation
    include_screen_text: bool = False  # clocks, counters, timers
    include_element_text: bool = True  # a field's own contents: needed for U6
    include_screen_id: bool = True
    # Regexes redacted before hashing anything textual. Defaults cover the usual
    # tickers: clock times, ISO timestamps, and bare run of digits with units.
    volatile_patterns: tuple[str, ...] = (
        r"\d{1,2}:\d{2}(:\d{2})?",
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}",
    )

    def redact(self, s: str) -> str:
        for pat in self.volatile_patterns:
            s = re.sub(pat, "•", s)
        return s


DEFAULT_FINGERPRINT_POLICY = FingerprintPolicy()

#: Hashes everything, including bounds and screen text. Correct for a static
#: target (a form, a settings page) where a moved element genuinely is a state
#: change; wrong for anything with ambient motion.
STRICT_FINGERPRINT_POLICY = FingerprintPolicy(
    include_bounds=True,
    include_screen_text=True,
)

#: For document-like targets: web pages, forms, settings screens. Screen text
#: counts as state, because on the web a great many real changes are text-only
#: -- a validation message appearing, an error being shown, a panel opening that
#: adds copy but no controls. Excluding it (as the default policy does, to
#: survive a swaying boss) makes a working button look dead and puts rule U9 out
#: of reach entirely. Bounds stay out: reflow moves things for reasons that are
#: not state changes, and rule U10 watches bounds directly anyway.
DOCUMENT_FINGERPRINT_POLICY = FingerprintPolicy(
    include_bounds=False,
    include_screen_text=True,
)


@dataclass(frozen=True, slots=True)
class Observation:
    """A normalised snapshot of the app under test at one instant."""

    elements: tuple[Element, ...] = ()
    screen_id: str = ""
    text: str = ""
    screenshot: str | None = None
    logs: tuple[str, ...] = ()
    net: tuple[dict[str, Any], ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)
    captured_at: float = field(default_factory=time.time)

    # -- identity ----------------------------------------------------------
    def compute_fingerprint(
        self, policy: FingerprintPolicy = DEFAULT_FINGERPRINT_POLICY
    ) -> str:
        """Hash of this observation under ``policy``.

        Two observations with the same fingerprint are "the same state" as far
        as the coverage map and the no-op detector are concerned. Note what is
        *never* hashed regardless of policy: the screenshot, the log delta and
        the network delta. Those are evidence, not identity -- a log line
        arriving must not make a state look new.
        """
        parts: list[str] = []
        if policy.include_screen_id:
            parts.append(f"screen={self.screen_id}")
        for el in sorted(self.elements, key=lambda e: e.identity):
            bits = [
                el.identity,
                f"enabled={int(el.enabled)}",
                f"selected={int(el.selected)}",
                f"focusable={int(el.focusable)}",
            ]
            if policy.include_element_text and el.text is not None:
                bits.append(f"text={policy.redact(el.text)}")
            if policy.include_bounds and el.bounds is not None:
                bits.append(f"bounds={','.join(str(b) for b in el.bounds)}")
            parts.append("|".join(bits))
        if policy.include_screen_text:
            parts.append(f"screen_text={policy.redact(self.text)}")
        blob = "\n".join(parts)
        return "sha1:" + hashlib.sha1(blob.encode("utf-8")).hexdigest()

    # -- lookups -----------------------------------------------------------
    def element(self, element_id: str) -> Element | None:
        for el in self.elements:
            if el.id == element_id:
                return el
        return None

    def by_identity(self, identity: str) -> Element | None:
        for el in self.elements:
            if el.identity == identity:
                return el
        return None

    def interactive(self) -> tuple[Element, ...]:
        return tuple(el for el in self.elements if el.interactive)

    @property
    def is_blind(self) -> bool:
        """True when the driver could supply no element tree.

        This is the vision-only degraded mode (ACTIVE_QA.md 10.3): several
        rulebook checks weaken because they need a tree, and the report has to
        say so rather than quietly present weaker results as equivalent.
        """
        return not self.elements

    def to_dict(self) -> dict[str, Any]:
        return {
            "screen_id": self.screen_id,
            "elements": [el.to_dict() for el in self.elements],
            "text": self.text,
            "screenshot": self.screenshot,
            "logs": list(self.logs),
            "net": [dict(n) for n in self.net],
            "meta": dict(self.meta),
            "captured_at": self.captured_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Observation":
        return cls(
            elements=tuple(Element.from_dict(e) for e in d.get("elements", [])),
            screen_id=d.get("screen_id", ""),
            text=d.get("text", ""),
            screenshot=d.get("screenshot"),
            logs=tuple(d.get("logs", [])),
            net=tuple(d.get("net", [])),
            meta=dict(d.get("meta", {})),
            captured_at=d.get("captured_at", 0.0),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    def with_elements(self, elements: Iterable[Element]) -> "Observation":
        return replace(self, elements=tuple(elements))


@dataclass(frozen=True, slots=True)
class Health:
    """Whether the app under test is still alive and usable."""

    ok: bool = True
    crashed: bool = False
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- The driver -------------------------------------------------------------


@runtime_checkable
class Driver(Protocol):
    """What every target kind must implement.

    Deliberately four operations. There is no ``checkpoint``/``restore`` pair:
    v1 runs one continuous session from a cold start and :meth:`relaunch` is the
    only reset (ACTIVE_QA.md 5.2). Adding snapshots later is additive; designing
    around them now would mean a per-target checkpoint format and a
    restore-fidelity problem, where a restored state that differs subtly from
    the real one yields findings that will not reproduce.
    """

    #: Short target kind, e.g. ``"web"``, ``"android"``, ``"roblox"``.
    kind: str

    #: How this target should be fingerprinted. Every driver declares one. It lives on the driver because
    #: ambient motion is a property of the *target*, not of the engine -- a web
    #: page is mostly still and its text is meaningful state, while a game world
    #: never stops moving and its on-screen text is half clocks and counters.
    #: One global policy cannot be right for both.
    fingerprint_policy: "FingerprintPolicy"

    def observe(self) -> Observation:
        """Capture the current state. Must not change it."""
        ...

    def act(self, action: Action) -> ActResult:
        """Carry out one action. Should not raise for an action that simply
        failed to land; report that in the result so the loop can continue."""
        ...

    def relaunch(self) -> None:
        """Return to a cold start: fresh process, fresh state, entry screen."""
        ...

    def health(self) -> Health:
        """Whether the target is alive. Called after every action."""
        ...


def fingerprint_policy_for(driver: Driver) -> FingerprintPolicy:
    """The policy a driver asks for, or the conservative default."""
    return getattr(driver, "fingerprint_policy", None) or DEFAULT_FINGERPRINT_POLICY


def supported_actions(driver: Driver) -> frozenset[str]:
    """Action kinds a driver claims to support.

    A driver may declare a narrower vocabulary than :data:`ACTION_KINDS` -- a
    touch-only Android target has no ``hover``, so rule U2 is not merely failing
    there, it is inapplicable. Absent a declaration, everything is assumed
    supported.
    """
    declared = getattr(driver, "actions", None)
    if not declared:
        return frozenset(ACTION_KINDS)
    return frozenset(declared) & ACTION_KINDS
