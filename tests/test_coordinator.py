import time
from types import SimpleNamespace

import llm
from agent.background import BackgroundManager
from agent.coordinator import RuntimeCoordinator
from agent.cron import CronScheduler
from agent.permissions import PermissionPolicy
from agent.runtime import AgentRuntime

def _response(text):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=5, output_tokens=3),
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


def _runtime(tmp_path, responses):
    client = FakeClient(responses)
    runtime = AgentRuntime(
        client=client,
        model="fake-model",
        workdir=tmp_path,
        interactive=False,
        permissions=PermissionPolicy(mode="auto", workdir=tmp_path),
    )
    return runtime, client


def _due_scheduler(tmp_path, moment):
    scheduler = CronScheduler(
        tmp_path / ".scheduled_tasks.json",
        clock=lambda: moment,
        autoload=False,
        interval=0.05,
    )
    job = scheduler.schedule("* * * * *", "run the scheduled checks", durable=False)
    assert not isinstance(job, str)
    scheduler.poll_due_jobs(moment)
    return scheduler, job


def test_run_user_input_returns_and_emits(tmp_path):
    emitted = []
    runtime, client = _runtime(tmp_path, [_response("hello there")])
    coordinator = RuntimeCoordinator(runtime, on_result=emitted.append)

    result = coordinator.run_user_input("hi")

    assert result == "hello there"
    assert emitted == ["hello there"]
    assert runtime.history[0] == {"role": "user", "content": "hi"}


def test_pump_runs_cron_job_and_acknowledges_it(tmp_path):
    from datetime import datetime

    moment = datetime(2026, 10, 19, 9, 30)
    runtime, client = _runtime(tmp_path, [_response("scheduled work done")])
    scheduler, job = _due_scheduler(tmp_path, moment)
    runtime.cron = scheduler
    coordinator = RuntimeCoordinator(runtime)

    result = coordinator.pump_once()

    assert result == "scheduled work done"
    assert len(client.messages.calls) == 1
    assert runtime.history[0]["role"] == "user"
    assert "[Scheduled task" in runtime.history[0]["content"]
    assert "run the scheduled checks" in runtime.history[0]["content"]
    assert scheduler.jobs[job.id].pending_delivery is False
    assert scheduler.consume_queue() == []


def test_agent_error_restores_cron_job(tmp_path, monkeypatch):
    from datetime import datetime

    monkeypatch.setattr(llm, "LLM_RETRIES", 0)
    moment = datetime(2026, 10, 19, 9, 30)
    runtime, _ = _runtime(tmp_path, [RuntimeError("provider down")])
    scheduler, job = _due_scheduler(tmp_path, moment)
    runtime.cron = scheduler
    coordinator = RuntimeCoordinator(runtime)

    result = coordinator.pump_once()

    assert result is not None and result.startswith("[Agent error")
    assert scheduler.jobs[job.id].pending_delivery is True
    assert [queued.id for queued in scheduler.consume_queue()] == [job.id]


def test_pump_wakes_for_background_results(tmp_path):
    runtime, client = _runtime(tmp_path, [_response("background handled")])
    background = BackgroundManager(workdir=tmp_path)
    runtime.background = background

    background.start("echo wake-me")
    deadline = time.time() + 15
    while time.time() < deadline and not background.has_pending():
        time.sleep(0.02)
    assert background.has_pending()

    coordinator = RuntimeCoordinator(runtime)
    result = coordinator.pump_once()

    assert result == "background handled"
    assert len(client.messages.calls) == 1
    assert any(
        isinstance(message.get("content"), str)
        and "<task_notification>" in message["content"]
        for message in runtime.history
    )


def test_pump_without_events_does_not_call_the_model(tmp_path):
    runtime, client = _runtime(tmp_path, [])
    runtime.background = BackgroundManager(workdir=tmp_path)
    coordinator = RuntimeCoordinator(runtime)

    assert coordinator.pump_once() is None
    assert client.messages.calls == []


def test_start_and_stop_lifecycle(tmp_path):
    runtime, _ = _runtime(tmp_path, [])
    scheduler = CronScheduler(tmp_path / ".scheduled_tasks.json", autoload=False, interval=0.05)
    runtime.cron = scheduler
    coordinator = RuntimeCoordinator(runtime, poll_interval=0.05)

    coordinator.start()
    assert coordinator.is_alive()
    assert scheduler.is_running()
    coordinator.stop()

    assert not coordinator.is_alive()
    assert not scheduler.is_running()
