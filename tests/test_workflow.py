import json
import threading

import pytest

from agent.workflow import (
    ExecutionState,
    RunnerOutput,
    WorkflowInputError,
    WorkflowManager,
)


class FakeRunner:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []
        self._lock = threading.Lock()

    def run(self, prompt, schema=None, label=None):
        with self._lock:
            self.calls.append({"prompt": prompt, "schema": schema, "label": label})
            index = len(self.calls)
        result = self.responder(prompt, schema, label, index)
        if isinstance(result, Exception):
            raise result
        return result


def _manager(tmp_path, runner, **kwargs):
    return WorkflowManager(
        tmp_path,
        client=object(),
        model="fake-model",
        store=tmp_path / ".runtime" / "workflows",
        runner=runner,
        **kwargs,
    )


def _two_step(ctx: ExecutionState, args):
    ctx.phase("Work")
    first = ctx.agent("first step", label="first")
    second = ctx.agent("second step", label="second")
    return {"first": first, "second": second}


def test_registration_validation_and_catalog(tmp_path):
    manager = _manager(tmp_path, FakeRunner(lambda *a: RunnerOutput("x", 1)))
    manager.register("simple", "A simple workflow", _two_step)

    assert "simple: A simple workflow" in manager.catalog_text()
    assert "review-changes" in manager.catalog_text()
    with pytest.raises(ValueError):
        manager.register("simple", "duplicate", _two_step)
    with pytest.raises(ValueError):
        manager.register("bad name", "bad", _two_step)
    with pytest.raises(ValueError):
        manager.register("not-callable", "bad", "nope")


def test_run_writes_snapshot_and_resume_replays_journal(tmp_path):
    runner = FakeRunner(lambda prompt, schema, label, index: RunnerOutput(f"result-{label}", 4))
    manager = _manager(tmp_path, runner)
    manager.register("simple", "Two steps", _two_step)

    payload = json.loads(manager.run("simple", {"x": 1}))

    assert payload["status"] == "completed"
    assert payload["result"] == {"first": "result-first", "second": "result-second"}
    assert payload["usage"] == {"agents": 2, "tokens": 8}
    run_id = payload["runId"]
    assert (manager.store / f"{run_id}.journal.jsonl").exists()
    assert (manager.store / f"{run_id}.output.json").exists()
    assert manager.last_run_id() == run_id
    assert len(runner.calls) == 2

    resumed = json.loads(manager.run("simple", {"x": 1}, resume_from_run_id=run_id))

    assert resumed["status"] == "completed"
    assert resumed["result"] == payload["result"]
    assert resumed["usage"] == {"agents": 0, "tokens": 0}
    assert len(runner.calls) == 2


def test_failed_run_resumes_completed_steps_only(tmp_path):
    state = {"fail": True}

    def responder(prompt, schema, label, index):
        if label == "second" and state["fail"]:
            return RuntimeError("boom")
        return RunnerOutput(f"ok-{label}", 3)

    runner = FakeRunner(responder)
    manager = _manager(tmp_path, runner)
    manager.register("resume-me", "Failure then resume", _two_step)

    first = json.loads(manager.run("resume-me", {}))

    assert first["status"] == "failed"
    assert "boom" in first["result"]["error"]
    assert len(runner.calls) == 2

    state["fail"] = False
    second = json.loads(
        manager.run("resume-me", {}, resume_from_run_id=first["runId"])
    )

    assert second["status"] == "completed"
    assert second["result"] == {"first": "ok-first", "second": "ok-second"}
    assert len(runner.calls) == 3  # first step cached, second re-run


