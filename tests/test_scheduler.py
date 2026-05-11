import datetime
from pandabot_core import scheduler


def _future(minutes: int = 60) -> str:
    """Return a naive local-time ISO string `minutes` from now."""
    return (datetime.datetime.now() + datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M")


def _past(minutes: int = 5) -> str:
    return (datetime.datetime.now() - datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M")


def test_init_and_add(tmp_path):
    import os
    os.environ["PANDABOT_DATA_DIR"] = str(tmp_path)
    scheduler.init_db()
    task_id = scheduler.add_task(
        fire_at_local=_future(),
        channel_id=12345,
        description="test task",
    )
    assert task_id is not None and task_id > 0


def test_no_due_tasks_in_future(tmp_path):
    import os
    os.environ["PANDABOT_DATA_DIR"] = str(tmp_path)
    scheduler.init_db()
    scheduler.add_task(fire_at_local=_future(), channel_id=1, description="future")
    due = scheduler.get_due_tasks()
    assert len(due) == 0


def test_due_tasks_in_past(tmp_path):
    import os
    os.environ["PANDABOT_DATA_DIR"] = str(tmp_path)
    scheduler.init_db()
    scheduler.add_task(fire_at_local=_past(), channel_id=1, description="past task")
    due = scheduler.get_due_tasks()
    assert len(due) == 1
    assert due[0]["description"] == "past task"


def test_mark_done(tmp_path):
    import os
    os.environ["PANDABOT_DATA_DIR"] = str(tmp_path)
    scheduler.init_db()
    task_id = scheduler.add_task(fire_at_local=_past(), channel_id=1, description="done task")
    scheduler.mark_done(task_id)
    due = scheduler.get_due_tasks()
    assert len(due) == 0


def test_cancel_task(tmp_path):
    import os
    os.environ["PANDABOT_DATA_DIR"] = str(tmp_path)
    scheduler.init_db()
    task_id = scheduler.add_task(fire_at_local=_future(), channel_id=1, description="cancel me")
    assert scheduler.cancel_task(task_id) is True
    pending = scheduler.list_pending()
    assert all(t["id"] != task_id for t in pending)


def test_list_pending(tmp_path):
    import os
    os.environ["PANDABOT_DATA_DIR"] = str(tmp_path)
    scheduler.init_db()
    scheduler.add_task(fire_at_local=_future(), channel_id=1, description="pending 1")
    scheduler.add_task(fire_at_local=_future(120), channel_id=1, description="pending 2")
    pending = scheduler.list_pending()
    assert len(pending) == 2
