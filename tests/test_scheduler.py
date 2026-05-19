import datetime
from pandabot_core import scheduler


def _future(minutes: int = 60) -> str:
    """Return a naive local-time ISO string `minutes` from now."""
    return (datetime.datetime.now() + datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M")


def _past(minutes: int = 5) -> str:
    return (datetime.datetime.now() - datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M")


def test_init_and_add(tmp_path, monkeypatch):
    monkeypatch.setenv("PANDABOT_DATA_DIR", str(tmp_path))
    scheduler.init_db()
    task_id = scheduler.add_task(
        fire_at_local=_future(),
        channel_id=12345,
        description="test task",
    )
    assert task_id is not None and task_id > 0


def test_no_due_tasks_in_future(tmp_path, monkeypatch):
    monkeypatch.setenv("PANDABOT_DATA_DIR", str(tmp_path))
    scheduler.init_db()
    scheduler.add_task(fire_at_local=_future(), channel_id=1, description="future")
    due = scheduler.get_due_tasks()
    assert len(due) == 0


def test_due_tasks_in_past(tmp_path, monkeypatch):
    monkeypatch.setenv("PANDABOT_DATA_DIR", str(tmp_path))
    scheduler.init_db()
    scheduler.add_task(fire_at_local=_past(), channel_id=1, description="past task")
    due = scheduler.get_due_tasks()
    assert len(due) == 1
    assert due[0]["description"] == "past task"


def test_mark_done(tmp_path, monkeypatch):
    monkeypatch.setenv("PANDABOT_DATA_DIR", str(tmp_path))
    scheduler.init_db()
    task_id = scheduler.add_task(fire_at_local=_past(), channel_id=1, description="done task")
    scheduler.mark_done(task_id)
    due = scheduler.get_due_tasks()
    assert len(due) == 0


def test_cancel_task(tmp_path, monkeypatch):
    monkeypatch.setenv("PANDABOT_DATA_DIR", str(tmp_path))
    scheduler.init_db()
    task_id = scheduler.add_task(fire_at_local=_future(), channel_id=1, description="cancel me")
    assert scheduler.cancel_task(task_id) is True
    pending = scheduler.list_pending()
    assert all(t["id"] != task_id for t in pending)


def test_list_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("PANDABOT_DATA_DIR", str(tmp_path))
    scheduler.init_db()
    scheduler.add_task(fire_at_local=_future(), channel_id=1, description="pending 1")
    scheduler.add_task(fire_at_local=_future(120), channel_id=1, description="pending 2")
    pending = scheduler.list_pending()
    assert len(pending) == 2


def test_monthly_recurrence_end_of_month(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv("PANDABOT_DATA_DIR", str(tmp_path))
    db_path = tmp_path / "scheduler.db"
    scheduler.init_db()

    # Jan 31 -> Feb 29 (leap year)
    original_datetime = datetime.datetime

    class MockDatetime(original_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz:
                return original_datetime(2024, 1, 31, 12, 0, tzinfo=datetime.timezone.utc).astimezone(tz)
            return original_datetime(2024, 1, 31, 12, 0)

        @classmethod
        def fromisoformat(cls, date_string):
            return original_datetime.fromisoformat(date_string)

    monkeypatch.setattr(datetime, "datetime", MockDatetime)

    task_id = scheduler.add_task(
        fire_at_local="2024-01-31T12:00:00",
        channel_id=123,
        description="End of month task",
        recurrence_rule="monthly:31"
    )

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        task = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()

    scheduler.schedule_next_recurring(task)

    pending = scheduler.list_pending()
    new_tasks = [t for t in pending if t["id"] != task_id]
    assert len(new_tasks) == 1
    new_task = new_tasks[0]

    # Should be Feb 29th 2024 12:00 UTC
    expected_fire_at = original_datetime(2024, 2, 29, 12, 0, tzinfo=datetime.timezone.utc).isoformat()
    assert new_task["fire_at"] == expected_fire_at
