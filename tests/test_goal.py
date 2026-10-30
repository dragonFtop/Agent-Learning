from types import SimpleNamespace

import pytest

from agent.goal import (
    GoalController,
    GoalError,
    GoalEvaluation,
    PromptGoalEvaluator,
    StopDecision,
    _parse_evaluation,
    transcript_text,
)
from agent.permissions import PermissionPolicy
from agent.runtime import AgentRuntime


def _text(text):
    return SimpleNamespace(type="text", text=text)


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


class FakeEvaluator:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def evaluate(self, condition, messages):
        self.calls.append({"condition": condition, "messages": list(messages)})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_parse_evaluation_accepts_fences_and_embedded_json():
    fenced = _parse_evaluation('```json\n{"ok": true, "reason": "done"}\n```')
    assert fenced == GoalEvaluation(ok=True, reason="done", impossible=False)

    embedded = _parse_evaluation(
        'Verdict: {"ok": false, "reason": "tests fail", "impossible": false}'
    )
    assert embedded == GoalEvaluation(ok=False, reason="tests fail", impossible=False)

    with pytest.raises(GoalError, match="invalid JSON"):
        _parse_evaluation("not json")
    with pytest.raises(GoalError, match="boolean 'ok'"):
        _parse_evaluation('{"reason": "done"}')
    with pytest.raises(GoalError, match="non-empty 'reason'"):
        _parse_evaluation('{"ok": true, "reason": "  "}')
    with pytest.raises(GoalError, match="both ok and impossible"):
        _parse_evaluation('{"ok": true, "reason": "x", "impossible": true}')


def test_transcript_text_clips_oversized_newest_message():
    messages = [
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "x" * 500},
    ]

    rendered = transcript_text(messages, max_characters=200)

    assert "[middle omitted]" in rendered
    assert len(rendered) <= 200
    assert rendered.startswith("ASSISTANT:")
    assert "start" not in rendered


def test_transcript_text_keeps_recent_messages_within_budget():
    messages = [
        {"role": "user", "content": "old message"},
        {"role": "assistant", "content": [{"type": "text", "text": "middle message"}]},
        {"role": "user", "content": [{"type": "tool_result", "content": "recent result"}]},
    ]

    rendered = transcript_text(messages, max_characters=60)

    assert "recent result" in rendered
    assert "old message" not in rendered


def test_controller_lifecycle_and_validation():
    controller = GoalController(FakeEvaluator([]), block_cap=2)

    assert controller.status_text() == "No goal set."
    with pytest.raises(GoalError, match="cannot be empty"):
        controller.set_goal("   ")
    with pytest.raises(GoalError, match="characters"):
        controller.set_goal("x" * 4001)

    state = controller.set_goal("all tests pass")
    assert state.condition == "all tests pass"
    assert "Goal active: all tests pass" in controller.status_text()

    message = controller.clear("user stopped")
    assert message == "Goal cleared: all tests pass"
    assert controller.active is None
    assert controller.status_text() == "No goal set."


def test_controller_continue_then_achieved():
    controller = GoalController(
        FakeEvaluator(
            [
                GoalEvaluation(ok=False, reason="tests still failing"),
                GoalEvaluation(ok=True, reason="suite is green"),
            ]
        )
    )
    controller.set_goal("all tests pass")

    first = controller.evaluate_after_turn([{"role": "user", "content": "hi"}], "done")

    assert first.action == "continue"
    assert first.reason == "tests still failing"
    assert "Goal not met" in first.message
    assert "all tests pass" in first.message

    second = controller.evaluate_after_turn([{"role": "user", "content": "hi"}], "done")

    assert second.action == "achieved"
    assert second.reason == "suite is green"
    assert controller.active is None
    assert "Goal achieved: all tests pass" in controller.status_text()


