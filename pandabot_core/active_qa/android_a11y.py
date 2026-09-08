"""
pandabot_core.active_qa.android_a11y
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The Android accessibility pack.

The analogue of :mod:`~pandabot_core.active_qa.wcag` for a touch target, drawn
from the checks Android's own Accessibility Scanner makes. Everything here is
answerable from the ``uiautomator`` dump plus the screen density, so it costs no
extra device round-trips beyond the observation the loop already took.

Three checks, and each corresponds to a way a real user gets stuck:

* **Unlabelled controls.** A button with no text and no ``contentDescription`` is
  announced by TalkBack as "button", which tells a blind user nothing. This is
  the single commonest Android accessibility defect.
* **Touch targets under 48dp.** Below that, people with imprecise motor control
  miss. It is a physical measurement, so it is computed from density rather than
  from raw pixels -- comparing pixels against a fixed number would pass
  everything on a high-density screen and fail everything on a low-density one.
* **Ambiguous duplicate labels.** Two controls announced identically in the same
  screen cannot be told apart by ear.

**Contrast is not checked here.** It needs the rendered pixels rather than the
tree, and guessing from the dump would produce confident nonsense. It is
declared in :data:`JUDGED_CHECKS` so its absence is not read as a pass.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Sequence

from pandabot_core.active_qa.contract import Element, Observation
from pandabot_core.active_qa.rules import MAJOR, MINOR, Finding

__all__ = [
    "MIN_TOUCH_TARGET_DP", "JUDGED_CHECKS", "coverage_note",
    "android_a11y_findings", "dp_to_px",
]

#: Android's own minimum. Material and the Accessibility Scanner agree on 48dp.
MIN_TOUCH_TARGET_DP = 48

#: Checks that need the rendered pixels or human judgement. Declared so a report
#: can list them as unchecked rather than letting silence imply a pass.
JUDGED_CHECKS = {
    "text-contrast": "whether text contrasts enough with what is behind it",
    "image-contrast": "whether icons and controls contrast with their background",
    "meaningful-labels": "whether a contentDescription actually describes the control",
    "traversal-order": "whether the announced order matches the visual order",
}


def coverage_note() -> str:
    return (
        f"Checked from the uiautomator tree: control labelling, "
        f"{MIN_TOUCH_TARGET_DP}dp touch targets, and duplicate announcements. "
        "Contrast and label quality need the rendered pixels or judgement and were "
        "not checked; a clean result here is not a statement that the app is "
        "accessible."
    )


def dp_to_px(dp: float, density_dpi: int) -> float:
    """Density-independent pixels to device pixels."""
    return dp * density_dpi / 160.0


def _named(el: Element) -> bool:
    return bool((el.label or "").strip())


def _size_px(el: Element) -> tuple[int, int] | None:
    if not el.bounds:
        return None
    return el.bounds[2] - el.bounds[0], el.bounds[3] - el.bounds[1]


def android_a11y_findings(
    observation: Observation,
    *,
    density_dpi: int = 160,
    state: str = "",
    screen_id: str = "",
    step_index: int = -1,
    min_dp: int = MIN_TOUCH_TARGET_DP,
) -> list[Finding]:
    """Every Android accessibility finding for one state."""
    out: list[Finding] = []
    if observation.meta.get("in_app") is False:
        # Something that is not the app under test is on screen: a permission
        # dialog, the notification shade, the launcher. Judging it would file
        # accessibility findings against Android itself. Measured: run directly
        # against the shade, this pack cheerfully reported "gmail" and a Cloud
        # Print notification as duplicate labels.
        return out
    controls = [el for el in observation.interactive() if el.enabled]
    if not controls:
        return out

    minimum = dp_to_px(min_dp, density_dpi)

    # -- unlabelled controls ------------------------------------------------
    unlabelled = [el for el in controls if not _named(el)]
    if unlabelled:
        where = ", ".join(
            f"{el.role} at {tuple(el.bounds)}" if el.bounds else el.role
            for el in unlabelled[:5])
        if len(unlabelled) > 5:
            where += f", and {len(unlabelled) - 5} more"
        out.append(Finding(
            rule="a11y/unlabelled-control",
            severity=MAJOR,
            summary=f"{len(unlabelled)} control(s) have no label for a screen reader",
            observed=(f"These controls carry neither text nor a contentDescription, "
                      f"so TalkBack announces only their type: {where}."),
            expected=("Every interactive control needs a contentDescription or "
                      "visible text describing what it does."),
            state=state, element_identity=unlabelled[0].identity,
            screen_id=screen_id, step_index=step_index,
            details={"count": len(unlabelled),
                     "check": "unlabelled-control"},
        ))

    # -- touch targets ------------------------------------------------------
    small: list[tuple[Element, int, int]] = []
    for el in controls:
        size = _size_px(el)
        if size is None:
            continue
        width, height = size
        if width < minimum or height < minimum:
            small.append((el, width, height))
    if small:
        el, width, height = small[0]
        listed = ", ".join(
            f'"{e.label or e.role}" ({w}x{h}px)' for e, w, h in small[:5])
        if len(small) > 5:
            listed += f", and {len(small) - 5} more"
        out.append(Finding(
            rule="a11y/touch-target",
            severity=MINOR,
            summary=f"{len(small)} control(s) are smaller than {min_dp}dp",
            observed=(f"At {density_dpi}dpi, {min_dp}dp is {minimum:.0f}px. "
                      f"Below minimum: {listed}."),
            expected=(f"Touch targets should be at least {min_dp}dp on each side so "
                      "people with imprecise motor control can hit them."),
            state=state, element_identity=el.identity,
            screen_id=screen_id, step_index=step_index,
            details={"count": len(small), "min_px": round(minimum),
                     "density_dpi": density_dpi, "check": "touch-target"},
        ))

    # -- ambiguous duplicates ----------------------------------------------
    labels = Counter((el.label or "").strip().lower()
                     for el in controls if _named(el))
    duplicates = {label: n for label, n in labels.items() if n > 1}
    if duplicates:
        listed = ", ".join(f'"{label}" x{n}'
                           for label, n in sorted(duplicates.items())[:5])
        first = next(el for el in controls
                     if (el.label or "").strip().lower() in duplicates)
        out.append(Finding(
            rule="a11y/duplicate-label",
            severity=MINOR,
            summary=f"{len(duplicates)} label(s) are used by more than one control",
            observed=(f"Controls announced identically cannot be told apart by ear: "
                      f"{listed}."),
            expected=("Controls on the same screen should be announced "
                      "distinguishably, even when their visible text repeats."),
            state=state, element_identity=first.identity,
            screen_id=screen_id, step_index=step_index,
            details={"duplicates": duplicates, "check": "duplicate-label"},
        ))

    return out
