"""s11: explicit background shell tasks.

``bash(run_in_background=true)`` returns immediately with a task id. A daemon
thread runs the command; when it finishes the result is queued as a
``<task_notification>``. The runtime drains the queue at the top of its next
iteration, so notifications arrive as ordinary user-role context.

Only independent commands belong here. Process cleanup reuses
``tools.bash.terminate_process_tree`` (taskkill /T on Windows, killpg on
POSIX).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from tools.bash import collect_process, format_outcome, spawn_shell
from tools.safety import is_dangerous_command

BACKGROUND_TIMEOUT = 300.0


class BackgroundManager:
    def __init__(self, workdir: str | Path | None = None) -> None:
        self.workdir = Path(workdir).resolve() if workdir is not None else None
        self.tasks: dict[str, dict] = {}
        self.results: dict[str, str] = {}
        self._notifications: list[str] = []
        self._counter = 0
        self._lock = threading.RLock()

    def _cwd(self) -> Path:
        if self.workdir is not None:
            return self.workdir
        from config import WORKDIR

        return WORKDIR

    # --------------------------------------------------------------- start
    def start(self, command: str, ctx: Any = None) -> str:
        command = str(command).strip()
        if not command:
            raise ValueError("Bash command cannot be empty")
        if is_dangerous_command(command):
            return "Error: Dangerous command blocked by the safety fuse"

        cwd = getattr(ctx, "cwd", None) or self._cwd()
        with self._lock:
            self._counter += 1
            task_id = f"bg_{self._counter:04d}"
            self.tasks[task_id] = {
                "command": command,
                "status": "running",
                "started_at": time.time(),
            }

        thread = threading.Thread(
            target=self._run,
            args=(task_id, command, cwd),
            daemon=True,
            name=f"background-{task_id}",
        )
        try:
            thread.start()
        except Exception:
            with self._lock:
                self.tasks.pop(task_id, None)
            raise
        return (
            f"[Background task {task_id} started] "
            "The result will arrive as a task_notification in a later turn."
        )

    def _run(self, task_id: str, command: str, cwd: str | Path) -> None:
        try:
            process = spawn_shell(command, cwd)
            outcome = collect_process(process, BACKGROUND_TIMEOUT)
            result = format_outcome(outcome)
            status = "completed" if outcome.exit_code == 0 and not outcome.timed_out else "failed"
        except Exception as error:  # noqa: BLE001 - reported to the model
            result = f"Error: {type(error).__name__}: {error}"
            status = "failed"

        with self._lock:
            task = self.tasks.get(task_id)
            if task is None:
                return
            task["status"] = status
            task["finished_at"] = time.time()
            self.results[task_id] = result
            self._notifications.append(
                f"<task_notification>\n"
                f"  <task_id>{task_id}</task_id>\n"
                f"  <status>{status}</status>\n"
                f"  <command>{task['command']}</command>\n"
                f"  <summary>{result[:800]}</summary>\n"
                f"</task_notification>"
            )

    # -------------------------------------------------------------- drain
    def drain_notifications(self) -> list[str]:
        with self._lock:
            notifications = list(self._notifications)
            self._notifications.clear()
            return notifications

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._notifications)

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {task_id: dict(task) for task_id, task in self.tasks.items()}

    def running_ids(self) -> list[str]:
        with self._lock:
            return [task_id for task_id, task in self.tasks.items() if task["status"] == "running"]
