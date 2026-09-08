"""
pandabot_core.active_qa.wcag
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The WCAG 2.2 AA pack for web targets.

The operator's requirement is flat: all web UX must follow WCAG. This module is
the mechanical part of honouring it, and it is deliberately explicit about how
much of the standard that actually covers.

Three passes, of which this module implements two:

**Automated (here).** ``axe-core`` runs at every distinct state the harness
reaches. Violations come back with a rule id, an impact level and a node
selector, which map straight onto a finding.

**Keyboard (here).** Tab through a state and check that every interactive
element is reachable (2.1.1) and that focus never gets stuck (2.1.2). Visible
focus (2.4.7) is already rule U3's job, so it is not duplicated.

**Judged (not built).** Whether alt text is *meaningful*, whether a label
*describes* its field, whether focus order matches visual order (2.4.3). Those
are judgement calls for the model, and :data:`JUDGED_CRITERIA` declares them so a
report can say they were not checked.

**Automated tooling catches roughly a third of WCAG failure modes.** That is not
a caveat to bury: a report that lists axe results under a heading saying "WCAG
2.2 AA" invites the reader to believe the page conforms, and it does not follow
from a clean axe run. :func:`coverage_note` exists to be printed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pandabot_core.active_qa.contract import Element, Observation
from pandabot_core.active_qa.rules import (
    BLOCKER, MAJOR, MINOR, POLISH, Finding,
)

__all__ = [
    "AXE_VERSION", "AXE_URL", "resolve_axe_source", "axe_findings",
    "keyboard_findings", "JUDGED_CRITERIA", "coverage_note", "WCAG_TAGS",
]

log = logging.getLogger("active_qa.wcag")

#: Pinned. An unpinned audit tool silently changes what "conformant" means
#: between runs, and a report that cannot say which ruleset produced it is not
#: reproducible.
AXE_VERSION = "4.10.2"
AXE_URL = f"https://cdnjs.cloudflare.com/ajax/libs/axe-core/{AXE_VERSION}/axe.min.js"

#: The rule tags that correspond to WCAG 2.2 level AA.
WCAG_TAGS = ("wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa")

# axe impact -> our severity. Deliberately not softened: the operator's
# requirement is that web UX follows WCAG, so a critical accessibility
# violation is a blocker rather than a polish item.
_IMPACT = {
    "critical": BLOCKER,
    "serious": MAJOR,
    "moderate": MINOR,
    "minor": POLISH,
}

#: Success criteria that need judgement rather than a checker. Declared so a
#: report can list them as unchecked instead of letting silence imply a pass.
JUDGED_CRITERIA = {
    "1.1.1": "whether alt text is meaningful, not merely present",
    "2.4.3": "whether focus order matches the visual reading order",
    "2.4.6": "whether headings and labels actually describe their content",
    "3.3.2": "whether an error message tells the user how to fix the problem",
}


def coverage_note() -> str:
    """What a clean automated pass does and does not establish."""
    return (
        f"Checked with axe-core {AXE_VERSION} against {', '.join(WCAG_TAGS)}, plus a "
        "keyboard reachability pass. Automated tooling detects roughly a third of "
        "WCAG failure modes: a clean result here is not a statement that the page "
        "conforms. Criteria needing human or model judgement are listed as unchecked."
    )


# --- Getting axe ------------------------------------------------------------


def resolve_axe_source(
    source: str | Path | None = None,
    *,
    cache_dir: str | Path | None = None,
    allow_download: bool = True,
    timeout: float = 30.0,
) -> str | None:
    """Return the axe-core JavaScript, or None if it cannot be obtained.

    Tried in order: an explicit path, a cached copy, then a pinned download.
    Returning None rather than raising is deliberate -- a run without axe is a
    run with a smaller rule pack, not a failed run, and the report says which
    packs were actually in force.
    """
    if source:
        path = Path(source)
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        if str(source).startswith(("http://", "https://")) and allow_download:
            return _download(str(source), timeout)
        log.warning("axe source %s does not exist", source)
        return None

    cache = Path(cache_dir) if cache_dir else \
        Path.home() / ".cache" / "pandabot-active-qa"
    cached = cache / f"axe-{AXE_VERSION}.min.js"
    if cached.exists():
        return cached.read_text(encoding="utf-8", errors="replace")

    if not allow_download:
        return None
    js = _download(AXE_URL, timeout)
    if js:
        try:
            cache.mkdir(parents=True, exist_ok=True)
            cached.write_text(js, encoding="utf-8")
        except OSError:
            pass
    return js


def _download(url: str, timeout: float) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return resp.read().decode("utf-8", "replace")
    except Exception as exc:
        log.warning("could not fetch axe-core from %s: %s", url, exc)
        return None


# --- Turning axe results into findings --------------------------------------


def axe_findings(results: Mapping[str, Any], *, state: str = "",
                 screen_id: str = "", step_index: int = -1,
                 max_nodes: int = 3) -> list[Finding]:
    """Convert an axe run into findings, one per violated rule.

    One finding per rule rather than per node: "colour contrast is too low" on
    forty list items is one defect with forty instances, and forty findings
    would bury everything else in the report.
    """
    out: list[Finding] = []
    for violation in results.get("violations", []) or []:
        rule_id = str(violation.get("id", "unknown"))
        nodes = violation.get("nodes", []) or []
        impact = str(violation.get("impact") or "moderate").lower()
        severity = _IMPACT.get(impact, MINOR)

        targets = []
        for node in nodes[:max_nodes]:
            target = node.get("target") or []
            if target:
                targets.append(str(target[0]))
        shown = ", ".join(f"`{t}`" for t in targets)
        if len(nodes) > len(targets):
            shown += f", and {len(nodes) - len(targets)} more"

        criteria = sorted({t for t in violation.get("tags", [])
                           if t.startswith("wcag") and t[4:5].isdigit()})
        help_text = str(violation.get("help") or violation.get("description") or "")

        out.append(Finding(
            rule=f"wcag/{rule_id}",
            severity=severity,
            summary=help_text or f"Accessibility rule {rule_id} was violated",
            observed=(f"{len(nodes)} element(s) violate `{rule_id}` "
                      f"(axe impact: {impact}). Affected: {shown or 'unspecified'}."),
            expected=str(violation.get("description") or help_text or
                         "The page should satisfy this success criterion."),
            state=state,
            # The first offending node identifies the defect for clustering; the
            # rest are instances of the same one.
            element_identity=targets[0] if targets else f"wcag:{rule_id}",
            screen_id=screen_id,
            step_index=step_index,
            details={
                "axe_rule": rule_id, "impact": impact, "nodes": len(nodes),
                "criteria": criteria, "help_url": violation.get("helpUrl", ""),
            },
        ))
    return out


# --- Keyboard ---------------------------------------------------------------


def keyboard_findings(
    reachable: Sequence[str],
    observation: Observation,
    *,
    state: str = "",
    screen_id: str = "",
    step_index: int = -1,
    trapped_at: str | None = None,
) -> list[Finding]:
    """Findings from a Tab traversal of one state.

    ``reachable`` is the element identities focus visited, in order.
    ``trapped_at`` is set when focus stopped moving, which is 2.1.2.
    """
    out: list[Finding] = []
    interactive = list(observation.interactive())

    if trapped_at is not None:
        from pandabot_core.active_qa.recorder import humanise_identity
        out.append(Finding(
            rule="wcag/keyboard-trap",
            severity=BLOCKER,
            summary=f'Keyboard focus is trapped on "{humanise_identity(trapped_at)}"',
            observed=("Repeated Tab presses did not move focus away from this "
                      "control, so a keyboard-only user cannot leave it."),
            expected="Focus must always be able to move on with Tab (WCAG 2.1.2).",
            state=state, element_identity=trapped_at, screen_id=screen_id,
            step_index=step_index, details={"criteria": ["2.1.2"]},
        ))
        return out

    seen = set(reachable)
    unreachable = [el for el in interactive if el.identity not in seen]
    if unreachable and reachable:
        from pandabot_core.active_qa.recorder import humanise_identity

        # The Element is in hand here, so use its actual label rather than
        # deriving a name from the identity: "Hidden from keyboard" tells a
        # reader which control this is, "unreachable" is the id it happened to
        # be given.
        def _name(el: Element) -> str:
            return el.label or humanise_identity(el.identity)

        names = ", ".join(f'"{_name(el)}"' for el in unreachable[:5])
        if len(unreachable) > 5:
            names += f", and {len(unreachable) - 5} more"
        out.append(Finding(
            rule="wcag/keyboard-reachable",
            severity=MAJOR,
            summary=f"{len(unreachable)} control(s) cannot be reached by keyboard",
            observed=(f"Tabbing through this screen reached {len(seen)} of "
                      f"{len(interactive)} interactive controls. Missed: {names}."),
            expected=("Every interactive control must be operable from a keyboard "
                      "(WCAG 2.1.1)."),
            state=state,
            element_identity=unreachable[0].identity,
            screen_id=screen_id, step_index=step_index,
            details={"criteria": ["2.1.1"], "reached": len(seen),
                     "total": len(interactive)},
        ))
    return out
