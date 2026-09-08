"""
pandabot_core.active_qa.advisor
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The model, behind the two questions the loop actually needs answered.

The exploration loop is policy-driven precisely so it can afford to run for an
hour (ACTIVE_QA.md 5 and 8). The model is not in the loop; it is consulted twice:

**orient**, once per newly discovered state -- "what is this screen, and what
does each control appear to promise?" The promises become *expectation records*,
stored once and read for free on every later step. That is what turns rule U7's
finding from "this button did nothing" into "this button did nothing, and the
docs say it should deduct credits", and it is the only affordable way to catch a
dead affordance at 30 tok/s.

**judge**, only when a detector has already fired -- "is this a real defect?"

Everything else is chosen by the frontier with no model call at all. The target
is one call per 10 to 20 actions.

Two failure policies, and they point in opposite directions on purpose:

* **orient fails empty.** No expectations is a worse run, not a broken one.
* **judge fails open.** If the model errors, times out, or answers unintelligibly,
  the finding is *kept*. Discarding a real defect because an HTTP call failed is
  much worse than carrying a candidate into triage, which will replay it anyway.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from pandabot_core.active_qa.contract import Observation
from pandabot_core.active_qa.loop import Orientation
from pandabot_core.active_qa.rules import Finding, StepContext

__all__ = ["LocalAdvisor", "AdvisorStats", "OpenAIChat", "describe_observation"]

log = logging.getLogger("active_qa.advisor")

_ORIENT_SYSTEM = """\
You are the orienting half of an automated UI testing harness. You are shown one \
screen of an application under test. You do not drive the application; a policy \
engine does that. Your job is to say what each control *appears to promise*, so \
the harness can later notice when a control does not deliver on it.

Answer with ONE JSON object and nothing else:

{"screen": "<a short name for this screen>",
 "notes": "<one or two sentences on what this screen is for>",
 "expectations": {"<element id>": "<what pressing or using it should do>"},
 "curiosity": {"<element id>": <0.0 to 1.0, how likely it is to hide a bug>}}

Rules for expectations:
- Use the element ids exactly as given. Do not invent ids.
- Say what should OBSERVABLY happen: a panel opens, a value changes, an error \
appears. Not "it works".
- If you genuinely cannot tell what a control promises, leave it out. A wrong \
expectation is worse than none, because the harness will report against it.
- Ground them in the project notes when they are given. Those are authoritative.
"""

_JUDGE_SYSTEM = """\
You are the judging half of an automated UI testing harness. A mechanical rule \
has fired on an interaction. Decide whether it is a real defect worth a human's \
time, or expected behaviour the rule could not know about.

Answer with ONE JSON object and nothing else:

{"verdict": "defect" | "not a defect", "why": "<one sentence>"}

