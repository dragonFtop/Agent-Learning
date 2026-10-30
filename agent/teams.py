"""s13: agent teams built on file mailboxes and explicit protocols.

The lead and teammates are separate ``AgentRuntime`` instances with separate
histories, but they share the workspace, the task board and one process. Team
coordination therefore uses simple, inspectable primitives:

* ``.mailboxes/<agent>.jsonl`` append-only files with destructive reads;
* request ids for plan approval and shutdown, so a stale response can never
  authorize a different assignment;
* a team-wide lock for the in-memory state (active records, protocol states).

Mailbox delivery is polled by the teammate threads and by the coordinator's
event pump. A teammate never mutates the lead's history directly.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.tasks import Task, TaskStore

VALID_AGENT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RESERVED_AGENT_NAMES = {"lead", "agent"}

WORKSPACE_TOOLS = {"bash", "read_file", "write_file", "edit_file", "glob"}
CHANGE_TOOLS = {"bash", "write_file", "edit_file"}


def validate_agent_name(name: str) -> str | None:
    if not isinstance(name, str) or not VALID_AGENT_NAME.fullmatch(name):
        return (
            "teammate name must be 1-64 letters, digits, underscores, or dashes"
        )
    if name.casefold() in RESERVED_AGENT_NAMES:
        return f"teammate name '{name}' is reserved by the runtime"
    return None


class MessageBus:
    """Thread-safe file mailboxes. A read consumes the inbox."""

    def __init__(self, workspace: str | Path, console: Any = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.directory = self.workspace / ".mailboxes"
        self.console = console
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)

    def _warn(self, text: str) -> None:
        if self.console is not None:
            self.console.warn(text)

    def _path(self, agent: str) -> Path:
        if not isinstance(agent, str) or not VALID_AGENT_NAME.fullmatch(agent):
            raise ValueError(f"Invalid mailbox recipient: {agent!r}")
        root = self.directory.resolve()
        if not root.is_relative_to(self.workspace):
            raise ValueError("Mailbox directory escapes the workspace")
        path = (root / f"{agent}.jsonl").resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Mailbox path escapes directory: {agent!r}")
        return path

    def _read_unlocked(self, agent: str) -> list[dict]:
        inbox = self._path(agent)
        if not inbox.exists():
            return []
        try:
            raw = inbox.read_text(encoding="utf-8")
        except OSError as error:
            self._warn(f"[bus] cannot read {agent}'s inbox: {error}")
            return []
        messages = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._warn(f"[bus] skipped a corrupted line in {agent}'s inbox")
                continue
            if isinstance(message, dict):
                messages.append(message)
        try:
            inbox.unlink()
        except OSError as error:
            self._warn(f"[bus] cannot clear {agent}'s inbox: {error}")
        return messages

    def send(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        msg_type: str = "message",
        metadata: dict | None = None,
    ) -> None:
        message = {
            "from": from_agent,
            "to": to_agent,
            "content": str(content),
            "type": msg_type,
            "ts": time.time(),
            "metadata": metadata or {},
        }
        with self._changed:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self._path(to_agent).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(message, ensure_ascii=True) + "\n")
            self._changed.notify_all()

    def read_inbox(self, agent: str) -> list[dict]:
        with self._lock:
            return self._read_unlocked(agent)

    def peek(self, agent: str) -> bool:
        with self._lock:
            inbox = self._path(agent)
            return inbox.exists() and inbox.stat().st_size > 0

    def wait_for_messages(self, agent: str, timeout: float | None = None) -> list[dict]:
        """Block until this process sees a message, a timeout, or a wake-up."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            while not self.peek(agent):
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return []
                self._changed.wait(remaining)
            return self._read_unlocked(agent)


@dataclass
class ProtocolState:
    request_id: str
    type: str  # plan_approval | shutdown
    sender: str
    target: str
    status: str = "pending"  # pending | approved | rejected
    payload: str = ""
    work_version: int | None = None
    task_id: str | None = None
    created_at: float = field(default_factory=time.time)


