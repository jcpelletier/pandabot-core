"""
pandabot_core.active_qa.brief
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Reading a project's ``PANDABOTQA.md``.

Every repo under test carries one at its root, and the harness reads it from the
branch under test. It is the project's standing brief: what the product is, how
to build and run it, how to reach the interesting states, what to focus on, what
is out of scope, deliberate behaviour that would otherwise read as a bug, and
known issues not to re-report (ACTIVE_QA.md 11).

One file, two halves. A fenced ``yaml`` block carries the machine-readable
settings the runner needs; prose carries everything the model reads. This module
parses both and hands back a :class:`ProjectBrief`.

Two things it is careful about.

**A missing brief is not an error.** The harness infers what it can, says so in
the test plan, and offers a generated starter. Refusing to run without one would
make the file a gate rather than a help.

**Suppression stays visible.** Waivers, out-of-scope items and known issues all
flow through to the report header, because a project that can quietly waive
rules can quietly waive its way to a clean report (§11.3). This module is where
they are read, not where they are hidden.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = ["ProjectBrief", "load_brief", "parse_brief", "starter_brief", "BRIEF_FILENAME"]

BRIEF_FILENAME = "PANDABOTQA.md"

_FENCE = re.compile(r"^```+\s*ya?ml\s*$", re.IGNORECASE)
_FENCE_END = re.compile(r"^```+\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")

# Prose sections are matched on a normalised prefix so a project can title them
# naturally ("Known issues (do not re-report)") without breaking the reader.
_SECTION_ALIASES = {
    "what this product is": "product",
    "what this is": "product",
    "how to run": "run",
    "how to build": "run",
    "how to reach": "states",
    "focus": "focus",
    "out of scope": "out_of_scope",
    "expected behaviour": "expected",
    "expected behavior": "expected",
    "known issues": "known_issues",
    "testability": "testability",
}


def _normalise(heading: str) -> str | None:
    low = heading.strip().lower()
    for prefix, key in _SECTION_ALIASES.items():
        if low.startswith(prefix):
            return key
    return None


def _bullets(text: str) -> list[str]:
    """Bullet lines from a prose section, with markdown emphasis stripped.

    Only top-level bullets: a nested list under an item is detail about that
    item, not a separate entry.
    """
    out: list[str] = []
    for line in text.splitlines():
        if not line.startswith(("- ", "* ")):
            continue
        item = line[2:].strip()
        item = re.sub(r"\*\*(.+?)\*\*", r"\1", item)
        item = re.sub(r"`(.+?)`", r"\1", item)
        if item:
            out.append(item)
    return out


@dataclass
class ProjectBrief:
    """A parsed ``PANDABOTQA.md``."""

    present: bool = False
    path: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)
    #: Problems encountered while parsing. Surfaced rather than swallowed: a
    #: brief the harness silently failed to read is worse than no brief, because
    #: the operator believes their waivers and focus took effect.
    warnings: list[str] = field(default_factory=list)

    # -- machine-readable half --------------------------------------------
    @property
    def target(self) -> str:
        return str(self.config.get("target", "") or "")

    @property
    def runner(self) -> str:
        return str(self.config.get("runner", "") or "")

    @property
    def package(self) -> str:
        return str(self.config.get("package", "") or "")

    @property
    def activity(self) -> str:
        return str(self.config.get("activity", "") or "")

    @property
    def build(self) -> str:
        return str(self.config.get("build", "") or "")

    @property
    def launch(self) -> str:
        return str(self.config.get("launch", "") or "")

    @property
    def rule_packs(self) -> list[str]:
        rules = self.config.get("rules") or {}
        packs = rules.get("packs") if isinstance(rules, Mapping) else None
        return [str(p) for p in packs] if isinstance(packs, Sequence) and \
            not isinstance(packs, str) else []

    @property
    def waivers(self) -> dict[str, str]:
        """Rule id -> stated reason. Every one is echoed in the report."""
        rules = self.config.get("rules") or {}
        raw = rules.get("waivers") if isinstance(rules, Mapping) else None
        out: dict[str, str] = {}
        if isinstance(raw, Sequence) and not isinstance(raw, str):
            for entry in raw:
                if isinstance(entry, Mapping) and entry.get("rule"):
                    out[str(entry["rule"])] = str(entry.get("reason", "")
                                                  or "no reason given")
        return out

    @property
    def normalise_labels(self) -> bool:
        """Whether this project's control labels carry live values.

        ``identity: {normalise_numbers: true}``. Opt-in per project because the
        normalisation is lossy: it makes an idle game's "Hire (500c + 1 Food)"
        stable and would collapse a list's "Item 1" and "Item 2" into one
        control. Only the project knows which shape its labels have.
        """
        raw = self.config.get("identity") or {}
        return bool(raw.get("normalise_numbers")) if isinstance(raw, Mapping) else False

    @property
    def scripts(self) -> dict[str, str]:
        raw = self.config.get("scripts") or {}
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, Mapping) else {}

    @property
    def bridge(self) -> dict[str, str]:
        raw = self.config.get("bridge") or {}
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, Mapping) else {}

    def budgets(self, **overrides: Any) -> Any:
        """A :class:`~pandabot_core.active_qa.loop.Budgets` from the brief.

        The operator's request wins over the file, and the file wins over the
        defaults -- a brief sets a project's normal working range, not a ceiling
        the person asking cannot move.
        """
        from pandabot_core.active_qa.loop import Budgets

        raw = self.config.get("budgets") or {}
        kwargs: dict[str, Any] = {}
        if isinstance(raw, Mapping):
            if raw.get("min_minutes") is not None:
                kwargs["min_seconds"] = float(raw["min_minutes"]) * 60
            if raw.get("max_minutes") is not None:
                kwargs["max_seconds"] = float(raw["max_minutes"]) * 60
            if raw.get("max_actions") is not None:
                kwargs["max_actions"] = int(raw["max_actions"])
        kwargs.update({k: v for k, v in overrides.items() if v is not None})
        return Budgets(**kwargs)

    # -- prose half --------------------------------------------------------
    @property
    def out_of_scope(self) -> list[str]:
        return _bullets(self.sections.get("out_of_scope", ""))

    @property
    def known_issues(self) -> list[str]:
        return _bullets(self.sections.get("known_issues", ""))

    @property
    def expected_behaviour(self) -> list[str]:
        """Deliberate behaviour that would otherwise read as a defect.

        The single highest-value section for precision: "oxygen falls when crew
        exceeds life support" is the mechanic, and a tester that does not know
        that files it as a bug.
        """
        return _bullets(self.sections.get("expected", ""))

    @property
    def focus(self) -> str:
        return self.sections.get("focus", "").strip()

    def model_context(self, *, limit: int = 6000) -> str:
        """The prose a model should read before forming expectations.

        Ordered by how much it changes the model's judgement, and truncated,
        because this is prepended to orient calls and the context budget is the
        binding constraint on a long run.
        """
        parts: list[str] = []
        for key, title in (("product", "What this product is"),
                           ("focus", "Focus for this run"),
                           ("expected", "Deliberate behaviour, not defects"),
                           ("states", "How to reach the interesting states"),
                           ("out_of_scope", "Out of scope")):
            body = self.sections.get(key, "").strip()
            if body:
                parts.append(f"## {title}\n{body}")
        text = "\n\n".join(parts)
        return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0]

    def summary(self) -> dict[str, Any]:
        """What the test plan should tell the operator it read."""
        return {
            "present": self.present,
            "path": self.path,
            "target": self.target,
            "runner": self.runner,
            "rule_packs": self.rule_packs,
            "normalise_labels": self.normalise_labels,
            "waivers": self.waivers,
            "out_of_scope": len(self.out_of_scope),
            "known_issues": len(self.known_issues),
            "expected_behaviour": len(self.expected_behaviour),
            "sections": sorted(self.sections),
            "warnings": list(self.warnings),
        }


# --- Parsing ----------------------------------------------------------------


def parse_brief(text: str, *, path: str = "") -> ProjectBrief:
    """Parse the contents of a ``PANDABOTQA.md``."""
    brief = ProjectBrief(present=True, path=path)

    lines = text.splitlines()
    # The config block is the FIRST fenced yaml block. Later ones are examples
    # in prose (a brief may well show a snippet of the app's own config), and
    # treating one of those as settings would be quietly wrong.
    config_lines: list[str] | None = None
    body_lines: list[str] = []
    in_config = False
    seen_config = False
    for line in lines:
        if not seen_config and not in_config and _FENCE.match(line):
            in_config, config_lines = True, []
            continue
        if in_config:
            if _FENCE_END.match(line):
                in_config, seen_config = False, True
                continue
            assert config_lines is not None
            config_lines.append(line)
            continue
        body_lines.append(line)

    if config_lines:
        brief.config = _parse_yaml("\n".join(config_lines), brief.warnings)

    current: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current and buffer:
            brief.sections[current] = "\n".join(buffer).strip()

    for line in body_lines:
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            key = _normalise(heading.group(2))
            # A sub-heading inside a section stays part of it: goober-ranch's
            # "### The gym pads" belongs to "How to reach the interesting states".
            if key is not None and level <= 2:
                flush()
                current, buffer = key, []
                continue
            if level <= 2 and current is not None:
                flush()
                current, buffer = None, []
                continue
        if current is not None:
            buffer.append(line)
    flush()
    return brief


def _parse_yaml(text: str, warnings: list[str]) -> dict[str, Any]:
    """Parse the config block, degrading rather than failing.

    PyYAML is not a pandabot-core dependency, and an unreadable config block
    must not stop a run: the prose half is still worth having, and the harness
    can fall back to inference. But it must say so -- a brief the harness
    silently failed to read is worse than no brief, because the operator
    believes their waivers took effect.
    """
    try:
        import yaml  # noqa: PLC0415 (deliberately lazy)
    except Exception:
        warnings.append(
            "PyYAML is not installed, so the config block was skipped. Target, "
            "runner, launch commands, rule packs and waivers were NOT read.")
        return {}
    try:
        data = yaml.safe_load(text)
    except Exception as exc:
        warnings.append(f"the config block is not valid YAML and was skipped: {exc}")
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        warnings.append("the config block is not a mapping and was skipped")
        return {}
    return data


def load_brief(repo_dir: str | Path, *, filename: str = BRIEF_FILENAME) -> ProjectBrief:
    """Read the brief from a checked-out repo.

    A missing file comes back as ``present=False`` rather than an error: the
    harness infers what it can and says so in the test plan.
    """
    path = Path(repo_dir) / filename
    if not path.exists():
        return ProjectBrief(present=False, path=str(path))
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        brief = ProjectBrief(present=False, path=str(path))
        brief.warnings.append(f"could not read {path}: {exc}")
        return brief
    return parse_brief(text, path=str(path))


# --- Scaffolding ------------------------------------------------------------


def starter_brief(repo: str = "", target: str = "web") -> str:
    """A starter ``PANDABOTQA.md`` for a project that has none.

    Offered rather than imposed. The quickest route to a good brief is a draft
    to argue with, and the TODOs are deliberately blunt so an unedited one is
    obvious in review.
    """
    name = repo.split("/")[-1] if repo else "this project"
    return f"""# PANDABOTQA.md — {name}

The standing brief for PandaBot-QA's exploratory testing runs. Keep it current
in the same commit as the feature it describes.

```yaml
target: {target}
runner: panda
build: ""              # TODO: how to build it
launch: ""             # TODO: how to start it
rules:
  packs: [universal]
  waivers: []          # {{rule: U2, reason: "..."}} — every waiver is echoed in the report
budgets: {{min_minutes: 60, max_minutes: 180, max_actions: 6000}}
```

## What this product is

TODO: one paragraph. Without this the harness infers the product's purpose from
source and gets it wrong, which poisons every expectation it forms.

## How to run it

TODO: build and launch, and anything non-obvious about the environment.

## How to reach the interesting states

TODO: the highest-value section. Debug commands, seed data, test accounts,
anything that skips a tutorial. Without it a run spends its budget grinding.

## Focus

TODO: what is new or risky. Absent this, coverage is spread evenly over stable
old screens.

## Out of scope

- TODO: anything the harness should not touch (debug tools, real payments).

## Expected behaviour worth knowing

- TODO: deliberate behaviour that would otherwise read as a defect.

## Known issues (do not re-report)

- TODO: prune this. An entry that has been fixed should be deleted, not left
  to suppress a regression.
"""
