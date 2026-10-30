"""s13: task-bound Git worktrees.

A worktree is a separate checkout of the same repository on its own branch.
Binding one to a durable task gives a teammate an isolated working directory
without copying the repository. The rules here are deliberately fail-closed:

* names are restricted and paths must stay under ``.worktrees``;
* a checkout is only usable when Git's registry reports the expected path and
  ``refs/heads/wt/<name>`` branch;
* removal refuses active tasks, live teammate assignments and dirty changes,
  and always retains the branch as a recovery point.
"""

from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path
from typing import Any

from agent.tasks import Task, TaskStore

VALID_WORKTREE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
GIT_TIMEOUT_SECONDS = 30
MAX_GIT_OUTPUT = 5000


def validate_worktree_name(name: str) -> str | None:
    if not isinstance(name, str) or not VALID_WORKTREE_NAME.fullmatch(name):
        return (
            "worktree name must be 1-64 letters, digits, dots, underscores, or "
            "dashes, and start with a letter or digit"
        )
    if name in {".", ".."} or ".." in name:
        return "worktree name cannot contain '..'"
    return None


class WorktreeManager:
    def __init__(
        self,
        workspace: str | Path,
        tasks: TaskStore,
        console: Any = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.tasks = tasks
        self.console = console
        self.worktrees_dir = self.workspace / ".worktrees"
        self._lock = threading.RLock()

    # ------------------------------------------------------------- helpers
    def _info(self, text: str) -> None:
        if self.console is not None:
            self.console.info(text)

    def path(self, name: str) -> Path:
        error = validate_worktree_name(name)
        if error:
            raise ValueError(error)
        root = self.worktrees_dir.resolve()
        if not root.is_relative_to(self.workspace):
            raise ValueError("Worktree directory escapes the workspace")
        path = (root / name).resolve()
        if path == root or not path.is_relative_to(root):
            raise ValueError(f"Worktree path escapes directory: {name!r}")
        return path

    def branch(self, name: str) -> str:
        return f"wt/{name}"

    def _git(self, args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
        """Run Git with argv (never a shell) and return raw combined output."""
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(cwd or self.workspace),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return False, f"{type(error).__name__}: {error}"
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        return result.returncode == 0, output

    def _shown(self, output: str) -> str:
        text = output or "(no output)"
        return text[:MAX_GIT_OUTPUT]

    def registered_worktrees(self) -> tuple[dict[Path, dict[str, str]], str | None]:
        ok, output = self._git(["worktree", "list", "--porcelain"])
        if not ok:
            return {}, f"cannot read Git worktree registry: {self._shown(output)}"
        entries: dict[Path, dict[str, str]] = {}
        current: dict[str, str] = {}
        for line in output.splitlines() + [""]:
            if not line.strip():
                raw_path = current.get("worktree")
                if raw_path:
                    entries[Path(raw_path).resolve()] = current
                current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        return entries, None

    def registered_worktree(self, name: str) -> tuple[Path | None, str | None]:
        try:
            path = self.path(name)
        except ValueError as error:
            return None, str(error)
        entries, error = self.registered_worktrees()
        if error:
            return None, error
        if path not in entries:
            return None, f"worktree '{name}' is not registered with Git"
        if not path.is_dir():
            return None, f"worktree '{name}' is missing at {path}"
        expected_branch = f"refs/heads/{self.branch(name)}"
        if entries[path].get("branch") != expected_branch:
            return (
                None,
                f"worktree '{name}' is not registered on expected branch "
                f"'{self.branch(name)}'",
            )
        return path, None

    def task_cwd(self, task: Task) -> tuple[Path | None, str | None]:
        """Resolve a task's working directory; unbound tasks use the workspace."""
        if not task.worktree:
            return self.workspace, None
        return self.registered_worktree(task.worktree)

    def _bound_tasks(self, name: str) -> list[Task]:
        return [task for task in self.tasks.list() if task.worktree == name]

    # -------------------------------------------------------------- create
    def create(self, name: str, task_id: str) -> str:
        error = validate_worktree_name(name)
        if error:
            return f"Error: {error}"
        try:
            path = self.path(name)
        except ValueError as exc:
            return f"Error: {exc}"
        branch = self.branch(name)

        with self._lock:
            try:
                task = self.tasks.load(task_id)
            except (OSError, ValueError, TypeError) as exc:
                return f"Error: Task {task_id} is not usable: {exc}"
            if task.status != "pending" or task.owner is not None:
                return f"Error: Task {task_id} must be pending and unowned"
            if task.worktree:
                return f"Error: Task {task_id} already uses worktree '{task.worktree}'"
            for other in self.tasks.list():
                if other.id != task_id and other.worktree == name:
                    return f"Error: Worktree '{name}' is already bound to another task"
            if path.exists():
                return f"Error: Worktree path already exists: {path}"

            ok, root = self._git(["rev-parse", "--show-toplevel"])
            if not ok or Path(root).resolve() != self.workspace:
                return "Error: Working directory must be the root of a Git repository"
            ok, branch_check = self._git(["check-ref-format", "--branch", branch])
            if not ok:
                return f"Error: Invalid worktree branch '{branch}': {self._shown(branch_check)}"
            exists, _ = self._git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
            if exists:
                return f"Error: Branch '{branch}' already exists"
            entries, registry_error = self.registered_worktrees()
            if registry_error:
                return f"Error: {registry_error}"
            if path in entries:
                return f"Error: Worktree path is already registered: {path}"

            self.worktrees_dir.mkdir(parents=True, exist_ok=True)
            ok, result = self._git(["worktree", "add", "-b", branch, str(path), "HEAD"])
            if not ok:
                entries, registry_error = self.registered_worktrees()
                branch_exists, _ = self._git(
                    ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
                )
                artifacts = []
                if path.exists():
                    artifacts.append(f"checkout path '{path}'")
                if registry_error is None and path in entries:
                    artifacts.append("registered Git worktree")
                if branch_exists:
                    artifacts.append(f"branch '{branch}'")
                if artifacts:
                    return (
                        "Partial operation: git worktree add reported an error after "
                        f"leaving {', '.join(artifacts)}. Task {task_id} remains unbound "
                        "and no Git data was deleted. Run `git worktree list`, inspect "
                        f"'{path}' and '{branch}', then keep or remove those artifacts "
                        f"manually after preserving any work. Git error: {self._shown(result)}"
                    )
                return f"Git error: {self._shown(result)}"

            try:
                self.tasks.bind_worktree(task_id, name)
            except Exception as exc:  # noqa: BLE001 - the checkout already exists
                return (
                    f"Partial success: Worktree '{name}' was created at {path} on "
                    f"branch '{branch}', but task binding failed: {exc}. Git data was "
                    "retained for manual recovery."
                )

        self._info(f"[worktree] created: {name} at {path}")
        return f"Worktree '{name}' created at {path} for task {task_id}"

    # -------------------------------------------------------------- remove
    def remove(self, name: str, discard_changes: bool = False) -> str:
        error = validate_worktree_name(name)
        if error:
            return f"Error: {error}"

        with self._lock:
            path, error = self.registered_worktree(name)
            if error:
                return f"Error: {error}"
            bound = self._bound_tasks(name)
            if not bound:
                return f"Error: Worktree '{name}' is not bound to a task"
            active = [task for task in bound if task.status != "completed"]
            if active:
                return (
                    f"Error: Worktree '{name}' is bound to active task "
                    f"{active[0].id}; complete it before removal"
                )
            ok, status = self._git(["status", "--porcelain", "--ignored"], cwd=path)
            if not ok:
                return f"Error: Cannot verify worktree '{name}' status: {self._shown(status)}"
            if status and not discard_changes:
                changed = len([line for line in status.splitlines() if line.strip()])
                return (
                    f"Error: Worktree '{name}' has {changed} uncommitted change(s); "
                    "preserve or discard them manually"
                )

            args = ["worktree", "remove"]
            if discard_changes:
                args.append("--force")
            args.append(str(path))
            ok, result = self._git(args)
            if not ok:
                return f"Git error: {self._shown(result)}"

            try:
                self.tasks.unbind_worktree(name)
            except Exception as exc:  # noqa: BLE001 - checkout is already gone
                return (
                    f"Partial success: Worktree '{name}' was removed and branch "
                    f"'{self.branch(name)}' retained, but task unbinding failed: {exc}. "
                    "Manual recovery is required."
                )

        self._info(f"[worktree] removed: {name}; branch retained")
        return f"Worktree '{name}' removed; branch '{self.branch(name)}' retained"