def test_controller_block_cap_and_impossible_and_error():
    capped = GoalController(
        FakeEvaluator([GoalEvaluation(ok=False, reason="nope")] * 3), block_cap=2
    )
    capped.set_goal("impossible thing")

    assert capped.evaluate_after_turn([]).action == "continue"
    assert capped.evaluate_after_turn([]).action == "continue"
    limit = capped.evaluate_after_turn([])
    assert limit.action == "limit"
    assert "blocked 2 consecutive turns" in limit.reason
    assert capped.active is not None

    impossible = GoalController(
        FakeEvaluator([GoalEvaluation(ok=False, reason="requires external credentials", impossible=True)])
    )
    impossible.set_goal("deploy to prod")
    failed = impossible.evaluate_after_turn([])
    assert failed.action == "failed"
    assert failed.reason == "requires external credentials"
    assert impossible.active is None
    assert "Goal failed" in impossible.status_text()

    broken = GoalController(FakeEvaluator([RuntimeError("model down")]))
    broken.set_goal("anything")
    error = broken.evaluate_after_turn([])
    assert error.action == "error"
    assert "RuntimeError" in error.reason
    assert broken.active is not None


def test_controller_defers_while_background_runs():
    evaluator = FakeEvaluator([])
    controller = GoalController(evaluator)
    controller.set_goal("all tests pass")

    decision = controller.evaluate_after_turn([], background_running=True)

    assert decision == StopDecision("defer", "background work is still running")
    assert evaluator.calls == []
    assert controller.active is not None


def test_prompt_evaluator_uses_tool_free_client_and_parses_output():
    client = FakeClient(
        [_response([_text('```json\n{"ok": false, "reason": "tests fail", "impossible": false}\n```')], "end_turn")]
    )
    evaluator = PromptGoalEvaluator(client, "fake-model")

    result = evaluator.evaluate("all tests pass", [{"role": "user", "content": "ran pytest"}])

    assert result == GoalEvaluation(ok=False, reason="tests fail", impossible=False)
    kwargs = client.messages.calls[0]
    assert "tools" not in kwargs
    assert "all tests pass" in kwargs["messages"][0]["content"]
    assert "ran pytest" in kwargs["messages"][0]["content"]


def _runtime(tmp_path, client, goal, background=None):
    runtime = AgentRuntime(
        client=client,
        model="fake-model",
        workdir=tmp_path,
        interactive=False,
        permissions=PermissionPolicy(mode="auto", workdir=tmp_path),
        goal=goal,
    )
    if background is not None:
        runtime.background = background
    return runtime


class FakeBackground:
    def __init__(self, running):
        self.running = running

    def drain_notifications(self):
        return []

    def running_ids(self):
        return list(self.running)


def test_runtime_continues_until_goal_is_met(tmp_path):
    client = FakeClient(
        [
            _response([_text("first attempt")], "end_turn"),
            _response([_text("second attempt")], "end_turn"),
        ]
    )
    controller = GoalController(
        FakeEvaluator(
            [
                GoalEvaluation(ok=False, reason="missing test output"),
                GoalEvaluation(ok=True, reason="verified"),
            ]
        )
    )
    controller.set_goal("all tests pass")
    runtime = _runtime(tmp_path, client, controller)

    final = runtime.run_turn("make the tests pass")

    assert final == "second attempt"
    assert len(client.messages.calls) == 2
    injected = [
        message
        for message in runtime.history
        if message["role"] == "user" and "[Goal not met]" in str(message["content"])
    ]
    assert len(injected) == 1
    assert controller.active is None


def test_runtime_returns_control_on_goal_limit(tmp_path):
    client = FakeClient(
        [
            _response([_text("still working")], "end_turn"),
            _response([_text("still working")], "end_turn"),
        ]
    )
    controller = GoalController(
        FakeEvaluator([GoalEvaluation(ok=False, reason="not verified")] * 2), block_cap=1
    )
    controller.set_goal("impossible thing")
    runtime = _runtime(tmp_path, client, controller)

    final = runtime.run_turn("keep going")

    assert "still working" in final
    assert "[goal limit:" in final
    assert controller.active is not None


def test_runtime_defers_evaluation_while_background_runs(tmp_path):
    client = FakeClient([_response([_text("waiting on job")], "end_turn")])
    evaluator = FakeEvaluator([])
    controller = GoalController(evaluator)
    controller.set_goal("job done")
    runtime = _runtime(tmp_path, client, controller, background=FakeBackground(["job-1"]))

    final = runtime.run_turn("run the job")

    assert final == "waiting on job"
    assert evaluator.calls == []
    assert controller.active is not None
