"""
pandabot_core.active_qa.fake
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
A scriptable in-memory :class:`~pandabot_core.active_qa.contract.Driver`.

The exploration loop, the coverage map and the rulebook detectors all need to be
testable without a browser, an emulator or Roblox Studio. :class:`FakeDriver` is
a tiny state machine that implements the full contract, so every layer above the
driver can be unit-tested deterministically and in milliseconds.

It is also where we can *construct the defects on purpose*. A fake screen with a
button wired to no transition is a dead affordance; one whose submit advances
regardless of input is a validation gap. That makes it possible to measure a
detector's precision against a known answer, which is exactly what the web
walking skeleton is meant to do for real (ACTIVE_QA.md 16) and what a unit test
can do cheaply first.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Callable

from pandabot_core.active_qa.contract import (
    DEFAULT_FINGERPRINT_POLICY,
    Action, ActResult, Driver, Element, Health, Observation,
)

__all__ = ["FakeScreen", "FakeDriver", "ambient_noise"]


# A transition maps (screen_id, element identity, action kind) to the next
# screen id. Returning None means "this action does nothing", which is how a
# dead control is modelled.
Transition = Callable[["FakeDriver", Action, Element], "str | None"]


@dataclass
class FakeScreen:
    """One state of the fake app."""

    screen_id: str
    elements: list[Element] = field(default_factory=list)
    text: str = ""
    #: ``{(element_identity, action_kind): next_screen_id}``. An entry mapping
    #: to the current screen is a real change of nothing; an *absent* entry is a
    #: no-op, which is what rule U7 should catch.
    transitions: dict[tuple[str, str], str] = field(default_factory=dict)
    #: Log lines emitted on arrival at this screen.
    logs: list[str] = field(default_factory=list)


class FakeDriver:
    """An in-memory app under test.

    >>> home = FakeScreen("home", [Element(id="e1", role="button", label="Go",
    ...                                    handle="#go")])
    >>> nxt = FakeScreen("next", [])
    >>> home.transitions[("h:#go", "click")] = "next"
    >>> d = FakeDriver({"home": home, "next": nxt}, entry="home")
    >>> d.act(Action(kind="click", target="e1")).ok
    True
    >>> d.observe().screen_id
    'next'
    """

    kind = "fake"
    #: The conservative default: bounds and screen text excluded. The fake is
    #: used to prove the ambient-motion guarantee, which is exactly what that
    #: policy exists for.
    fingerprint_policy = DEFAULT_FINGERPRINT_POLICY

    def __init__(
        self,
        screens: dict[str, FakeScreen],
        entry: str,
        *,
        actions: frozenset[str] | None = None,
        ambient: Callable[[int], str] | None = None,
        crash_after: int | None = None,
    ) -> None:
        if entry not in screens:
            raise ValueError(f"entry screen {entry!r} is not in screens")
        self.screens = screens
        self.entry = entry
        self.actions = actions          # read by contract.supported_actions
        #: Called with the observation counter to produce text that changes on
        #: every observe(), simulating a clock or a swaying boss. Used to prove
        #: the default fingerprint policy ignores ambient motion.
        self.ambient = ambient
        self.crash_after = crash_after

        self.current = entry
        self.relaunches = 0
        self.act_count = 0
        self._observe_count = itertools.count()
        self._pending_logs: list[str] = list(screens[entry].logs)
        #: Text typed into each element, keyed by identity. Lets a fake echo
        #: input back (rule U6 passes) or swallow it (U6 fails).
        self.typed: dict[str, str] = {}
        self.echo_typing = True
        self.crashed = False

    # -- contract ----------------------------------------------------------
    def observe(self) -> Observation:
        screen = self.screens[self.current]
        n = next(self._observe_count)
        elements = []
        for el in screen.elements:
            if self.echo_typing and el.identity in self.typed:
                el = Element(**{**el.to_dict(), "text": self.typed[el.identity],
                                "bounds": el.bounds})
            elements.append(el)
        logs = tuple(self._pending_logs)
        self._pending_logs.clear()
        text = screen.text
        if self.ambient is not None:
            text = f"{text} {self.ambient(n)}".strip()
        return Observation(
            elements=tuple(elements),
            screen_id=screen.screen_id,
            text=text,
            logs=logs,
            meta={"observe_index": n},
        )

    def act(self, action: Action) -> ActResult:
        if self.crashed:
            return ActResult(ok=False, error="app has crashed")
        self.act_count += 1
        if self.crash_after is not None and self.act_count > self.crash_after:
            self.crashed = True
            self._pending_logs.append("FATAL EXCEPTION: simulated crash")
            return ActResult(ok=False, error="app has crashed")

        if action.kind == "wait":
            return ActResult(ok=True, duration_ms=int(action.value or 0))
        if action.kind == "back":
            self.current = self.entry
            return ActResult(ok=True)

        obs = self.observe()
        el = obs.element(action.target) if action.target else None
        if el is None:
            return ActResult(ok=False, error=f"no such element: {action.target}")
        if not el.enabled and action.kind in ("click", "type", "press_hold"):
            # A disabled control that silently accepts input is rule U5's
            # failure; a fake that refuses is the correct baseline.
            return ActResult(ok=False, error="element is disabled")

        if action.kind == "type":
            self.typed[el.identity] = str(action.value)

        screen = self.screens[self.current]
        nxt = screen.transitions.get((el.identity, action.kind))
        if nxt is not None and nxt in self.screens:
            self.current = nxt
            self._pending_logs.extend(self.screens[nxt].logs)
        return ActResult(ok=True)

    def relaunch(self) -> None:
        self.relaunches += 1
        self.current = self.entry
        self.typed.clear()
        self.crashed = False
        self.act_count = 0
        self._pending_logs = list(self.screens[self.entry].logs)

    def health(self) -> Health:
        if self.crashed:
            return Health(ok=False, crashed=True, message="simulated crash")
        return Health(ok=True)


def ambient_noise(n: int) -> str:
    """A stand-in for a world that is never still.

    Feed this as ``FakeDriver(ambient=ambient_noise)`` to prove that the default
    fingerprint policy does not treat a ticking clock as a state change. With
    ``STRICT_FINGERPRINT_POLICY`` the same driver produces a new state on every
    observation, which is the bug this guards against.
    """
    return f"clock {n // 60:02d}:{n % 60:02d} boss sway {n}"


# Structural check: the fake really does satisfy the protocol.
_: type[Driver] = FakeDriver  # type: ignore[assignment]
