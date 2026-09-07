"""
pandabot_core.active_qa
~~~~~~~~~~~~~~~~~~~~~~~
Engine for **active QA**: exploratory UI/UX testing that drives a product like a
user, for a long time, with no script.

This is the counterpart to the deterministic acceptance gate. The gate answers
"did the thing the story asked for get built"; this answers "is it any good to
use", and it looks for the classes of defect nobody wrote a check for: missing
input validation, controls with no pressed or selected state, and UI that looks
like it should do something and does not.

The full design lives in ``ACTIVE_QA.md`` in the PandaEcosystem repo. What is
here so far:

============================  ====================================================
:mod:`~.contract`             The uniform driver contract every target implements
:mod:`~.recorder`             Full-session action recording and replayable traces
:mod:`~.fake`                 A scriptable in-memory driver for tests
============================  ====================================================

Nothing in this package imports Discord, a browser, or an LLM client. The
drivers and the orchestrator live above it.
"""

from __future__ import annotations

from pandabot_core.active_qa.contract import (
    ACTION_KINDS,
    DEFAULT_FINGERPRINT_POLICY,
    STRICT_FINGERPRINT_POLICY,
    Action,
    ActionKind,
    ActResult,
    Driver,
    DriverError,
    Element,
    FingerprintPolicy,
    Health,
    Observation,
    supported_actions,
)
from pandabot_core.active_qa.fake import FakeDriver, FakeScreen, ambient_noise
from pandabot_core.active_qa.recorder import SessionRecorder, Step, Trace

__all__ = [
    # contract
    "ACTION_KINDS", "Action", "ActionKind", "ActResult",
    "Element", "Observation", "Health",
    "FingerprintPolicy", "DEFAULT_FINGERPRINT_POLICY", "STRICT_FINGERPRINT_POLICY",
    "Driver", "DriverError", "supported_actions",
    # recorder
    "SessionRecorder", "Step", "Trace",
    # fake
    "FakeDriver", "FakeScreen", "ambient_noise",
]
