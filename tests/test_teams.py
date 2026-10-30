import json
from types import SimpleNamespace

import pytest

from agent.coordinator import RuntimeCoordinator
from agent.permissions import PermissionPolicy
from agent.runtime import AgentRuntime
from agent.tasks import TaskStore
from agent.teams import MessageBus, TeamManager, validate_agent_name


def _usage():
    return SimpleNamespace(input_tokens=5, output_tokens=3)


def _text(text):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=_usage(),
    )


def _tool(name, args, tool_id="toolu_1"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id=tool_id, name=name, input=args)],
        stop_reason="tool_use",
        usage=_usage(),
    )


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def _manager(tmp_path, responses=(), worktrees=None):
    client = FakeClient(responses)
    runtime = AgentRuntime(
        client=client,
        model="fake-model",
        workdir=tmp_path,
        interactive=False,
        permissions=PermissionPolicy(mode="auto", workdir=tmp_path),
    )
    runtime.tasks = TaskStore(tmp_path / ".tasks", workspace=tmp_path)
    teams = TeamManager(
        runtime,
        tasks=runtime.tasks,
        worktrees=worktrees,
        idle_interval=0.02,
        console=runtime.console,
    )
    runtime.teams = teams
    return runtime, client, teams


def test_agent_name_validation():
    assert validate_agent_name("worker-1") is None
    assert "reserved" in validate_agent_name("lead")
    assert "reserved" in validate_agent_name("LEAD")
    assert validate_agent_name("../bad") is not None
    assert validate_agent_name("a" * 65) is not None


def test_message_bus_is_destructive_and_skips_corrupt_lines(tmp_path):
    bus = MessageBus(tmp_path)
    bus.send("lead", "worker", "hello", "message", {"n": 1})

    assert bus.peek("worker")
    messages = bus.read_inbox("worker")
    assert messages[0]["content"] == "hello"
    assert messages[0]["metadata"] == {"n": 1}
    assert bus.read_inbox("worker") == []

    inbox = tmp_path / ".mailboxes" / "worker.jsonl"
    inbox.write_text(
        "{not json\n"
        + json.dumps({"from": "lead", "to": "worker", "content": "ok", "type": "message"})
        + "\n",
        encoding="utf-8",
    )
    assert [message["content"] for message in bus.read_inbox("worker")] == ["ok"]

    with pytest.raises(ValueError):
        bus.send("lead", "../escape", "x")
    with pytest.raises(ValueError):
        bus.read_inbox("worker/../../escape")


def test_spawn_rejects_reserved_and_duplicate_names(tmp_path):
    _, _, teams = _manager(tmp_path)

    assert "reserved" in teams.spawn("lead", "r", "p", start=False)
    assert "spawned" in teams.spawn("Worker", "engineer", "p", start=False)
    assert "already exists" in teams.spawn("worker", "engineer", "p", start=False)


def test_spawn_claims_task_and_gate_blocks_workspace(tmp_path):
    runtime, _, teams = _manager(tmp_path)
    task = runtime.tasks.create("write feature")

    message = teams.spawn(
        "worker", "engineer", "Write the feature", task_id=task.id, start=False
    )

    assert "spawned" in message
    claimed = runtime.tasks.load(task.id)
    assert claimed.status == "in_progress"
    assert claimed.owner == "worker"
    record = teams.record("worker")
    assert record.cwd.resolve() == tmp_path.resolve()
    assert "[Assigned task" in record.runtime.runtime.history[0]["content"]
    assert teams.gate_check("worker", "write_file") is None


def test_gate_without_assignment_blocks_workspace_tools(tmp_path):
    _, _, teams = _manager(tmp_path)
    teams.spawn("worker", "engineer", "Wait for work", start=False)

    assert "claim a Task" in teams.gate_check("worker", "read_file")
    assert "claim a Task" in teams.gate_check("worker", "bash")


def test_plan_gate_submit_approve_then_write(tmp_path):
    runtime, client, teams = _manager(
        tmp_path,
        [
            _tool("submit_plan", {"plan": "Write a.txt"}),
            _text("waiting for approval"),
        ],
    )
    task = runtime.tasks.create("write a.txt")
    teams.spawn(
        "worker", "engineer", "Write a.txt", task_id=task.id, require_plan=True, start=False
    )
    teammate = teams.record("worker").runtime

    assert "plan status is required" in teams.gate_check("worker", "write_file")
    assert teammate.work() == "idle"
    record = teams.record("worker")
    assert record.gate == "pending"
    assert record.status == "waiting_approval"

    events = teams.drain_lead_inbox()
    requests = [event for event in events if event["type"] == "plan_approval_request"]
    assert requests
    request_id = requests[0]["metadata"]["request_id"]
    assert request_id in teams.format_events(events)

    assert "approved" in teams.review_plan(request_id, True)
    assert teammate.handle_inbox() is False
    assert teams.record("worker").gate == "approved"

    client.messages.responses.extend(
        [
            _tool("write_file", {"path": "a.txt", "content": "ok"}, tool_id="toolu_2"),
            _text("done"),
        ]
    )
    assert teammate.work() == "idle"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "ok"
    events = teams.drain_lead_inbox()
    assert any(event["type"] == "result" for event in events)