@dataclass
class TeammateRecord:
    name: str
    role: str
    require_plan: bool = False
    status: str = "working"  # working | waiting_approval | idle | stopping | stopped
    gate: str = "not_required"  # not_required | required | pending | approved | rejected
    plan_request_id: str | None = None
    task_id: str | None = None
    cwd: Path | None = None
    version: int = 0
    thread: threading.Thread | None = None
    runtime: Any = None


class TeamManager:
    def __init__(
        self,
        runtime: Any,
        *,
        tasks: TaskStore | None = None,
        worktrees: Any = None,
        console: Any = None,
        idle_interval: float = 2.0,
    ) -> None:
        self.runtime = runtime
        self.workspace = Path(getattr(runtime, "workdir", Path.cwd())).resolve()
        self.tasks = tasks if tasks is not None else getattr(runtime, "tasks", None)
        self.worktrees = worktrees
        self.client = getattr(runtime, "client", None)
        self.model = getattr(runtime, "model", None)
        self.registry = getattr(runtime, "registry", None)
        self.permissions = getattr(runtime, "permissions", None)
        self.skill_loader = getattr(runtime, "skill_loader", None)
        self.console = console if console is not None else getattr(runtime, "console", None)
        self.idle_interval = max(0.01, float(idle_interval))
        self.bus = MessageBus(self.workspace, self.console)
        self.active: dict[str, TeammateRecord] = {}
        self.pending_requests: dict[str, ProtocolState] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()

    # ------------------------------------------------------------ utilities
    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def _info(self, text: str) -> None:
        if self.console is not None:
            self.console.info(text)

    def _warn(self, text: str) -> None:
        if self.console is not None:
            self.console.warn(text)

    def record(self, name: str) -> TeammateRecord | None:
        with self._lock:
            return self.active.get(name)

    def set_status(self, name: str, status: str) -> None:
        with self._lock:
            record = self.active.get(name)
            if record is not None:
                record.status = status

    def _new_request_id(self) -> str:
        while True:
            request_id = f"req_{secrets.token_hex(3)}"
            if request_id not in self.pending_requests:
                return request_id

    def send_to_lead(self, name: str, content: str, msg_type: str) -> None:
        try:
            self.bus.send(name, "lead", content, msg_type)
        except (OSError, ValueError) as error:
            self._warn(f"[bus] cannot deliver {msg_type} from {name}: {error}")

    def report_exception(self, name: str, error: BaseException) -> None:
        self.send_to_lead(name, f"{type(error).__name__}: {error}", "error")

    # ------------------------------------------------------------- lifecycle
    def spawn(
        self,
        name: str,
        role: str,
        prompt: str,
        task_id: str | None = None,
        require_plan: bool = False,
        start: bool = True,
    ) -> str:
        if self.stopping:
            return "Error: the team manager is shutting down"
        name = str(name or "").strip()
        error = validate_agent_name(name)
        if error:
            return f"Error: {error}"
        role = str(role or "").strip()
        prompt = str(prompt or "").strip()
        if not role:
            return "Error: role is required"
        if not prompt:
            return "Error: prompt is required"
        task_id = str(task_id).strip() if task_id else None
        if task_id and self.tasks is None:
            return "Error: the task board is not available in this session"

        with self._lock:
            if any(existing.casefold() == name.casefold() for existing in self.active):
                return f"Error: Teammate '{name}' already exists"
            record = TeammateRecord(
                name=name,
                role=role,
                require_plan=bool(require_plan),
                gate="required" if require_plan else "not_required",
            )
            self.active[name] = record

        if task_id:
            try:
                claimed = self.tasks.claim(task_id, owner=name)
            except (OSError, ValueError, TypeError) as exc:
                claimed = f"Error: {exc}"
            if not claimed.startswith("Claimed "):
                self._drop_record(name)
                return f"Cannot spawn teammate '{name}': {claimed}"
            error = self._bind_assignment(record, task_id)
            if error:
                try:
                    self.tasks.release(task_id, owner=name)
                except (OSError, ValueError, TypeError):
                    pass
                self._drop_record(name)
                return f"Cannot spawn teammate '{name}': {error}"

        from agent.teammate import TeammateRuntime

        teammate = TeammateRuntime(self, record, prompt)
        with self._lock:
            record.runtime = teammate
        if start:
            thread = threading.Thread(
                target=teammate.run,
                name=f"teammate-{name}",
                daemon=True,
            )
            record.thread = thread
            thread.start()

        self._info(f"[teammate] {name} spawned as {role}")
        assigned = f" for {task_id}" if task_id else " without an initial Task"
        return (
            f"Teammate '{name}' spawned as {role}{assigned}. "
            "End this turn; the runtime will deliver its events."
        )

    def _drop_record(self, name: str) -> None:
        with self._lock:
            self.active.pop(name, None)

    def stop_all(self, timeout: float = 3.0) -> None:
        self._stop.set()
        with self._lock:
            records = list(self.active.values())
        for record in records:
            try:
                self.request_shutdown(record.name)
            except Exception:  # noqa: BLE001 - best effort during shutdown
                pass
        deadline = time.monotonic() + max(0.0, timeout)
        for record in records:
            thread = record.thread
            if (
                thread is not None
                and thread.is_alive()
                and thread is not threading.current_thread()
            ):
                thread.join(max(0.0, deadline - time.monotonic()))

    def teammate_finished(self, name: str) -> None:
        self.release_abandoned(name)
        with self._lock:
            record = self.active.pop(name, None)
            if record is not None:
                record.status = "stopped"

    # ------------------------------------------------------------ assignment
    def _bind_assignment(self, record: TeammateRecord, task_id: str) -> str | None:
        with self._lock:
            if self.tasks is None:
                return "Error: the task board is not available in this session"
            try:
                task = self.tasks.load(task_id)
            except (OSError, ValueError, TypeError) as exc:
                return f"Error: Task {task_id} is not usable: {exc}"
            if task.owner != record.name or task.status not in ("in_progress", "completed"):
                return f"Error: Task {task_id} is not claimed by {record.name}"
            if record.task_id and record.task_id != task_id:
                try:
                    current = self.tasks.load(record.task_id)
                except (OSError, ValueError, TypeError):
                    current = None
                if current is not None and current.status == "in_progress":
                    return (
                        f"Error: {record.name} already has active task "
                        f"{record.task_id}; complete it first"
                    )
            if task.worktree:
                if self.worktrees is None:
                    return (
                        f"Error: Worktree '{task.worktree}' is not available "
                        "in this session"
                    )
                path, error = self.worktrees.task_cwd(task)
                if error:
                    return f"Error: {error}"
            else:
                path = self.workspace
            record.task_id = task_id
            record.cwd = path
            record.version += 1
            record.plan_request_id = None
            record.gate = "required" if record.require_plan else "not_required"
            if record.runtime is not None:
                record.runtime.set_assignment(task_id, path)
            return None

    def after_claim(self, name: str, task_id: str) -> str | None:
        with self._lock:
            record = self.active.get(name)
            if record is None:
                return f"Error: Teammate '{name}' is not active"
            if record.task_id and record.task_id != task_id:
                return (
                    f"Error: {name} already has an active assignment; "
                    "complete it before claiming another task"
                )
            return self._bind_assignment(record, task_id)

    def _clear_assignment(self, record: TeammateRecord) -> None:
        record.task_id = None
        record.cwd = None
        record.version += 1
        record.plan_request_id = None
        record.gate = "required" if record.require_plan else "not_required"
        if record.runtime is not None:
            record.runtime.set_assignment(None, None)

    def maybe_release_assignment(self, name: str) -> bool:
        with self._lock:
            record = self.active.get(name)
            if record is None or not record.task_id:
                return False
            try:
                task = self.tasks.load(record.task_id) if self.tasks else None
            except (OSError, ValueError, TypeError):
                task = None
            if task is not None and task.status != "completed":
                return False
            self._clear_assignment(record)
            return True

    def release_abandoned(self, name: str) -> None:
        with self._lock:
            record = self.active.get(name)
            if record is None or not record.task_id:
                return
            task_id = record.task_id
            try:
                task = self.tasks.load(task_id) if self.tasks else None
            except (OSError, ValueError, TypeError):
                task = None
            if task is not None and task.status == "in_progress" and task.owner == name:
                try:
                    self.tasks.release(task_id, owner=name)
                except (OSError, ValueError, TypeError) as error:
                    self._warn(f"[teammate] could not release {task_id}: {error}")
            self._clear_assignment(record)

    def claim_next_ready(self, name: str) -> Task | None:
        with self._lock:
            record = self.active.get(name)
            if record is None or record.task_id is not None:
                return None
            if record.gate == "pending":
                return None
            candidates = self.tasks.ready_tasks() if self.tasks else []
        for task in candidates:
            if task.worktree:
                if self.worktrees is None:
                    continue
                _, error = self.worktrees.task_cwd(task)
                if error:
                    self._warn(f"[teammate] skipping {task.id}: {error}")
                    continue
            try:
                claimed = self.tasks.claim(task.id, owner=name)
            except (OSError, ValueError, TypeError) as error:
                self._warn(f"[teammate] claim {task.id} failed: {error}")
                continue
            if not claimed.startswith("Claimed "):
                continue
            with self._lock:
                record = self.active.get(name)
                if record is None:
                    try:
                        self.tasks.release(task.id, owner=name)
                    except (OSError, ValueError, TypeError):
                        pass
                    return None
                error = self._bind_assignment(record, task.id)
            if error:
                try:
                    self.tasks.release(task.id, owner=name)
                except (OSError, ValueError, TypeError):
                    pass
                self._warn(f"[teammate] cannot bind {task.id}: {error}")
                continue
            try:
                return self.tasks.load(task.id)
            except (OSError, ValueError, TypeError):
                return task
        return None

    # -------------------------------------------------------------- messaging
    def send_message(self, to: str, content: str) -> str:
        content = str(content)
        with self._lock:
            if to not in self.active:
                return f"Teammate '{to}' is not active"
        self.bus.send("lead", to, content)
        return f"Sent to {to}"

    def send_from(self, from_name: str, to: str, content: str) -> str:
        with self._lock:
            if from_name not in self.active:
                return f"Error: Teammate '{from_name}' is not active"
            if to != "lead" and to not in self.active:
                return f"Agent '{to}' is not active"
        self.bus.send(from_name, to, str(content))
        return f"Sent to {to}"

    def list_text(self) -> str:
        with self._lock:
            records = sorted(self.active.values(), key=lambda record: record.name)
        if not records:
            return "No active teammates."
        lines = []
        for record in records:
            details = []
            if record.task_id:
                details.append(f"task={record.task_id}")
            if record.gate not in ("not_required", "approved"):
                details.append(f"plan={record.gate}")
            suffix = f" ({', '.join(details)})" if details else ""
            lines.append(f"{record.name}: {record.status}{suffix}")
        return "\n".join(lines)

    def summary_text(self) -> str:
        return self.list_text()

    # -------------------------------------------------------------- protocols
    def submit_plan(self, name: str, plan: str) -> str:
        plan = str(plan or "").strip()
        if not plan:
            return "Error: plan cannot be empty"
        with self._lock:
            record = self.active.get(name)
            if record is None:
                return f"Error: Teammate '{name}' is not active"
            if record.gate == "pending":
                return "A plan is already waiting for review."
            request_id = self._new_request_id()
            self.pending_requests[request_id] = ProtocolState(
                request_id=request_id,
                type="plan_approval",
                sender=name,
                target="lead",
                payload=plan,
                work_version=record.version,
                task_id=record.task_id,
            )
            record.gate = "pending"
            record.plan_request_id = request_id
            record.status = "waiting_approval"
        self.bus.send(
            name,
            "lead",
            plan,
            "plan_approval_request",
            {"request_id": request_id},
        )
        return f"Plan submitted ({request_id}). Wait for Lead's decision."

    def apply_plan_response(self, name: str, message: dict) -> str:
        metadata = message.get("metadata") or {}
        request_id = str(metadata.get("request_id", ""))
        with self._lock:
            record = self.active.get(name)
            state = self.pending_requests.get(request_id)
            approve = bool(metadata.get("approve", False))
            valid = (
                record is not None
                and message.get("from") == "lead"
                and message.get("to") == name
                and state is not None
                and state.type == "plan_approval"
                and state.sender == name
                and state.target == "lead"
                and state.status in {"approved", "rejected"}
                and approve == (state.status == "approved")
                and record.plan_request_id == request_id
                and state.work_version == record.version
                and state.task_id == record.task_id
            )
            if not valid:
                return "[Ignored plan response: request mismatch]"
            record.gate = state.status
            record.plan_request_id = None
            record.status = "working"
            outcome = state.status
        content = str(message.get("content", ""))
        return f"[Plan {outcome}] {content}"

    def review_plan(self, request_id: str, approve: bool, feedback: str = "") -> str:
        with self._lock:
            state = self.pending_requests.get(request_id)
            if state is None:
                return f"Request {request_id} not found"
            if state.type != "plan_approval":
                return f"Request {request_id} is not a plan"
            if state.status != "pending":
                return f"Request {request_id} already {state.status}"
            record = self.active.get(state.sender)
            if record is None:
                return f"Teammate '{state.sender}' is no longer active"
            if record.plan_request_id != request_id:
                return f"Request {request_id} is not the current plan"
            if state.work_version != record.version or state.task_id != record.task_id:
                return f"Request {request_id} belongs to an earlier assignment"
            state.status = "approved" if approve else "rejected"
            target = state.sender
        content = feedback or (
            "Plan approved." if approve else "Revise the plan and submit it again."
        )
        self.bus.send(
            "lead",
            target,
            content,
            "plan_approval_response",
            {"request_id": request_id, "approve": bool(approve)},
        )
        return f"Plan {state.status} ({request_id})"

    def request_plan(self, name: str, task: str) -> str:
        with self._lock:
            record = self.active.get(name)
            if record is None:
                return f"Teammate '{name}' is not active"
            if record.gate == "pending":
                return "A plan is already waiting for review."
            record.gate = "required"
        content = str(task or "").strip() or "Submit a plan before changing the workspace."
        self.bus.send("lead", name, content, "plan_request")
        return f"Plan requested from {name}"

    def request_shutdown(self, name: str) -> str:
        with self._lock:
            record = self.active.get(name)
            if record is None:
                return f"Teammate '{name}' is not active"
            for state in self.pending_requests.values():
                if (
                    state.type == "shutdown"
                    and state.target == name
                    and state.status == "pending"
                ):
                    return f"Shutdown already requested from {name} ({state.request_id})"
            request_id = self._new_request_id()
            self.pending_requests[request_id] = ProtocolState(
                request_id=request_id,
                type="shutdown",
                sender="lead",
                target=name,
            )
        self.bus.send(
            "lead",
            name,
            "Finish the current step and shut down.",
            "shutdown_request",
            {"request_id": request_id},
        )
        return f"Shutdown requested from {name} ({request_id})"

    def accept_shutdown(self, name: str, message: dict) -> tuple[bool, str]:
        metadata = message.get("metadata") or {}
        request_id = str(metadata.get("request_id", ""))
        with self._lock:
            record = self.active.get(name)
            state = self.pending_requests.get(request_id)
            valid = (
                record is not None
                and message.get("from") == "lead"
                and message.get("to") == name
                and state is not None
                and state.type == "shutdown"
                and state.sender == "lead"
                and state.target == name
                and state.status == "pending"
                and record.status != "stopping"
            )
            if not valid:
                return False, "[Ignored shutdown request: request mismatch]"
            record.status = "stopping"
        return True, request_id

    def _match_response(
        self,
        response_type: str,
        request_id: str,
        approve: bool,
        from_agent: str,
        to_agent: str,
    ) -> bool:
        expected = {
            "shutdown": "shutdown_response",
            "plan_approval": "plan_approval_response",
        }
        with self._lock:
            state = self.pending_requests.get(request_id)
            valid = (
                state is not None
                and expected.get(state.type) == response_type
                and from_agent == state.target
                and to_agent == state.sender
                and state.status == "pending"
            )
            if not valid:
                self._warn(f"[protocol] ignored {response_type} {request_id}")
                return False
            state.status = "approved" if approve else "rejected"
        return True

    def drain_lead_inbox(self) -> list[dict]:
        messages = self.bus.read_inbox("lead")
        for message in messages:
            metadata = message.get("metadata") or {}
            request_id = str(metadata.get("request_id", ""))
            msg_type = str(message.get("type", ""))
            if request_id and msg_type.endswith("_response"):
                self._match_response(
                    msg_type,
                    request_id,
                    bool(metadata.get("approve", False)),
                    str(message.get("from", "")),
                    str(message.get("to", "")),
                )
        return messages

    def drain_inbox(self) -> list[dict]:
        return self.drain_lead_inbox()

    @staticmethod
    def format_events(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            metadata = message.get("metadata") or {}
            request_id = metadata.get("request_id")
            suffix = f" request_id={request_id}" if request_id else ""
            lines.append(
                f"[{message.get('type', 'message')}{suffix}] "
                f"{message.get('from', '?')}: {message.get('content', '')}"
            )
        return "[Team events]\n" + "\n".join(lines)

    # ----------------------------------------------------------------- gates
    def gate_check(self, name: str, tool_name: str) -> str | None:
        """PreToolUse gate for workspace tools and plan approval."""
        with self._lock:
            record = self.active.get(name)
            if record is None:
                return "Blocked: teammate is not active."
            if tool_name not in WORKSPACE_TOOLS:
                return None
            if record.task_id is None or record.cwd is None:
                return "Blocked: claim a Task before using workspace tools."
            try:
                task = self.tasks.load(record.task_id) if self.tasks else None
            except (OSError, ValueError, TypeError):
                task = None
            if task is None:
                return f"Blocked: assigned task {record.task_id} is not usable."
            if task.owner != name or task.status not in ("in_progress", "completed"):
                return "Blocked: the current task assignment is no longer active."
            if task.worktree:
                if self.worktrees is None:
                    return f"Blocked: worktree '{task.worktree}' is not available."
                path, error = self.worktrees.registered_worktree(task.worktree)
                if error:
                    return f"Blocked: {error}"
                if Path(record.cwd).resolve() != path:
                    return "Blocked: the assigned worktree changed during this task."
            if tool_name in CHANGE_TOOLS and record.gate not in ("not_required", "approved"):
                return (
                    f"Blocked: plan status is {record.gate}. Submit a plan with "
                    "submit_plan and wait for Lead approval before changing the "
                    "workspace."
                )
            return None

    # -------------------------------------------------------------- worktrees
    def worktree_users(self, name: str) -> list[str]:
        if self.worktrees is None:
            return []
        try:
            path = self.worktrees.path(name)
        except (OSError, ValueError):
            return []
        with self._lock:
            records = list(self.active.values())
        return sorted(
            record.name
            for record in records
            if record.cwd is not None and Path(record.cwd).resolve() == path
        )

    def remove_worktree(self, name: str, discard_changes: bool = False) -> str:
        users = self.worktree_users(name)
        if users:
            return (
                f"Error: Worktree '{name}' is still in use by "
                f"{', '.join(users)}; wait for the turn to end"
            )
        if self.worktrees is None:
            return "Error: worktrees are not available in this session"
        return self.worktrees.remove(name, discard_changes=discard_changes)
