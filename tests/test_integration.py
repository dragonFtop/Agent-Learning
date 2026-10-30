import json
import re
from types import SimpleNamespace

from agent.goal import GoalController
from agent.mcp import MCPManager
from agent.permissions import PermissionPolicy
from agent.prompts import build_system_prompt
from agent.runtime import AgentRuntime
from agent.tasks import TaskStore
from agent.workflow import RunnerOutput, WorkflowManager
from tools.builtin import build_base_registry


def _text(text):
    return SimpleNamespace(type="text", text=text)


def _tool_use(tool_id, name, args):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=args)


def _response(content):
    return SimpleNamespace(
        content=content,
        stop_reason="tool_use" if any(
            getattr(block, "type", None) == "tool_use" for block in content
        ) else "end_turn",
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


class FakeEvaluator:
    def evaluate(self, condition, messages):
        raise AssertionError("goal evaluator should not run in this test")


def _runtime(tmp_path, client, *, mode="allowlist"):
    return AgentRuntime(
        client=client if client is not None else object(),
        model="fake-model",
        workdir=tmp_path,
        interactive=False,
        permissions=PermissionPolicy(mode=mode, workdir=tmp_path),
        registry=build_base_registry(),
    )


def _tool_names(kwargs):
    return {tool["name"] for tool in kwargs.get("tools", [])}


def test_mcp_tool_appears_dynamically_and_dispatches(tmp_path):
    registry = build_base_registry()
    policy = PermissionPolicy(mode="allowlist", workdir=tmp_path)
    mcp = MCPManager(
        tmp_path,
        registry,
        permissions=policy,
        config_path=tmp_path / ".mcp.json",
    )
    client = FakeClient(
        [
            _response([_tool_use("tu_1", "connect_mcp", {"name": "docs"})]),
            _response([_tool_use("tu_2", "mcp__docs__search", {"query": "harness"})]),
            _response([_text("searched the docs")]),
        ]
    )
    runtime = AgentRuntime(
        client=client,
        model="fake-model",
        workdir=tmp_path,
        interactive=False,
        permissions=policy,
        registry=registry,
    )
    runtime.mcp = mcp

    try:
        result = runtime.run_turn("connect docs and search for harness")

        assert result == "searched the docs"
        assert "mcp__docs__search" not in _tool_names(client.messages.calls[0])
        assert "mcp__docs__search" in _tool_names(client.messages.calls[1])
        assert "Connected MCP servers" in client.messages.calls[1]["system"]
        assert "docs: search, get_version" in client.messages.calls[1]["system"]

        connect_result = runtime.history[2]["content"][0]
        search_result = runtime.history[4]["content"][0]
        assert connect_result["tool_use_id"] == "tu_1"
        assert "Connected to MCP server 'docs'" in connect_result["content"]
        assert search_result["tool_use_id"] == "tu_2"
        assert "[docs] Found 3 results for 'harness'" in search_result["content"]
    finally:
        mcp.close_all()


def test_todo_and_task_tools_share_history_and_dynamic_prompt(tmp_path):
    runtime = _runtime(tmp_path, None, mode="auto")
    runtime.tasks = TaskStore(tmp_path / ".tasks", workspace=tmp_path)
    first_client = FakeClient(
        [
            _response(
                [
                    _tool_use(
                        "tu_1",
                        "todo_write",
                        {"todos": [{"content": "plan the work", "status": "in_progress"}]},
                    ),
                    _tool_use("tu_2", "create_task", {"subject": "write docs"}),
                ]
            ),
            _response([_text("planned")]),
        ]
    )
    runtime.client = first_client

    assert runtime.run_turn("plan the work") == "planned"

    created = runtime.history[2]["content"][1]["content"]
    task_id = re.search(r"task_[0-9a-f]{8}", created).group(0)
    assert "Created" in created
    assert "plan the work" in first_client.messages.calls[1]["system"]
    assert "write docs" in first_client.messages.calls[1]["system"]

    second_client = FakeClient(
        [
            _response(
                [
                    _tool_use("tu_3", "claim_task", {"task_id": task_id}),
                    _tool_use("tu_4", "complete_task", {"task_id": task_id}),
                ]
            ),
            _response([_text("done")]),
        ]
    )
    runtime.client = second_client

    assert runtime.run_turn("finish the task") == "done"

    results = runtime.history[6]["content"]
    assert results[0]["content"] == f"Claimed {task_id} (write docs)"
    assert results[1]["content"] == f"Completed {task_id} (write docs)"
    assert runtime.tasks.load(task_id).status == "completed"
    assert runtime.tasks.load(task_id).owner == "lead"


def test_run_workflow_tool_invokes_registered_host_workflow(tmp_path):
    class FakeRunner:
        def __init__(self):
            self.calls = []

        def run(self, prompt, schema=None, label=None):
            self.calls.append(label)
            return RunnerOutput("work-result", 7)

    runner = FakeRunner()
    manager = WorkflowManager(
        tmp_path,
        client=object(),
        model="fake-model",
        store=tmp_path / ".runtime" / "workflows",
        runner=runner,
    )
    manager.register("tiny", "One agent step", lambda ctx, args: ctx.agent("do it", label="work"))

    client = FakeClient(
        [
            _response(
                [
                    _tool_use(
                        "tu_1",
                        "run_workflow",
                        {"name": "tiny", "args": {"x": 1}},
                    )
                ]
            ),
            _response([_text("workflow finished")]),
        ]
    )
    runtime = _runtime(tmp_path, client, mode="auto")
    runtime.workflow = manager

    assert runtime.run_turn("run the tiny workflow") == "workflow finished"

    payload = json.loads(runtime.history[2]["content"][0]["content"])
    assert payload["status"] == "completed"
    assert payload["result"] == "work-result"
    assert payload["usage"] == {"agents": 1, "tokens": 7}
    assert runner.calls == ["work"]
    assert manager.last_run_id() == payload["runId"]
    assert "tiny: One agent step" in client.messages.calls[0]["system"]


def test_system_prompt_includes_all_attached_services(tmp_path):
    runtime = _runtime(tmp_path, None, mode="auto")
    runtime.tasks = TaskStore(tmp_path / ".tasks", workspace=tmp_path)
    runtime.tasks.create("write docs")
    runtime.todos.update([{"content": "plan the work", "status": "in_progress"}])
    runtime.goal = GoalController(FakeEvaluator())
    runtime.goal.set_goal("docs are written")

    mcp = MCPManager(
        tmp_path,
        runtime.registry,
        permissions=runtime.permissions,
        config_path=tmp_path / ".mcp.json",
    )
    mcp.connect("docs")
    runtime.mcp = mcp

    manager = WorkflowManager(
        tmp_path,
        client=object(),
        model="fake-model",
        store=tmp_path / ".runtime" / "workflows",
        runner=lambda *args, **kwargs: RunnerOutput("unused", 0),
    )
    manager.register("tiny", "One agent step", lambda ctx, args: ctx.agent("do it"))
    runtime.workflow = manager

    try:
        prompt = build_system_prompt(runtime)
    finally:
        mcp.close_all()

    assert "Session todo list" in prompt
    assert "plan the work" in prompt
    assert "Task board" in prompt
    assert "write docs" in prompt
    assert "Connected MCP servers" in prompt
    assert "docs: search, get_version" in prompt
    assert "tiny: One agent step" in prompt
    assert "Goal active: docs are written" in prompt
