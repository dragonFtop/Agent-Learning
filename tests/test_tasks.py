import json

import pytest

from agent.tasks import TASK_ID_PATTERN, TaskStore


def _store(tmp_path):
    return TaskStore(tmp_path / ".tasks", workspace=tmp_path)


def test_create_and_load_task(tmp_path):
    store = _store(tmp_path)
    task = store.create("Write schema", "add tables")

    assert TASK_ID_PATTERN.fullmatch(task.id)
    assert (tmp_path / ".tasks" / f"{task.id}.json").is_file()
    loaded = store.load(task.id)
    assert loaded.subject == "Write schema"
    assert loaded.status == "pending"
    assert loaded.blockedBy == []
    assert not list((tmp_path / ".tasks").glob("*.tmp"))


def test_dependencies_block_until_completed(tmp_path):
    store = _store(tmp_path)
    schema = store.create("schema")
    api = store.create("api")
    store.update_dependencies(api.id, [schema.id])

    blocked = store.claim(api.id, owner="lead")
    assert "Blocked by" in blocked
    assert store.load(api.id).status == "pending"

    assert "Claimed" in store.claim(schema.id, owner="lead")
    assert "Completed" in store.complete(schema.id, owner="lead")

    claimed = store.claim(api.id, owner="lead")
    assert f"Claimed {api.id}" in claimed
    completed = store.complete(api.id, owner="lead")
    assert f"Completed {api.id}" in completed


def test_complete_reports_newly_unblocked_tasks(tmp_path):
    store = _store(tmp_path)
    first = store.create("first")
    second = store.create("second")
    store.update_dependencies(second.id, [first.id])
    store.claim(first.id, owner="lead")

    message = store.complete(first.id, owner="lead")
    assert "Unblocked: second" in message


def test_cycle_and_self_dependency_rejected(tmp_path):
    store = _store(tmp_path)
    a = store.create("a")
    b = store.create("b")
    c = store.create("c")
    store.update_dependencies(a.id, [b.id])
    store.update_dependencies(b.id, [c.id])

    with pytest.raises(ValueError, match="cycle"):
        store.update_dependencies(c.id, [a.id])
    with pytest.raises(ValueError, match="itself"):
        store.update_dependencies(c.id, [c.id])
    with pytest.raises(ValueError, match="not found"):
        store.update_dependencies(c.id, ["task_deadbeef"])


def test_dependencies_locked_after_claim(tmp_path):
    store = _store(tmp_path)
    a = store.create("a")
    b = store.create("b")
    store.claim(a.id, owner="lead")

    with pytest.raises(ValueError, match="pending and unowned"):
        store.update_dependencies(a.id, [b.id])


def test_owner_must_match_to_complete_and_release(tmp_path):
    store = _store(tmp_path)
    task = store.create("work")
    store.claim(task.id, owner="lead")

    wrong = store.complete(task.id, owner="other")
    assert "owned by lead" in wrong
    assert "Released" in store.release(task.id, owner="lead")
    assert store.load(task.id).status == "pending"
    assert store.load(task.id).owner is None


def test_claim_next_and_ready_tasks(tmp_path):
    store = _store(tmp_path)
    first = store.create("first")
    second = store.create("second")
    store.update_dependencies(second.id, [first.id])

    assert [task.id for task in store.ready_tasks()] == [first.id]
    assert "Claimed" in store.claim_next(owner="lead")
    assert store.load(first.id).status == "in_progress"
    assert "No ready tasks" in store.claim_next(owner="lead")


def test_list_skips_corrupted_records(tmp_path):
    store = _store(tmp_path)
    good = store.create("good")
    (tmp_path / ".tasks" / "task_deadbeef.json").write_text("{not json", encoding="utf-8")

    assert [task.id for task in store.list()] == [good.id]
    assert "good" in store.render_list()


def test_invalid_task_ids_rejected(tmp_path):
    store = _store(tmp_path)
    for bad in ("../task_deadbeef", "task_xyz", "task_deadbeef.json"):
        with pytest.raises(ValueError):
            store.load(bad)
        assert store.exists(bad) is False


def test_render_and_json_include_dependencies(tmp_path):
    store = _store(tmp_path)
    a = store.create("a")
    b = store.create("b", "details")
    store.update_dependencies(b.id, [a.id])
    store.claim(a.id, owner="lead")
    store.set_worktree(a.id, ".worktrees/a")

    rendered = store.render_list()
    assert f"blockedBy: {a.id}" in rendered
    assert "worktree: .worktrees/a" in rendered
    data = json.loads(store.get_json(b.id))
    assert data["description"] == "details"
    assert data["blockedBy"] == [a.id]
