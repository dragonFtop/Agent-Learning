"""s14: MCP plugin tools.

Two client flavors live behind one manager:

* in-process mock servers (``docs``, ``deploy``) so the lesson runs without
  external processes;
* line-delimited JSON-RPC over stdio for servers declared in ``.mcp.json``.

Discovery is explicit (``connect_mcp``). Every discovered tool is registered
as ``mcp__<server>__<tool>`` with ``source="mcp"``, so the normal registry,
prompt assembly and permission hook see it without a special code path in the
loop. Authorization comes from the host policy map, never from server-supplied
descriptions or annotations.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any

from tools.bash import terminate_process_tree
from tools.registry import ToolRegistry, ToolSpec

VALID_SERVER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DISALLOWED_CHARS = re.compile(r"[^A-Za-z0-9_-]")
MAX_MCP_TOOL_NAME = 64
MAX_TOOL_OUTPUT = 50000
MAX_LIST_PAGES = 20
DEFAULT_TIMEOUT = 10.0

# Host-owned authorization. A tool that is not listed here defaults to
# "confirm" and goes through PermissionPolicy.
MCP_HOST_POLICY = {
    ("docs", "search"): "allow",
    ("docs", "get_version"): "allow",
    ("deploy", "status"): "allow",
    ("deploy", "trigger"): "confirm",
}


class MCPError(Exception):
    """A discovery or transport failure that should be shown to the model."""


def validate_server_name(name: str) -> str | None:
    if not isinstance(name, str) or not VALID_SERVER_NAME.fullmatch(name):
        return (
            "MCP server name must be 1-64 letters, digits, dots, underscores, or "
            "dashes, and start with a letter or digit"
        )
    return None


def normalize_mcp_name(name: str) -> str:
    """Replace characters outside the model tool-name alphabet."""
    normalized = _DISALLOWED_CHARS.sub("_", str(name))
    if not normalized:
        raise MCPError("MCP names cannot normalize to an empty string")
    return normalized


# --------------------------------------------------------------------- mocks
class MockMCPClient:
    """Small in-process stand-in for MCP ``tools/list`` and ``tools/call``."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.tools: list[dict] = []
        self._handlers: dict[str, Any] = {}

    def start(self) -> None:
        return None

    def register(self, tool_defs: list[dict], handlers: dict[str, Any]) -> None:
        names = [tool.get("name") for tool in tool_defs]
        if any(not isinstance(name, str) or not name for name in names):
            raise MCPError("Every MCP tool needs a non-empty name")
        if len(set(names)) != len(names):
            raise MCPError(f"Duplicate MCP tool name on server {self.name!r}")
        missing = [name for name in names if name not in handlers]
        if missing:
            raise MCPError(f"Missing MCP handlers: {', '.join(missing)}")
        self.tools = list(tool_defs)
        self._handlers = dict(handlers)

    def call_tool(self, tool_name: str, args: dict) -> str:
        handler = self._handlers.get(tool_name)
        if handler is None:
            return f"MCP error: unknown tool '{tool_name}'"
        try:
            return str(handler(**(args or {})))
        except Exception as error:  # noqa: BLE001 - report server faults as text
            return f"MCP error: {type(error).__name__}: {error}"

    def close(self) -> None:
        return None

    def is_running(self) -> bool:
        return True

    def error_text(self) -> str:
        return ""


def _mock_server_docs() -> MockMCPClient:
    server = MockMCPClient("docs")
    server.register(
        tool_defs=[
            {
                "name": "search",
                "description": "Search the documentation.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
            {
                "name": "get_version",
                "description": "Get the documentation API version.",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ],
        handlers={
            "search": lambda query: f"[docs] Found 3 results for '{query}'",
            "get_version": lambda: "[docs] API v2.1.0",
        },
    )
    return server


def _mock_server_deploy() -> MockMCPClient:
    server = MockMCPClient("deploy")
    server.register(
        tool_defs=[
            {
                "name": "trigger",
                "description": "Trigger a deployment.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"service": {"type": "string"}},
                    "required": ["service"],
                },
            },
            {
                "name": "status",
                "description": "Check deployment status.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"service": {"type": "string"}},
                    "required": ["service"],
                },
            },
        ],
        handlers={
            "trigger": lambda service: f"[deploy] Triggered: {service}",
            "status": lambda service: f"[deploy] {service}: running (v1.4.2)",
        },
    )
    return server


