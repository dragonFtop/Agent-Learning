from types import SimpleNamespace

from memory.context_compact import (
    PERSIST_MARKER,
    SNIP_MARKER,
    SUMMARY_MARKER,
    ContextManager,
)


class FakeMessages:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )


class FakeClient:
    def __init__(self, text="summary of earlier work"):
        self.messages = FakeMessages(text)


def _pair(tool_id: str, result: str):
    return [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": tool_id, "name": "read_file", "input": {"path": "a"}}
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": result}],
        },
    ]


def test_persist_large_results_writes_file_and_preview(tmp_path):
    messages = _pair("tu_1", "A" * 5000)
    manager = ContextManager(
        messages,
        workdir=tmp_path,
        output_dir=tmp_path / ".tool_outputs",
        transcript_dir=tmp_path / ".transcripts",
    )
    persisted = manager.persist_large_tool_results(limit=1000)

    assert persisted == 1
    content = messages[1]["content"][0]["content"]
    assert content.startswith(PERSIST_MARKER)
    assert "A" * 1000 in content
    files = list((tmp_path / ".tool_outputs").glob("*.txt"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == "A" * 5000

    # A second pass must not persist the same preview again.
    assert manager.persist_large_tool_results(limit=1000) == 0


def test_snip_keeps_first_and_recent_turns_and_pairing(tmp_path):
    messages = [
        {"role": "user", "content": "turn one"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    ]
    for index in range(2, 8):
        messages.append({"role": "user", "content": f"turn {index}"})
        messages.extend(_pair(f"tu_{index}", f"result {index}"))

    manager = ContextManager(messages, workdir=tmp_path)
    result = manager.snip_compact(messages, max_messages=8)

    assert len(result) < len(messages)
    assert result[0] == messages[0]
    assert any(
        isinstance(message.get("content"), str) and SNIP_MARKER in message["content"]
        for message in result
    )
    # Every surviving tool_use must still have its tool_result.
    tool_uses = {
        block["id"]
        for message in result
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "tool_use"
    }
    results = {
        block["tool_use_id"]
        for message in result
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    }
    assert tool_uses <= results


def test_snip_skips_short_histories(tmp_path):
    messages = _pair("tu_1", "x")
    manager = ContextManager(messages, workdir=tmp_path)
    assert manager.snip_compact(messages, max_messages=50) is messages


def test_tool_result_budget_persists_oldest_large_results(tmp_path):
    messages = []
    for index in range(4):
        messages.extend(_pair(f"tu_{index}", str(index) * 3000))
    # A later assistant turn marks earlier results as consumed.
    messages.append({"role": "assistant", "content": [{"type": "text", "text": "done"}]})

    manager = ContextManager(messages, workdir=tmp_path, keep_recent=1)
    persisted = manager.tool_result_budget(messages, max_chars=7000)

    assert persisted >= 1
    persisted_blocks = [
        block
        for message in messages
        for block in (message.get("content") if isinstance(message.get("content"), list) else [])
        if block.get("type") == "tool_result"
        and str(block.get("content", "")).startswith(PERSIST_MARKER)
    ]
    assert persisted_blocks
    # The most recent result stays untouched.
    assert not str(messages[-2]["content"][0]["content"]).startswith(PERSIST_MARKER)


def test_auto_compact_replaces_history_with_summary_and_keeps_tail(tmp_path):
    messages = [
        {"role": "user", "content": "please investigate the bug " * 200},
        *_pair("tu_1", "y" * 3000),
        {"role": "assistant", "content": [{"type": "text", "text": "working on it"}]},
    ]
    client = FakeClient("The user asked to investigate a bug; one file was read.")
    manager = ContextManager(
        messages,
        client=client,
        workdir=tmp_path,
        threshold=100,
        transcript_dir=tmp_path / ".transcripts",
        output_dir=tmp_path / ".tool_outputs",
    )

    result = manager.auto_compact_memory()

    assert result is not messages
    assert result[0]["role"] == "user"
    assert SUMMARY_MARKER in result[0]["content"]
    assert "The user asked to investigate" in result[0]["content"]
    assert result[-1] is messages[-1]
    assert list((tmp_path / ".transcripts").glob("*.jsonl"))
    assert client.messages.calls


def test_auto_compact_returns_original_when_summary_fails(tmp_path):
    class BrokenClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("provider down")

    messages = [
        {"role": "user", "content": "long question " * 500},
        {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
    ]
    manager = ContextManager(messages, client=BrokenClient(), workdir=tmp_path, threshold=10)
    assert manager.auto_compact_memory() is messages
