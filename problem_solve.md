# Problem Solve：待修复问题清单与修复方案

> 基准：2026-09-17 工程化改造后的代码（命名规范化、scripts/、tests/、memory/ 修复已完成）。
> 本文档覆盖评审中**仍未解决**的问题。每个步骤如下：问题阐述 → 解决思路 → 修改代码。
> 按"步骤 1 → 5"从上往下应用，**每一步应用后项目都保持可运行、测试全绿**，可以分次提交。

## 问题总览

| 步骤 | 问题 | 严重度 | 涉及文件 |
|---|---|---|---|
| 1 | bash 工具：denylist 误杀 `/dev/null`、不返回 exit code、非沙箱却无声明 | 高 | `tools/bash.py` |
| 2 | 工具分发零防护；`task` schema 缺 `description`；工具描述占位级 | 高/中 | `tools/registry.py`、`tools/__init__.py` |
| 3 | 杂项：`bytesto` 文案错误；缺少 lint 配置 | 低 | `tools/file_ops.py`、新增 `ruff.toml` |
| 4 | 结构重构：父子循环重复、config 过载与依赖方向、`WORKDIR=cwd`、无重试、父循环无上限 | 结构/高 | `config.py`、新增 `llm.py` `prompts.py` `agent/loop.py`、`agent/parent_agent.py`、`agent/sub_agent.py`、`tools/registry.py`(1 行)、`scripts/debug_response.py` |
| 5 | 验证与收尾（补测试、README 同步） | — | `tests/`、`README.md` |

---

## 步骤 1：修复 bash 工具（tools/bash.py）

### 问题阐述

1. **denylist 误杀合法命令**：`"> /dev/"` 在危险列表里，导致 `command > /dev/null` 这类最常见的丢弃输出写法被拦截。
2. **不返回 exit code**：模型只看到输出文本，无法判断命令成功还是失败，会基于错误前提继续行动（例如 `pytest` 失败后它以为通过了）。
3. **安全声明缺失**：字符串黑名单拦不住任何有意图的绕过（命令拼接、变量展开、绝对路径），且 file 工具的 `safe_path` 沙箱可以被 bash 直接绕过——"看起来像沙箱"比"明确不是沙箱"更危险，必须在代码里声明清楚。

### 解决思路

- 用正则精确匹配 `rm -rf /`（用负向前瞻避免误伤 `rm -rf /tmp`），删掉 `> /dev/`，补充 `mkfs`、`dd if=`。
- 非零退出码时，在返回文本首行明确写出 exit code。
- `cwd` 改用 `WORKDIR`（步骤 4 之后它恒为项目根目录），bash 与文件工具共享同一基准目录。
- 用一段注释把"保险丝 ≠ 安全边界"讲清楚，后续要做真隔离时以权限确认层或容器替代。

### 修改代码（完整替换 tools/bash.py）

```python
import re
import subprocess

from langsmith import traceable

from config import WORKDIR

# 这是防手滑的"保险丝"，不是安全边界：正则匹配无法阻止有意图的绕过
# （命令拼接、变量展开、读写绝对路径等），file 工具的 safe_path 沙箱
# 也不能约束 bash。真正的隔离需要权限确认层或容器。
_BLOCKED_PATTERNS = [
    re.compile(r"\brm\s+-\S+\s+/{1,2}(?![a-zA-Z])"),  # rm -rf /（删除根目录，不误伤 /tmp 等）
    re.compile(r"\bsudo\b"),
    re.compile(r"\b(shutdown|reboot|mkfs)\b"),
    re.compile(r"\bdd\s+if="),
]
TIMEOUT_SECONDS = 120
MAX_OUTPUT = 50000


def _is_dangerous(command: str) -> bool:
    return any(p.search(command) for p in _BLOCKED_PATTERNS)


@traceable(name="run_bash", run_type="tool")
def run_bash(command: str) -> str:
    if _is_dangerous(command):
        return "Error: Dangerous command blocked"
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            encoding="utf-8",
            errors="ignore",
        )
    except subprocess.TimeoutExpired:
        return f"Error: Timeout ({TIMEOUT_SECONDS}s)"

    out = (result.stdout + result.stderr).strip()
    body = out[:MAX_OUTPUT] if out else "(no output)"
    if result.returncode != 0:
        return f"Command failed (exit code {result.returncode}):\n{body}"
    return body
```

