"""
pandabot_core.active_qa.coverage
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The coverage map and the frontier queue: what the loop has seen, and what it
should try next.

This is the part that makes an hour of exploration actually explore. The naive
design -- ask the model what to click, repeat -- fails twice over: it revisits
the same three screens forever, and at ~30 tok/s a model call per click buys
only ~140 actions an hour, which is not exploration (ACTIVE_QA.md 5 and 8).

So the loop is **policy-driven with the model as an occasional advisor**. This
module is the policy. It keeps a state graph keyed on observation fingerprints,
tracks which (state, element, action) triples have been tried, and hands back
the next thing to do. The model is consulted only when a genuinely new state
appears, and its opinion enters as a *curiosity* score on candidates -- one
term in a priority, not the decision itself.

Priority is deliberately simple and inspectable::

    priority = novelty + curiosity - visit_penalty

``novelty`` rewards untouched elements and actions that have historically led
somewhere new; ``curiosity`` is the model's score for the candidate, if it gave
one; ``visit_penalty`` grows with how often this state has been worked, which is
what stops the loop grinding one screen. Everything is a plain number so a
report can explain why a run went where it did.
"""

from __future__ import annotations

import heapq
import itertools
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Iterator, Sequence

from pandabot_core.active_qa.contract import (
    ACTION_KINDS, Action, Element, Observation,
)

__all__ = [
    "Candidate", "StateNode", "Edge", "CoverageMap", "Frontier",
    "PriorityWeights", "DEFAULT_WEIGHTS", "default_candidates",
]


# --- Candidates -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """One thing that could be tried in one state."""

    state: str                  # fingerprint of the state it applies to
    element_identity: str       # stable across observations, unlike Element.id
    kind: str                   # action kind
    value: Any = None
    reason: str = ""            # carried into Action.reason, then the report
    curiosity: float = 0.0      # the model's interest score, 0 if it never saw it
    #: Which strategy card seeded this, if any. Lets the report say "found by
    #: boundary values" and lets the frontier avoid replaying a card twice.
    source: str = "explore"

    @property
    def key(self) -> tuple[str, str, str, str]:
        """Identity for "have I already tried this"."""
        return (self.state, self.element_identity, self.kind, _value_key(self.value))

    def to_action(self, observation: Observation) -> Action | None:
        """Bind to a concrete element in ``observation``, or None if it is gone.

        Elements are addressed by :attr:`Candidate.element_identity`, which is
        stable across observations; the per-observation ``Element.id`` an action
        needs is only resolved at the moment of acting. A candidate whose
        element has vanished simply drops.
        """
        el = observation.by_identity(self.element_identity)
        if el is None:
            return None
        return Action(kind=self.kind, target=el.id, value=self.value,
                      reason=self.reason or self.source)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _value_key(value: Any) -> str:
    if value is None:
        return ""
    return json.dumps(value, sort_keys=True, default=str)


# --- The map ----------------------------------------------------------------


@dataclass
class StateNode:
    """One distinct state, keyed by observation fingerprint."""

    fingerprint: str
    screen_id: str = ""
    visits: int = 0
    first_seen_step: int = -1
    #: Element identities seen in this state, and how often each was acted on.
    element_touches: dict[str, int] = field(default_factory=dict)
    #: True once the model has oriented here (so we never pay for it twice).
    oriented: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Edge:
    """An observed transition: acting in one state landed in another."""

    src: str
    kind: str
    element_identity: str
    dst: str

    @property
    def is_self_loop(self) -> bool:
        """Nothing observably changed. The primitive rule U7 builds on."""
        return self.src == self.dst


