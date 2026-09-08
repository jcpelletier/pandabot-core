"""
pandabot_core.active_qa.recorder
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The full-session action recorder.

This module exists because of one decision in ``ACTIVE_QA.md`` 5.2: **v1 has no
checkpoint/restore.** With snapshots, a candidate finding's trace could start at
a checkpoint near the failure, and confirming it would mean restoring and
replaying three actions. Without them, the only way back to a known state is a
cold start, so **a trace that does not begin at launch cannot be replayed at
all.**

That makes complete recording a correctness requirement rather than a
convenience: the recorder captures every action for the whole session, tracks
where each cold start began, and slices a candidate's replayable trace out of
the run. Get this wrong and findings cannot be confirmed, which is the one
thing that keeps the report credible.

A session is a sequence of :class:`Step` s divided into **epochs**. An epoch
starts at a cold start (the first launch, or any :meth:`Driver.relaunch`). The
replayable trace for a step is every step from the start of *its* epoch up to
and including it -- replaying that from a cold start reproduces the state the
step ran against.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator, Sequence

from pandabot_core.active_qa.contract import Action, ActResult

__all__ = ["Step", "Trace", "SessionRecorder", "humanise_identity"]


def humanise_identity(identity: str) -> str:
    """Turn a stable element identity back into something readable.

    ``h:[data-testid="dead-1"]`` becomes ``dead-1``; ``r:button|l:Confirm``
    becomes ``Confirm``. Purely for the report -- identity itself stays exact.
    """
    if not identity:
        return ""
    if identity.startswith("r:") and "|l:" in identity:
        label = identity.split("|l:", 1)[1]
        return label or identity
    if identity.startswith("r:") and "|b:" in identity:
        return f"the {identity[2:].split('|', 1)[0]} at "                f"({identity.split('|b:', 1)[1]})"
    if identity.startswith("h:"):
        handle = identity[2:]
        for quote in ('"', "'"):
            if quote in handle:
                inner = handle.split(quote)[1]
                if inner:
                    return inner
        return handle.lstrip("#.")
    return identity


def _describe(step: "Step") -> str:
    """One repro line for a step, naming the control rather than its id."""
    action = step.action
    who = humanise_identity(step.element_identity)
    verb = action.kind.replace("_", " ")
    if action.kind == "wait":
        return f"wait {action.value}ms"
    if action.kind == "back":
        return "go back"
    target = f' "{who}"' if who else (f" {action.target}" if action.target else "")
    if action.kind == "type":
        value = str(action.value)
        shown = value if len(value) <= 40 else value[:37] + "..."
        return f"type {shown!r} into{target}" if target else f"type {shown!r}"
    return f"{verb}{target}"


@dataclass(frozen=True, slots=True)
class Step:
    """One recorded action and what it did."""

    index: int          # position in the whole session, 0-based, never reused
    epoch: int          # which cold start this step belongs to
    action: Action
    before_fingerprint: str = ""
    after_fingerprint: str = ""
    result: ActResult = field(default_factory=ActResult)
    before_screenshot: str | None = None
    after_screenshot: str | None = None
    logs: tuple[str, ...] = ()          # log delta produced by this action
    screen_id: str = ""                 # screen the action was taken on
    #: Stable identity of the element acted on. Action.target is a
    #: per-observation id, so replay has to re-resolve the element by identity
    #: against a fresh observation -- without this a trace is not replayable at
    #: all, which makes it useless as evidence.
    element_identity: str = ""
    duration_ms: int = 0

    @property
    def changed(self) -> bool:
        """Whether the observed state moved at all.

        The primitive behind rule U7 (no button with no understood output). It
        is deliberately fingerprint-only: what counts as a change is decided by
        the :class:`~pandabot_core.active_qa.contract.FingerprintPolicy` used to
        compute the fingerprints, not re-litigated here.
        """
        return self.before_fingerprint != self.after_fingerprint

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["action"] = self.action.to_dict()
        d["result"] = self.result.to_dict()
        d["logs"] = list(self.logs)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Step":
        return cls(
            index=d["index"],
            epoch=d["epoch"],
            action=Action.from_dict(d["action"]),
            before_fingerprint=d.get("before_fingerprint", ""),
            after_fingerprint=d.get("after_fingerprint", ""),
            result=ActResult(**d.get("result", {})),
            before_screenshot=d.get("before_screenshot"),
            after_screenshot=d.get("after_screenshot"),
            logs=tuple(d.get("logs", [])),
            screen_id=d.get("screen_id", ""),
            element_identity=d.get("element_identity", ""),
            duration_ms=d.get("duration_ms", 0),
        )


@dataclass(frozen=True, slots=True)
class Trace:
    """A replayable action sequence, always starting from a cold start.

    Hand this to a fresh driver, replay :attr:`actions` in order, and you are in
    the state the finding was observed in. It is also what lands in the evidence
    bundle as ``AQ-<n>-trace.json``.
    """

    epoch: int
    steps: tuple[Step, ...]
    #: Index (into :attr:`steps`) of the step this trace was cut for -- the one
    #: that produced the candidate. Always the last step.
    target_index: int

    @property
    def actions(self) -> tuple[Action, ...]:
        return tuple(s.action for s in self.steps)

    @property
    def target(self) -> Step:
        return self.steps[self.target_index]

    def __len__(self) -> int:
        return len(self.steps)

    #: Interactions the harness performs to measure a control's appearance, not
    #: things a user does. They belong in the trace (a faithful replay needs
    #: them) and never in the repro steps.
    PROBE_KINDS = frozenset({"press_hold", "hover", "focus"})

    def repro_steps(self, *, include_probes: bool = False) -> list[str]:
        """Numbered steps a person can actually follow.

        Two things separate this from a dump of the trace. Probe actions are
        dropped, because "hover on e1" is the harness measuring a hover state,
        not a step in reproducing a bug. And controls are named by their label
        rather than by the per-observation id they happened to have, because
        "click on e2" tells a reader nothing.

        Runs of identical consecutive actions collapse ("press Confirm x4"), so
        a spam-submit finding does not produce forty identical lines.
        """
        lines: list[str] = []
        run_desc: str | None = None
        run_count = 0

        def flush() -> None:
            if run_desc is None:
                return
            suffix = f" (x{run_count})" if run_count > 1 else ""
            lines.append(f"{len(lines) + 1}. {run_desc}{suffix}")

        for step in self.steps:
            if not include_probes and step.action.kind in self.PROBE_KINDS:
                continue
            desc = _describe(step)
            if desc == run_desc:
                run_count += 1
                continue
            flush()
            run_desc, run_count = desc, 1
        flush()
        return lines

    def rebind(self, index: int, observation: Any) -> Any | None:
        """Resolve step ``index``'s action against a fresh observation.

        Element ids are only meaningful inside the observation that produced
        them, so replaying a recorded action verbatim would target whatever
        happens to be ``e7`` this time round. Re-resolving by identity is what
        makes a trace replayable at all; None means the element is gone and the
        replay has diverged from the recorded run.
        """
        from pandabot_core.active_qa.contract import Action

        step = self.steps[index]
        if not step.action.target:
            return step.action
        if not step.element_identity:
            return None
        el = observation.by_identity(step.element_identity)
        if el is None:
            return None
        return Action(kind=step.action.kind, target=el.id,
                      value=step.action.value, reason=step.action.reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch,
            "target_index": self.target_index,
            "steps": [s.to_dict() for s in self.steps],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Trace":
        return cls(
            epoch=d["epoch"],
            steps=tuple(Step.from_dict(s) for s in d["steps"]),
            target_index=d["target_index"],
        )


class SessionRecorder:
    """Records every action of one exploration session.

    Usage mirrors the loop: call :meth:`mark_launch` at the cold start and after
    every relaunch, then :meth:`record` for each action. :meth:`trace_for` cuts
    the replayable trace for any step, which is what triage confirms against and
    what the report ships as evidence.
    """

    def __init__(self, session_id: str = "") -> None:
        self.session_id = session_id
        self._steps: list[Step] = []
        self._epoch = -1  # no launch yet; mark_launch() moves this to 0

    # -- recording ---------------------------------------------------------
    def mark_launch(self) -> int:
        """Begin a new epoch. Call once at the cold start, then after every
        :meth:`~pandabot_core.active_qa.contract.Driver.relaunch`."""
        self._epoch += 1
        return self._epoch

    def record(
        self,
        action: Action,
        *,
        before_fingerprint: str = "",
        after_fingerprint: str = "",
        result: ActResult | None = None,
        before_screenshot: str | None = None,
        after_screenshot: str | None = None,
        logs: Sequence[str] = (),
        screen_id: str = "",
        duration_ms: int = 0,
        element_identity: str = "",
    ) -> Step:
        """Append one step. Returns it, so the caller can hold the index."""
        if self._epoch < 0:
            raise RuntimeError(
                "mark_launch() must be called before the first record(): a trace "
                "that does not begin at a cold start cannot be replayed"
            )
        step = Step(
            index=len(self._steps),
            epoch=self._epoch,
            action=action,
            before_fingerprint=before_fingerprint,
            after_fingerprint=after_fingerprint,
            result=result or ActResult(),
            before_screenshot=before_screenshot,
            after_screenshot=after_screenshot,
            logs=tuple(logs),
            screen_id=screen_id,
            duration_ms=duration_ms,
            element_identity=element_identity,
        )
        self._steps.append(step)
        return step

    # -- reading -----------------------------------------------------------
    @property
    def steps(self) -> tuple[Step, ...]:
        return tuple(self._steps)

    @property
    def epoch(self) -> int:
        return self._epoch

    def __len__(self) -> int:
        return len(self._steps)

    def __iter__(self) -> Iterator[Step]:
        return iter(self._steps)

    def epoch_steps(self, epoch: int) -> tuple[Step, ...]:
        return tuple(s for s in self._steps if s.epoch == epoch)

    def trace_for(self, index: int) -> Trace:
        """The replayable trace for the step at ``index``.

        Every step from the start of that step's epoch up to and including it.
        Replaying these actions against a cold-started driver reproduces the
        state the step ran against -- which is the whole point of recording from
        launch rather than from a checkpoint.
        """
        if not 0 <= index < len(self._steps):
            raise IndexError(f"no step at index {index}")
        target = self._steps[index]
        steps = tuple(
            s for s in self._steps if s.epoch == target.epoch and s.index <= index
        )
        return Trace(epoch=target.epoch, steps=steps, target_index=len(steps) - 1)

    def trace_for_step(self, step: Step) -> Trace:
        return self.trace_for(step.index)

    def duration_ms(self) -> int:
        return sum(s.duration_ms for s in self._steps)

    def stats(self) -> dict[str, Any]:
        """Session counters for the report header."""
        return {
            "session_id": self.session_id,
            "actions": len(self._steps),
            "epochs": self._epoch + 1,
            "relaunches": max(self._epoch, 0),
            "screens": len({s.screen_id for s in self._steps if s.screen_id}),
            "no_change_actions": sum(1 for s in self._steps if not s.changed),
            "failed_actions": sum(1 for s in self._steps if not s.result.ok),
            "duration_ms": self.duration_ms(),
        }

    # -- persistence -------------------------------------------------------
    # The session survives a pause (ACTIVE_QA.md 8.2: the model can be unloaded
    # out from under a run, and the loop pauses rather than crashing). Resuming
    # needs the trace back, so the recorder round-trips through JSON.
    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "epoch": self._epoch,
            "steps": [s.to_dict() for s in self._steps],
        }

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRecorder":
        rec = cls(session_id=d.get("session_id", ""))
        rec._steps = [Step.from_dict(s) for s in d.get("steps", [])]
        rec._epoch = d.get("epoch", -1)
        return rec

    @classmethod
    def from_json(cls, blob: str) -> "SessionRecorder":
        return cls.from_dict(json.loads(blob))