### 验证

```bash
py - <<'PY'
from tools.bash import run_bash
print(run_bash("git status --short"))      # 正常输出
print("---")
print(run_bash('py -c "import sys; sys.exit(3)"'))  # 应看到 exit code 3
PY
```

---

## 步骤 2：重写 tools/registry.py（分发防护 + schema 修复 + 工具描述）

### 问题阐述

1. **工具分发零防护**：两个循环里都是 `handler(**block.input)`，模型把参数名填错（在弱模型上非常常见，比如把 `path` 写成 `file_path`）会直接抛异常杀死整个会话。规范做法是**把错误作为 tool_result 回传**，让模型看到错误并自我修正。
2. **schema 与消费端不一致**：代码读 `block.input.get("description")`（parent_agent.py），但 `task` 的 schema 里没有这个字段——模型永远不会填，日志恒为 "subtask"。
3. **工具描述占位级**：`"Run a shell command."` 是模型选工具、填参数时唯一的依据，信息量太少。应说明：做什么、何时用、参数含义、有什么限制。

### 解决思路

- 新增 `execute_tool(name, tool_input) -> str`：统一分发点。未知工具、参数错误（`KeyError`/`TypeError`）、工具内部异常，全部转成 `"Error: ..."` 文本返回，绝不向上抛。
- 输出统一截断 50000 字符（此前只有子代理截断，行为漂移）。
- 重写全部 schema 描述，并给每个参数加 `description`。
- `task` 补上 `description` 字段（日志用），`prompt` 说明"子代理看不到本会话，必须自带完整上下文"。

> 注意：`execute_tool` 在本步骤新增后，要等步骤 4 的循环重构才被接入。如果你想立即生效，把两个循环里 `handler = TOOLS_HANDLERS.get(...)` 那两行替换成 `output = execute_tool(block.name, block.input)` 即可（各 1 行）。

### 修改代码（完整替换 tools/registry.py）

```python
from .bash import run_bash
from .file_ops import run_edit, run_read, run_write

MAX_TOOL_OUTPUT = 50000


def _load_skill(name: str) -> str:
    from config import SKILL_LOADER  # 步骤 4 后改为: from prompts import SKILL_LOADER
    return SKILL_LOADER.get_content(name)


TOOLS_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "load_skill": lambda **kw: _load_skill(kw["name"]),
}


def execute_tool(name: str, tool_input: dict) -> str:
    """统一执行工具：任何异常都转换成文本返回给模型，而不是让 Agent 循环崩溃。"""
    handler = TOOLS_HANDLERS.get(name)
    if handler is None:
        return f"Error: Unknown tool '{name}'. Available: {', '.join(TOOLS_HANDLERS)}"
    try:
        output = handler(**tool_input)
    except TypeError as e:
        return f"Error: Invalid arguments for '{name}': {e}"
    except Exception as e:
        return f"Error: '{name}' failed with {type(e).__name__}: {e}"
    return str(output)[:MAX_TOOL_OUTPUT]


CHILD_TOOLS = [
    {
        "name": "bash",
        "description": (
            "Run a shell command in the project working directory. "
            "Returns combined stdout/stderr and reports the exit code on failure. "
            "Use for git, running tests, listing files, or any CLI operation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command, e.g. 'git status' or 'py -m pytest -q'.",
                }
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a UTF-8 text file inside the project directory. "
            "Use 'limit' to read only the first N lines of a large file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the project root."},
                "limit": {"type": "integer", "description": "Read only the first N lines (optional)."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Create or overwrite a file inside the project directory. "
            "Parent directories are created automatically. "
            "Prefer edit_file for small changes to existing files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the project root."},
                "content": {"type": "string", "description": "Full content to write."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace the first exact occurrence of old_text with new_text in a file. "
            "old_text must match the file byte-for-byte, including whitespace. "
            "Read the file first to copy the exact text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the project root."},
                "old_text": {"type": "string", "description": "Exact existing text to replace."},
                "new_text": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "load_skill",
        "description": (
            "Load the full instructions of a specialized skill by name. "
            "Available skill names and short descriptions are listed in the system prompt. "
            "Call this before starting an unfamiliar task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name, e.g. 'weather'."}
            },
            "required": ["name"],
        },
    },
]

PARENT_TOOLS = CHILD_TOOLS + [
    {
        "name": "task",
        "description": (
            "Spawn a subagent that works in a fresh context to complete a self-contained "
            "subtask, then returns a text summary. The subagent cannot see this conversation, "
            "so include all necessary context in 'prompt'. Use for multi-step or exploratory work."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Short label for the subtask (shown in logs).",
                },
                "prompt": {
                    "type": "string",
                    "description": "Complete, self-contained instructions for the subagent.",
                },
            },
            "required": ["prompt"],
        },
    },
]
```

