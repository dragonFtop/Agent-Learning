"""Shell execution with a timeout, exit-code reporting and process-tree cleanup.

The same process primitives are reused by the background task manager (s11),
so a timed-out or cancelled background command also cleans up its children.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tools.safety import is_dangerous_command

TIMEOUT_SECONDS = 120
MAX_OUTPUT = 50000


@dataclass
class ProcessOutcome:
    output: str
    exit_code: int | None
    timed_out: bool = False


def _cwd(cwd: str | Path | None) -> Path:
    if cwd is not None:
        return Path(cwd)
    from config import WORKDIR

    return WORKDIR


def _popen_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def spawn_shell(command: str, cwd: str | Path | None = None) -> subprocess.Popen:
    return subprocess.Popen(
        command,
        shell=True,
        cwd=str(_cwd(cwd)),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_popen_kwargs(),
    )


def terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            text=True,
            errors="ignore",
        )
        try:
            process.kill()
        except OSError:
            pass
        return
    import signal

    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def collect_process(process: subprocess.Popen, timeout: float | None = TIMEOUT_SECONDS) -> ProcessOutcome:
    timed_out = False
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_tree(process)
        output, _ = process.communicate()
    output = (output or "").strip()
    exit_code = process.returncode
    if len(output) > MAX_OUTPUT:
        output = output[:MAX_OUTPUT] + "\n... (output truncated)"
    return ProcessOutcome(output=output, exit_code=exit_code, timed_out=timed_out)


def format_outcome(outcome: ProcessOutcome) -> str:
    if outcome.timed_out:
        return f"Error: Timeout\n{outcome.output}".strip()
    body = outcome.output if outcome.output else "(no output)"
    return f"{body}\n[exit_code: {outcome.exit_code}]"


def run_bash(
    command: str,
    cwd: str | Path | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> str:
    if is_dangerous_command(command):
        return "Error: Dangerous command blocked by the safety fuse"
    try:
        process = spawn_shell(command, cwd)
        return format_outcome(collect_process(process, timeout))
    except Exception as error:  # noqa: BLE001
        return f"Error: {error}"
