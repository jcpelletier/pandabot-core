"""Tests for the PANDABOTQA.md reader.

The last block parses the two real briefs from the pilot repos when they are
checked out beside this one. A parser tested only against its own fixtures is a
parser tested against its own assumptions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pandabot_core.active_qa.brief import (
    BRIEF_FILENAME, ProjectBrief, load_brief, parse_brief, starter_brief,
)

BRIEF = """# PANDABOTQA.md — Space Trader

Preamble prose that belongs to no section.

```yaml
target: android
runner: devbox
package: com.example.spacetrader
activity: .MainActivity
build: "./gradlew :app:assembleDebug"
launch: "python run_emulator.py"
rules:
  packs: [universal, android_a11y]
  waivers:
    - {rule: U2, reason: "touch-only build, no pointer device"}
    - {rule: U3, reason: "no hardware keyboard flow"}
budgets: {min_minutes: 45, max_minutes: 180, max_actions: 6000}
scripts:
  seed: "qa/seed.sh"
bridge:
  kind: debug_console
```

## What this product is

A single-player space trading game.

## How to reach the interesting states

Open the debug console with a triple tap.

### A sub-heading that belongs to this section

`grant` gives 9999 credits.

## Focus

The station module build panel is new.

## Out of scope

- **The debug console itself.** It is a test tool.
- Anything that would spend real currency.
  - a nested note that is detail, not its own item

## Expected behaviour worth knowing

- **Oxygen falls when crew exceeds life support.** That is the mechanic.
- Higher-tier commodities cannot be bought from ships.

## Known issues (do not re-report)

- GAM-41: the freighter sprite clips the docking arm.
"""


def _brief() -> ProjectBrief:
    return parse_brief(BRIEF, path="PANDABOTQA.md")


# --- The config block -----------------------------------------------------

def test_config_scalars_are_read():
    b = _brief()
    assert b.present
    assert b.target == "android" and b.runner == "devbox"
    assert b.package == "com.example.spacetrader"
    assert b.activity == ".MainActivity"
    assert "gradlew" in b.build and "run_emulator" in b.launch


def test_rule_packs_and_waivers_are_read():
    b = _brief()
    assert b.rule_packs == ["universal", "android_a11y"]
    assert b.waivers == {"U2": "touch-only build, no pointer device",
                         "U3": "no hardware keyboard flow"}


def test_a_waiver_without_a_reason_still_records_one():
    """Every waiver is echoed in the report, so none may be silent."""
    b = parse_brief("```yaml\nrules:\n  waivers:\n    - {rule: U5}\n```\n")
    assert b.waivers == {"U5": "no reason given"}


def test_scripts_and_bridge_are_read():
    b = _brief()
    assert b.scripts == {"seed": "qa/seed.sh"}
    assert b.bridge == {"kind": "debug_console"}


def test_budgets_come_from_the_brief():
    budgets = _brief().budgets()
    assert budgets.min_seconds == 45 * 60
    assert budgets.max_seconds == 180 * 60
    assert budgets.max_actions == 6000


def test_the_operator_overrides_the_brief():
    """A brief sets a project's normal range, not a ceiling the person asking
    cannot move."""
    budgets = _brief().budgets(min_seconds=120.0)
    assert budgets.min_seconds == 120.0
    assert budgets.max_actions == 6000


def test_only_the_first_yaml_block_is_treated_as_config():
    """A brief may show a snippet of the app's own config in prose."""
    text = BRIEF + "\n## Testability notes\n\n```yaml\ntarget: nonsense\n```\n"
    assert parse_brief(text).target == "android"


def test_a_malformed_config_block_warns_rather_than_failing():
    b = parse_brief("```yaml\n: : not valid : :\n  - [\n```\n\n## Focus\n\nstill read\n")
    assert b.present
    assert b.warnings and "not valid YAML" in b.warnings[0]
    assert b.focus == "still read", "the prose half must survive"


def test_a_non_mapping_config_block_is_skipped_with_a_warning():
    b = parse_brief("```yaml\n- just\n- a list\n```\n")
    assert b.config == {} and b.warnings


# --- The prose half -------------------------------------------------------

def test_sections_are_matched_on_their_natural_titles():
    b = _brief()
    assert set(b.sections) >= {"product", "states", "focus", "out_of_scope",
                               "expected", "known_issues"}