MOCK_SERVERS = {"docs": _mock_server_docs, "deploy": _mock_server_deploy}


# --------------------------------------------------------------------- stdio
class _Pending:
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Any = None
        self.error: str | None = None


def _popen_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


class StdioMCPClient:
    """Line-delimited JSON-RPC client for a locally spawned MCP server."""

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: Path,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.name = name
        self.command = command
        self.args = list(args)
        self.env = env
        self.cwd = cwd
        self.timeout = timeout
        self.tools: list[dict] = []
        self.process: subprocess.Popen | None = None
        self.server_info: dict = {}

        self._next_id = 0
        self._pending: dict[int, _Pending] = {}
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._broken: str | None = None
        self._stderr_tail = ""
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self.process is not None:
            raise MCPError(f"MCP client '{self.name}' is already started")
        try:
            self.process = subprocess.Popen(
                [self.command, *self.args],
                cwd=str(self.cwd),
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **_popen_kwargs(),
            )
        except OSError as error:
            raise MCPError(
                f"cannot start MCP server '{self.name}': {type(error).__name__}: {error}"
            ) from error

        self._reader = threading.Thread(
            target=self._read_stdout,
            name=f"mcp-reader-{self.name}",
            daemon=True,
        )
        self._stderr_reader = threading.Thread(
            target=self._read_stderr,
            name=f"mcp-stderr-{self.name}",
            daemon=True,
        )
        self._reader.start()
        self._stderr_reader.start()

        try:
            result = self._request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "agent-learning", "version": "1.0"},
                },
            )
            self.server_info = result if isinstance(result, dict) else {}
            self._notify("notifications/initialized", {})
            self.tools = self._discover_tools()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except (OSError, ValueError):
            pass
        terminate_process_tree(process)
        try:
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass

        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
            if self._broken is None:
                self._broken = f"MCP server '{self.name}' closed"
        for item in pending:
            item.error = self._broken
            item.event.set()

        for stream in (process.stdout, process.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        for thread in (self._reader, self._stderr_reader):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=1)

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def error_text(self) -> str:
        return self._stderr_tail

    # --------------------------------------------------------------- request
    def _write(self, message: dict) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise MCPError(f"MCP server '{self.name}' is not running")
        payload = json.dumps(message, ensure_ascii=False) + "\n"
        try:
            with self._write_lock:
                process.stdin.write(payload)
                process.stdin.flush()
        except (OSError, ValueError) as error:
            raise MCPError(
                f"cannot write to MCP server '{self.name}': {type(error).__name__}: {error}"
            ) from error

    def _request(self, method: str, params: dict, *, timeout: float | None = None) -> Any:
        with self._lock:
            if self._broken:
                raise MCPError(self._broken)
            self._next_id += 1
            request_id = self._next_id
            pending = _Pending()
            self._pending[request_id] = pending

        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            self._write(message)
        except MCPError:
            with self._lock:
                self._pending.pop(request_id, None)
            raise

        if not pending.event.wait(timeout or self.timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            detail = self._stderr_tail.strip()
            suffix = f" (stderr: {detail[-500:]})" if detail else ""
            raise MCPError(
                f"timeout after {timeout or self.timeout:.1f}s waiting for "
                f"'{method}' from MCP server '{self.name}'{suffix}"
            )
        if pending.error:
            raise MCPError(pending.error)
        return pending.result

    def _notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _discover_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor: str | None = None
        for _ in range(MAX_LIST_PAGES):
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params)
            if not isinstance(result, dict):
                raise MCPError(f"MCP server '{self.name}' returned an invalid tools/list result")
            page = result.get("tools", [])
            if not isinstance(page, list):
                raise MCPError(f"MCP server '{self.name}' returned a non-list tools field")
            tools.extend(page)
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
        raise MCPError(f"MCP server '{self.name}' returned too many tool pages")

    def call_tool(self, tool_name: str, args: dict) -> str:
        try:
            result = self._request(
                "tools/call",
                {"name": tool_name, "arguments": args or {}},
            )
        except MCPError as error:
            return f"MCP error: {error}"
        if not isinstance(result, dict):
            return "MCP error: invalid tools/call result"

        texts: list[str] = []
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text", "")))
        if not texts and "structuredContent" in result:
            texts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
        body = "\n".join(texts) or "(no text content)"
        if result.get("isError"):
            body = f"MCP error: {body}"
        if len(body) > MAX_TOOL_OUTPUT:
            body = body[:MAX_TOOL_OUTPUT] + "\n... (output truncated)"
        return body

    # ---------------------------------------------------------------- readers
    def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                request_id = message.get("id")
                if request_id is None:
                    continue
                if "method" in message:
                    # Server-initiated request: this client supports no
                    # client-side capabilities, so answer method-not-found.
                    try:
                        self._write(
                            {
                                "jsonrpc": "2.0",
                                "id": request_id,
                                "error": {
                                    "code": -32601,
                                    "message": "Client does not support this method",
                                },
                            }
                        )
                    except MCPError:
                        pass
                    continue
                with self._lock:
                    pending = self._pending.pop(request_id, None)
                if pending is None:
                    continue
                if "error" in message:
                    error = message.get("error") or {}
                    if isinstance(error, dict):
                        pending.error = (
                            f"MCP server error {error.get('code')}: {error.get('message')}"
                        )
                    else:
                        pending.error = f"MCP server error: {error}"
                else:
                    pending.result = message.get("result")
                pending.event.set()
        except (OSError, ValueError):
            pass
        finally:
            with self._lock:
                if self._broken is None:
                    self._broken = (
                        f"MCP server '{self.name}' closed its output stream"
                    )
                pending_items = list(self._pending.values())
                self._pending.clear()
            for item in pending_items:
                item.error = self._broken
                item.event.set()

    def _read_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            for line in process.stderr:
                with self._lock:
                    self._stderr_tail = (self._stderr_tail + line)[-4000:]
        except (OSError, ValueError):
            pass


