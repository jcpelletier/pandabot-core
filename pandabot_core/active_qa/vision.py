"""
pandabot_core.active_qa.vision
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Vision-only mode, for targets that expose no UI tree at all.

A Unity game is one ``SurfaceView``. A canvas app is one ``<canvas>``. A Roblox
place viewed through the retail client is pixels. ``uiautomator`` and the DOM
have nothing to say about any of them, so :attr:`Observation.is_blind` is true
and the engine has literally no work to queue -- measured: a blind observation
produces zero candidates and the loop finds nothing to do.

This module is the fallback. The model looks at a screenshot and proposes where
the controls are; those proposals are synthesised into ordinary
:class:`~pandabot_core.active_qa.contract.Element` objects, so everything above
the driver -- the frontier, the rulebook, the recorder, triage -- carries on
unchanged.

**It is genuinely worse, and the report must say so.** This is not the same
capability with a different backend. :data:`WEAKENED_RULES` names exactly which
checks stop working and why, because presenting a vision run as equivalent to a
tree-backed one would be the most consequential dishonesty this harness could
commit: it would report a clean run on a target it could barely see.

Concretely, against a tree-backed run:

* **Identity is positional.** With no handle and usually no reliable text,
  identity falls back to quantised bounds, so a scroll or a resize invents new
  elements and loses old ones.
* **Element state is invisible.** Enabled, selected and a field's contents are
  properties of the tree. They cannot be read from pixels reliably, so the rules
  that depend on them cannot run.
* **The model is in the loop far more.** Every new screen needs a vision call,
  which is several times the cost of a text one, so throughput drops hard.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from pandabot_core.active_qa.contract import Element, Observation

__all__ = [
    "propose_elements", "WEAKENED_RULES", "degraded_note", "VISION_SYSTEM",
    "encode_image",
]

log = logging.getLogger("active_qa.vision")

#: Rules that cannot work, or work badly, without a tree. Keyed by rule id, with
#: the reason -- so a report can list them as unavailable rather than passed.
WEAKENED_RULES: dict[str, str] = {
    "U4": "which option in a group is selected is a tree property, not a visible one",
    "U5": "whether a control is disabled cannot be read from pixels reliably",
    "U6": "a field's contents cannot be read back, so typed input cannot be verified",
    "U10": "element bounds are the model's estimate, so a layout shift cannot be "
           "distinguished from a re-estimate",
    "U12": "reachability depends on stable element identity, which is positional here",
}

VISION_SYSTEM = """\
You are looking at a screenshot of an application under test. Identify the \
controls a user could interact with: buttons, text fields, toggles, tabs, menu \
items, and anything else clearly meant to be clicked or typed into.

Answer with ONE JSON object and nothing else:

{"controls": [
  {"label": "<the visible text, or a short description if it has none>",
   "role": "button" | "textbox" | "checkbox" | "tab" | "link" | "slider" | "option",
   "box": [<left>, <top>, <right>, <bottom>]}
]}

`box` is in PERCENTAGES of the image, 0 to 100, measured from the top left.

Rules:
- Only include things that look interactive. Decorative art, background, score \
readouts and static labels are not controls.
- Be precise about boxes. They are used to aim a click, so a box that covers \
half the screen is useless.
- If you see no interactive controls, return {"controls": []}. Inventing \
controls is worse than reporting none, because the harness will try to click \
them and report the failure as a defect.
"""


def degraded_note(count: int = 0) -> str:
    """What a vision run is and is not, for the report header."""
    return (
        "Vision-only run: the target exposed no UI tree, so controls were located "
        f"by the model from screenshots ({count} proposed on the last screen). "
        "Element identity is positional and therefore unstable across scrolling, "
        "element state (enabled, selected, field contents) is unavailable, and the "
        "rules that depend on it did not run. Findings are weaker than a run "
        "against a target with a real tree, and an absence of findings says much "
        "less."
    )


def encode_image(path: str | Path) -> str | None:
    """Read an image as a data URI, or None if it cannot be read."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        log.warning("could not read %s: %s", p, exc)
        return None
    suffix = p.suffix.lower().lstrip(".") or "png"
    mime = {"jpg": "jpeg"}.get(suffix, suffix)
    return f"data:image/{mime};base64," + base64.b64encode(raw).decode("ascii")


