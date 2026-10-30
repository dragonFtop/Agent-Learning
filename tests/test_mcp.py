import json
import sys
from types import SimpleNamespace

import pytest

from agent.mcp import MCPManager, normalize_mcp_name, validate_server_name
from agent.permissions import PermissionPolicy
from agent.prompts import build_system_prompt
from tools.builtin import build_base_registry
from tools.registry import ToolContext, ToolSpec


class RecordingAsk:
    def __init__(self, answer="deny"):
        self.answer = answer
        self.calls = []

    def __call__(self, tool_name, args, reason, ctx):
        self.calls.append((tool_name, args, reason))
        return self.answer


FAKE_SERVER = r'''
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    TOOLS = json.load(handle)


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        continue
    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "0.1"},
            },
        })
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "echo":
            send({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {"type": "text", "text": "echo: " + str(arguments.get("text", ""))}
                    ]
                },
            })
        else:
            send({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": "unknown tool " + str(name)}],
                    "isError": True,
                },
            })
    else:
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "method not found"},
        })
'''


def _fake_server(tmp_path, tools, name="fake"):
    script = tmp_path / f"{name}_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    tools_file = tmp_path / f"{name}_tools.json"
    tools_file.write_text(json.dumps(tools), encoding="utf-8")
    return {"command": sys.executable, "args": [str(script), str(tools_file)]}


def _write_config(tmp_path, servers):
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8"
    )


def _echo_tool():
    return {
        "name": "echo",
        "description": "Echo text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }


def _manager(tmp_path, timeout=5.0):
    registry = build_base_registry()
    policy = PermissionPolicy(mode="allowlist", workdir=tmp_path)
    manager = MCPManager(
        tmp_path,
        registry,
        permissions=policy,
        config_path=tmp_path / ".mcp.json",
        timeout=timeout,
    )
    return manager, registry, policy


@pytest.fixture
def env(tmp_path):
    manager, registry, policy = _manager(tmp_path)
    yield SimpleNamespace(
        tmp_path=tmp_path,
        manager=manager,
        registry=registry,
        policy=policy,
    )
    manager.close_all()


def test_server_name_validation_and_normalization():
    assert validate_server_name("docs-1") is None
    assert validate_server_name("a.b_c") is None
    assert validate_server_name("../bad") is not None
    assert validate_server_name("") is not None
    assert normalize_mcp_name("my.server") == "my_server"
    assert normalize_mcp_name("search docs") == "search_docs"


def test_connect_mock_registers_prefixed_tools(env):
    message = env.manager.connect("docs")

    assert "Connected to MCP server 'docs'" in message
    spec = env.registry.get("mcp__docs__search")
    assert spec is not None
    assert spec.source == "mcp"
    assert spec.profiles == ("lead", "teammate")
    assert spec.input_schema["type"] == "object"

    ctx = ToolContext(cwd=env.tmp_path, interactive=True)
    assert (
        env.registry.dispatch("mcp__docs__search", {"query": "runs"}, ctx)
        == "[docs] Found 3 results for 'runs'"
    )
    assert "already connected" in env.manager.connect("docs")
    assert "Connected MCP servers:" in env.manager.summary_text()
    assert "mcp__docs__search [allow]" in env.manager.list_tools_text()


def test_unknown_server_reports_available(env):
    message = env.manager.connect("missing")

    assert "Unknown MCP server 'missing'" in message
    assert "docs" in message and "deploy" in message


def test_host_policy_allow_and_confirm(env):
    env.manager.connect("docs")
    env.manager.connect("deploy")
    ask = RecordingAsk(answer="deny")
    env.policy._ask = ask
    ctx = ToolContext(cwd=env.tmp_path, interactive=True)

    assert env.policy.check("mcp__docs__search", {"query": "x"}, ctx) is None
    assert env.policy.check("mcp__deploy__status", {"service": "api"}, ctx) is None
    assert env.policy.check("mcp__deploy__trigger", {"service": "api"}, ctx) is not None
    assert ask.calls[-1][0] == "mcp__deploy__trigger"

    worker_ask = RecordingAsk(answer="allow")
    env.policy._ask = worker_ask
    worker = ToolContext(cwd=env.tmp_path, interactive=False)
    assert env.policy.check("mcp__deploy__trigger", {"service": "api"}, worker) is not None
    assert worker_ask.calls == []  # non-interactive contexts fail closed

    session_ask = RecordingAsk(answer="session")
    env.policy._ask = session_ask
    assert env.policy.check("mcp__deploy__trigger", {"service": "api"}, ctx) is None
    assert env.policy.check("mcp__deploy__trigger", {"service": "web"}, ctx) is None
    assert len(session_ask.calls) == 1


def test_builtin_collision_rejected(env):
    env.registry.register(
        ToolSpec(
            name="mcp__docs__search",
            description="squatter",
            input_schema={"type": "object", "properties": {}},
            handler=lambda args, ctx: "squatter",
        )
    )

    message = env.manager.connect("docs")

    assert "collision" in message.lower()
    assert "mcp__docs__get_version" not in env.registry
    assert env.manager.connected_servers() == []


def test_normalization_collision_rejected(env):
    tools = [
        {"name": "a.b", "description": "dot", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "a_b", "description": "underscore", "inputSchema": {"type": "object", "properties": {}}},
    ]
    _write_config(env.tmp_path, {"fake": _fake_server(env.tmp_path, tools)})

    message = env.manager.connect("fake")

    assert "collision" in message.lower()
    assert "mcp__fake__a_b" not in env.registry
    assert env.manager.connected_servers() == []


def test_server_names_that_normalize_alike_are_rejected(env):
    tool = _echo_tool()
    _write_config(
        env.tmp_path,
        {
            "a.b": _fake_server(env.tmp_path, [tool], name="dotted"),
            "a_b": _fake_server(env.tmp_path, [tool], name="plain"),
        },
    )

    assert "Connected" in env.manager.connect("a.b")
    message = env.manager.connect("a_b")

    assert "collides" in message
    assert env.manager.connected_servers() == ["a.b"]


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        (
            {
                "name": "x" * 60,
                "description": "too long",
                "inputSchema": {"type": "object", "properties": {}},
            },
            "longer than 64",
        ),
        (
            {
                "name": "not_object",
                "description": "bad schema",
                "inputSchema": {"type": "array"},
            },
            "must be an object",
        ),
    ],
)
def test_invalid_discovered_tools_reject_the_connection(env, tool, expected):
    _write_config(env.tmp_path, {"fake": _fake_server(env.tmp_path, [tool])})

    message = env.manager.connect("fake")

    assert "MCP error" in message
    assert expected in message
    assert not any(name.startswith("mcp__") for name in env.registry.names())
    assert env.manager.connected_servers() == []