Be conservative about dismissing. The harness will independently replay anything \
you keep, so a false keep costs a little time, while a false dismissal loses a \
real bug permanently. Dismiss only when you can name the reason it is fine -- \
typically because the project notes say so.
"""


@dataclass
class AdvisorStats:
    orient_calls: int = 0
    judge_calls: int = 0
    failures: int = 0
    dismissed: int = 0
    seconds: float = 0.0
    prompt_chars: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "orient_calls": self.orient_calls, "judge_calls": self.judge_calls,
            "failures": self.failures, "dismissed": self.dismissed,
            "seconds": round(self.seconds, 1),
            "prompt_chars": self.prompt_chars,
        }


# --- Transport --------------------------------------------------------------


class OpenAIChat:
    """A minimal OpenAI-compatible chat client.

    Deliberately not the shared ``pandabot_core.llm`` provider: that carries
    tool-calling, usage logging and a provider abstraction this does not need,
    and the endpoint here is a local Ollama on another machine reached over a
    tunnel. ``requests`` is already a core dependency.
    """

    def __init__(self, base_url: str, model: str, *, timeout: float = 120.0,
                 api_key: str = "", options: Mapping[str, Any] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.api_key = api_key
        self.options = dict(options or {})

    def complete(self, system: str, user: str) -> str:
        import requests  # noqa: PLC0415 - core dependency, imported lazily anyway

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            # Low temperature: this is classification and description, not
            # writing. A creative orient invents promises that do not exist and
            # the harness then reports against them.
            "temperature": 0.2,
        }
        payload.update(self.options)
        resp = requests.post(f"{self.base_url}/v1/chat/completions",
                             json=payload, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"] or ""


# --- Prompt building --------------------------------------------------------


def describe_observation(observation: Observation, *, max_elements: int = 60) -> str:
    """Render a screen as compact text for the model.

    Text, not a screenshot. A vision turn costs several times a text turn at the
    same token budget, and the element tree already carries what orient needs.
    Screenshots are for the report and for targets with no tree at all.
    """
    lines: list[str] = []
    if observation.screen_id:
        lines.append(f"Screen: {observation.screen_id}")
    elements = list(observation.interactive())
    if len(elements) > max_elements:
        # Truncated rather than silently dropped: an orient that saw half the
        # screen should not read as an orient that saw all of it.
        lines.append(f"(showing {max_elements} of {len(elements)} controls)")
        elements = elements[:max_elements]
    lines.append("Controls:")
    for el in elements:
        bits = [f"  {el.identity} — {el.role}"]
        if el.label:
            bits.append(f'labelled "{el.label}"')
        if not el.enabled:
            bits.append("DISABLED")
        if el.selected:
            bits.append("selected")
        if el.text is not None:
            bits.append(f"contains {el.text!r}")
        lines.append(", ".join(bits))
    text = (observation.text or "").strip()
    if text:
        lines.append("Visible text:")
        lines.append(text[:1500])
    return "\n".join(lines)


def _extract_json(raw: str) -> dict[str, Any] | None:
    """Pull one JSON object out of a model's answer.

    Local models wrap JSON in prose and fences however they like, and a
    thinking model may emit a whole reasoning block first, so the last complete
    object in the text is the answer rather than the first.
    """
    if not raw:
        return None
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    candidates = []
    depth = 0
    start = -1
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidates.append(raw[start:i + 1])
    for blob in reversed(candidates):
        try:
            data = json.loads(blob)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None


# --- The advisor ------------------------------------------------------------


class LocalAdvisor:
    """An :class:`~pandabot_core.active_qa.loop.Advisor` backed by a chat model."""

    def __init__(
        self,
        client: Any,
        *,
        project_context: str = "",
        max_elements: int = 60,
        enable_judge: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.project_context = project_context
        self.max_elements = max_elements
        self.enable_judge = enable_judge
        self.clock = clock
        self.stats = AdvisorStats()

    # -- orient ------------------------------------------------------------
    def orient(self, observation: Observation, fingerprint: str) -> Orientation:
        if observation.is_blind:
            # Nothing to describe and no ids to hang expectations on.
            return Orientation()
        prompt = describe_observation(observation, max_elements=self.max_elements)
        if self.project_context:
            prompt = (f"Project notes (authoritative):\n{self.project_context}\n\n"
                      f"---\n\n{prompt}")
        raw = self._call(_ORIENT_SYSTEM, prompt, "orient")
        if raw is None:
            return Orientation()

        data = _extract_json(raw)
        if data is None:
            self.stats.failures += 1
            log.warning("orient: could not parse a JSON object from the answer")
            return Orientation()

        known = {el.identity for el in observation.interactive()}
        expectations = {}
        for key, value in (data.get("expectations") or {}).items():
            # Silently dropping a hallucinated id would leave the model's
            # invented promise nowhere; keeping it would attach an expectation
            # to a control that does not exist and report against it.
            if key in known and isinstance(value, str) and value.strip():
                expectations[key] = value.strip()
        curiosity = {}
        for key, value in (data.get("curiosity") or {}).items():
            if key in known:
                try:
                    curiosity[key] = max(0.0, min(1.0, float(value)))
                except (TypeError, ValueError):
                    continue
        notes = str(data.get("notes") or data.get("screen") or "").strip()
        return Orientation(expectations=expectations, curiosity=curiosity, notes=notes)

    # -- judge -------------------------------------------------------------
    def judge(self, finding: Finding, context: StepContext) -> bool:
        if not self.enable_judge:
            return True
        prompt = self._judge_prompt(finding, context)
        raw = self._call(_JUDGE_SYSTEM, prompt, "judge")
        if raw is None:
            return True          # fail open

        data = _extract_json(raw)
        if data is None:
            self.stats.failures += 1
            return True          # fail open
        verdict = str(data.get("verdict", "")).strip().lower()
        if verdict.startswith("not"):
            self.stats.dismissed += 1
            log.info("judge dismissed %s on %s: %s", finding.rule,
                     finding.element_identity, data.get("why", ""))
            return False
        return True

    def _judge_prompt(self, finding: Finding, context: StepContext) -> str:
        parts = []
        if self.project_context:
            parts.append(f"Project notes (authoritative):\n{self.project_context}\n---")
        parts.append(f"Rule that fired: {finding.rule}")
        parts.append(f"Summary: {finding.summary}")
        parts.append(f"Observed: {finding.observed}")
        parts.append(f"Expected: {finding.expected}")
        parts.append(f"Action taken: {context.action.describe()}")
        parts.append(f"State changed: {'yes' if context.changed else 'no'}")
        if context.before.logs:
            parts.append("Logs during this step:\n" +
                         "\n".join(context.before.logs[:10]))
        parts.append("\nScreen at the time:\n" +
                     describe_observation(context.before,
                                          max_elements=self.max_elements))
        return "\n".join(parts)

    # -- plumbing ----------------------------------------------------------
    def _call(self, system: str, user: str, kind: str) -> str | None:
        started = self.clock()
        try:
            raw = self.client.complete(system, user)
        except Exception as exc:
            self.stats.failures += 1
            self.stats.seconds += self.clock() - started
            log.warning("%s call failed: %s", kind, exc)
            return None
        self.stats.seconds += self.clock() - started
        self.stats.prompt_chars += len(system) + len(user)
        if kind == "orient":
            self.stats.orient_calls += 1
        else:
            self.stats.judge_calls += 1
        return raw
