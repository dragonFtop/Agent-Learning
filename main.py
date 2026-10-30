"""CLI entry point.

The REPL only reads input and dispatches slash commands. All turns go through
``RuntimeCoordinator`` so user input, cron firings, team events and background
notifications share one lead history and one turn lock.
"""

from __future__ import annotations

import json
import sys

import config
from agent.background import BackgroundManager
from agent.coordinator import ConsoleBroker, RuntimeCoordinator
from agent.cron import CronScheduler
from agent.goal import GoalController, GoalError, PromptGoalEvaluator
from agent.mcp import MCPManager
from agent.runtime import AgentRuntime
from agent.tasks import TaskStore
from agent.teams import TeamManager
from agent.workflow import WorkflowManager
from agent.worktree import WorktreeManager
from memory.longterm import LongTermMemory


class Console:
    def info(self, text: str) -> None:
        print(text)

    def tool(self, name: str, args: dict) -> None:
        preview = ", ".join(f"{key}={str(value)[:40]}" for key, value in list(args.items())[:2])
        print(f"\033[90m[tool] {name}({preview})\033[0m")

    def warn(self, text: str) -> None:
        print(f"\033[33m{text}\033[0m")


def build_runtime(console: Console) -> AgentRuntime:
    runtime = AgentRuntime(
        client=config.client,
        model=config.MODEL,
        workdir=config.WORKDIR,
        console=console,
    )
    runtime.tasks = TaskStore(workspace=config.WORKDIR)
    if config.TEAMS_ENABLED:
        runtime.worktrees = WorktreeManager(
            workspace=config.WORKDIR,
            tasks=runtime.tasks,
            console=console,
        )
        runtime.teams = TeamManager(
            runtime,
            tasks=runtime.tasks,
            worktrees=runtime.worktrees,
            console=console,
        )
    if config.BACKGROUND_ENABLED:
        runtime.background = BackgroundManager(workdir=config.WORKDIR)
    if config.CRON_ENABLED:
        runtime.cron = CronScheduler(console=console)
    if config.MEMORY_ENABLED:
        runtime.memory = LongTermMemory(model=config.MODEL)
    if config.MCP_ENABLED:
        runtime.mcp = MCPManager(
            workspace=config.WORKDIR,
            registry=runtime.registry,
            permissions=runtime.permissions,
            config_path=config.MCP_CONFIG_FILE,
            console=console,
        )
    if config.WORKFLOW_ENABLED:
        runtime.workflow = WorkflowManager(
            workspace=config.WORKDIR,
            client=config.client,
            model=config.MODEL,
            console=console,
        )
    runtime.goal = GoalController(
        PromptGoalEvaluator(config.client, config.MODEL),
        block_cap=config.GOAL_BLOCK_CAP,
    )
    return runtime


def handle_command(command: str, runtime: AgentRuntime, coordinator: RuntimeCoordinator) -> bool:
    """Return False when the REPL should exit."""
    name, _, argument = command[1:].partition(" ")
    name = name.strip().lower()
    argument = argument.strip()

    if name in ("q", "quit", "exit"):
        return False
    if name in ("help", "?"):
        print(
            "/help            show this help\n"
            "/tasks           list the durable task board\n"
            "/teammates       list active teammates\n"
            "/crons           list scheduled jobs\n"
            "/mcp             list MCP servers and tools\n"
            "/workflows       list workflows and the last run\n"
            "/workflow NAME [JSON args]  run a workflow directly\n"
            "/goal [condition|clear]     show, set, or clear the goal loop\n"
            "/memory          show the memory catalog\n"
            "/compact [focus] summarize the conversation\n"
            "/quit            leave the session"
        )
    elif name == "tasks":
        print(runtime.tasks.render_list() if runtime.tasks else "Task board disabled.")
    elif name == "teammates":
        print(runtime.teams.list_text() if runtime.teams else "Agent teams disabled.")
    elif name == "crons":
        print(runtime.cron.list_text() if runtime.cron else "Cron scheduler disabled.")
    elif name == "mcp":
        print(runtime.mcp.list_tools_text() if runtime.mcp else "MCP disabled.")
    elif name == "workflows":
        print(runtime.workflow.list_text() if runtime.workflow else "Workflows disabled.")
    elif name == "workflow":
        if runtime.workflow is None:
            print("Workflows disabled.")
        elif not argument:
            print(runtime.workflow.catalog_text())
        else:
            workflow_name, _, raw_args = argument.partition(" ")
            workflow_args = {}
            if raw_args.strip():
                try:
                    workflow_args = json.loads(raw_args)
                except json.JSONDecodeError as error:
                    print(f"Error: args must be valid JSON: {error}")
                    return True
                if not isinstance(workflow_args, dict):
                    print("Error: args must be a JSON object.")
                    return True
            print(coordinator.run_locked(runtime.workflow.run, workflow_name, workflow_args))
    elif name == "goal":
        goal = runtime.goal
        if goal is None:
            print("Goal loop disabled.")
        elif not argument:
            print(coordinator.run_locked(goal.status_text))
        elif argument.lower() in ("clear", "stop", "off", "reset", "none", "cancel"):
            print(coordinator.run_locked(goal.clear, argument.lower()))
        else:
            try:
                state = coordinator.run_locked(goal.set_goal, argument)
            except GoalError as error:
                print(f"Error: {error}")
            else:
                print(f"Goal set: {state.condition}")
                print(f"Stop-hook block cap: {goal.block_cap}")
    elif name == "memory":
        catalog = runtime.memory.catalog_text() if runtime.memory else ""
        print(catalog or "No memories stored.")
    elif name == "compact":
        print(coordinator.run_locked(runtime.force_compact, argument))
    else:
        print(f"Unknown command: /{name}. Type /help.")
    return True


def _configure_stdio() -> None:
    """Use UTF-8 for piped input/output on Windows, where the default is GBK."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def main() -> None:
    _configure_stdio()
    console = Console()
    runtime = build_runtime(console)
    coordinator = RuntimeCoordinator(
        runtime,
        console=console,
        broker=ConsoleBroker(),
        on_result=print,
    )

    config.ensure_runtime_dirs()
    coordinator.start()
    print(f"Agent ready. Workspace: {config.WORKDIR}")
    print(f"Model: {config.MODEL} | permission mode: {config.PERMISSION_MODE}")
    print("Type /help for commands; q / exit / empty line quits.\n")

    try:
        while True:
            try:
                query = input("\033[36mDragonfeng >> \033[0m")
            except (EOFError, KeyboardInterrupt):
                print()
                break

            stripped = query.strip()
            if not stripped or stripped.lower() in ("q", "quit", "exit"):
                break
            if stripped.startswith("/"):
                if not handle_command(stripped, runtime, coordinator):
                    break
                continue
            coordinator.run_user_input(stripped)
            print()
    finally:
        coordinator.stop()


if __name__ == "__main__":
    main()
