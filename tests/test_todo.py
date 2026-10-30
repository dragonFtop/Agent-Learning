import pytest

from agent.todo import MAX_TODOS, TodoManager


def test_update_and_render():
    todos = TodoManager()
    rendered = todos.update(
        [
            {"content": "read code", "status": "completed"},
            {"content": "write fix", "status": "in_progress"},
            {"content": "run tests", "status": "pending"},
        ]
    )
    assert "[x] read code" in rendered
    assert "[>] write fix" in rendered
    assert "[ ] run tests" in rendered
    assert "(1/3 completed)" in rendered


def test_accepts_json_and_python_literal_strings():
    todos = TodoManager()
    todos.update('[{"content": "a", "status": "pending"}]')
    assert todos.items == [{"content": "a", "status": "pending"}]
    todos.update("[{'content': 'b', 'status': 'completed'}]")
    assert todos.items == [{"content": "b", "status": "completed"}]


def test_default_status_is_pending():
    todos = TodoManager()
    todos.update([{"content": "a"}])
    assert todos.items[0]["status"] == "pending"


def test_rejects_invalid_input():
    todos = TodoManager()
    with pytest.raises(ValueError):
        todos.update("not json")
    with pytest.raises(ValueError):
        todos.update({"content": "x"})
    with pytest.raises(ValueError):
        todos.update([{"content": "", "status": "pending"}])
    with pytest.raises(ValueError):
        todos.update([{"content": "x", "status": "done"}])
    with pytest.raises(ValueError):
        todos.update([{"content": str(i), "status": "pending"} for i in range(MAX_TODOS + 1)])


def test_only_one_in_progress():
    todos = TodoManager()
    with pytest.raises(ValueError):
        todos.update(
            [
                {"content": "a", "status": "in_progress"},
                {"content": "b", "status": "in_progress"},
            ]
        )


def test_should_remind_only_with_items_every_three_turns():
    todos = TodoManager()
    assert not todos.should_remind(10)
    todos.update([{"content": "a"}])
    assert not todos.should_remind(2)
    assert todos.should_remind(3)
    assert todos.should_remind(4)


def test_empty_render():
    assert TodoManager().render() == "No todos."