同时更新 `tools/__init__.py`，把 `execute_tool` 导出：

```python
from .registry import CHILD_TOOLS, PARENT_TOOLS, TOOLS_HANDLERS, execute_tool
from .skill_loader import SkillLoader

__all__ = ["CHILD_TOOLS", "PARENT_TOOLS", "TOOLS_HANDLERS", "execute_tool", "SkillLoader"]
```

### 验证

```bash
py - <<'PY'
from tools import execute_tool
print(execute_tool("nope", {}))                          # Unknown tool
print(execute_tool("read_file", {"file_path": "R.md"}))  # 参数名错误 -> Error 文本
print(execute_tool("read_file", {"path": "README.md", "limit": 1}))
PY
py -m pytest -q   # 仍然全绿
```

---

## 步骤 3：杂项修复（文案 + lint）

### 问题阐述

1. `tools/file_ops.py` 返回 `"Wrote {n} bytesto {path} successfully"`（`bytesto` 拼写错误，且这个字符串会进入模型上下文，应该准确简洁）。
2. 项目没有 lint/format 配置，风格问题（如上面这处）只能靠人眼发现。

### 修改代码

`tools/file_ops.py`（1 行替换）：

```python
# 旧：return f"Wrote {len(content)} bytesto {path} successfully"
# 新：
return f"Wrote {len(content)} bytes to {path}"
```

新增 `ruff.toml`（项目根目录）：

```toml
line-length = 100
target-version = "py310"
```

`requirements-dev.txt` 追加：

```text
ruff
```

使用（安装后）：

```bash
py -m pip install -r requirements-dev.txt
py -m ruff check .        # 静态检查
py -m ruff format .       # 格式化（可选）
```

> 顺带约定：今后新增注释只解释"为什么"（隐藏约束、踩过的坑），不解释"做什么"。

---

## 步骤 4：结构重构（核心）

### 问题阐述

这一步一次解决 5 个问题（它们互相纠缠，拆开改会互相返工）：

1. **父子 Agent 循环是两份拷贝**（约 90% 同构），且已行为漂移：截断、轮次上限、异常表现都不一致。Agent 主循环应该只有一处实现。
2. **`config.py` 职责过载、依赖方向混乱**：它同时是环境变量读取 + 客户端工厂 + 全局单例容器 + 提示词工厂，还得在模块中间 import `tools`（config.py:17），靠"WORKDIR 定义在前"的顺序侥幸避免循环导入。规范方向：**config 不允许 import 项目内其他模块**，依赖图成为无环 DAG。
3. **`WORKDIR = Path.cwd()`**：从别的目录启动时，skills 加载与文件沙箱全部错位。
4. **模型调用无重试**：网络抖动 / 限流 / 服务端 5xx 直接崩溃。
5. **父循环 `while True` 无轮次上限**：模型陷入工具循环就是无限烧 token（子代理有 30 轮保护，父反而没有）。

### 解决思路

拆成三层，各归其位：

```text
config.py     仅环境变量与路径：load_dotenv、WORKDIR、MODEL、SKILL_DIR（不 import 任何项目模块）
llm.py        Anthropic 客户端 + create_message（指数退避重试）
prompts.py    初始化 SKILL_LOADER，构造父/子系统提示词
agent/loop.py 唯一的 Agent 循环 run_agent()：调模型 -> 分发工具 -> 回传结果，带 max_turns
```

重构后的依赖方向（无环、不依赖 import 顺序）：

```text
config.py  ←────────── llm.py
    ↑   ↑
    │   └───────────── prompts.py ──→ tools/skill_loader.py
    │                                    ↑
    └── tools/{bash,file_ops}.py         │
              ↑                          │
   tools/registry.py ──(惰性 import)─────┘
              ↑
   agent/loop.py ──→ llm.py, tools/
              ↑
   agent/{parent_agent,sub_agent}.py ──→ prompts.py
```

