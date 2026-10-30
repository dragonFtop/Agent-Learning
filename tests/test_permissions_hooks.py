import threading

from agent.hooks import HookBus
from agent.permissions import PermissionPolicy
from tools.registry import ToolContext


class RecordingAsk:
    def __init__(self, answer="deny"):
        self.answer = answer
        self.calls = []

    def __call__(self, tool_name, args, reason, ctx):
        self.calls.append((tool_name, args, reason))
        return self.answer


def _ctx(tmp_path, interactive=True):
    return ToolContext(cwd=tmp_path, interactive=interactive)


def test_hook_bus_first_non_none_wins():
    bus = HookBus()
    seen = []

    def first(*args):
        seen.append("first")
        return None

    def second(*args):
        seen.append("second")
        return "blocked"

    def third(*args):
        seen.append("third")
        return "also blocked"

    bus.register("PreToolUse", first)
    bus.register("PreToolUse", second)
    bus.register("PreToolUse", third)
    assert bus.trigger("PreToolUse", "x") == "blocked"
    assert seen == ["first", "second"]


def test_hook_bus_rejects_unknown_event():
    bus = HookBus()
    try:
        bus.register("Nope", lambda: None)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_deny_list_and_safe_allowlist(tmp_path):
    policy = PermissionPolicy(mode="allowlist", workdir=tmp_path)
    ask = RecordingAsk()
    policy._ask = ask
    ctx = _ctx(tmp_path)

    assert policy.check("bash", {"command": "ls -la"}, ctx) is None
    assert policy.check("bash", {"command": "rm -rf /"}, ctx) is not None
    assert ask.calls == []  # deny list never reaches the prompt

    assert policy.check("bash", {"command": "curl http://example.com"}, ctx) is not None
    assert ask.calls[-1][0] == "bash"


def test_allow_once_and_session_approval(tmp_path):
    policy = PermissionPolicy(mode="allowlist", workdir=tmp_path)
    ask = RecordingAsk(answer="session")
    policy._ask = ask
    ctx = _ctx(tmp_path)

    assert policy.check("bash", {"command": "curl http://example.com"}, ctx) is None
    assert policy.check("bash", {"command": "curl http://other.example"}, ctx) is None
    assert len(ask.calls) == 1  # second curl reuses the session approval


def test_write_inside_workspace_allowed_outside_denied(tmp_path):
    policy = PermissionPolicy(mode="allowlist", workdir=tmp_path)
    ctx = _ctx(tmp_path)
    assert policy.check("write_file", {"path": "a.txt", "content": "x"}, ctx) is None
    assert policy.check("write_file", {"path": "../escape.txt", "content": "x"}, ctx) is not None


def test_strict_mode_asks_for_writes(tmp_path):
    policy = PermissionPolicy(mode="strict", workdir=tmp_path)
    ask = RecordingAsk(answer="deny")
    policy._ask = ask
    ctx = _ctx(tmp_path)
    assert policy.check("write_file", {"path": "a.txt", "content": "x"}, ctx) is not None
    assert ask.calls


def test_non_interactive_context_fails_closed(tmp_path):
    policy = PermissionPolicy(mode="allowlist", workdir=tmp_path)
    ask = RecordingAsk(answer="allow")
    policy._ask = ask
    ctx = _ctx(tmp_path, interactive=False)
    assert policy.check("bash", {"command": "curl http://example.com"}, ctx) is not None
    assert ask.calls == []


def test_high_impact_tool_requires_approval(tmp_path):
    policy = PermissionPolicy(mode="allowlist", workdir=tmp_path)
    ask = RecordingAsk(answer="allow")
    policy._ask = ask
    ctx = _ctx(tmp_path)
    assert policy.check("spawn_teammate", {"name": "a"}, ctx) is None
    assert ask.calls[-1][0] == "spawn_teammate"