def test_schema_retry_and_invalid_output(tmp_path):
    responses = iter([RunnerOutput("not json", 2), RunnerOutput({"ok": True}, 3)])
    runner = FakeRunner(lambda *args: next(responses))
    manager = _manager(tmp_path, runner)

    def structured(ctx, args):
        return ctx.agent(
            "decide",
            schema={
                "type": "object",
                "required": ["ok"],
                "properties": {"ok": {"type": "boolean"}},
            },
            label="decide",
        )

    manager.register("structured", "Structured output", structured)
    payload = json.loads(manager.run("structured", {}))

    assert payload["status"] == "completed"
    assert payload["result"] == {"ok": True}
    assert payload["usage"] == {"agents": 1, "tokens": 5}
    assert len(runner.calls) == 2

    always_bad = FakeRunner(lambda *args: RunnerOutput("nope", 1))
    broken = _manager(tmp_path / "broken", always_bad)
    broken.register("structured", "Structured output", structured)
    failed = json.loads(broken.run("structured", {}))

    assert failed["status"] == "failed"
    assert "invalid output" in failed["result"]["error"]


def test_agent_cap_and_token_budget_stop_the_run(tmp_path):
    runner = FakeRunner(lambda prompt, schema, label, index: RunnerOutput("x", 5))
    capped = _manager(tmp_path / "capped", runner, agent_cap=1)
    capped.register("two", "Two agents", _two_step)

    cap_payload = json.loads(capped.run("two", {}))

    assert cap_payload["status"] == "failed"
    assert "cap reached" in cap_payload["result"]["error"]

    budgeted = _manager(tmp_path / "budgeted", runner)
    budgeted.register("two", "Two agents", _two_step)

    budget_payload = json.loads(budgeted.run("two", {"budget": 1}))

    assert budget_payload["status"] == "failed"
    assert "token budget exceeded" in budget_payload["result"]["error"]


def test_review_changes_pipeline_and_parallel_verification(tmp_path):
    def responder(prompt, schema, label, index):
        if schema and "findings" in schema.get("properties", {}):
            return RunnerOutput(
                {"findings": [{"title": f"issue-{label}", "severity": "high"}]}, 5
            )
        return RunnerOutput({"isReal": True, "reason": "reproduced"}, 5)

    runner = FakeRunner(responder)
    manager = _manager(tmp_path, runner)

    payload = json.loads(manager.run("review-changes", {"changes": "query = f'...'"}))
    result = payload["result"]

    assert payload["status"] == "completed"
    assert payload["usage"]["agents"] == 8  # 4 dimensions x (audit + verify)
    assert len(result["confirmed"]) == 4
    assert {finding["dimension"] for finding in result["confirmed"]} == {
        "correctness",
        "security",
        "performance",
        "style",
    }
    assert all(finding["severity"] == "high" for finding in result["confirmed"])


def test_run_input_errors_are_returned_without_starting(tmp_path):
    runner = FakeRunner(lambda *args: RunnerOutput("x", 1))
    manager = _manager(tmp_path, runner)

    assert "unknown workflow" in manager.run("missing")
    assert "must be an object" in manager.run("review-changes", args=[])
    assert "invalid workflow runId" in manager.run(
        "review-changes", resume_from_run_id="nope"
    )
    assert "resume snapshot not found" in manager.run(
        "review-changes", resume_from_run_id="wf_review-changes_0123456789abcdef"
    )
    assert runner.calls == []


def test_resume_requires_matching_workflow_and_args(tmp_path):
    runner = FakeRunner(lambda *args: RunnerOutput("x", 1))
    manager = _manager(tmp_path, runner)
    manager.register("simple", "Two steps", _two_step)
    manager.register("other", "Other", lambda ctx, args: "other")

    payload = json.loads(manager.run("simple", {"x": 1}))
    run_id = payload["runId"]

    assert "does not match workflow name" in manager.run(
        "other", resume_from_run_id=run_id
    )
    assert "args do not match" in manager.run(
        "simple", {"x": 2}, resume_from_run_id=run_id
    )


def test_corrupt_resume_journal_is_rejected(tmp_path):
    runner = FakeRunner(lambda prompt, schema, label, index: RunnerOutput("x", 1))
    manager = _manager(tmp_path, runner)
    manager.register("simple", "Two steps", _two_step)
    payload = json.loads(manager.run("simple", {}))
    journal = manager.store / f"{payload['runId']}.journal.jsonl"
    journal.write_text("{not json\n", encoding="utf-8")

    message = manager.run("simple", {}, resume_from_run_id=payload["runId"])

    assert "invalid resume journal" in message