def test_stdio_roundtrip_and_cleanup(env):
    _write_config(env.tmp_path, {"fake": _fake_server(env.tmp_path, [_echo_tool()])})

    message = env.manager.connect("fake")

    assert "Connected to MCP server 'fake'" in message
    assert "echo" in message
    assert env.manager.policy_for("mcp__fake__echo") == "confirm"
    ctx = ToolContext(cwd=env.tmp_path, interactive=True)
    assert env.registry.dispatch("mcp__fake__echo", {"text": "hi"}, ctx) == "echo: hi"

    client = env.manager._clients["fake"]
    assert client.is_running()

    env.manager.close_all()

    assert not client.is_running()
    assert "mcp__fake__echo" not in env.registry
    assert env.manager.summary_text().startswith("No MCP servers connected.")
    assert "mcp__fake__echo" not in env.policy.mcp_policies


def test_stdio_timeout_reports_error_and_registers_nothing(tmp_path):
    manager, registry, _ = _manager(tmp_path, timeout=0.3)
    script = tmp_path / "slow_server.py"
    script.write_text(
        "import time\nwhile True:\n    time.sleep(1)\n",
        encoding="utf-8",
    )
    _write_config(
        tmp_path,
        {"slow": {"command": sys.executable, "args": [str(script)]}},
    )

    try:
        message = manager.connect("slow")
        assert "timeout" in message.lower()
        assert not any(name.startswith("mcp__") for name in registry.names())
        assert manager.connected_servers() == []
    finally:
        manager.close_all()


def test_unreadable_config_is_reported_but_mocks_still_work(env):
    (env.tmp_path / ".mcp.json").write_text("{not json", encoding="utf-8")

    text = env.manager.summary_text()
    assert "config error" in text.lower()
    assert "Connected" in env.manager.connect("docs")


def test_system_prompt_lists_connected_servers(env):
    env.manager.connect("docs")
    runtime = SimpleNamespace(
        profile="lead",
        cwd=env.tmp_path,
        workdir=env.tmp_path,
        mcp=env.manager,
    )

    prompt = build_system_prompt(runtime)

    assert "mcp__<server>__<tool>" in prompt
    assert "docs: search, get_version" in prompt
