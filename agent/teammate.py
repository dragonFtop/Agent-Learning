"""s13: a persistent teammate runs on its own ``AgentRuntime``.

One teammate is one thread, one history and one working directory. It is not
a second agent loop: every turn is the same ``AgentRuntime.run`` used by the
lead. The teammate adds only the team protocol around it — inbox handling,
plan gates, task claiming and idle re-entry.
"""

from __future__ import annotations

import config
from agent.hooks import HookBus
from agent.runtime import AgentRuntime
from llm import block_get


class TeammateRuntime:
    def __init__(self, manager, record, prompt: str) -> None:
        self.manager = manager
        self.record = record
        self.name = record.name

        content = str(prompt).strip()
        task = self._assigned_task()
        if task is not None:
            content += (
                f"\n\n[Assigned task {task.id}] {task.subject}\n"
                f"{task.description}\nWork directory: {record.cwd}"
            )
        if record.gate == "required":
            content += (
                "\n\n[Plan required] Submit a plan with submit_plan and wait for "
                "Lead approval before changing files or using bash."
            )

        hooks = HookBus()
        hooks.register("PreToolUse", self._gate_hook)
        hooks.register("PreToolUse", self._permission_hook)
        self.runtime = AgentRuntime(
            client=manager.client,
            model=manager.model,
            workdir=manager.workspace,
            profile="teammate",
            agent_name=self.name,
            role=record.role,
            history=[{"role": "user", "content": content}],
            interactive=False,
            hooks=hooks,
            permissions=manager.permissions,
            registry=manager.registry,
            skill_loader=manager.skill_loader,
            console=manager.console,
            max_turns=config.SUBAGENT_MAX_TURNS,
        )
        self.runtime.tasks = manager.tasks
        self.runtime.teams = manager
        self.runtime.task_id = record.task_id
        self.runtime.set_cwd(record.cwd)

    # ------------------------------------------------------------- hooks
    def _assigned_task(self):
        if not self.record.task_id or self.manager.tasks is None:
            return None
        try:
            return self.manager.tasks.load(self.record.task_id)
        except (OSError, ValueError, TypeError):
            return None

    def _gate_hook(self, block, ctx) -> str | None:
        name = str(block_get(block, "name", ""))
        return self.manager.gate_check(self.name, name)

    def _permission_hook(self, block, ctx) -> str | None:
        name = str(block_get(block, "name", ""))
        args = block_get(block, "input", {}) or {}
        permissions = self.manager.permissions
        if permissions is None:
            return None
        return permissions.check(name, args, ctx)

    def set_assignment(self, task_id, cwd) -> None:
        self.runtime.task_id = task_id
        self.runtime.set_cwd(cwd)

    # ------------------------------------------------------------- inbox
    def handle_inbox(self) -> bool:
        return self.handle_messages(self.manager.bus.read_inbox(self.name))

    def handle_messages(self, messages: list[dict]) -> bool:
        """Append work messages to history; return True for a valid shutdown."""
        work_messages: list[str] = []
        for message in messages:
            msg_type = str(message.get("type") or "message")
            if msg_type == "shutdown_request":
                accepted, notice = self.manager.accept_shutdown(self.name, message)
                if not accepted:
                    work_messages.append(notice)
                    continue
                try:
                    self.manager.bus.send(
                        self.name,
                        "lead",
                        "Shutdown acknowledged.",
                        "shutdown_response",
                        {"request_id": notice, "approve": True},
                    )
                except (OSError, ValueError):
                    pass
                return True
            if msg_type == "plan_approval_response":
                work_messages.append(self.manager.apply_plan_response(self.name, message))
                continue
            if msg_type == "plan_request":
                work_messages.append(f"[Plan required] {message.get('content', '')}")
                continue
            work_messages.append(
                f"[Message from {message.get('from', '?')}] "
                f"{message.get('content', '')}"
            )
        if work_messages:
            self.runtime.history.append(
                {"role": "user", "content": "\n".join(work_messages)}
            )
        return False

    # -------------------------------------------------------------- turns
    def work(self) -> str:
        """Run one full teammate turn. Return ``idle`` or ``stop``."""
        if self.handle_inbox():
            return "stop"
        self.manager.set_status(self.name, "working")
        text = self.runtime.run(source="teammate")

        if text.startswith("[Agent error") or text.startswith("[Stopped:"):
            self.manager.send_to_lead(self.name, text, "error")
            return "stop"

        record = self.manager.record(self.name)
        if record is None:
            return "stop"
        if record.gate == "pending":
            self.manager.set_status(self.name, "waiting_approval")
            return "idle"

        summary = (text or self.runtime.last_text()).strip()
        if summary:
            self.manager.send_to_lead(self.name, summary, "result")
        self.manager.maybe_release_assignment(self.name)
        self.manager.set_status(self.name, "idle")
        self.manager.send_to_lead(self.name, "Waiting for more work.", "idle_notification")
        return "idle"

    def wait_for_work(self) -> bool:
        """Wait for a message or atomically claim the next ready task."""
        while not self.manager.stopping:
            inbox = self.manager.bus.wait_for_messages(
                self.name, self.manager.idle_interval
            )
            if inbox:
                return not self.handle_messages(inbox)
            task = self.manager.claim_next_ready(self.name)
            if task is None:
                continue
            record = self.manager.record(self.name)
            if record is None:
                return False
            cwd = record.cwd or self.manager.workspace
            self.runtime.history.append(
                {
                    "role": "user",
                    "content": (
                        f"[Auto-claimed task {task.id}] {task.subject}\n"
                        f"{task.description}\nWork directory: {cwd}"
                    ),
                }
            )
            return True
        return False

    def run(self) -> None:
        try:
            state = "continue"
            while not self.manager.stopping:
                if state == "idle" and not self.wait_for_work():
                    break
                state = self.work()
                if state == "stop":
                    break
        except Exception as error:  # noqa: BLE001 - report and leave cleanly
            self.manager.report_exception(self.name, error)
        finally:
            self.manager.teammate_finished(self.name)
