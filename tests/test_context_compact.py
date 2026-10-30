from memory import ContextManager


def _messages_with_tool_results(count: int) -> list:
    messages = []
    for i in range(count):
        messages.append({
            "role": "assistant",
            "content": [{"type": "tool_use", "id": f"tu_{i}", "name": "read_file", "input": {}}],
        })
        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": f"tu_{i}", "content": "x" * 200}],
        })
    return messages


def test_short_history_untouched():
    messages = _messages_with_tool_results(2)
    result = ContextManager(messages).micro_compact_memory()
    assert result is messages
    assert messages[1]["content"][0]["content"] == "x" * 200


def test_recent_results_kept_intact():
    messages = _messages_with_tool_results(5)
    ContextManager(messages).micro_compact_memory()
    assert messages[-1]["content"][0]["content"] == "x" * 200


def test_old_long_results_elided_with_tool_name():
    messages = _messages_with_tool_results(5)
    ContextManager(messages).micro_compact_memory()
    elided = messages[1]["content"][0]["content"]
    assert "Previous tool used: read_file" in elided


def test_compaction_tail_keeps_pending_tool_result_pair():
    messages = [
        {"role": "user", "content": "查天气"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "tu_1", "name": "bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "晴 26°C"}]},
    ]
    tail = ContextManager(messages)._compaction_tail()
    # tool_result 必须和它对应的 assistant（含 tool_use）成对保留
    assert tail == messages[-2:]


def test_compaction_tail_keeps_plain_last_user_message():
    messages = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！"},
        {"role": "user", "content": "今天天气如何？"},
    ]
    tail = ContextManager(messages)._compaction_tail()
    assert tail == messages[-1:]


def test_auto_compact_skips_when_only_fresh_tail_exceeds_threshold():
    # 模拟 livelock 场景：大 tool_result 刚拿到、还没被模型消费。
    # 待摘要部分只有最初的提问，远小于阈值 → 跳过压缩（不生成 transcript、不调用摘要模型）
    messages = [
        {"role": "user", "content": "查天气"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "tu_1", "name": "bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "x" * 50000}]},
    ]
    cm = ContextManager(messages)
    assert cm.estimate_tokens() > 5000
    result = cm.auto_compact_memory()
    assert result is messages
