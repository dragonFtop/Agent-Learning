from types import SimpleNamespace

from agent.hooks import HookBus
from agent.permissions import PermissionPolicy
from agent.runtime import AgentRuntime
from tools.registry import ToolContext, ToolRegistry, ToolSpec


def _text(text):
    return SimpleNamespace(type="text", text=text)


def _thinking(text):
    return SimpleNamespace(type="thinking", thinking=text)


def _tool_use(tool_id, name, args):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=args)


def _response(content, stop_reason):
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
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


def _runtime(tmp_path, responses, **kwargs):
    client = FakeClient(responses)
    runtime = AgentRuntime(
        client=client,
        model="fake-model",
        workdir=tmp_path,
        interactive=False,
        permissions=PermissionPolicy(mode="auto", workdir=tmp_path),
        **kwargs,
    )
    return runtime, client


def test_tool_use_result_pairing_and_thinking_preserved(tmp_path):
    (tmp_path / "hello.txt").write_text("hello from file", encoding="utf-8")
    thinking = _thinking("I should read the file")
    first = _response(
        [thinking, _tool_use("tu_1", "read_file", {"path": "hello.txt"})],
        "tool_use",
    )
    second = _response([_text("done")], "end_turn")
    runtime, client = _runtime(tmp_path, [first, second])

    result = runtime.run_turn("read hello.txt")

    assert result == "done"
    assert runtime.history[1]["content"] is first.content
    assert runtime.history[1]["content"][0] is thinking
    tool_results = runtime.history[2]["content"]
    assert len(tool_results) == 1
    assert tool_results[0]["tool_use_id"] == "tu_1"
    assert "hello from file" in tool_results[0]["content"]
    # The second model call must contain the paired tool_result.
    def block_type(block):
        return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)

    second_call_messages = client.messages.calls[1]["messages"]
    assert any(
        isinstance(message.get("content"), list)
        and any(block_type(block) == "tool_result" for block in message["content"])
        for message in second_call_messages
    )


def test_permission_denial_becomes_tool_result(tmp_path):
    denied = PermissionPolicy(
        mode="strict",
        workdir=tmp_path,
        ask=lambda tool, args, reason, ctx: "deny",
    )
    client = FakeClient(
        [
            _response(
                [_tool_use("tu_2", "write_file", {"path": "../escape.txt", "content": "x"})],
                "tool_use",
            ),
            _response([_text("understood")], "end_turn"),
        ]
    )
    runtime = AgentRuntime(
        client=client,
        model="fake-model",
        workdir=tmp_path,
        interactive=True,
        permissions=denied,
    )

    result = runtime.run_turn("try to escape")

    assert result == "understood"
    tool_result = runtime.history[2]["content"][0]
    assert "Permission denied" in tool_result["content"]
    assert not (tmp_path.parent / "escape.txt").exists()


def test_hook_can_block_tool_execution(tmp_path):
    calls = []

    def handler(args, ctx):
        calls.append(args)
        return "should not run"

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="danger",
            description="test",
            input_schema={"type": "object", "properties": {}},
            handler=handler,
        )
    )
    hooks = HookBus()
    hooks.register("PreToolUse", lambda block, ctx: "blocked by hook")
    client = FakeClient(
        [
            _response([_tool_use("tu_3", "danger", {})], "tool_use"),
            _response([_text("ok")], "end_turn"),
        ]
    )
    runtime = AgentRuntime(
        client=client,
        model="fake-model",
        workdir=tmp_path,
        interactive=False,
        hooks=hooks,
        registry=registry,
        permissions=PermissionPolicy(mode="auto", workdir=tmp_path),
    )

    assert runtime.run_turn("go") == "ok"
    assert calls == []
    assert runtime.history[2]["content"][0]["content"] == "blocked by hook"


def test_unknown_tool_returns_error_result(tmp_path):
    client = FakeClient(
        [
            _response([_tool_use("tu_4", "nope", {})], "tool_use"),
            _response([_text("recovered")], "end_turn"),
        ]
    )
    runtime = AgentRuntime(
        client=client,
        model="fake-model",
        workdir=tmp_path,
        interactive=False,
        permissions=PermissionPolicy(mode="auto", workdir=tmp_path),
    )

    assert runtime.run_turn("go") == "recovered"
    assert "unknown tool" in runtime.history[2]["content"][0]["content"]


def test_stop_hook_can_force_another_turn(tmp_path):
    calls = {"count": 0}

    def stop_hook(history):
        calls["count"] += 1
        if calls["count"] == 1:
            return "keep going"
        return None

    hooks = HookBus()
    hooks.register("Stop", stop_hook)
    client = FakeClient(
        [
            _response([_text("first answer")], "end_turn"),
            _response([_text("second answer")], "end_turn"),
        ]
    )
    runtime = AgentRuntime(
        client=client,
        model="fake-model",
        workdir=tmp_path,
        interactive=False,
        hooks=hooks,
        permissions=PermissionPolicy(mode="auto", workdir=tmp_path),
    )

    assert runtime.run_turn("go") == "second answer"
    assert {"role": "user", "content": "keep going"} in runtime.history
    assert len(client.messages.calls) == 2
