"""s03: permission policy.

Three gates run before every tool call:

1. hard deny list / destructive command patterns
2. path sandbox and per-tool rules
3. interactive approval ("allow once", "always this session", "deny")

The policy is shared by lead, subagents and teammates. That is deliberate: a
session approval given in the parent applies to delegated work, while a worker
running without a terminal can never trigger an interactive prompt.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Callable

from tools.file_ops import safe_path
from tools.safety import classify_command, first_token, is_dangerous_command

AskCallback = Callable[[str, dict, str, Any], str]

READ_ONLY_TOOLS = {
    "read_file",
    "glob",
    "load_skill",
    "todo_write",
    "compact",
    "list_tasks",
    "get_task",
    "list_crons",
    "list_teammates",
    "submit_plan",
    "list_skills",
    "list_workflows",
    "connect_mcp",
    "list_mcp_tools",
}

# Writes inside the workspace are the normal coding workflow. In strict mode
# they still ask; in allowlist mode the path sandbox is the boundary.
FILE_WRITE_TOOLS = {"write_file", "edit_file"}

# Operations that touch shared or persistent state.
HIGH_IMPACT_TOOLS = {
    "task",
    "create_task",
    "update_task",
    "claim_task",
    "complete_task",
    "schedule_cron",
    "cancel_cron",
    "spawn_teammate",
    "request_shutdown",
    "request_plan",
    "review_plan",
    "create_worktree",
    "remove_worktree",
    "send_message",
    "run_workflow",
}

# A teammate runs without a terminal, so these protocol and task-board tools
# must not fall through to the interactive approval path. Ownership rules in
# TaskStore and TeamManager still constrain what each call may do.
TEAMMATE_ALLOWED_TOOLS = {
    "claim_task",
    "complete_task",
    "send_message",
    "submit_plan",
}


def _default_ask(tool_name: str, args: dict, reason: str, ctx: Any) -> str:
    preview = _preview(args)
    print()
    print(f"[permission] {reason}")
    print(f"  tool: {tool_name}")
    print(f"  args: {preview}")
    try:
        choice = input("  Allow? [y] once / [a] always this session / [N] deny: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "deny"
    if choice in ("y", "yes"):
        return "allow"
    if choice in ("a", "always"):
        return "session"
    return "deny"


def _preview(args: dict, width: int = 300) -> str:
    parts = []
    for key, value in args.items():
        text = str(value).replace("\n", " ")
        if len(text) > 80:
            text = text[:77] + "..."
        parts.append(f"{key}={text}")
    joined = ", ".join(parts)
    return joined[:width]


class PermissionPolicy:
    def __init__(
        self,
        *,
        mode: str = "allowlist",
        workdir: str | Path | None = None,
        ask: AskCallback | None = None,
    ) -> None:
        self.mode = mode if mode in ("allowlist", "strict", "auto") else "allowlist"
        self.workdir = Path(workdir).resolve() if workdir else Path.cwd().resolve()
        self._ask = ask or _default_ask
        self._session_allowed: set[str] = set()
        # Filled by MCPManager from the host policy map; server-provided
        # annotations never decide authorization.
        self.mcp_policies: dict[str, str] = {}
        self._lock = threading.RLock()

    def _session_key(self, tool_name: str, args: dict) -> str:
        if tool_name == "bash":
            token = first_token(str(args.get("command", "")))
            return f"bash:{token}" if token else "bash"
        if tool_name.startswith("mcp__"):
            parts = tool_name.split("__")
            server = parts[1] if len(parts) > 1 else tool_name
            return f"mcp:{server}"
        return tool_name

    def _already_allowed(self, key: str) -> bool:
        with self._lock:
            return key in self._session_allowed

    def _remember(self, key: str) -> None:
        with self._lock:
            self._session_allowed.add(key)

    def _can_ask(self, ctx: Any) -> bool:
        if ctx is None:
            return False
        if not getattr(ctx, "interactive", False):
            return False
        return threading.current_thread() is threading.main_thread()

    def _ask_user(self, tool_name: str, args: dict, reason: str, ctx: Any) -> str | None:
        if not self._can_ask(ctx):
            return "Permission denied: interactive approval is unavailable in this context."
        try:
            answer = self._ask(tool_name, args, reason, ctx)
        except Exception:  # noqa: BLE001 - a broken prompt must fail closed
            return "Permission denied: approval prompt failed."
        if answer in ("allow", "session"):
            if answer == "session":
                self._remember(self._session_key(tool_name, args))
            return None
        return "Permission denied by the user."

    def check(self, tool_name: str, args: dict, ctx: Any) -> str | None:
        """Return None to allow, or a denial reason string."""
        args = args or {}

        if (
            getattr(ctx, "profile", "") == "teammate"
            and tool_name in TEAMMATE_ALLOWED_TOOLS
        ):
            return None
        if tool_name == "bash":
            return self._check_bash(args, ctx)
        if tool_name in FILE_WRITE_TOOLS:
            return self._check_file_write(tool_name, args, ctx)
        if tool_name.startswith("mcp__"):
            return self._check_mcp(tool_name, args, ctx)
        if tool_name in READ_ONLY_TOOLS:
            return None
        if tool_name in HIGH_IMPACT_TOOLS:
            if self.mode == "auto":
                return None
            key = self._session_key(tool_name, args)
            if self._already_allowed(key):
                return None
            return self._ask_user(tool_name, args, f"{tool_name} changes shared or persistent state", ctx)

        # Unknown / extension tools: auto allows, both interactive modes ask.
        if self.mode == "auto":
            return None
        key = self._session_key(tool_name, args)
        if self._already_allowed(key):
            return None
        return self._ask_user(tool_name, args, "tool is not in the known-safe list", ctx)

    def _check_bash(self, args: dict, ctx: Any) -> str | None:
        command = str(args.get("command", ""))
        verdict = classify_command(command)
        if verdict == "deny" or is_dangerous_command(command):
            return (
                "Permission denied: command matches the destructive-command deny list. "
                "Do not retry it; choose a safer command."
            )
        if self.mode == "auto":
            return None
        if self.mode == "allowlist" and verdict == "safe":
            return None
        key = self._session_key("bash", args)
        if self._already_allowed(key):
            return None
        return self._ask_user(
            "bash",
            args,
            "command is not on the read-only allowlist",
            ctx,
        )

    def _check_file_write(self, tool_name: str, args: dict, ctx: Any) -> str | None:
        path = str(args.get("path", ""))
        cwd = getattr(ctx, "cwd", self.workdir) if ctx is not None else self.workdir
        try:
            safe_path(path, cwd)
        except ValueError as error:
            return f"Permission denied: {error}. Writes must stay inside the workspace."
        if self.mode in ("allowlist", "auto"):
            return None
        key = self._session_key(tool_name, args)
        if self._already_allowed(key):
            return None
        return self._ask_user(tool_name, args, "write operation requires approval in strict mode", ctx)

    def _check_mcp(self, tool_name: str, args: dict, ctx: Any) -> str | None:
        if self.mcp_policies.get(tool_name) == "allow":
            return None
        if self.mode == "auto":
            return None
        key = self._session_key(tool_name, args)
        if self._already_allowed(key):
            return None
        return self._ask_user(
            tool_name,
            args,
            "external MCP tool call is not host-approved",
            ctx,
        )

    def grant_session(self, key: str) -> None:
        self._remember(key)


def build_default_policy(workdir: str | Path | None = None) -> PermissionPolicy:
    mode = os.getenv("PERMISSION_MODE", "allowlist").strip().lower()
    return PermissionPolicy(mode=mode, workdir=workdir)