class CoverageMap:
    """The state graph, plus what has been tried where.

    Deliberately does no detection of its own. It answers "is this new", "what
    have I not tried here", and "where should I go to find open work" -- the
    detectors and the report read from it, but the judgement lives elsewhere.
    """

    def __init__(self) -> None:
        self.states: dict[str, StateNode] = {}
        self.edges: list[Edge] = []
        self._tried: set[tuple[str, str, str, str]] = set()
        #: How often each action kind produced a new state anywhere in the run.
        #: Feeds novelty: on an app where typing opens doors and scrolling never
        #: does, the loop should notice without being told.
        self._kind_payoff: dict[str, list[int]] = {}

    # -- observing ---------------------------------------------------------
    def observe(self, observation: Observation, fingerprint: str,
                step: int = -1) -> tuple[StateNode, bool]:
        """Record a visit. Returns the node and whether it is newly discovered."""
        node = self.states.get(fingerprint)
        is_new = node is None
        if node is None:
            node = StateNode(fingerprint=fingerprint,
                             screen_id=observation.screen_id,
                             first_seen_step=step)
            self.states[fingerprint] = node
        node.visits += 1
        for el in observation.interactive():
            node.element_touches.setdefault(el.identity, 0)
        return node, is_new

    def record_transition(self, src: str, candidate: Candidate, dst: str) -> Edge:
        """Record that acting on ``candidate`` in ``src`` landed in ``dst``."""
        edge = Edge(src=src, kind=candidate.kind,
                    element_identity=candidate.element_identity, dst=dst)
        self.edges.append(edge)
        self._tried.add(candidate.key)
        node = self.states.get(src)
        if node is not None:
            node.element_touches[candidate.element_identity] = (
                node.element_touches.get(candidate.element_identity, 0) + 1
            )
        payoff = self._kind_payoff.setdefault(candidate.kind, [0, 0])
        payoff[1] += 1
        if src != dst:
            payoff[0] += 1
        return edge

    # -- querying ----------------------------------------------------------
    def has_tried(self, candidate: Candidate) -> bool:
        return candidate.key in self._tried

    def visits(self, fingerprint: str) -> int:
        node = self.states.get(fingerprint)
        return node.visits if node else 0

    def touches(self, fingerprint: str, element_identity: str) -> int:
        node = self.states.get(fingerprint)
        if node is None:
            return 0
        return node.element_touches.get(element_identity, 0)

    def kind_payoff(self, kind: str) -> float:
        """Fraction of this action kind's attempts that changed state.

        Starts optimistic (an untried kind scores 1.0) so every kind gets tried
        before any is written off.
        """
        changed, total = self._kind_payoff.get(kind, [0, 0])
        if total == 0:
            return 1.0
        return changed / total

    def dead_ends(self) -> list[str]:
        """States with no observed edge leading anywhere else. Rule U12."""
        escapes: dict[str, bool] = {f: False for f in self.states}
        for e in self.edges:
            if not e.is_self_loop:
                escapes[e.src] = True
        return sorted(f for f, ok in escapes.items() if not ok)

    def route_to_open_work(self, frm: str, open_states: Iterable[str]) -> list[Edge] | None:
        """Shortest observed path from ``frm`` to any state with open frontier.

        With no checkpoints (ACTIVE_QA.md 5.2), the only ways to reach a state
        are to walk there or to relaunch and replay. This finds the walk. None
        means there is no known route, and the caller should relaunch instead.
        """
        targets = {s for s in open_states if s in self.states}
        if not targets:
            return None
        if frm in targets:
            return []
        adjacency: dict[str, list[Edge]] = {}
        for e in self.edges:
            if not e.is_self_loop:
                adjacency.setdefault(e.src, []).append(e)

        queue: list[tuple[str, list[Edge]]] = [(frm, [])]
        seen = {frm}
        while queue:
            node, path = queue.pop(0)
            for edge in adjacency.get(node, []):
                if edge.dst in seen:
                    continue
                new_path = path + [edge]
                if edge.dst in targets:
                    return new_path
                seen.add(edge.dst)
                queue.append((edge.dst, new_path))
        return None

    def stats(self) -> dict[str, Any]:
        return {
            "states": len(self.states),
            "transitions": len(self.edges),
            "self_loops": sum(1 for e in self.edges if e.is_self_loop),
            "tried": len(self._tried),
            "screens": len({n.screen_id for n in self.states.values() if n.screen_id}),
            "dead_ends": len(self.dead_ends()),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "states": {f: n.to_dict() for f, n in self.states.items()},
            "edges": [asdict(e) for e in self.edges],
            "tried": [list(k) for k in sorted(self._tried)],
            "kind_payoff": {k: list(v) for k, v in self._kind_payoff.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CoverageMap":
        m = cls()
        for f, n in d.get("states", {}).items():
            m.states[f] = StateNode(**n)
        m.edges = [Edge(**e) for e in d.get("edges", [])]
        m._tried = {tuple(k) for k in d.get("tried", [])}  # type: ignore[misc]
        m._kind_payoff = {k: list(v) for k, v in d.get("kind_payoff", {}).items()}
        return m


# --- Priority ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PriorityWeights:
    """Knobs on the frontier's ordering. Tuned by measurement, not taste."""

    untouched_element: float = 3.0
    kind_payoff: float = 1.0
    curiosity: float = 2.0
    element_touch_penalty: float = 1.5
    state_visit_penalty: float = 0.25
    #: Adversarial cards should not outrank plain exploration of a fresh screen;
    #: they exist for when novelty has stalled. Slightly negative keeps them
    #: available but behind anything genuinely new.
    strategy_bias: float = -0.5


DEFAULT_WEIGHTS = PriorityWeights()


class Frontier:
    """The queue of untried work, ordered by priority.

    A max-heap over :class:`Candidate` s. Priority is recomputed at push time
    from the coverage map, which means an entry can go stale as the map moves
    underneath it; :meth:`pop` therefore re-checks that a candidate is still
    untried and still worth doing, and drops it if not. That is cheaper and far
    simpler than keeping the heap perfectly ordered, and the ordering only has
    to be good, not optimal.
    """

    def __init__(self, coverage: CoverageMap,
                 weights: PriorityWeights = DEFAULT_WEIGHTS) -> None:
        self.coverage = coverage
        self.weights = weights
        self._heap: list[tuple[float, int, Candidate]] = []
        self._queued: set[tuple[str, str, str, str]] = set()
        self._counter = itertools.count()

    # -- scoring -----------------------------------------------------------
    def priority(self, candidate: Candidate) -> float:
        w = self.weights
        touches = self.coverage.touches(candidate.state, candidate.element_identity)
        visits = self.coverage.visits(candidate.state)
        score = 0.0
        if touches == 0:
            score += w.untouched_element
        score += w.kind_payoff * self.coverage.kind_payoff(candidate.kind)
        score += w.curiosity * candidate.curiosity
        score -= w.element_touch_penalty * touches
        score -= w.state_visit_penalty * max(visits - 1, 0)
        if candidate.source != "explore":
            score += w.strategy_bias
        return score

    # -- queue -------------------------------------------------------------
    def push(self, candidate: Candidate) -> bool:
        """Queue a candidate. Returns False if it was already tried or queued."""
        if self.coverage.has_tried(candidate) or candidate.key in self._queued:
            return False
        heapq.heappush(
            self._heap,
            (-self.priority(candidate), next(self._counter), candidate),
        )
        self._queued.add(candidate.key)
        return True

    def extend(self, candidates: Iterable[Candidate]) -> int:
        return sum(1 for c in candidates if self.push(c))

    def pop(self, state: str | None = None) -> Candidate | None:
        """Highest-priority candidate, optionally restricted to one state.

        Restricting to a state is the normal case: with no checkpoints we can
        only act where we already are. Passing None is for a caller that has
        just walked somewhere and wants whatever is best globally.
        """
        deferred: list[tuple[float, int, Candidate]] = []
        result: Candidate | None = None
        while self._heap:
            entry = heapq.heappop(self._heap)
            candidate = entry[2]
            self._queued.discard(candidate.key)
            if self.coverage.has_tried(candidate):
                continue
            if state is not None and candidate.state != state:
                deferred.append(entry)
                continue
            result = candidate
            break
        for entry in deferred:
            heapq.heappush(self._heap, entry)
            self._queued.add(entry[2].key)
        return result

    def open_states(self) -> set[str]:
        """States that still have queued work. Feeds :meth:`CoverageMap.route_to_open_work`."""
        return {c.state for _, _, c in self._heap
                if not self.coverage.has_tried(c)}

    def has_work(self, state: str | None = None) -> bool:
        if state is None:
            return any(not self.coverage.has_tried(c) for _, _, c in self._heap)
        return any(c.state == state and not self.coverage.has_tried(c)
                   for _, _, c in self._heap)

    def drop_state(self, state: str) -> int:
        """Forget queued work for one state (e.g. it proved unreachable)."""
        kept = [e for e in self._heap if e[2].state != state]
        dropped = len(self._heap) - len(kept)
        self._heap = kept
        heapq.heapify(self._heap)
        self._queued = {e[2].key for e in self._heap}
        return dropped

    def __len__(self) -> int:
        return len(self._heap)

    def __iter__(self) -> Iterator[Candidate]:
        """Candidates in priority order. Does not consume the queue."""
        return iter([c for _, _, c in sorted(self._heap)])


# --- Seeding ----------------------------------------------------------------


def default_candidates(
    observation: Observation,
    fingerprint: str,
    *,
    supported: Iterable[str] = ACTION_KINDS,
    probe_text: str = "QA probe",
) -> list[Candidate]:
    """The baseline things worth trying in a state, with no model involved.

    This is what the loop seeds a new state with before (or instead of) asking
    the model to orient. It covers plain interaction plus the captures the
    rulebook needs: ``press_hold`` for U1, ``hover`` for U2, ``focus`` for U3,
    and a typed probe for U6. A driver that does not support a kind simply does
    not get those candidates, which is how a touch-only target ends up with U2
    marked inapplicable rather than failing.
    """
    supported = frozenset(supported)
    out: list[Candidate] = []
    for el in observation.interactive():
        def add(kind: str, *, value: Any = None, reason: str = "") -> None:
            if kind in supported:
                out.append(Candidate(state=fingerprint, element_identity=el.identity,
                                     kind=kind, value=value, reason=reason))

        if el.role in ("textbox", "input", "combobox"):
            add("type", value=probe_text, reason="does the field echo input (U6)")
            add("focus", reason="focus indicator (U3)")
        else:
            add("click", reason="explore")
            add("press_hold", reason="pressed state (U1)")
            add("hover", reason="hover state (U2)")
            if el.focusable:
                add("focus", reason="focus indicator (U3)")
    return out