def _extract_json(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    depth, start, found = 0, -1, []
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                found.append(raw[start:i + 1])
    for blob in reversed(found):
        try:
            data = json.loads(blob)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None


_ROLES = frozenset({"button", "textbox", "checkbox", "tab", "link", "slider",
                    "option", "radio", "switch", "combobox", "menuitem"})


def propose_elements(
    client: Any,
    screenshot: str | Path,
    *,
    viewport: tuple[int, int],
    max_controls: int = 40,
    project_context: str = "",
) -> list[Element]:
    """Ask the model where the controls are. Returns synthesised elements.

    ``client`` needs a ``complete_with_image(system, user, image_data_uri)``.
    Any failure returns an empty list: a vision run that cannot see is a run with
    nothing to do, which the loop already handles, rather than an exception that
    ends the session.
    """
    data_uri = encode_image(screenshot)
    if data_uri is None:
        return []

    prompt = "Identify the interactive controls in this screenshot."
    if project_context:
        prompt = (f"Project notes (authoritative):\n{project_context}\n\n---\n\n"
                  f"{prompt}")
    try:
        raw = client.complete_with_image(VISION_SYSTEM, prompt, data_uri)
    except Exception as exc:
        log.warning("vision call failed: %s", exc)
        return []

    data = _extract_json(raw)
    if data is None:
        log.warning("vision: could not parse a JSON object from the answer")
        return []

    width, height = viewport
    out: list[Element] = []
    for i, item in enumerate(data.get("controls") or [], start=1):
        if not isinstance(item, Mapping):
            continue
        box = item.get("box")
        if not (isinstance(box, Sequence) and len(box) == 4):
            continue
        try:
            left, top, right, bottom = (float(v) for v in box)
        except (TypeError, ValueError):
            continue
        # Percentages to pixels, clamped. A model that returns 0-1 instead of
        # 0-100 would otherwise put every control in the top-left corner, so
        # rescale that case rather than aiming at nothing.
        if max(left, top, right, bottom) <= 1.0:
            left, top, right, bottom = (v * 100 for v in (left, top, right, bottom))
        px = (
            max(0, min(width, round(left / 100 * width))),
            max(0, min(height, round(top / 100 * height))),
            max(0, min(width, round(right / 100 * width))),
            max(0, min(height, round(bottom / 100 * height))),
        )
        if px[2] - px[0] < 2 or px[3] - px[1] < 2:
            continue          # a degenerate box cannot be aimed at
        role = str(item.get("role", "button")).lower()
        if role not in _ROLES:
            role = "button"
        label = " ".join(str(item.get("label", "")).split())[:80]
        out.append(Element(
            id=f"v{i}",
            role=role,
            label=label,
            bounds=px,
            # Deliberately no handle: there is nothing stable to hang one on, and
            # inventing one would claim an identity strength this does not have.
            handle=None,
            # State is a tree property and cannot be read from pixels. Claiming
            # "enabled" would be a guess the rulebook would then act on.
            enabled=True,
            focusable=False,
        ))
        if len(out) >= max_controls:
            break
    return out


def vision_observation(base: Observation, elements: Sequence[Element]) -> Observation:
    """Splice proposed controls into an observation, marked as vision-derived.

    Everything above the driver then works unchanged -- the frontier queues them,
    the recorder records them, triage clusters them -- while ``meta['vision']``
    keeps the report honest about where they came from.
    """
    meta = dict(base.meta)
    meta["vision"] = True
    meta["vision_controls"] = len(elements)
    return Observation(
        elements=tuple(elements),
        screen_id=base.screen_id,
        text=base.text,
        screenshot=base.screenshot,
        logs=base.logs,
        net=base.net,
        meta=meta,
        captured_at=base.captured_at,
    )
