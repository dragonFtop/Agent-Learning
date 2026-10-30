import json
from types import SimpleNamespace

from memory.longterm import (
    LongTermMemory,
    extract_json_array,
    memory_slug,
    should_store,
)


class FakeMessages:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )


class FakeClient:
    def __init__(self, payload):
        self.messages = FakeMessages(payload)


class BrokenClient:
    class messages:
        @staticmethod
        def create(**kwargs):
            raise RuntimeError("provider down")


def _runtime(client, history):
    return SimpleNamespace(client=client, model="fake-model", history=history)


def test_write_records_and_rebuild_index(tmp_path):
    memory = LongTermMemory(tmp_path / ".memory", model="fake-model")
    memory.write("Responds in Chinese", "user", "User prefers Chinese answers", "Always answer in Chinese.")
    memory.write("Project uses DeepSeek", "project", "Cloud model endpoint", "Uses api.deepseek.com/anthropic.")

    records = memory.records()
    assert {record["name"] for record in records} == {"Responds in Chinese", "Project uses DeepSeek"}
    index = memory.catalog_text()
    assert "[Responds in Chinese](responds-in-chinese.md)" in index
    assert "User prefers Chinese answers" in index
    assert memory.index_path.read_text(encoding="utf-8").strip() == index


def test_memory_slug_and_validation(tmp_path):
    assert memory_slug("Hello World!") == "hello-world"
    assert memory_slug("中文 记录") == "中文-记录"
    memory = LongTermMemory(tmp_path / ".memory", model="fake-model")
    try:
        memory.write("x", "invalid", "d", "b")
    except ValueError as error:
        assert "Unknown memory type" in str(error)
    else:
        raise AssertionError("invalid type must raise")
    assert memory.read_file("../escape.md") is None


def test_should_store_filters_temporary_and_duplicates():
    existing = [
        {
            "name": "Uses pytest",
            "type": "project",
            "description": "Test runner",
            "body": "Run py -m pytest.",
        }
    ]
    temporary = {
        "name": "Uses pytest",
        "type": "project",
        "scope": "current_task",
        "description": "Test runner",
        "body": "Run py -m pytest.",
    }
    assert not should_store(temporary, existing + [])
    assert not should_store(
        {
            "name": "Temp",
            "type": "project",
            "scope": "persistent",
            "description": "Temporary fact",
            "body": "Only for this session.",
        },
        existing,
    )
    duplicate = {
        "name": "Uses pytest",
        "type": "project",
        "scope": "persistent",
        "description": "Test runner",
        "body": "Run py -m pytest.",
    }
    assert not should_store(duplicate, existing)
    valid = {
        "name": "Prefers Chinese",
        "type": "user",
        "scope": "persistent",
        "description": "Reply language",
        "body": "Answer in Chinese.",
    }
    assert should_store(valid, existing)


def test_extract_json_array_ignores_prose():
    assert extract_json_array('sure: [1, 2] and [3]') == [1, 2]
    assert extract_json_array("no array") == []


def test_keyword_selection_handles_chinese(tmp_path):
    memory = LongTermMemory(tmp_path / ".memory", model="fake-model")
    memory.write("模型接入方式", "reference", "DeepSeek 云端接口", "使用 Anthropic 兼容端点。")
    memory.write("测试命令", "project", "pytest 运行方式", "py -m pytest -q。")

    selected = memory.keyword_selection(
        memory.records(), "项目怎么配置模型接口？", max_items=2
    )
    assert selected == ["模型接入方式.md"]


def test_recall_llm_selection_and_char_limit(tmp_path):
    memory = LongTermMemory(tmp_path / ".memory", model="fake-model")
    for index in range(5):
        memory.write(
            f"Record {index}",
            "project",
            f"Description {index}",
            f"Body {index} " + "x" * 100,
        )
    client = FakeClient([0])
    recalled = memory.recall(
        [{"role": "user", "content": "what do you remember about record zero?"}],
        client,
        "fake-model",
    )
    assert '<memory name="Record 0"' in recalled
    assert "Record 4" not in recalled
    assert "the current request wins" in recalled


def test_recall_falls_back_to_keywords_when_llm_fails(tmp_path):
    memory = LongTermMemory(tmp_path / ".memory", model="fake-model")
    for index in range(5):
        memory.write(
            f"Record {index}",
            "project",
            f"Unique topic {index}",
            f"Body {index}",
        )
    recalled = memory.recall(
        [{"role": "user", "content": "tell me about unique topic 3"}],
        BrokenClient(),
        "fake-model",
    )
    assert '<memory name="Record 3"' in recalled


def test_after_turn_stores_only_valid_persistent_records(tmp_path):
    memory = LongTermMemory(tmp_path / ".memory", model="fake-model")
    memory.write("Existing", "project", "Already stored", "Do not duplicate.")
    payload = [
        {
            "name": "Prefers short answers",
            "type": "user",
            "scope": "persistent",
            "description": "Style preference",
            "body": "Keep responses concise.",
        },
        {
            "name": "Session limit",
            "type": "project",
            "scope": "current_task",
            "description": "Temporary",
            "body": "Only this task.",
        },
        {
            "name": "Temporary marker",
            "type": "project",
            "scope": "persistent",
            "description": "Temporary",
            "body": "Only for this session.",
        },
        {
            "name": "Existing",
            "type": "project",
            "scope": "persistent",
            "description": "Already stored",
            "body": "Do not duplicate.",
        },
    ]
    history = [
        {"role": "user", "content": "Please always answer concisely in future sessions."},
        {"role": "assistant", "content": [{"type": "text", "text": "Understood, I will be concise."}]},
    ]

    stored = memory.after_turn(_runtime(FakeClient(payload), history))

    assert stored == 1
    names = {record["name"] for record in memory.records()}
    assert names == {"Existing", "Prefers short answers"}


def test_after_turn_skips_short_or_empty_dialogue(tmp_path):
    memory = LongTermMemory(tmp_path / ".memory", model="fake-model")
    client = FakeClient([])
    assert memory.after_turn(_runtime(client, [{"role": "user", "content": "hi"}])) == 0
    assert client.messages.calls == []


def test_consolidate_merges_records(tmp_path):
    memory = LongTermMemory(tmp_path / ".memory", model="fake-model", consolidate_threshold=10)
    for index in range(10):
        memory.write(f"Record {index}", "project", f"Desc {index}", f"Body {index}")
    payload = [
        {
            "name": "Merged A",
            "type": "project",
            "description": "First merged record",
            "body": "Body A",
        },
        {
            "name": "Merged B",
            "type": "user",
            "description": "Second merged record",
            "body": "Body B",
        },
    ]
    count = memory.consolidate(FakeClient(payload), model="fake-model")

    assert count == 2
    assert {record["name"] for record in memory.records()} == {"Merged A", "Merged B"}
    assert len(list(memory.store_dir.glob("*.md"))) == 3  # two records + index


def test_consolidate_rejects_duplicate_slugs(tmp_path):
    memory = LongTermMemory(tmp_path / ".memory", model="fake-model", consolidate_threshold=2)
    memory.write("A", "project", "First", "Body A")
    memory.write("B", "project", "Second", "Body B")
    payload = [
        {"name": "Same", "type": "project", "description": "One", "body": "x"},
        {"name": "Same", "type": "project", "description": "Two", "body": "y"},
    ]
    assert memory.consolidate(FakeClient(payload), model="fake-model") == 0
    assert {record["name"] for record in memory.records()} == {"A", "B"}