# ------------------------------------------------------------------- manager
class MCPManager:
    """Connect servers, validate their tools, and register them in the pool."""

    def __init__(
        self,
        workspace: str | Path,
        registry: ToolRegistry,
        *,
        permissions: Any = None,
        config_path: str | Path | None = None,
        console: Any = None,
        timeout: float = DEFAULT_TIMEOUT,
        include_mocks: bool = True,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.registry = registry
        self.permissions = permissions
        self.config_path = (
            Path(config_path).resolve()
            if config_path is not None
            else self.workspace / ".mcp.json"
        )
        self.console = console
        self.timeout = timeout
        self.include_mocks = include_mocks

        self._clients: dict[str, Any] = {}
        self._specs: dict[str, list[ToolSpec]] = {}
        self._policies: dict[str, str] = {}
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- config
    def _read_config(self) -> tuple[dict[str, Any], str | None]:
        if not self.config_path.exists():
            return {}, None
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            return {}, f"cannot read {self.config_path.name}: {type(error).__name__}: {error}"
        if not isinstance(data, dict):
            return {}, f"{self.config_path.name} must contain a JSON object"
        servers = data.get("mcpServers")
        if servers is None:
            return {}, f"{self.config_path.name} is missing the 'mcpServers' object"
        if not isinstance(servers, dict):
            return {}, f"'mcpServers' in {self.config_path.name} must be an object"
        return servers, None

    def _build_stdio(self, name: str, server: dict) -> StdioMCPClient:
        if not isinstance(server, dict):
            raise MCPError(f"MCP server '{name}' configuration must be an object")
        command = server.get("command")
        if not isinstance(command, str) or not command.strip():
            raise MCPError(f"MCP server '{name}' needs a non-empty 'command' string")
        args = server.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise MCPError(f"MCP server '{name}' 'args' must be a list of strings")
        configured_env = server.get("env", {})
        if not isinstance(configured_env, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in configured_env.items()
        ):
            raise MCPError(f"MCP server '{name}' 'env' must map strings to strings")
        env = dict(os.environ)
        env.update(configured_env)

        raw_cwd = server.get("cwd")
        if raw_cwd is None:
            cwd = self.workspace
        else:
            if not isinstance(raw_cwd, str):
                raise MCPError(f"MCP server '{name}' 'cwd' must be a string")
            cwd = Path(raw_cwd)
            if not cwd.is_absolute():
                cwd = self.workspace / cwd
            cwd = cwd.resolve()
            if not cwd.is_relative_to(self.workspace):
                raise MCPError(f"MCP server '{name}' cwd must stay inside the workspace")
        if not cwd.is_dir():
            raise MCPError(f"MCP server '{name}' cwd does not exist: {cwd}")

        return StdioMCPClient(
            name,
            command.strip(),
            args,
            env=env,
            cwd=cwd,
            timeout=self.timeout,
        )

    def available_servers(self) -> tuple[list[str], str | None]:
        names = set(MOCK_SERVERS) if self.include_mocks else set()
        servers, error = self._read_config()
        names.update(servers)
        return sorted(names), error

    # --------------------------------------------------------------- connect
    def connect(self, name: str) -> str:
        name = str(name).strip()
        error = validate_server_name(name)
        if error:
            return f"Error: {error}"

        with self._lock:
            if name in self._clients:
                return f"MCP server '{name}' is already connected"

            servers, config_error = self._read_config()
            client: Any = None
            if name in servers:
                try:
                    client = self._build_stdio(name, servers[name])
                    client.start()
                except (MCPError, ValueError) as exc:
                    if client is not None:
                        try:
                            client.close()
                        except Exception:  # noqa: BLE001 - close is best effort
                            pass
                    return f"MCP error: {exc}"
            elif self.include_mocks and name in MOCK_SERVERS:
                client = MOCK_SERVERS[name]()
                client.start()
            else:
                available, _ = self.available_servers()
                suffix = f" Config error: {config_error}" if config_error else ""
                choices = ", ".join(available) or "(none)"
                return f"Unknown MCP server '{name}'. Available: {choices}.{suffix}"

            try:
                specs, policies = self._prepare(name, client)
                self._register(name, specs, policies)
            except (MCPError, ValueError) as exc:
                client.close()
                return f"MCP error: {exc}"

            self._clients[name] = client

        self._info(
            f"[mcp] connected: {name} -> "
            + ", ".join(tool.get("name", "?") for tool in client.tools)
        )
        discovered = ", ".join(tool.get("name", "?") for tool in client.tools) or "(none)"
        return (
            f"Connected to MCP server '{name}'. Discovered "
            f"{len(client.tools)} tools: {discovered}"
        )

    def _prepare(self, name: str, client: Any) -> tuple[list[ToolSpec], dict[str, str]]:
        safe_server = normalize_mcp_name(name)
        for other in self._clients:
            if normalize_mcp_name(other) == safe_server:
                raise MCPError(
                    f"server name '{name}' collides with connected server '{other}' "
                    "after normalization"
                )

        origins = {
            tool_name: "an already registered tool" for tool_name in self.registry.names()
        }
        specs: list[ToolSpec] = []
        policies: dict[str, str] = {}
        for tool_def in client.tools:
            if not isinstance(tool_def, dict):
                raise MCPError(f"server '{name}' returned a non-object tool definition")
            raw_name = tool_def.get("name")
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise MCPError(f"server '{name}' returned a tool without a name")
            raw_name = raw_name.strip()
            prefixed = f"mcp__{safe_server}__{normalize_mcp_name(raw_name)}"
            if len(prefixed) > MAX_MCP_TOOL_NAME:
                raise MCPError(
                    f"MCP tool name is longer than {MAX_MCP_TOOL_NAME} characters: "
                    f"{prefixed}"
                )
            if prefixed in origins:
                raise MCPError(
                    f"MCP tool name collision after normalization: {prefixed!r} "
                    f"conflicts with {origins[prefixed]}"
                )

            schema = tool_def.get("inputSchema") or {}
            if not isinstance(schema, dict):
                raise MCPError(f"invalid input schema for MCP tool {name!r}/{raw_name!r}")
            if schema.get("type", "object") != "object":
                raise MCPError(
                    f"input schema for MCP tool {name!r}/{raw_name!r} must be an object"
                )
            schema = {**schema, "type": "object"}
            if not isinstance(schema.get("properties"), dict):
                schema["properties"] = {}

            description = str(tool_def.get("description") or "").strip()
            if not description:
                description = f"MCP tool '{raw_name}' from server '{name}'."
            origin = f"MCP tool {name!r}/{raw_name!r}"
            origins[prefixed] = origin
            specs.append(
                ToolSpec(
                    name=prefixed,
                    description=description,
                    input_schema=schema,
                    handler=self._make_handler(client, raw_name),
                    source="mcp",
                    profiles=("lead", "teammate"),
                )
            )
            policies[prefixed] = MCP_HOST_POLICY.get((name, raw_name), "confirm")
        return specs, policies

    def _make_handler(self, client: Any, raw_name: str):
        def handler(args: dict, ctx: Any) -> str:
            return client.call_tool(raw_name, args or {})

        return handler

    def _register(
        self,
        name: str,
        specs: list[ToolSpec],
        policies: dict[str, str],
    ) -> None:
        registered: list[str] = []
        try:
            for spec in specs:
                self.registry.register(spec)
                registered.append(spec.name)
        except Exception as error:  # noqa: BLE001 - roll back this server's tools
            for tool_name in registered:
                self.registry.unregister(tool_name)
            raise MCPError(f"cannot register MCP tools for '{name}': {error}") from error
        self._specs[name] = specs
        self._policies.update(policies)
        if self.permissions is not None and hasattr(self.permissions, "mcp_policies"):
            self.permissions.mcp_policies.update(policies)

    def _unregister(self, name: str) -> None:
        for spec in self._specs.pop(name, []):
            self.registry.unregister(spec.name)
            self._policies.pop(spec.name, None)
            if self.permissions is not None and hasattr(self.permissions, "mcp_policies"):
                self.permissions.mcp_policies.pop(spec.name, None)

    # --------------------------------------------------------------- queries
    def policy_for(self, tool_name: str) -> str:
        with self._lock:
            return self._policies.get(tool_name, "confirm")

    def connected_servers(self) -> list[str]:
        with self._lock:
            return sorted(self._clients)

    def tool_names(self) -> list[str]:
        with self._lock:
            return sorted(self._policies)

    def summary_text(self) -> str:
        with self._lock:
            clients = dict(self._clients)
            available, error = self.available_servers()
        lines: list[str] = []
        if clients:
            lines.append("Connected MCP servers:")
            for name, client in sorted(clients.items()):
                tools = ", ".join(str(tool.get("name", "?")) for tool in client.tools)
                lines.append(f"- {name}: {tools or '(no tools)'}")
        else:
            lines.append("No MCP servers connected.")
        unconnected = [name for name in available if name not in clients]
        if unconnected:
            lines.append("Available servers: " + ", ".join(unconnected))
        if error:
            lines.append(f"MCP config error: {error}")
        return "\n".join(lines)

    def list_tools_text(self) -> str:
        with self._lock:
            clients = dict(self._clients)
            policies = dict(self._policies)
            available, error = self.available_servers()
        lines: list[str] = []
        if not clients:
            lines.append("No MCP servers connected.")
        for name, client in sorted(clients.items()):
            lines.append(f"{name} [connected]")
            for tool_def in client.tools:
                raw_name = str(tool_def.get("name", "?"))
                prefixed = f"mcp__{normalize_mcp_name(name)}__{normalize_mcp_name(raw_name)}"
                policy = policies.get(prefixed, "confirm")
                description = str(tool_def.get("description") or "").strip()
                lines.append(f"- {prefixed} [{policy}]: {description}")
            if not client.is_running():
                lines.append(f"  (server is not running: {client.error_text()[-300:].strip()})")
        unconnected = [name for name in available if name not in clients]
        if unconnected:
            lines.append("Available: " + ", ".join(unconnected))
        if error:
            lines.append(f"MCP config error: {error}")
        return "\n".join(lines)

    # --------------------------------------------------------------- cleanup
    def disconnect(self, name: str) -> str:
        with self._lock:
            client = self._clients.pop(name, None)
            if client is None:
                return f"Error: MCP server '{name}' is not connected"
            self._unregister(name)
        client.close()
        return f"Disconnected MCP server '{name}'"

    def close_all(self) -> None:
        with self._lock:
            clients = list(self._clients.items())
            self._clients.clear()
            for name in list(self._specs):
                self._unregister(name)
        for name, client in clients:
            try:
                client.close()
            except Exception as error:  # noqa: BLE001 - shutdown must continue
                self._warn(f"[mcp] failed to close '{name}': {type(error).__name__}: {error}")

    # ---------------------------------------------------------------- output
    def _info(self, text: str) -> None:
        if self.console is not None:
            self.console.info(text)

    def _warn(self, text: str) -> None:
        if self.console is not None:
            self.console.warn(text)
