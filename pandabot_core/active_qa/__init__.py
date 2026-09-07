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
:mod:`~.coverage`             State graph and the frontier queue that picks the next action
:mod:`~.rules`                The universal UX rulebook, U1 to U12, as detectors
:mod:`~.strategies`           The strategy deck, played when novelty stalls
:mod:`~.loop`                 The exploration loop that ties it all together
:mod:`~.triage`               Clustering and replay-confirmation before reporting
:mod:`~.journal`              Running HTML narrative of the run, with frames
:mod:`~.drivers`              Concrete drivers (web via Playwright, so far)
:mod:`~.fake`                 A scriptable in-memory driver for tests
============================  ====================================================

Nothing in this package imports Discord, a browser, or an LLM client. The
drivers and the orchestrator live above it.
"""

from __future__ import annotations

from pandabot_core.active_qa.contract import (
    ACTION_KINDS,
    DEFAULT_FINGERPRINT_POLICY,
    DOCUMENT_FINGERPRINT_POLICY,
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
    fingerprint_policy_for,
    supported_actions,
)
from pandabot_core.active_qa.coverage import (
    DEFAULT_WEIGHTS,
    Candidate,
    CoverageMap,
    Edge,
    Frontier,
    PriorityWeights,
    StateNode,
    default_candidates,
)
from pandabot_core.active_qa.fake import FakeDriver, FakeScreen, ambient_noise
from pandabot_core.active_qa.journal import (
    EventKind,
    FrameStore,
    Journal,
    JournalEvent,
    render_html,
)
from pandabot_core.active_qa.loop import (
    Advisor,
    Budgets,
    ExplorationLoop,
    LoopResult,
    LoopStats,
    Orientation,
    StopReason,
)
from pandabot_core.active_qa.rules import (
    BLOCKER,
    MAJOR,
    MINOR,
    POLISH,
    RULES,
    Finding,
    Rule,
    StateContext,
    StateProbe,
    StepContext,
    implemented_rules,
    rule,
    run_session_detectors,
    run_state_detectors,
    run_step_detectors,
    severity_rank,
)
from pandabot_core.active_qa.recorder import SessionRecorder, Step, Trace
from pandabot_core.active_qa.strategies import DECK, Card, Deck, implemented_cards
from pandabot_core.active_qa.triage import (
    FindingCluster,
    TriagedFinding,
    TriageReport,
    cluster_findings,
    replay_once,
    triage,
)

__all__ = [
    # contract
    "ACTION_KINDS", "Action", "ActionKind", "ActResult",
    "Element", "Observation", "Health",
    "FingerprintPolicy", "DEFAULT_FINGERPRINT_POLICY", "STRICT_FINGERPRINT_POLICY",
    "DOCUMENT_FINGERPRINT_POLICY",
    "Driver", "DriverError", "supported_actions", "fingerprint_policy_for",
    # coverage
    "Candidate", "CoverageMap", "Edge", "StateNode",
    "Frontier", "PriorityWeights", "DEFAULT_WEIGHTS", "default_candidates",
    # recorder
    "SessionRecorder", "Step", "Trace",
    # rules
    "RULES", "Rule", "rule", "implemented_rules", "Finding",
    "StepContext", "StateContext", "StateProbe",
    "run_step_detectors", "run_state_detectors", "run_session_detectors",
    "BLOCKER", "MAJOR", "MINOR", "POLISH", "severity_rank",
    # triage
    "triage", "cluster_findings", "replay_once",
    "TriageReport", "TriagedFinding", "FindingCluster",
    # strategies
    "DECK", "Card", "Deck", "implemented_cards",
    # journal
    "Journal", "JournalEvent", "EventKind", "FrameStore", "render_html",
    # loop
    "ExplorationLoop", "Budgets", "LoopResult", "LoopStats", "StopReason",
    "Advisor", "Orientation",
    # fake
    "FakeDriver", "FakeScreen", "ambient_noise",
]