父代理用 `dispatch` 参数注入 `task` 特殊处理，子代理走默认分发——两份循环合并成一份，行为不可能再漂移。

### 修改代码

#### 4.1 替换 config.py（瘦身）

```python
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

# 加载配置中的 ANTHROPIC_BASE_URL 后，删除 ANTHROPIC_AUTH_TOKEN 以避免冲突
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 以本文件所在目录为项目根：无论从哪里启动，skills/ 与沙箱根目录都保持一致
WORKDIR = Path(__file__).resolve().parent

MODEL = os.environ.get("MODEL_ID")
if not MODEL:
    raise RuntimeError("缺少环境变量 MODEL_ID：请复制 .env.example 为 .env 并填写")

SKILL_DIR = WORKDIR / "skills"
```

#### 4.2 新增 llm.py

```python
import os
import time

import anthropic
from anthropic import Anthropic
from langsmith.wrappers import wrap_anthropic

import config  # noqa: F401  确保 .env 已加载、环境变量预处理已执行（不依赖调用方顺序）

# 使用 wrap_anthropic 包装后，每次 LLM 调用会自动追踪到 LangSmith
# （需设置 LANGSMITH_API_KEY 和 LANGSMITH_TRACING=true）
client = wrap_anthropic(Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL")))

# 只重试"重试可能成功"的错误：网络中断/超时、限流、服务端 5xx。
# 参数错误（400）等重试无意义，直接抛出。
RETRYABLE_ERRORS = (
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)
MAX_ATTEMPTS = 3


def create_message(**kwargs):
    """带指数退避（1s、2s）的模型调用；参数与 client.messages.create 相同。"""
    for attempt in range(MAX_ATTEMPTS):
        try:
            return client.messages.create(**kwargs)
        except RETRYABLE_ERRORS:
            if attempt == MAX_ATTEMPTS - 1:
                raise
            time.sleep(2 ** attempt)
```

#### 4.3 新增 prompts.py

（提示词文本保持与现状一字不差，只搬家。）

```python
from config import SKILL_DIR, WORKDIR
from tools.skill_loader import SkillLoader

SKILL_LOADER = SkillLoader(SKILL_DIR)

SKILL_SYSTEM = f"""You are a coding agent at {WORKDIR}. Use load_skill to access specialized knowledge before tackling unfamiliar topics.
Skills available:
{SKILL_LOADER.get_descriptions()}"""

PARENTAGENT_SYSTEM = f"You are a parent agent. Talk with the user or command subagent use tools to solve tasks.\n\n{SKILL_SYSTEM}"
SUBAGENT_SYSTEM = f"You are a subagent. help the parentagent to solve tasks.\n\n{SKILL_SYSTEM}"
```

#### 4.4 新增 agent/loop.py（唯一的 Agent 循环）

```python
from langsmith import traceable

from config import MODEL
from llm import create_message
from tools import execute_tool

MAX_TURNS = 20  # 父循环轮次上限，防止工具循环烧 token


@traceable(name="run_agent", run_type="chain")
def run_agent(*, system: str, tools: list, messages: list, max_turns: int,
              dispatch=execute_tool):
    """通用 Agent 循环：调模型 -> 执行工具 -> 回传结果，直到模型不再调用工具。

    - dispatch(name, tool_input) 负责把一次工具调用变成结果字符串；
      默认走 execute_tool，父代理传入自定义函数以拦截 task。
    - 返回模型最后一次响应；达到 max_turns 仍未结束则返回 None。
    """
    response = None
    for _ in range(max_turns):
        response = create_message(
            model=MODEL,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return response

        results = [
            {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": dispatch(block.name, block.input),
            }
            for block in response.content
            if block.type == "tool_use"
        ]
        if not results:  # 异常响应：避免发出空的 tool_result 消息破坏协议
            return response
        messages.append({"role": "user", "content": results})
    return None
```

#### 4.5 替换 agent/parent_agent.py

