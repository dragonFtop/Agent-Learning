"""s06: subagent entry point.

The actual loop is ``AgentRuntime._run``. A subagent is a runtime with a fresh
history, a restricted tool profile and no interactive approvals. This module
keeps a small compatibility wrapper so scripts and older code can keep calling
``run_subagent``.
"""

from __future__ import annotations

from typing import Any


def run_subagent(prompt: str, *, runtime: Any = None, description: str = "subtask") -> str:
    if runtime is None:
        raise TypeError("run_subagent() requires a runtime; use AgentRuntime.spawn_subagent()")
    return runtime.spawn_subagent(prompt, description=description)
