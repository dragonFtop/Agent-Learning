"""The one agent loop (s01/s02/s15).

Everything else in the harness is a service around this loop:

    user message -> prepare context -> call model
        -> tool_use? execute tools -> append tool_result -> repeat
        -> otherwise: Stop hook / goal gate / memory extraction -> return

Subagents and teammates are just additional ``AgentRuntime`` instances with
their own history, cwd and tool profile. There is no second loop.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import config
from agent.hooks import HookBus
from agent.permissions import PermissionPolicy, build_default_policy
from agent.prompts import build_system_prompt
from agent.todo import TodoManager
from llm import PromptTooLongError, block_get, block_type, call_llm, extract_text
from memory.context_compact import ContextManager
from tools.builtin import build_base_registry
from tools.output_limit import cap_output
from tools.registry import ToolContext, ToolRegistry


class NullConsole:
    def info(self, *args: Any, **kwargs: Any) -> None: ...
    def tool(self, *args: Any, **kwargs: Any) -> None: ...
    def warn(self, *args: Any, **kwargs: Any) -> None: ...


class AgentRuntime:
    def __init__(
        self,
        *,
        client: Any = None,
        model: str | None = None,
        workdir: str | Path | None = None,
        profile: str = "lead",
        agent_name: str = "lead",
        role: str = "",
        history: list | None = None,
        interactive: bool = True,
        hooks: HookBus | None = None,
        permissions: PermissionPolicy | None = None,
        registry: ToolRegistry | None = None,
        skill_loader: Any = None,
        context_manager: Any = None,
        console: Any = None,
        max_turns: int | None = None,
        goal: Any = None,
    ) -> None:
        self.client = client if client is not None else config.client
        self.model = model or config.MODEL
        self.workdir = Path(workdir).resolve() if workdir else config.WORKDIR
        self.profile = profile
        self.agent_name = agent_name
        self.role = role
        self.history: list = history if history is not None else []
        self.interactive = interactive
        self.console = console if console is not None else NullConsole()
        self.max_turns = max_turns if max_turns is not None else config.MAX_AGENT_TURNS

        self.hooks = hooks if hooks is not None else HookBus()
        if hooks is None:
            self.hooks.register("PreToolUse", self._permission_hook)

        self.permissions = permissions or build_default_policy(self.workdir)
        self.registry = registry if registry is not None else build_base_registry()
        if skill_loader is None:
            from config import SKILL_LOADER

            skill_loader = SKILL_LOADER
        self.skill_loader = skill_loader
        self.context_manager = (
            context_manager
            if context_manager is not None
            else ContextManager(
                self.history,
                workdir=self.workdir,
                output_dir=self.workdir / ".tool_outputs",
            )
        )

        # Feature services are attached by the coordinator/main wiring. Keeping
        # them optional lets subagents run with only the tools they need.
        self.todos = TodoManager() if profile == "lead" else None
        self.tasks = None
        self.worktrees = None
        self.background = None
        self.memory = None
        self.mcp = None
        self.workflow = None
        self.teams = None
        self.cron = None
        self.goal = goal

        self.task_id: str | None = None
        self._cwd_override: Path | None = None
        self._recalled_memories = ""
        self._turn_rounds = 0
        self._turns_since_todo_update = 0
        self._reactive_used = False
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ state
    @property
    def cwd(self) -> Path:
        return self._cwd_override or self.workdir

    def set_cwd(self, cwd: str | Path | None) -> None:
        self._cwd_override = Path(cwd).resolve() if cwd else None

    def tool_context(self, *, interactive: bool | None = None) -> ToolContext:
        return ToolContext(
            runtime=self,
            cwd=self.cwd,
            profile=self.profile,
            agent_name=self.agent_name,
            task_id=self.task_id,
            interactive=self.interactive if interactive is None else interactive,
        )

    def last_text(self) -> str:
        for message in reversed(self.history):
            if message.get("role") != "assistant":
                continue
            text = extract_text(message.get("content"))
            if text.strip():
                return text.strip()
        return ""

    # --------------------------------------------------------------- one turn
    def run_turn(self, user_text: str | None = None, *, source: str = "user") -> str:
        if user_text is not None:
            content = user_text
            injected = self.hooks.trigger("UserPromptSubmit", user_text)
            if isinstance(injected, str) and injected.strip():
                content = f"{injected}\n\n{user_text}"
            self.history.append({"role": "user", "content": content})
        self._refresh_memories()
        return self._run(source=source)

    def run(self, *, source: str = "user") -> str:
        """Continue the current history without appending a new user message."""
        return self._run(source=source)

    def _run(self, *, source: str) -> str:
        with self._lock:
            self._turn_rounds = 0
            self._reactive_used = False
            if self.goal is not None:
                try:
                    self.goal.begin_query()
                except Exception:
                    pass

            while True:
                if self._turn_rounds >= self.max_turns:
                    self.console.warn(f"[max turns reached: {self.max_turns}]")
                    return "[Stopped: maximum number of turns reached]"
                self._turn_rounds += 1

                self._inject_background_notifications()
                self._maybe_todo_reminder()
                self._prepare_context()

                try:
                    result = call_llm(
                        self.client,
                        model=self.model,
                        system=build_system_prompt(self),
                        messages=self.history,
                        tools=self.registry.subset(self.profile).to_anthropic_tools(),
                        max_tokens=config.MAX_TOKENS,
                    )
                except PromptTooLongError:
                    if self._reactive_used:
                        return "[Error: context is still too long after reactive compaction]"
                    self._reactive_used = True
                    self._force_reactive_compact()
                    continue
                except Exception as error:  # noqa: BLE001 - keep the CLI alive
                    self.console.warn(f"[LLM error] {type(error).__name__}: {error}")
                    return f"[Agent error: {type(error).__name__}: {error}]"

                # Preserve assistant content exactly. DeepSeek's thinking mode
                # requires the original thinking blocks on replay; never rebuild
                # this payload.
                self.history.append({"role": "assistant", "content": result.response.content})

                tool_blocks = [
                    block for block in result.response.content if block_type(block) == "tool_use"
                ]
                if not tool_blocks:
                    final = self._handle_stop(result.text, source)
                    if final is not None:
                        return final
                    continue

                results = self._execute_tool_blocks(tool_blocks)
                self.history.append({"role": "user", "content": results})

    # -------------------------------------------------------------- execution
    def _permission_hook(self, block: Any, ctx: ToolContext) -> str | None:
        name = str(block_get(block, "name", ""))
        args = block_get(block, "input", {}) or {}
        return self.permissions.check(name, args, ctx)

    def _execute_tool_blocks(self, blocks: list) -> list:
        ctx = self.tool_context()
        registry = self.registry.subset(self.profile)
        results: list[dict] = []
        for block in blocks:
            name = str(block_get(block, "name", ""))
            args = block_get(block, "input", {}) or {}
            tool_use_id = block_get(block, "id")
            self.console.tool(name, args)

            blocked = self.hooks.trigger("PreToolUse", block, ctx)
            if blocked is not None:
                output = str(blocked)
            else:
                output = registry.dispatch(name, args, ctx)

            replaced = self.hooks.trigger("PostToolUse", block, output, ctx)
            if replaced is not None:
                output = str(replaced)
            output = cap_output(name, str(output), workdir=self.workdir)

            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": output,
                }
            )

            if name == "todo_write":
                self._turns_since_todo_update = 0
            else:
                self._turns_since_todo_update += 1

        return results

    def _handle_stop(self, text: str, source: str) -> str | None:
        """Return a final string, or None to keep looping."""
        stop_message = self.hooks.trigger("Stop", self.history)
        if stop_message is not None:
            self.history.append({"role": "user", "content": str(stop_message)})
            return None

        if self.goal is not None and getattr(self.goal, "active", False) and source == "user":
            background = getattr(self, "background", None)
            background_running = False
            if background is not None:
                try:
                    background_running = bool(background.running_ids())
                except Exception:  # noqa: BLE001 - goal gating must not fail the turn
                    background_running = False
            try:
                decision = self.goal.evaluate_after_turn(
                    self.history,
                    text,
                    background_running=background_running,
                )
            except Exception as error:  # noqa: BLE001 - never leave a goal loop runaway
                self.console.warn(f"[goal evaluator error] {error}")
                self._remember_after_turn()
                return text
            action = getattr(decision, "action", "allow")
            if action == "continue":
                message = getattr(decision, "message", "") or getattr(decision, "reason", "")
                if not message:
                    return text
                self.history.append({"role": "user", "content": message})
                return None
            if action == "achieved":
                self._remember_after_turn()
                return text
            if action == "defer":
                self.console.info("[goal] deferred while background work runs")
                self._remember_after_turn()
                return text
            if action in ("failed", "limit", "error"):
                self._remember_after_turn()
                reason = getattr(decision, "reason", "")
                self.console.warn(f"[goal {action}] {reason}")
                return f"{text}\n\n[goal {action}: {reason}]" if text else f"[goal {action}: {reason}]"
            return text

        self._remember_after_turn()
        return text

    def _remember_after_turn(self) -> None:
        if self.profile != "lead" or self.memory is None:
            return
        try:
            self.memory.after_turn(self)
        except Exception as error:  # noqa: BLE001 - memory is best effort
            self.console.warn(f"[memory skipped] {type(error).__name__}: {error}")

    def _refresh_memories(self) -> None:
        if self.memory is None:
            return
        try:
            self._recalled_memories = self.memory.recall(self.history, self.client, self.model)
        except Exception as error:  # noqa: BLE001 - recall failure must not block the turn
            self._recalled_memories = ""
            self.console.warn(f"[memory recall skipped] {type(error).__name__}: {error}")

    # --------------------------------------------------------------- context
    def _prepare_context(self) -> None:
        manager = self.context_manager
        if manager is None:
            return
        manager.messages = self.history
        if hasattr(manager, "prepare"):
            manager.prepare(self.history)
            return
        manager.micro_compact_memory()
        if manager.estimate_tokens() > config.CONTEXT_TOKEN_THRESHOLD:
            compacted = manager.auto_compact_memory()
            if compacted is not self.history:
                self.history[:] = compacted

    def _force_reactive_compact(self) -> None:
        manager = self.context_manager
        if manager is None:
            return
        manager.messages = self.history
        if hasattr(manager, "reactive_compact"):
            manager.reactive_compact(self.history)
            return
        compacted = manager.auto_compact_memory()
        if compacted is not self.history:
            self.history[:] = compacted

    def force_compact(self, focus: str = "") -> str:
        manager = self.context_manager
        if manager is None:
            return "Error: context compaction is not available"
        manager.messages = self.history
        if hasattr(manager, "force_compact"):
            manager.force_compact(self.history, focus)
        else:
            compacted = manager.auto_compact_memory()
            if compacted is not self.history:
                self.history[:] = compacted
        return "Context compacted."

    # -------------------------------------------------------------- injections
    def _inject_background_notifications(self) -> None:
        background = self.background
        if background is None:
            return
        try:
            notifications = background.drain_notifications()
        except Exception:
            return
        if notifications:
            self.history.append({"role": "user", "content": "\n\n".join(notifications)})

    def _maybe_todo_reminder(self) -> None:
        todos = self.todos
        if todos is None or not getattr(todos, "items", None):
            return
        should = False
        if hasattr(todos, "should_remind"):
            should = todos.should_remind(self._turns_since_todo_update)
        elif self._turns_since_todo_update >= 3:
            should = True
        if should:
            self.history.append(
                {
                    "role": "user",
                    "content": "<reminder>Update your todo list before continuing.</reminder>",
                }
            )
            self._turns_since_todo_update = 0
            if hasattr(todos, "mark_reminded"):
                todos.mark_reminded()

    # --------------------------------------------------------------- subagent
    def spawn_subagent(self, prompt: str, *, description: str = "subtask") -> str:
        self.console.info(f"[subagent:{description}] starting with fresh context")
        sub = AgentRuntime(
            client=self.client,
            model=self.model,
            workdir=self.workdir,
            profile="subagent",
            agent_name=f"{self.agent_name}/subagent",
            history=[{"role": "user", "content": prompt}],
            interactive=False,
            hooks=self.hooks,
            permissions=self.permissions,
            registry=self.registry,
            skill_loader=self.skill_loader,
            context_manager=None,
            console=self.console,
            max_turns=config.SUBAGENT_MAX_TURNS,
        )
        result = sub.run(source="subagent")
        self.console.info(f"[subagent:{description}] finished")
        return result or "(no summary)"