def test_a_subheading_stays_inside_its_section():
    """goober-ranch's "### The gym pads" belongs to "how to reach the states"."""
    b = _brief()
    assert "9999 credits" in b.sections["states"]
    assert "A sub-heading" in b.sections["states"]


def test_bullets_are_extracted_with_emphasis_stripped():
    b = _brief()
    assert b.out_of_scope[0] == "The debug console itself. It is a test tool."
    assert len(b.out_of_scope) == 2, "a nested note is detail, not its own item"


def test_expected_behaviour_is_available_for_precision():
    """The highest-value section: "oxygen falls" is the mechanic, and a tester
    that does not know files it as a bug."""
    items = _brief().expected_behaviour
    assert any("Oxygen falls" in i for i in items)
    assert len(items) == 2


def test_known_issues_are_extracted():
    assert _brief().known_issues == [
        "GAM-41: the freighter sprite clips the docking arm."]


def test_preamble_prose_belongs_to_no_section():
    assert "Preamble prose" not in "".join(_brief().sections.values())


def test_model_context_leads_with_what_matters():
    ctx = _brief().model_context()
    assert ctx.index("What this product is") < ctx.index("Focus for this run")
    assert "Deliberate behaviour" in ctx
    assert "Oxygen falls" in ctx


def test_model_context_is_truncated_to_a_budget():
    long = BRIEF.replace("A single-player space trading game.", "x " * 8000)
    assert len(parse_brief(long).model_context(limit=1200)) <= 1200


# --- A missing brief ------------------------------------------------------

def test_a_missing_brief_is_not_an_error(tmp_path):
    """Refusing to run without one would make the file a gate rather than a help."""
    b = load_brief(tmp_path)
    assert b.present is False
    assert b.waivers == {} and b.out_of_scope == [] and b.target == ""
    assert b.budgets().max_actions > 0, "defaults still apply"


def test_load_reads_a_real_file(tmp_path):
    (tmp_path / BRIEF_FILENAME).write_text(BRIEF, encoding="utf-8")
    b = load_brief(tmp_path)
    assert b.present and b.target == "android"


def test_summary_tells_the_operator_what_was_read():
    s = _brief().summary()
    assert s["present"] and s["target"] == "android"
    assert s["waivers"] == {"U2": "touch-only build, no pointer device",
                            "U3": "no hardware keyboard flow"}
    assert s["known_issues"] == 1 and s["out_of_scope"] == 2


# --- The starter ----------------------------------------------------------

def test_the_starter_parses_as_a_valid_brief():
    b = parse_brief(starter_brief("jcpelletier/Thing", target="web"))
    assert b.present and b.target == "web" and b.runner == "panda"
    assert set(b.sections) >= {"product", "run", "states", "focus",
                               "out_of_scope", "expected", "known_issues"}


def test_the_starter_is_obviously_unedited():
    """An unedited starter should be visible in review."""
    assert starter_brief().count("TODO") >= 6


# --- Against the real pilot briefs ----------------------------------------

_PILOTS = {
    "Space-Trader": Path(r"C:\Users\genes\GitHub\Space-Trader"),
    "goober-ranch": Path(r"C:\Users\genes\GitHub\goober-ranch"),
}


@pytest.mark.parametrize("name", sorted(_PILOTS))
def test_the_real_pilot_briefs_parse(name):
    repo = _PILOTS[name]
    if not (repo / BRIEF_FILENAME).exists():
        pytest.skip(f"{name} is not checked out beside this repo")
    b = load_brief(repo)

    assert b.present, f"{name}'s brief should be present"
    assert not b.warnings, f"{name}'s brief produced warnings: {b.warnings}"
    assert b.target in ("web", "android", "roblox", "desktop"), b.target
    assert b.runner in ("panda", "devbox"), b.runner
    assert b.rule_packs, "a brief should declare its rule packs"
    assert b.waivers, f"{name} waives rules and each needs a stated reason"
    assert all(reason != "no reason given" for reason in b.waivers.values())
    # The sections that carry the weight.
    for key in ("product", "states", "out_of_scope", "expected"):
        assert b.sections.get(key), f"{name} is missing the {key!r} section"
    assert b.model_context(), "there should be prose for the model to read"
