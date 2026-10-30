"""s04: a tiny event bus that lets features hook the loop without editing it.

Events:
    UserPromptSubmit(query)             before a user turn is appended
    PreToolUse(block, ctx)              before a tool handler; non-None blocks
    PostToolUse(block, output, ctx)     after a handler; non-None replaces output
    Stop(history)                       when the model stops using tools

The first non-None return value wins. That single rule is enough for
permission checks, logging, output rewriting and stop-hook style automation.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

HookCallback = Callable[..., Any]


class HookBus:
    EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")

    def __init__(self) -> None:
        self._hooks: dict[str, list[HookCallback]] = {event: [] for event in self.EVENTS}
        self._lock = threading.RLock()

    def register(self, event: str, callback: HookCallback) -> None:
        if event not in self._hooks:
            raise ValueError(f"Unknown hook event: {event}")
        with self._lock:
            self._hooks[event].append(callback)

    def trigger(self, event: str, *args: Any) -> Any:
        if event not in self._hooks:
            raise ValueError(f"Unknown hook event: {event}")
        with self._lock:
            callbacks = list(self._hooks[event])
        for callback in callbacks:
            result = callback(*args)
            if result is not None:
                return result
        return None

    def clear(self, event: str | None = None) -> None:
        with self._lock:
            if event is None:
                for callbacks in self._hooks.values():
                    callbacks.clear()
            elif event in self._hooks:
                self._hooks[event].clear()
            else:
                raise ValueError(f"Unknown hook event: {event}")

    def count(self, event: str) -> int:
        with self._lock:
            return len(self._hooks.get(event, []))
