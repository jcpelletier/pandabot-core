"""
pandabot_core.active_qa.strategies
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The strategy deck: what to try when plain exploration stops finding anything.

A run has a minimum operating time because the brief is explicitly "keep
pressing and trying alternatives" (ACTIVE_QA.md 5). But a loop that has clicked
every button on every reachable screen has genuinely run out of *exploration*
long before it has run out of *time*. The deck is what it does with the rest:
adversarial input, awkward sequencing, and the things a user does that a happy
path never covers.

Cards are drawn when novelty stalls, one at a time, and each re-seeds the
frontier with a class of candidates. Almost all of them are pure functions of
the current observation, so playing a card costs no model call -- which is what
makes a long run affordable.

A card that cannot apply to a state simply yields nothing, and the deck moves
on. Cards needing machinery that does not exist yet (network denial, permission
refusal) are declared with ``implemented=False`` for the same reason the rulebook
declares its pending rules: so a report can say what was actually exercised.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from pandabot_core.active_qa.contract import Element, Observation
from pandabot_core.active_qa.coverage import Candidate

__all__ = [
    "Card", "DECK", "Deck", "card_names", "implemented_cards",
    "boundary_values", "empty_and_blank", "long_and_weird", "rapid_fire",
    "keyboard_only", "idle", "interruption",
]


_TEXT_ROLES = frozenset({"textbox", "input", "combobox"})


def _fields(obs: Observation) -> list[Element]:
    return [e for e in obs.interactive() if e.role in _TEXT_ROLES and e.enabled]


def _buttons(obs: Observation) -> list[Element]:
    return [e for e in obs.interactive() if e.role not in _TEXT_ROLES and e.enabled]


def _typed(state: str, el: Element, value: str, card: str, why: str) -> Candidate:
    return Candidate(state=state, element_identity=el.identity, kind="type",
                     value=value, reason=f"{card}: {why}", source=card)


# --- The cards --------------------------------------------------------------


def boundary_values(obs: Observation, state: str) -> list[Candidate]:
    """Numbers at and just past the edges, where off-by-one lives."""
    values = [
        ("0", "zero"),
        ("-1", "negative"),
        ("0.0001", "fractional"),
        (str(sys.maxsize), "very large"),
        (str(sys.maxsize + 1), "past 64-bit"),
        ("-" + str(sys.maxsize), "very negative"),
    ]
    return [_typed(state, el, v, "boundary values", why)
            for el in _fields(obs) for v, why in values]


def empty_and_blank(obs: Observation, state: str) -> list[Candidate]:
    """The commonest missing validation of all: submitting nothing."""
    values = [("", "empty"), (" ", "single space"), ("   \t  ", "whitespace only")]
    out = [_typed(state, el, v, "empty and blank", why)
           for el in _fields(obs) for v, why in values]
    # Blanking a field is only half the experiment; the other half is trying to
    # submit afterwards, which is where the validation gap actually shows.
    out += [Candidate(state=state, element_identity=el.identity, kind="click",
                      reason="empty and blank: submit with the field cleared",
                      source="empty and blank")
            for el in _buttons(obs)]
    return out


def long_and_weird(obs: Observation, state: str) -> list[Candidate]:
    """Text that is longer, wider or more structured than anyone planned for."""
    values = [
        ("x" * 10_000, "10k characters"),
        ("🐼🎮🔥 emoji ⚡️", "emoji and astral characters"),
        ("مرحبا שלום", "right-to-left script"),
        ("<script>alert(1)</script>", "markup injection"),
        ("'; DROP TABLE users;--", "SQL-shaped input"),
        ("line one\nline two\r\nline three", "embedded newlines"),
        ("   padded   ", "leading and trailing whitespace"),
        ("../../etc/passwd", "path traversal shape"),
        ("%s %d {0} ${x}", "format-string shape"),
    ]
    return [_typed(state, el, v, "long and weird", why)
            for el in _fields(obs) for v, why in values]


def rapid_fire(obs: Observation, state: str, *, repeats: int = 4) -> list[Candidate]:
    """Double-submit, double-charge, double-navigate.

    Each repeat is a distinct candidate (the value differs), so the frontier
    does not collapse them into one already-tried entry.
    """
    return [Candidate(state=state, element_identity=el.identity, kind="click",
                      value={"repeat": n},
                      reason=f"rapid fire: press {n} of {repeats} in quick succession",
                      source="rapid fire")
            for el in _buttons(obs) for n in range(2, repeats + 1)]


def keyboard_only(obs: Observation, state: str) -> list[Candidate]:
    """Traverse by Tab and Enter alone.

    Doubles as the keyboard half of the WCAG pack: everything interactive must
    be reachable (2.1.1), focus must be visible (2.4.7), and there must be no
    trap (2.1.2).
    """
    out: list[Candidate] = []
    for el in obs.interactive():
        out.append(Candidate(state=state, element_identity=el.identity, kind="focus",
                             reason="keyboard only: is this reachable by keyboard",
                             source="keyboard only"))
        out.append(Candidate(state=state, element_identity=el.identity, kind="key",
                             value="Enter",
                             reason="keyboard only: activate without a pointer",
                             source="keyboard only"))
    return out


def idle(obs: Observation, state: str, *, seconds: int = 60) -> list[Candidate]:
    """Leave it alone and watch. Timers, leaks, spurious state changes."""
    if not obs.elements:
        return []
    return [Candidate(state=state, element_identity="", kind="wait",
                      value=seconds * 1000,
                      reason=f"idle: leave the screen untouched for {seconds}s",
                      source="idle")]


def interruption(obs: Observation, state: str) -> list[Candidate]:
    """Back out mid-flow and come back. Where half-finished state gets lost."""
    if not obs.elements:
        return []
    return [Candidate(state=state, element_identity="", kind="back",
                      reason="interruption: leave mid-flow, then return",
                      source="interruption")]


# --- The deck ---------------------------------------------------------------

CardFn = Callable[[Observation, str], list[Candidate]]


@dataclass(frozen=True, slots=True)
class Card:
    name: str
    fn: CardFn | None
    description: str
    implemented: bool = True

    def deal(self, obs: Observation, state: str) -> list[Candidate]:
        if self.fn is None:
            return []
        return self.fn(obs, state)


DECK: tuple[Card, ...] = (
    Card("empty and blank", empty_and_blank,
         "Submit empty, whitespace-only, and blanked fields"),
    Card("boundary values", boundary_values,
         "Zero, negative, fractional and past-64-bit numbers"),
    Card("long and weird", long_and_weird,
         "10k characters, emoji, RTL, markup, SQL shapes, newlines"),
    Card("rapid fire", rapid_fire,
         "Double- and triple-press, testing idempotency"),
    Card("interruption", interruption,
         "Leave mid-flow and return; state should survive"),
    Card("keyboard only", keyboard_only,
         "Traverse by Tab and Enter alone; also feeds the WCAG pack"),
    Card("idle", idle,
         "Leave the screen untouched and watch for timers and drift"),
    # Declared, not built. Each needs a driver capability that does not exist
    # yet, and saying so is better than a card that silently deals nothing.
    Card("denial", None,
         "Deny a permission or kill the network mid-flow",
         implemented=False),
    Card("round trip", None,
         "Save, quit, relaunch, and compare state",
         implemented=False),
    Card("out of order", None,
         "Reach a screen by a route the happy path does not use",
         implemented=False),
)


def card_names() -> list[str]:
    return [c.name for c in DECK]


def implemented_cards() -> list[Card]:
    return [c for c in DECK if c.implemented]


class Deck:
    """Deals cards one at a time, in order, without repeating within a state.

    Order is deliberate rather than random: the cheapest and highest-yield cards
    come first (empty input finds more bugs per action than emoji does), and a
    deterministic order makes a run reproducible, which matters when the point
    of the exercise is to measure a false-positive rate.
    """

    def __init__(self, cards: Sequence[Card] | None = None) -> None:
        # None means "the standard deck"; an empty sequence means "no cards".
        # Conflating those (via a falsy default) silently handed the full deck
        # to a caller that asked for none.
        self.cards = tuple(implemented_cards()) if cards is None else tuple(cards)
        #: (state fingerprint, card name) pairs already played.
        self._played: set[tuple[str, str]] = set()

    def next_card(self, state: str) -> Card | None:
        """The next unplayed card for this state, or None when the deck is out."""
        for card in self.cards:
            if (state, card.name) not in self._played:
                return card
        return None

    def play(self, obs: Observation, state: str) -> tuple[Card | None, list[Candidate]]:
        """Draw and deal the next card that actually produces work.

        Skips past cards that yield nothing here (no text fields, so no boundary
        values) so that a stall is not wasted on an inapplicable card, and marks
        every card it passes as played so the deck does drain.
        """
        while True:
            card = self.next_card(state)
            if card is None:
                return None, []
            self._played.add((state, card.name))
            candidates = card.deal(obs, state)
            if candidates:
                return card, candidates

    def exhausted(self, state: str) -> bool:
        return self.next_card(state) is None

    def played_cards(self) -> list[str]:
        """Distinct cards played anywhere this session, for the report."""
        return sorted({name for _, name in self._played})

    def reset(self) -> None:
        self._played.clear()
