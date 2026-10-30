"""Compatibility wrapper for the original parent-agent entry point.

New code should build an ``AgentRuntime`` once and call ``run_turn`` so that
todos, tasks, memory and team state persist across user turns. This wrapper
creates a fresh runtime per call and is kept for old scripts only.
"""

from __future__ import annotations

from agent.runtime import AgentRuntime


def agent_loop(messages: list) -> str:
    from config import MODEL, WORKDIR, client

    runtime = AgentRuntime(
        client=client,
        model=MODEL,
        workdir=WORKDIR,
        history=messages,
    )
    return runtime.run()