def test_rejected_plan_keeps_writes_blocked(tmp_path):
    runtime, _, teams = _manager(tmp_path)
    task = runtime.tasks.create("feature")
    teams.spawn(
        "worker", "engineer", "Do work", task_id=task.id, require_plan=True, start=False
    )
    teammate = teams.record("worker").runtime

    submitted = teams.submit_plan("worker", "Use bash")
    request_id = submitted.split("(")[1].split(")")[0]
    assert "rejected" in teams.review_plan(request_id, False, "No bash")
    assert teammate.handle_inbox() is False

    assert teams.record("worker").gate == "rejected"
    assert "plan status is rejected" in teams.gate_check("worker", "edit_file")
    assert "Plan submitted" in teams.submit_plan("worker", "Use edit_file")


def test_idle_teammate_claims_and_completes_task(tmp_path):
    runtime, client, teams = _manager(tmp_path)
    task = runtime.tasks.create("auto task")
    teams.spawn("worker", "engineer", "Wait for tasks", start=False)
    teammate = teams.record("worker").runtime

    assert teammate.wait_for_work() is True
    record = teams.record("worker")
    assert record.task_id == task.id
    assert runtime.tasks.load(task.id).status == "in_progress"
    assert "[Auto-claimed task" in teammate.runtime.history[-1]["content"]

    client.messages.responses.extend(
        [
            _tool("complete_task", {"task_id": task.id}),
            _text("completed"),
        ]
    )
    assert teammate.work() == "idle"

    assert runtime.tasks.load(task.id).status == "completed"
    assert teams.record("worker").task_id is None
    events = teams.drain_lead_inbox()
    assert any(event["type"] == "result" for event in events)
    assert any(event["type"] == "idle_notification" for event in events)


def test_shutdown_releases_unfinished_task(tmp_path):
    runtime, _, teams = _manager(tmp_path)
    task = runtime.tasks.create("work")
    teams.spawn("worker", "engineer", "Do work", task_id=task.id, start=False)
    teammate = teams.record("worker").runtime

    assert "Shutdown requested" in teams.request_shutdown("worker")
    assert teammate.handle_inbox() is True
    events = teams.drain_lead_inbox()
    responses = [event for event in events if event["type"] == "shutdown_response"]
    assert responses
    assert teams.pending_requests[responses[0]["metadata"]["request_id"]].status == "approved"

    teams.teammate_finished("worker")

    assert teams.record("worker") is None
    released = runtime.tasks.load(task.id)
    assert released.status == "pending"
    assert released.owner is None


def test_teammate_sends_message_to_lead(tmp_path):
    _, _, teams = _manager(tmp_path)
    teams.spawn("worker", "engineer", "Wait", start=False)

    assert "Sent" in teams.send_from("worker", "lead", "status update")

    events = teams.drain_lead_inbox()
    assert events[0]["from"] == "worker"
    assert events[0]["content"] == "status update"


def test_coordinator_pumps_team_events_into_lead(tmp_path):
    runtime, _, teams = _manager(tmp_path, [_text("team handled")])
    teams.bus.send("worker", "lead", "status update", "message")
    coordinator = RuntimeCoordinator(runtime)

    assert coordinator.pump_once() == "team handled"

    assert "[Team events]" in runtime.history[0]["content"]
    assert "status update" in runtime.history[0]["content"]
    assert teams.bus.read_inbox("lead") == []


def test_stop_all_marks_manager_stopping(tmp_path):
    _, _, teams = _manager(tmp_path)
    teams.spawn("worker", "engineer", "Wait", start=False)

    teams.stop_all(timeout=0.1)

    assert teams.stopping
    record = teams.record("worker")
    assert record is not None
    assert any(
        state.type == "shutdown" and state.target == "worker"
        for state in teams.pending_requests.values()
    )
    assert "shutting down" in teams.spawn("other", "engineer", "Wait", start=False)


def test_coordinator_stop_stops_teams(tmp_path):
    runtime, _, teams = _manager(tmp_path)
    teams.spawn("worker", "engineer", "Wait", start=False)
    coordinator = RuntimeCoordinator(runtime, poll_interval=0.05)

    coordinator.start()
    coordinator.stop()

    assert teams.stopping
