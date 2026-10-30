"""s05: session todo list.

This is deliberately *not* the persistent task board (s10). Todos are a
short-lived planning aid for the current conversation; tasks are durable,
shareable work items with dependencies.
"""

from __future__ import annotations

import ast
import json

MAX_TODOS = 20
REMINDER_EVERY = 3


class TodoManager:
    def __init__(self, reminder_every: int = REMINDER_EVERY) -> None:
        self.items: list[dict] = []
        self.reminder_every = max(1, reminder_every)

    def update(self, todos: list | str) -> str:
        if isinstance(todos, str):
            try:
                todos = json.loads(todos)
            except json.JSONDecodeError:
                try:
                    todos = ast.literal_eval(todos)
                except (SyntaxError, ValueError) as error:
                    raise ValueError("todos must be a list or a JSON array string") from error

        if not isinstance(todos, list):
            raise ValueError("todos must be a list")
        if len(todos) > MAX_TODOS:
            raise ValueError(f"At most {MAX_TODOS} todos are allowed")

        validated: list[dict] = []
        in_progress = 0
        for index, todo in enumerate(todos):
            if not isinstance(todo, dict):
                raise ValueError(f"todos[{index}] must be an object")
            content = str(todo.get("content", "")).strip()
            status = str(todo.get("status", "pending")).lower()
            if not content:
                raise ValueError(f"todos[{index}] requires content")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"todos[{index}] has invalid status '{status}'")
            if status == "in_progress":
                in_progress += 1
            validated.append({"content": content, "status": status})

        if in_progress > 1:
            raise ValueError("Only one todo can be in_progress at a time")

        self.items = validated
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "No todos."
        markers = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        lines = [f"{markers[item['status']]} {item['content']}" for item in self.items]
        done = sum(item["status"] == "completed" for item in self.items)
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)

    def should_remind(self, turns_since_update: int) -> bool:
        return bool(self.items) and turns_since_update >= self.reminder_every