```python
from langsmith import traceable

from prompts import PARENTAGENT_SYSTEM
from tools import PARENT_TOOLS, execute_tool

from .loop import MAX_TURNS, run_agent
from .sub_agent import run_subagent


def _dispatch(name: str, tool_input: dict) -> str:
    """父代理专属分发：task 派发子代理，其余走默认工具执行。"""
    if name != "task":
        return execute_tool(name, tool_input)
    try:
        desc = tool_input.get("description", "subtask")
        print(f">task ({desc}): {tool_input['prompt'][:80]}")
        return run_subagent(tool_input["prompt"])
    except Exception as e:  # 子代理失败不应杀死父循环
        return f"Error: subagent failed with {type(e).__name__}: {e}"


@traceable(name="agent_loop", run_type="chain")
def agent_loop(messages: list, max_turns: int = MAX_TURNS):
    response = run_agent(
        system=PARENTAGENT_SYSTEM,
        tools=PARENT_TOOLS,
        messages=messages,
        max_turns=max_turns,
        dispatch=_dispatch,
    )
    if response is None:
        print(f"[warn] 达到最大轮数 {max_turns}，本轮提前结束")
```

#### 4.6 替换 agent/sub_agent.py

```python
from langsmith import traceable

from prompts import SUBAGENT_SYSTEM
from tools import CHILD_TOOLS

from .loop import run_agent

SUBAGENT_MAX_TURNS = 30


@traceable(name="subagent", run_type="chain")
def run_subagent(prompt: str) -> str:
    sub_messages = [{"role": "user", "content": prompt}]
    response = run_agent(
        system=SUBAGENT_SYSTEM,
        tools=CHILD_TOOLS,
        messages=sub_messages,
        max_turns=SUBAGENT_MAX_TURNS,
    )
    if response is None:
        return "(subagent reached max turns without a final answer)"
    return "".join(
        b.text for b in response.content if hasattr(b, "text")
    ) or "(no summary)"
```

#### 4.7 两个一行修改

`tools/registry.py` 中 `_load_skill` 的惰性 import 改指向新模块：

```python
# 旧：from config import SKILL_LOADER
from prompts import SKILL_LOADER
```

`scripts/debug_response.py` 的 import 拆分（`client` 已搬去 `llm`）：

```python
# 旧：from config import client, MODEL
from config import MODEL
from llm import client
```

> 提醒：替换后不要保留旧名字（`config.client`、`config.PARENTAGENT_SYSTEM` 等已不存在）。全项目只有上面这两处 + 4.5 / 4.6 引用过它们，无需再搜。

### 验证

```bash
py -m pytest -q    # 期望全绿
py - <<'PY'
import agent, llm, prompts, tools, memory, scripts
print("imports ok")
PY
py -m scripts.debug_agent_loop   # 端到端（需先启动 Ollama 与 LiteLLM）
```

---

## 步骤 5：验证与收尾

### 5.1 新增分发防护的测试（tests/test_execute_tool.py）

```python
from tools import execute_tool


def test_unknown_tool_returns_error_text():
    result = execute_tool("nope", {})
    assert "Unknown tool" in result


def test_wrong_argument_name_returns_error_text():
    result = execute_tool("read_file", {"file_path": "README.md"})
    assert result.startswith("Error")


def test_normal_call_returns_content():
    result = execute_tool("read_file", {"path": "README.md", "limit": 1})
    assert not result.startswith("Error")
```

### 5.2 全量回归

```bash
py -m pytest -q        # 期望 12 passed（9 个已有 + 3 个新增）
py -m ruff check .
```

手工验证建议：

- 让 Agent 执行一个会失败的 bash 命令，确认它能看到 exit code 并自行调整；
- 对 Agent 说"派一个子代理做点事"，确认 task 日志与子代理总结正常；
- 故意在 `_dispatch` 里让子代理抛错一次，确认父循环返回 Error 文本而不是崩溃。

### 5.3 README 同步清单

- 项目结构树：新增 `llm.py`、`prompts.py`、`agent/loop.py`；
- 模块职责：`config.py` 改为"仅环境变量与路径配置"，新增三行；
- 已知限制：**删除**"默认工作目录取 `Path.cwd()`，请确保从项目根目录启动"（已修复）；
- 运行流程（简述）：第 2 步改述为"父 Agent 调用 `agent/loop.py` 的通用循环"。

---

## 修复后仍然存在的已知边界（本次不处理，明确记录）

- bash 仍是"保险丝"而非安全沙箱；若要真隔离，后续做权限确认层（参考 Claude Code 的 permission 机制）或容器执行。
- 无流式输出；无日志分级（print 裸奔）。
- `memory/context_compact.py` 修好了但还未接入循环——可作为下一步"上下文管理"的起点。
