"""s10: durable task board with dependencies.

One JSON file per task in ``.tasks/task_<8hex>.json``. The store is shared by
the lead and teammates, so every mutation takes a process-wide lock and an
atomic file replace. The agent builds the whole graph first (create), then
adds edges (update), then claims work whose dependencies are complete.

Task lifecycle::

    pending --claim--> in_progress --complete--> completed
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path

try:  # POSIX cross-process lock
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None

try:  # Windows cross-process lock
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None

TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")
TASK_STATUSES = ("pending", "in_progress", "completed")
_LOCK_STATE = threading.local()


def _default_directory() -> Path:
    from config import TASKS_DIR

    return TASKS_DIR


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blockedBy: list[str]
    worktree: str | None = None


class TaskStore:
    def __init__(self, directory: str | Path | None = None, workspace: str | Path | None = None) -> None:
        self.directory = Path(directory) if directory is not None else _default_directory()
        self.workspace = Path(workspace).resolve() if workspace is not None else None
        self._lock = threading.RLock()

    # ------------------------------------------------------------- locking
    def _root(self, create: bool = False) -> Path:
        if create:
            self.directory.mkdir(parents=True, exist_ok=True)
        root = self.directory.resolve()
        if self.workspace is not None and not root.is_relative_to(self.workspace):
            raise ValueError("Task store escapes the workspace")
        return root

    @contextmanager
    def _locked(self, create: bool = True):
        """RLock plus a best-effort cross-process file lock."""
        with self._lock:
            depth = getattr(_LOCK_STATE, "depth", 0)
            handle = None
            if depth == 0:
                root = self._root(create=create)
                handle = (root / ".lock").open("a+b")
                if msvcrt is not None and os.name == "nt":
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    except OSError:
                        pass  # best effort; the in-process RLock still holds
                elif fcntl is not None:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    except OSError:
                        pass
                _LOCK_STATE.handle = handle
            _LOCK_STATE.depth = depth + 1
            try:
                yield
            finally:
                _LOCK_STATE.depth -= 1
                if depth == 0:
                    locked_handle = getattr(_LOCK_STATE, "handle", None)
                    if locked_handle is not None:
                        try:
                            if msvcrt is not None and os.name == "nt":
                                locked_handle.seek(0)
                                msvcrt.locking(locked_handle.fileno(), msvcrt.LK_UNLCK, 1)
                            elif fcntl is not None:
                                fcntl.flock(locked_handle.fileno(), fcntl.LOCK_UN)
                        except OSError:
                            pass
                        locked_handle.close()
                        del _LOCK_STATE.handle

    # --------------------------------------------------------------- paths
    def _path(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        return self._root() / f"{task_id}.json"

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    # --------------------------------------------------------------- CRUD
    def create(self, subject: str, description: str = "") -> Task:
        subject = str(subject).strip()
        if not subject:
            raise ValueError("Task subject cannot be empty")
        with self._locked():
            for _ in range(100):
                task = Task(
                    id=f"task_{secrets.token_hex(4)}",
                    subject=subject,
                    description=str(description),
                    status="pending",
                    owner=None,
                    blockedBy=[],
                )
                try:
                    with self._path(task.id).open("x", encoding="utf-8") as handle:
                        json.dump(asdict(task), handle, indent=2)
                        handle.write("\n")
                    return task
                except FileExistsError:
                    continue
        raise RuntimeError("Could not allocate a unique task ID")

    def load(self, task_id: str) -> Task:
        path = self._path(task_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Task file is not an object: {task_id}")
        if data.get("id") != task_id:
            raise ValueError(f"Task file ID does not match {task_id}")
        known = {field.name for field in fields(Task)}
        task = Task(**{key: value for key, value in data.items() if key in known})
        if task.status not in TASK_STATUSES:
            raise ValueError(f"Invalid task status: {task.status}")
        task.blockedBy = [str(item) for item in task.blockedBy]
        return task

    def save(self, task: Task) -> None:
        with self._locked():
            self._atomic_write(self._path(task.id), json.dumps(asdict(task), indent=2) + "\n")

    def exists(self, task_id: str) -> bool:
        try:
            return self._path(task_id).is_file()
        except ValueError:
            return False

    def list(self) -> list[Task]:
        if not self.directory.exists():
            return []
        tasks = []
        for path in sorted(self.directory.glob("task_*.json")):
            try:
                tasks.append(self.load(path.stem))
            except (OSError, ValueError, TypeError):
                continue
        return tasks

    # -------------------------------------------------------- dependencies
    def _depends_on(self, task_id: str, target_id: str) -> bool:
        pending = [task_id]
        visited = set()
        while pending:
            current = pending.pop()
            if current == target_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            try:
                pending.extend(self.load(current).blockedBy)
            except (OSError, ValueError):
                continue
        return False

    def update_dependencies(self, task_id: str, add_blocked_by: list[str]) -> Task:
        if not isinstance(add_blocked_by, list):
            raise ValueError("addBlockedBy must be a list of task IDs")
        with self._locked():
            task = self.load(task_id)
            if task.status != "pending" or task.owner is not None:
                raise ValueError(
                    f"Task {task_id} dependencies can only be updated while pending and unowned"
                )
            dependencies = list(dict.fromkeys(str(item) for item in add_blocked_by))
            for dependency in dependencies:
                if dependency == task_id:
                    raise ValueError("Task cannot depend on itself")
                if not self.exists(dependency):
                    raise ValueError(f"Dependency not found: {dependency}")
                if dependency not in task.blockedBy and self._depends_on(dependency, task_id):
                    raise ValueError(f"Dependency cycle detected: {task_id} -> {dependency}")
            task.blockedBy.extend(
                dependency for dependency in dependencies if dependency not in task.blockedBy
            )
            self._atomic_write(self._path(task.id), json.dumps(asdict(task), indent=2) + "\n")
            return task

    def incomplete_dependencies(self, task: Task) -> list[str]:
        incomplete = []
        for dependency in task.blockedBy:
            try:
                if self.load(dependency).status != "completed":
                    incomplete.append(dependency)
            except (OSError, ValueError):
                incomplete.append(dependency)
        return incomplete

    def can_start(self, task_id: str) -> bool:
        return not self.incomplete_dependencies(self.load(task_id))

    # ------------------------------------------------------------- claims
    def claim(self, task_id: str, owner: str = "agent") -> str:
        with self._locked():
            task = self.load(task_id)
            if task.status != "pending":
                return f"Task {task_id} is {task.status}, cannot claim"
            if task.owner is not None:
                return f"Task {task_id} is already owned by {task.owner}"
            dependencies = self.incomplete_dependencies(task)
            if dependencies:
                return f"Blocked by: {', '.join(dependencies)}"
            task.owner = owner
            task.status = "in_progress"
            self._atomic_write(self._path(task.id), json.dumps(asdict(task), indent=2) + "\n")
            return f"Claimed {task.id} ({task.subject})"

    def claim_next(self, owner: str = "agent") -> str:
        with self._locked():
            for task in self.list():
                if task.status == "pending" and not self.incomplete_dependencies(task):
                    return self.claim(task.id, owner)
        return "No ready tasks to claim"

    def release(self, task_id: str, owner: str = "agent") -> str:
        with self._locked():
            task = self.load(task_id)
            if task.status != "in_progress" or task.owner != owner:
                return f"Task {task_id} is not in progress under {owner}"
            task.status = "pending"
            task.owner = None
            self._atomic_write(self._path(task.id), json.dumps(asdict(task), indent=2) + "\n")
            return f"Released {task_id}"

    def complete(self, task_id: str, owner: str = "agent") -> str:
        with self._locked():
            task = self.load(task_id)
            if task.status != "in_progress":
                return f"Task {task_id} is {task.status}, cannot complete"
            if task.owner != owner:
                return f"Task {task_id} is owned by {task.owner}, not {owner}"

            ready_before = {
                candidate.id
                for candidate in self.list()
                if candidate.status == "pending" and candidate.blockedBy and self.can_start(candidate.id)
            }
            task.status = "completed"
            self._atomic_write(self._path(task.id), json.dumps(asdict(task), indent=2) + "\n")
            unblocked = [
                candidate.subject
                for candidate in self.list()
                if candidate.status == "pending"
                and candidate.blockedBy
                and candidate.id not in ready_before
                and self.can_start(candidate.id)
            ]
            message = f"Completed {task.id} ({task.subject})"
            if unblocked:
                message += f"\nUnblocked: {', '.join(unblocked)}"
            return message

    def set_worktree(self, task_id: str, worktree: str | None) -> Task:
        with self._locked():
            task = self.load(task_id)
            task.worktree = worktree
            self._atomic_write(self._path(task.id), json.dumps(asdict(task), indent=2) + "\n")
            return task

    def bind_worktree(self, task_id: str, worktree: str) -> Task:
        """Atomically attach a worktree while the task is still claimable."""
        with self._locked():
            task = self.load(task_id)
            if task.status != "pending" or task.owner is not None:
                raise ValueError(f"Task {task_id} must be pending and unowned")
            if task.worktree:
                raise ValueError(f"Task {task_id} already uses worktree '{task.worktree}'")
            for other in self.list():
                if other.id != task_id and other.worktree == worktree:
                    raise ValueError(f"Worktree '{worktree}' is already bound to another task")
            task.worktree = worktree
            self._atomic_write(self._path(task.id), json.dumps(asdict(task), indent=2) + "\n")
            return task

    def unbind_worktree(self, worktree: str) -> list[Task]:
        with self._locked():
            unbound = []
            for task in self.list():
                if task.worktree == worktree:
                    task.worktree = None
                    self._atomic_write(
                        self._path(task.id), json.dumps(asdict(task), indent=2) + "\n"
                    )
                    unbound.append(task)
            return unbound

    # --------------------------------------------------------------- views
    def render_list(self, limit: int = 50) -> str:
        tasks = self.list()
        if not tasks:
            return "No tasks. Use create_task to add some."
        markers = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        lines = []
        for task in tasks[:limit]:
            marker = markers.get(task.status, "[?]")
            dependencies = f" (blockedBy: {', '.join(task.blockedBy)})" if task.blockedBy else ""
            owner = f" [{task.owner}]" if task.owner else ""
            worktree = f" (worktree: {task.worktree})" if task.worktree else ""
            lines.append(
                f"{marker} {task.id}: {task.subject} [{task.status}]{owner}{dependencies}{worktree}"
            )
        if len(tasks) > limit:
            lines.append(f"... ({len(tasks) - limit} more tasks)")
        return "\n".join(lines)

    def get_json(self, task_id: str) -> str:
        return json.dumps(asdict(self.load(task_id)), indent=2)

    def summary_text(self) -> str:
        return self.render_list(limit=20)

    def ready_tasks(self) -> list[Task]:
        return [
            task
            for task in self.list()
            if task.status == "pending" and not self.incomplete_dependencies(task)
        ]
