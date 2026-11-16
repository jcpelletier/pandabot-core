"""llm_usage: the builder and story tags (builder-tiers phase 6) and per-story totals."""
import sqlite3

from pandabot_core.llm import usage


def test_log_call_tags_builder_and_story_and_story_usage_totals_them():
    usage.init_db()
    usage.log_call("harness:j:1", "deepseek-v4-flash", 1000, 100, "run 1",
                   context="goal:7", provider="openai_compat", builder="harness:desktop", story=12)
    usage.log_call("harness:j2:1", "deepseek-v4-flash", 2000, 200, "run 1",
                   context="goal:7", provider="openai_compat", builder="harness:deepseek", story=12)
    usage.log_call("goal-7", "deepseek-v4-flash", 500, 50, "pr-review",
                   context="goal:7", provider="openai_compat", story=12)
    usage.log_call("goal-7", "deepseek-v4-flash", 10, 1, "guard", context="goal:7",
                   provider="openai_compat", story=13)
    usage.log_call("other", "deepseek-v4-flash", 10, 1, "x", context="goal:8",
                   provider="openai_compat", story=12)

    by_story = usage.story_usage("goal:7")
    s12 = by_story[12]
    assert s12["builders"] == ["harness:desktop", "harness:deepseek"]
    assert s12["runs"] == 2 and s12["calls"] == 3
    assert s12["in_tokens"] == 3500 and s12["out_tokens"] == 350
    assert s12["cost_usd"] > 0
    assert by_story[13]["runs"] == 0 and by_story[13]["builders"] == []
    assert set(by_story) == {12, 13}


def test_old_rows_and_untagged_calls_still_work():
    # A table from before phase 6: no builder or story column.
    with sqlite3.connect(usage._db()) as conn:
        conn.execute("""CREATE TABLE llm_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
            conversation_id TEXT NOT NULL, model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
            cost_usd REAL NOT NULL, user_message TEXT NOT NULL,
            context TEXT NOT NULL DEFAULT 'main', provider TEXT NOT NULL DEFAULT 'anthropic')""")
        conn.execute("INSERT INTO llm_usage (ts, conversation_id, model, input_tokens, "
                     "output_tokens, cost_usd, user_message, context) "
                     "VALUES ('2026-11-01T00:00:00Z', 'c', 'm', 5, 1, 0.5, 'old', 'goal:3')")
    usage.init_db()
    usage.log_call("c2", "deepseek-v4-flash", 5, 1, "new", context="goal:3")
    by_story = usage.story_usage("goal:3")
    assert by_story[None]["calls"] == 2 and by_story[None]["runs"] == 0


def test_story_usage_is_empty_without_a_table():
    assert usage.story_usage("goal:1") == {}
