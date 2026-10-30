# 从零手写 Claude Code 式 Agent Harness

> 本文讲解本仓库如何用纯 Python + Anthropic SDK 复现一个 Claude Code 风格的
> agent harness。内容参照开源项目 **shareAI-lab/learn-claude-code**（MIT）的
> 17 课（s01–s17），并针对 Python 3.14、Windows 11、DeepSeek 的
> Anthropic 兼容端点做了重写与集成。
>
> 原始博客地址已经失效，本文以该开源项目的课程结构为复现基准。本仓库代码不是
> 逐课示例的拷贝，而是把 17 个机制合并进同一个可运行的 CLI。

---

## 目录

0. [快速开始](#0-快速开始)
1. [设计哲学](#1-设计哲学)
2. [全局架构](#2-全局架构)
3. [核心抽象](#3-核心抽象)
4. [十七课逐章](#4-十七课逐章)
5. [一次 turn 的完整时序](#5-一次-turn-的完整时序)
6. [持久化布局](#6-持久化布局)
7. [并发模型与 Windows 适配](#7-并发模型与-windows-适配)
8. [DeepSeek thinking 兼容说明](#8-deepseek-thinking-兼容说明)
9. [测试策略](#9-测试策略)
10. [扩展指南与故障排查](#10-扩展指南与故障排查)
11. [十七课验收清单](#11-十七课验收清单)
- [附录 A：工具清单](#附录-a工具清单)
- [附录 B：配置项](#附录-b配置项)

---

## 0. 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 准备配置
cp .env.example .env
#   编辑 .env，至少填入 MODEL_ID 与鉴权信息

# 3. 启动
python main.py

# 4. 测试（不会访问真实 API）
pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall -q .
```

DeepSeek Anthropic 兼容端点的典型配置：

```env
MODEL_ID=deepseek-chat
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_API_KEY=your-api-key
```

`config.py` 在检测到 `ANTHROPIC_BASE_URL` 时会移除
`ANTHROPIC_AUTH_TOKEN`，避免 SDK 同时发送两套凭据。

交互命令：

| 命令 | 作用 |
|---|---|
| `/help` | 显示命令列表 |
| `/tasks` | 持久任务板 |
| `/teammates` | 活跃队友 |
| `/crons` | 定时任务 |
| `/mcp` | MCP 服务器与工具 |
| `/workflows` | 已注册工作流与最近一次运行 |
| `/workflow NAME [JSON]` | 直接运行工作流 |
| `/goal [条件\|clear]` | 查看、设置、清除目标闭环 |
| `/memory` | 长期记忆目录 |
| `/compact [focus]` | 手动压缩上下文 |
| `/quit` 或 `q` / `exit` | 退出 |

---

## 1. 设计哲学

### 1.1 Agency 来自模型，Harness 只提供边界

这个项目刻意不把任务拆成 if/else 工作流。模型负责决定下一步做什么；
harness 只负责四件事：

1. **执行**：把 `tool_use` 安全地变成真实的文件和命令操作；
2. **反馈**：把结果（包括拒绝和异常）变成 `tool_result` 回灌给模型；
3. **约束**：权限、路径沙箱、预算、轮数上限；
4. **状态**：上下文压缩、任务板、记忆、团队消息、定时事件。

除了 `agent/workflow.py` 中由宿主显式注册的固定编排，模型每时每刻都可以
自由选择工具。新增能力的原则是“加一个工具/服务”，而不是“改主循环”。

### 1.2 一个循环

整个进程只有一个 agent 循环：`AgentRuntime._run()`。父代理、子代理、队友
都是同一种 `AgentRuntime`，只是 `profile`、历史、工作目录和工具子集不同。
这避免了“父循环 + 子循环 + 队友循环”三套逻辑逐渐分叉。

### 1.3 状态外置，进程可恢复

任务板、记忆、cron、workflow journal、邮箱都落在工作区文件里。进程重启后：

- 任务和 cron 仍在；
- 长期记忆仍可召回；
- workflow 可以用 `runId` 续跑；
- 邮箱里的未读消息仍在（队友进程本身不会恢复）。

对话历史本身不持久化，这是有意的：会话是一次性的，长期知识应进入 `.memory/`。

### 1.4 不用框架

没有 LangChain、AutoGen 或编排 DSL。依赖只有 Anthropic SDK、python-dotenv
和 LangSmith（可选追踪）。控制流全部是普通 Python，便于调试和学习。

### 1.5 安全是熔断器，不是沙箱

`tools/safety.py`、`PermissionPolicy` 和路径沙箱能阻止常见事故，但都不是
操作系统级隔离。真正的隔离需要容器或虚拟机；本文档第 10 节会说明边界。

### 1.6 成本可见

记忆抽取/召回、目标评估、workflow 每个 agent 步骤都是额外模型调用。所有
此类功能都有开关或预算上限，且默认只在有意义时触发。

---

## 2. 全局架构

```text
┌─────────────────────────────────────────────────────────────┐
│ main.py: REPL / slash commands                              │
└───────────────┬─────────────────────────────────────────────┘
                │ run_user_input / run_locked
                v
┌─────────────────────────────────────────────────────────────┐
│ RuntimeCoordinator                                          │
│  • 全局 turn RLock                                          │
│  • 事件泵线程（cron 队列 / 团队邮箱 / 后台唤醒）              │
│  • 停止时统一关闭 cron、teammates、MCP                        │
└───────────────┬─────────────────────────────────────────────┘
                │ 同一份 lead history
                v
┌─────────────────────────────────────────────────────────────┐
│ AgentRuntime._run()                                         │
│                                                             │
│  while True:                                                │
│    注入后台通知 / todo 提醒                                   │
│    ContextManager.prepare()      ← L0–L4 压缩                │
│    build_system_prompt(runtime)  ← 动态状态                  │
│    registry.subset(profile)      ← 动态工具池                │
│    call_llm()                                               │
│    append assistant（原样）                                   │
│    ├─ 有 tool_use：PreToolUse → handler → PostToolUse        │
│    │                每个 tool_use 恰好一个 tool_result       │
│    └─ 无 tool_use：Stop hook → goal → memory → 返回          │
└───────────────┬─────────────────────────────────────────────┘
                │ 服务由 main.py 挂到 runtime 上
                v
┌──────────────┬──────────────┬──────────────┬────────────────┐
│ TaskStore    │ Background   │ Cron         │ TeamManager    │
│ TodoManager  │ Memory       │ MCP          │ Workflow       │
│ SkillLoader  │ ContextMgr   │ Goal         │ Worktree       │
└──────────────┴──────────────┴──────────────┴────────────────┘
```

### 2.1 目录导览

```text
main.py                  REPL、slash 命令、依赖装配
config.py                env、client、路径、feature flags、SkillLoader
llm.py                   唯一模型调用入口：重试、usage、超长识别、文本提取
agent/
  runtime.py             唯一主循环；profile: lead/subagent/teammate
  coordinator.py         事件泵与 turn 锁
  prompts.py             动态 system prompt
  hooks.py               四类事件钩子
  permissions.py         硬拒 → 路径沙箱 → allowlist → 交互确认
  todo.py                s05 会话 todo
  sub_agent.py           s06 兼容入口（实际复用 AgentRuntime）
  parent_agent.py        s01 兼容入口 agent_loop(history)
  tasks.py               s10 持久任务板
  background.py          s11 后台命令
  cron.py                s12 cron 调度
  teams.py               s13 邮箱、协议、团队状态
  teammate.py            s13 队友线程
  worktree.py            s13 Git worktree
  mcp.py                 s14 mock + stdio MCP
  workflow.py            s16 宿主注册编排 + journal 续跑
  goal.py                s17 目标闭环
tools/
  registry.py            ToolContext / ToolSpec / ToolRegistry
  builtin.py             所有内置工具 spec
  bash.py                进程启动、超时、进程树清理
  file_ops.py            沙箱化文件操作
  output_limit.py        s08 L0 输出截断
  skill_loader.py        s07 技能目录与按需加载
  safety.py              命令分类与危险模式
  frontmatter.py         YAML-ish frontmatter 解析/生成
memory/
  context_compact.py     s08 分层压缩
  longterm.py            s09 长期记忆
tests/                   fake client / fake runner / fake server 单测
skills/weather/SKILL.md  示例技能
scripts/                 手动调试脚本
```

---

## 3. 核心抽象

### 3.1 ToolContext

`ToolContext`（`tools/registry.py`）是工具 handler 的唯一执行上下文：

| 字段 | 含义 |
|---|---|
| `runtime` | 当前 `AgentRuntime`，用于访问服务与状态 |
| `cwd` | 当前沙箱根目录；队友在 worktree 中时指向 worktree |
| `profile` | `lead` / `subagent` / `teammate` |
| `agent_name` | 任务所有权和团队消息的发送者 |
| `task_id` | 当前认领的持久任务 |
| `interactive` | 是否允许交互式权限询问 |
| `extra` | 预留扩展字典 |

工具不读全局变量。同一个 `bash` 工具在父代理、子代理和 worktree 里的行为
一致，但 cwd 不同。

### 3.2 ToolSpec 与 ToolRegistry

```python
ToolSpec(
    name="read_file",
    description="...",
    input_schema={...},
    handler=callable,
    profiles=("lead", "subagent", "teammate"),
)
```

- `profiles` 决定工具池子集；新增工具不需要改循环。
- `register/unregister/get/names/subset/to_anthropic_tools` 全部持有一把
  `RLock`，因为 MCP 可能在队友线程运行时动态注册工具。
- `dispatch()` 统一把异常转换为 `Error: ...` 字符串，保证模型总能收到
  `tool_result`，不会因为工具异常破坏 `tool_use/tool_result` 配对。
- `build_base_registry()` 在 `tools/builtin.py` 中组装所有内置工具。

### 3.3 HookBus

四类事件（`agent/hooks.py`）：

| 事件 | 时机 | 非 None 返回值的含义 |
|---|---|---|
| `UserPromptSubmit` | 用户消息入历史前 | 前置注入文本 |
| `PreToolUse` | 工具执行前 | 阻断并作为 tool_result |
| `PostToolUse` | 工具执行后 | 替换输出 |
| `Stop` | 模型不再调用工具时 | 追加一条 user 消息并继续循环 |

规则只有一条：**按注册顺序执行，第一个非 None 的返回值获胜**。权限检查
就是默认注册的 `PreToolUse` 钩子；队友的计划门禁是另一个 `PreToolUse`。

### 3.4 PermissionPolicy

检查顺序（`agent/permissions.py`）：

1. 队友协议白名单（`claim_task` 等）直接放行；
2. `bash`：危险模式硬拒；`auto` 放行；`allowlist` 下仅无 shell 元字符的
   安全命令放行；其余询问；
3. `write_file/edit_file`：先检查路径不能逃出 `ctx.cwd`；
4. `mcp__*`：宿主策略 `allow` 放行，否则询问；
5. 只读工具放行；
6. 高影响工具（任务、cron、队友、workflow 等）在 `auto` 下放行，其余询问；
7. 未知工具：`auto` 放行，其他模式询问。

不能询问的环境（非主线程或 `interactive=False`）一律 **fail closed**。
会话内选择 “always” 会记住 `bash:<命令>` 或 `mcp:<server>` 等键。

`PERMISSION_MODE=allowlist` 是默认值，也是学习项目里比较实用的折中：

- 读文件、`git status`、`pytest` 等直接跑；
- 写工作区文件直接跑（路径沙箱是边界）；
- 删除、安装、网络、未知命令、高影响状态变更需要确认。

### 3.5 ContextManager

`memory/context_compact.py` 实现分层压缩，详见 s08 与第 5 节：

```text
L0 cap_output                 单条工具结果 ≤ 6000 字符，溢出落盘
L1 persist_large_tool_results > 30000 字符结果落盘，上下文留预览与路径
L2 snip_compact               > 50 条消息时按完整 turn 组丢弃最早片段
L3 micro_compact_memory       已消费的旧 tool_result → 占位符
L3.5 tool_result_budget       工具结果总字符 ≤ 200000，先压最大且已消费的
L4 auto_compact_memory        仍超 5000 token 估算阈值 → LLM 摘要
```

所有层都遵守两个硬约束：不修改 assistant 消息；不拆散
`tool_use/tool_result` 配对。

### 3.6 AgentRuntime

`AgentRuntime` 持有：

- `history`、`client`、`model`、`workdir/cwd`、`profile`；
- `hooks`、`permissions`、`registry`、`context_manager`、`skill_loader`；
- 可选服务：`todos`、`tasks`、`worktrees`、`background`、`memory`、`mcp`、
  `workflow`、`teams`、`cron`、`goal`。

关键方法：

- `run_turn(text, source="user")`：追加用户消息、刷新记忆召回、进入循环；
- `run(source=...)`：不追加用户消息，继续已有历史（供 cron/团队/后台使用）；
- `spawn_subagent(prompt)`：新建一个隔离的 runtime 并运行；
- `force_compact(focus)`：手动摘要压缩。

`_run()` 是唯一的循环。每轮：

1. 检查 `max_turns`（默认 50）；
2. 注入后台通知；
3. 必要时追加 todo 提醒（默认 3 轮未更新）；
4. 压缩上下文；
5. 调用模型；
6. 原样追加 assistant 内容；
7. 有工具调用则逐个执行并追加一条包含全部结果的 user 消息；否则走
   Stop → goal → memory 并返回。

### 3.7 RuntimeCoordinator

`agent/coordinator.py` 解决“多个事件源如何进入同一份历史”：

- `run_user_input`、`run_locked`、`pump_once` 共用一把 `RLock`，任何两个
  turn 都不会交错；
- 事件泵线程默认每秒轮询；
- cron 线程只把到期任务放入队列，真正的 turn 由事件泵在锁内执行；
- cron turn 成功后 `acknowledge`，失败或 `[Agent error...]` 时 `restore`；
- 团队消息被格式化为一条 user 消息注入；
- 后台上具只负责唤醒 runtime，通知由 runtime 在下一轮自行 drain；
- `stop()` 依次停止事件泵、cron、队友、MCP。

---

## 4. 十七课逐章

### s01 — Agent Loop

**问题**：模型只会返回文本，如何让它连续使用工具完成任务？

**机制**（`agent/runtime.py`）：

```text
while rounds < max_turns:
    response = LLM(history, tools, system)
    append assistant(response.content)   # 原样，不重建
    tool_uses = [block for block in content if type == "tool_use"]
    if not tool_uses:
        finish hooks → return text
    results = execute(tool_uses)
    append user(results)
```

**设计取舍**：

- 循环条件只看“有没有工具调用”，不看 `stop_reason`，避免供应商字段差异；
- 一次 assistant 消息中的所有工具调用，其 `tool_result` 合并进**一条** user
  消息，这是 Anthropic 消息格式的要求；
- `max_turns` 是失控保险丝；达到后返回明确的停止文本；
- LLM 异常被转成字符串返回，CLI 不崩溃。

**练习**：把 `max_turns` 设为 1，观察模型只规划不执行时的输出。

---

### s02 — Tool Use

**问题**：工具 schema、处理函数和可见性如何组织，才能“加工具不改循环”？

**机制**（`tools/registry.py`、`tools/builtin.py`）：

- `ToolSpec` 同时携带 schema、handler、来源和 profile；
- `ToolRegistry.subset(profile)` 生成某个代理的工具池；
- `dispatch(name, args, ctx)` 查找并执行，异常转文本；
- 内置工具从文件操作、bash，到任务、团队、MCP、workflow 一共 30 个。

**设计取舍**：

- handler 返回字符串；结构化 JSON 由工具自己序列化，模型侧始终是文本；
- 未知工具也返回可读错误，保证配对；
- 动态工具（MCP）与静态工具共用同一个注册表，因此 prompt 和工具池自动同步。

**练习**：新增一个 `get_time` 工具，只改 `builtin.py`，确认循环代码零改动。

---

### s03 — Permission

**问题**：如何既让模型自主工作，又避免误删或越界？

**机制**（`agent/permissions.py`、`tools/safety.py`）：

- 硬拒 regex：`rm -rf /`、`sudo`、`mkfs`、`format C:`、fork bomb 等；
- 含 shell 元字符（`; & | < > \` $ %` 换行）的命令不视为“安全命令”；
- 安全命令 allowlist：`ls/cat/grep/find/git status|diff|log...`、
  `python --version`、`pytest` 等；
- 文件写入必须经 `safe_path` 解析到 `ctx.cwd` 内；
- 没有终端线程时拒绝询问，即 fail closed；
- 用户可选择 once / always session / deny。

**设计取舍**：正则只是熔断器，不是沙箱。权限层的价值在于“默认安全”，而不是
对抗恶意用户。

**练习**：在 `allowlist` 模式下让模型执行 `echo hi > x.txt`，观察它被询问；
再试 `cat x.txt`，观察直接放行。

---

### s04 — Hooks

**问题**：权限、日志、输出改写、停止条件都想介入循环，是否要把它们写进
`while`？

**机制**（`agent/hooks.py`）：

- 4 个事件，顺序执行，首个非 None 返回生效；
- runtime 默认把 `PermissionPolicy.check` 注册为 `PreToolUse`；
- 队友额外注册计划门禁；
- `Stop` 钩子可返回文本，使模型“再想一轮”。

**设计取舍**：钩子不吞异常（异常会向上冒泡），这样扩展点的 bug 不会被静默
忽略；同时主循环只保留很少几个固定的调用点。

**练习**：注册一个 `PostToolUse` 钩子，把所有超过 1000 行的输出替换为
“请分页读取”。

---

### s05 — TodoWrite

**问题**：长任务中模型容易忘记下一步。

**机制**（`agent/todo.py`）：

- 会话级列表，最多 20 项；只允许一个 `in_progress`；
- 每 3 轮未更新且列表非空时，注入
  `<reminder>Update your todo list before continuing.</reminder>`；
- todo 渲染进 system prompt 的 “Session todo list” 段；
- 会话结束即消失，**不是**持久任务板。

**设计取舍**：todo 是注意力锚点，不承担协作和恢复职责；持久工作交给 s10。

**练习**：连续 4 轮不调用 `todo_write`，观察提醒出现的位置。

---

### s06 — Subagent

**问题**：探索性任务会污染主上下文。

**机制**（`agent/runtime.py:spawn_subagent`）：

- 新建 `AgentRuntime`，`profile="subagent"`；
- 历史只有第一条任务提示；
- 共享 `hooks/permissions/registry/skill_loader`，但没有 todo；
- `max_turns=30`，`interactive=False`；
- 只把最终文本返回给父代理，中间工具输出全部丢弃。

**设计取舍**：子代理不是另一个循环，而是同一循环的隔离实例。代价是父代理
看不到细节，收益是上下文极其干净。

**练习**：让子代理调查 10 个文件，再从父历史里确认只多了一条摘要。

---

### s07 — Skill

**问题**：把所有领域知识塞进 system prompt 太贵。

**机制**（`tools/skill_loader.py`）：

- 扫描 `skills/**/SKILL.md`，解析 frontmatter 的 `name/description/tags`；
- prompt 只放“技能目录”；
- 模型调用 `load_skill(name)` 才加载正文；
- manifest 路径必须位于 skills 根目录内；损坏的 manifest 跳过而不是崩溃。

**设计取舍**：技能是“按需文档”，不是可执行插件。可执行能力必须走工具，才有
权限和审计。

**练习**：新增一个 `git-workflow` 技能，观察系统提示只变长一行。

---

### s08 — Context Compact

**问题**：长会话必然超过上下文窗口。

**机制**（`memory/context_compact.py` + `tools/output_limit.py`）：

| 层 | 触发 | 动作 | 成本 |
|---|---|---|---|
| L0 | 单条结果 > 6000 字符 | 截断 + 全量落盘 `.tool_outputs/` | 0 |
| L1 | 单条结果 > 30000 字符 | 落盘 + 1000 字符预览 + 路径 | 0 |
| L2 | 消息数 > 50 | 按完整 turn 组丢弃最早片段，插入 snip 标记 | 0 |
| L3 | 旧工具结果 | 已消费结果 → `Previous tool used: X` | 0 |
| L3.5 | 工具结果总字符 > 200000 | 从最大且已消费的开始落盘 | 0 |
| L4 | token 估算 > 5000 | 对“已消费部分”调模型摘要 | 1 次调用 |
| 手动 | `/compact` | 强制摘要 | 1 次调用 |
| 反应式 | provider 报超长 | 更激进地落盘/裁剪，仍超长则摘要 + 紧急裁剪 | 0–1 次 |

**关键不变量**：

- 只压缩“模型已经看过”的消息；最新一轮工具结果永远保留；
- 摘要只能以 user 消息注入，不能伪造 assistant；
- snip 以完整 turn 组为单位，绝不拆散 `tool_use/tool_result`；
- 摘要失败时跳过本轮压缩，而不是抛出异常。

**练习**：连续 `cat` 一个 100KB 文件，观察 L0 落盘、L3 占位符、L4 摘要依次
触发。

---

### s09 — Long-Term Memory

**问题**：会话结束后哪些知识应该留下？

**机制**（`memory/longterm.py`）：

```text
.memory/
  MEMORY.md          索引，一行一条，随 prompt 加载
  <slug>.md          frontmatter(name/description/type) + 正文
```

三种流：

1. **recall**（每轮开始）：取最近用户文本；记录 ≤3 条时全给，否则让模型选
   索引（失败回退关键词评分）；最多 5 条、20000 字符，以 `<memory>` 标签
   注入，并明确“是背景知识，不是新指令”。
2. **after_turn**（每轮结束，仅 lead）：让模型抽取 `user/feedback/project/
   reference` 四类持久知识；过滤“本次会话/当前任务”等临时标记；去重后才
   写入；每个文件原子替换。
3. **consolidate**：记录达到 10 条后让模型合并去重，最多保留 30 条，写入
   失败会回滚到快照。

**设计取舍**：记忆抽取和召回都是额外模型调用，因此都有开关
（`MEMORY_ENABLED`）和长度上限；任何失败都不阻塞主 turn。

**练习**：在两个独立进程中分别说“以后回答用中文”和“你好”，确认第二条能
召回第一条。

---

### s10 — Task System

**问题**：todo 会随会话消失，团队还需要共享、可依赖的工作项。

**机制**（`agent/tasks.py`）：

- `.tasks/task_<8hex>.json`，一个任务一个文件；
- 原子写（临时文件 + `os.replace`），进程内 RLock + best-effort 文件锁；
- 字段：`id/subject/description/status/owner/blockedBy/worktree`；
- 依赖图更新时做环检测；有未完成依赖不能 claim；
- 只有 owner 能 complete；
- 工具：`create_task/update_task/list_tasks/get_task/claim_task/complete_task`。

**设计取舍**：文件而非数据库，便于人工检查、Git 忽略和跨进程恢复；状态转换
在锁内完成，避免队友线程与 lead 同时 claim。

**练习**：建 A、B 两个任务，令 B 依赖 A，尝试先 claim B，再完成 A 后 claim B。

---

### s11 — Background Bash

**问题**：构建、测试、服务器等长命令会卡住当前 turn。

**机制**（`agent/background.py`）：

- `bash(run_in_background=true)` 立即返回 `bg_0001`；
- daemon 线程运行命令，超时 300 秒；
- 完成后写入队列：
  `<task_notification><task_id>...<status>...<summary>...</task_notification>`；
- runtime 在下一轮开头 drain 队列，以 user 消息注入；
- 协调器在检测到待处理通知时主动唤醒 runtime；
- 进程清理复用 `terminate_process_tree`（Windows `taskkill /T /F`，POSIX
  `killpg`）。

**设计取舍**：通知是“下一轮上下文”，不是实时中断；这也意味着模型不会在命令
执行到一半时被重新调用。goal 循环在后台任务运行时 defer，避免过早判定。

**练习**：后台执行 `sleep 5`，立即结束当前回答，再输入任意消息，观察通知。

---

### s12 — Cron

**问题**：如何让 agent 在未来的时间点自行开启 turn？

**机制**（`agent/cron.py`）：

- 标准 5 字段 cron（分 时 日 月 周）；
- `.scheduled_tasks.json` 持久化 durable 任务；
- 调度线程只做 `poll_due_jobs()`：到期任务标记 `pending_delivery`、按分钟
  去重、写入队列；
- 协调器在 turn 锁内 `consume_queue()`，执行后 `acknowledge`；
- 一次性任务成功后删除；失败时 `restore`；
- 崩溃恢复：`pending_delivery` 的任务重启后仍会入队。

**设计取舍**：调度线程绝不直接调用模型，避免与用户 turn 交错；ack 成功才
算交付，保证至少一次语义。

**练习**：创建一个每分钟触发的一次性任务，观察 `/crons` 状态与触发后的清理。

---

### s13 — Teams + Worktree

**问题**：单 agent 难以并行处理相互独立的大块工作；并行写同一目录又会冲突。

**机制**：

- `.mailboxes/<agent>.jsonl`：追加写、读取即消费的文件邮箱；
- `TeamManager`：队友记录、状态、协议请求；
- `TeammateRuntime`：每个队友一个线程、一份历史、一个 cwd；
- 队友空闲时自动 `claim_next_ready()` 领取任务；
- 计划协议：`submit_plan → plan_approval_request → review_plan →
  plan_approval_response`；每次请求有 request id、版本号和 task id，过期
  响应直接忽略；
- 门禁：`gate_check` 在 `pending/required` 时阻断 `bash/write_file/edit_file`；
- shutdown 请求/确认协议；
- worktree：`.worktrees/<name>` + 分支 `wt/<name>`，只能绑定 pending 且
  无 owner 的任务；删除时拒绝活跃任务、正在使用的队友和未提交改动，且
  始终保留分支。

**Windows 说明**：全程使用 `git worktree` 子命令和普通路径，不依赖符号链接；
git 缺失时明确报错，不做“降级删除”。

**设计取舍**：队友共享同一进程和注册表，但不共享历史；通信走可检查的文件，
便于调试和崩溃后查看。队员崩溃会向 lead 发送 error 并释放未完成任务。

**练习**：建两个任务与一个 worktree，让队友在 worktree 中改文件，确认主
工作区不受影响。

---

### s14 — MCP

**问题**：如何接入外部工具生态而不把每个集成写死在代码里？

**机制**（`agent/mcp.py`）：

- 内置 mock 服务器 `docs`（`search/get_version`）与 `deploy`
  （`trigger/status`），用于零依赖演示与测试；
- `.mcp.json` 配置 stdio 服务器：

```json
{
  "mcpServers": {
    "my-tools": {
      "command": "python",
      "args": ["server.py"],
      "env": {"TOKEN": "..."},
      "cwd": "."
    }
  }
}
```

- `StdioMCPClient`：行分隔 JSON-RPC；
  `initialize → notifications/initialized → tools/list（分页）→ tools/call`；
  读线程 + 事件 + 超时；stderr 独立消费；
- 工具名规范化为 `mcp__<server>__<tool>`；
- 注册前检查：服务器名冲突、与内置工具冲突、名称 ≤64 字符、schema 必须是
  object；任一失败则回滚已注册的工具；
- 授权由宿主决定：`MCP_HOST_POLICY` 中 `allow` 的工具直接放行，其余需要
  用户确认；**绝不信任服务器自带的 readOnly 注解**；
- 关闭时 terminate 整个进程树并唤醒所有等待请求。

**设计取舍**：MCP 工具直接进入同一个注册表，因此 system prompt、权限钩子和
队友工具池自动生效，不需要在主循环里增加分支。

**练习**：连接 `deploy`，调用 `status`（放行）和 `trigger`（需确认），观察
策略差异。

---

### s15 — Integrated Harness

**问题**：前面的机制各自能跑，但事件源、状态和工具池如何统一？

**机制**（`agent/coordinator.py` + `agent/prompts.py`）：

- 所有输入源串行进入同一份 lead 历史：用户、cron、团队事件、后台唤醒；
- 每轮调用模型前重建 system prompt，包含：
  当前 profile、环境说明、工作目录、工作准则、上下文说明、session todo、
  skill 目录、memory 目录与召回内容、任务板、团队状态、MCP 状态、
  workflow 目录、goal 状态、当前时间；
- 工具池每次调用时由 `registry.subset(profile)` 生成，MCP 动态注册立即可见；
- CLI 提供 `/tasks`、`/teammates`、`/crons`、`/mcp`、`/workflows`、`/goal`、
  `/memory`、`/compact`，并且状态变更命令与 turn 共用锁。

**设计取舍**：prompt 是“每轮快照”，不是启动时字符串。代价是每轮少量字符串
拼接，收益是状态永远最新。

**练习**：连接 MCP 后不重启，直接问“你有哪些外部工具”，确认 prompt 已更新。

---

### s16 — Workflow

**问题**：有些流程的**形状**是固定的（审计 → 并行验证），让模型每轮重新
规划既慢又不稳定。

**机制**（`agent/workflow.py`）：

- 工作流是宿主注册的 Python 函数，模型只能选择 name 和 JSON args；
- `ExecutionState` 暴露 `phase/log/agent/parallel/pipeline/workflow`；
- `agent(prompt, schema)` 带 schema 校验与一次重试；
- journal：`<runId>.journal.jsonl`，每个步骤以
  `kind|label|prompt|schema` 的 SHA-256 稳定键缓存；
- 续跑：`resume_from_run_id` 必须匹配原 workflow 名和 args；已完成步骤直接
  回放，不重复付费；
- 预算：`args.budget` 是总 token 上限；还有全局 agent 调用上限（1000）与
  并发信号量（默认 8）；
- 产物：`.runtime/workflows/<runId>.json`（快照）、`.output.json`（结果）、
  `.journal.jsonl`（步骤日志）、`last_run.txt`；
- 示例 `review-changes`：4 个维度流水线审计，每条发现再并行对抗验证。

**设计取舍**：workflow 是同步实现，`parallel/pipeline` 用短生命周期线程池；
嵌套只允许一层，避免不可控的 fan-out。失败结果是 JSON 而不是异常，调用方
拿到 `runId` 后可以决定是否续跑。

**练习**：把 `review-changes` 跑到一半（fake runner 抛错），再用返回的
`runId` 续跑，确认已完成的审计步骤命中 journal。

---

### s17 — Goal Loop

**问题**：用户说“把测试全部修好”，模型可能在没完成时提前停下。

**机制**（`agent/goal.py`）：

- `/goal <条件>` 设置会话目标；`/goal clear` 清除；
- 当模型产生**无工具调用**的回合且 goal 激活时，调用独立的、无工具的
  evaluator 评估整段 transcript；
- evaluator 只返回 `{"ok": bool, "reason": str, "impossible": bool}`；
- 决策：`continue` 注入 user 续跑消息；`achieved` 结束；`failed`
  （impossible）结束；`error` 结束；`defer`（后台仍在运行或 evaluator
  返回 defer）先不判定；`limit` 达到连续阻断上限后交还控制权；
- 默认 `GOAL_BLOCK_CAP=8`，每次新的用户 turn 重置连续计数；
- transcript 24K 字符上限，只裁剪超长的最新消息；
- evaluator 异常绝不让循环失控：记录原因并返回。

**设计取舍**：evaluator 与执行者分离，减少“自我表扬”；goal 不是无限循环，
block cap、agent max turns 和后台 defer 共同兜底。

**练习**：设置一个当前无法完成的 goal，观察 evaluator 连续阻断后达到上限并
返回 `[goal limit: ...]`。

---

## 5. 一次 turn 的完整时序

```text
用户输入
  │
  ▼
main.py → coordinator.run_user_input
  │  with turn_lock:
  ▼
AgentRuntime.run_turn
  │  hooks.trigger(UserPromptSubmit) 可前置注入
  │  history += user
  │  memory.recall(history) → 注入到 prompt 的 Memory 段
  ▼
┌─────────────── _run loop ───────────────┐
│ 每轮：                                    │
│  1. 检查 max_turns                        │
│  2. background.drain_notifications()      │
│     → history += user(task_notification)  │
│  3. todo 提醒（≥3 轮未更新）              │
│  4. context_manager.prepare(history)      │
│  5. build_system_prompt(runtime)          │
│     registry.subset(profile).tools        │
│  6. call_llm（重试、usage、超长识别）      │
│     超长 → reactive_compact 一次 → 重试   │
│  7. history += assistant（原样）           │
│  8. 有 tool_use?                          │
│     ├─ 是：for each block                │
│     │     PreToolUse（权限/门禁）          │
│     │     handler 或 后台启动              │
│     │     PostToolUse（可替换输出）        │
│     │     cap_output                      │
│     │   一次 append user(全部 tool_result) │
│     │   继续循环                          │
│     └─ 否：Stop hooks                     │
│            → goal.evaluate_after_turn     │
│            → memory.after_turn            │
│            → 返回最终文本                  │
└───────────────────────────────────────────┘
  │
  ▼
coordinator 打印结果
```

事件源进入同一循环：

```text
cron thread ──poll_due_jobs──▶ queue ──┐
teammate threads ──mailboxes──▶ inbox ─┤
background threads ──notifications─────┤
                                      ▼
                    coordinator.pump_once（同一把 turn 锁）
                    ├─ cron 到期：run_turn(prompts, source="cron")
                    ├─ 团队事件：run_turn(event text, source="team")
                    └─ 后台通知：run(source="background")
```

注意：后台通知在 runtime 循环开头 drain；协调器只负责在有空闲时唤醒它，
因此不会在模型调用中途插入消息。

---

## 6. 持久化布局

所有运行时目录默认位于 `AGENT_WORKDIR`（默认当前目录）：

| 路径 | 内容 | 由谁写 |
|---|---|---|
| `.memory/` | `MEMORY.md` 索引 + `<slug>.md` 记忆 | `LongTermMemory` |
| `.tasks/` | `task_<8hex>.json` + `.lock` | `TaskStore` |
| `.mailboxes/` | `<agent>.jsonl` 文件邮箱 | `MessageBus` |
| `.worktrees/` | 任务绑定的 Git worktree | `WorktreeManager` |
| `.runtime/workflows/` | workflow 快照、journal、输出、last_run | `WorkflowManager` |
| `.scheduled_tasks.json` | durable cron 任务 | `CronScheduler` |
| `.transcripts/` | 压缩前完整历史 JSONL | `ContextManager` |
| `.tool_outputs/` | 超长工具结果/压缩溢写 | `cap_output` / `ContextManager` |
| `skills/` | `SKILL.md` 技能清单 | 用户维护 |
| `.mcp.json` | stdio MCP 服务器配置 | 用户维护 |

`.env`、`.transcripts/`、`.tool_outputs/`、`.idea/`、`__pycache__/` 已在
`.gitignore` 中。`.memory/` 是否入库由用户决定；学习项目通常建议保留示例，
但不要提交包含隐私的记忆。

---

## 7. 并发模型与 Windows 适配

### 7.1 锁矩阵

| 资源 | 锁 | 说明 |
|---|---|---|
| lead 历史 / turn | `RuntimeCoordinator._turn_lock`（RLock） | 所有 turn 串行 |
| runtime 内部 | `AgentRuntime._lock`（RLock） | 防止同一 runtime 重入 |
| registry | `ToolRegistry._lock`（RLock） | MCP 动态注册 vs 工具池读取 |
| 任务文件 | `TaskStore._lock` + 文件锁 | 多线程/跨进程 best effort |
| workflow run | 进程内 Lock + msvcrt/fcntl | 防止同一 runId 重复执行 |
| cron | `_lock`（RLock） | 队列和 durable 状态 |
| 团队状态 | `TeamManager._lock`（RLock） | 记录、协议、认领 |
| MCP | `MCPManager._lock`（RLock） | 连接、注册、关闭 |
| 后台任务 | `BackgroundManager._lock`（RLock） | 任务表与通知队列 |

### 7.2 Windows 关键处理

- **没有 `fcntl` / `select` / `killpg`**：跨进程锁优先 `msvcrt.locking`，
  POSIX 用 `fcntl`，两者都是 best effort；线程锁才是权威。
- **进程树清理**：`tools/bash.py:terminate_process_tree` 在 Windows 使用
  `taskkill /F /T /PID`，POSIX 使用 `killpg` 并升级到 SIGKILL。
- **原子写**：所有持久 JSON 先写同目录临时文件，再 `os.replace`。
- **路径**：全部使用 `pathlib`，文件工具用 `resolve().is_relative_to(root)`
  防逃逸；worktree 使用 `git` argv 数组而不是 shell 字符串。
- **退出顺序**：`coordinator.stop()`：
  `stop event → join pump → stop cron → teams.stop_all → mcp.close_all`。
  子进程和线程都是 daemon，但会先尝试优雅关闭。
- **Git Bash**：`bash` 工具使用系统 shell（Windows 上可能是 cmd.exe 或
  Git Bash）；命令通过 `subprocess(shell=True)` 执行，输出按 UTF-8 解码，
  非法字节以替换字符处理。

### 7.3 权限与线程

`PermissionPolicy._can_ask()` 要求：

1. `ctx.interactive` 为真；
2. 当前线程是主线程。

因此后台线程、cron 触发的 turn、队友线程永远不会弹权限询问；没有会话授权
时一律拒绝。这样避免“无人值守时卡在 input()”。

---

## 8. DeepSeek thinking 兼容说明

DeepSeek 的 Anthropic 兼容端点在使用 thinking 模式时要求：历史中的 assistant
消息必须原样包含它返回的 thinking 块，否则请求会被 400 拒绝。本项目的硬约束：

1. **assistant 内容原样追加**：

   ```python
   self.history.append({"role": "assistant", "content": result.response.content})
   ```

   绝不重建、筛选或重新序列化该列表。

2. **所有注入使用 user 消息**：todo 提醒、后台通知、团队事件、cron 提示、
   压缩摘要、goal 续跑消息全部是 `role: "user"`。

3. **不伪造 assistant 消息**：压缩摘要不会附加“Understood”之类的假回复。
   历史里出现一条没有 thinking 块的 assistant 消息，DeepSeek 可能直接拒绝。

4. **压缩不触碰 assistant**：L1–L3 只修改 user 消息里的 `tool_result`；
   L4 把旧历史整体替换为一条 user 摘要 + 保留的尾部。

5. **工具结果严格配对**：权限拒绝、handler 异常、后台启动、MCP 报错都返回
   `tool_result`；一次 assistant 的所有 `tool_use` 在下一轮都有对应结果。

6. **超长处理走反应式压缩**：`PromptTooLongError` 不重试原请求，而是先压
   一次再重试；每轮最多一次，避免无限循环。

如果接入原生 Anthropic 或其他兼容端点，这些约束仍然成立，只是 thinking
块的格式可能不同；代码不需要改。

---

## 9. 测试策略

测试全部使用 fake client / fake runner / fake evaluator / 假 MCP stdio
server，**不会发起真实 API 调用**。

| 测试文件 | 覆盖点 |
|---|---|
| `test_registry.py` | ToolSpec、profile 子集、dispatch 异常、动态注册 |
| `test_permissions_hooks.py` | 硬拒、沙箱、allowlist、fail-closed、hook 阻断/替换 |
| `test_runtime_loop.py` | tool_use/result 配对、thinking 原样、未知工具、Stop hook |
| `test_safe_path.py` | 路径逃逸与符号链接边界 |
| `test_skill_loader.py` | 技能扫描、frontmatter、路径安全 |
| `test_context_compact.py` | 旧实现兼容行为 |
| `test_context_pipeline.py` | L1–L4、turn 分组、预算、reactive/manual 压缩 |
| `test_memory_longterm.py` | 抽取过滤、去重、召回、合并、原子写 |
| `test_todo.py` | 校验、in_progress 唯一、提醒节奏 |
| `test_tasks.py` | 依赖、环检测、claim/complete 所有权、worktree 绑定 |
| `test_background.py` | 后台启动、通知、超时清理 |
| `test_cron.py` | 5 字段解析、按分钟去重、ack/restore、持久化 |
| `test_teams.py` | 邮箱、计划/关闭协议、任务声明、门禁 |
| `test_worktree.py` | 创建/删除保护、路径逃逸、git 缺失降级 |
| `test_mcp.py` | mock、策略、命名冲突、stdio 往返、超时清理 |
| `test_workflow.py` | 注册校验、journal 续跑、schema 重试、预算、并行 |
| `test_goal.py` | evaluator 解析、continue/achieved/limit/error/defer |
| `test_coordinator.py` | turn 锁、cron/团队/后台事件泵 |
| `test_integration.py` | MCP 动态工具、任务+todo 跨 turn、workflow 工具、动态 prompt |

运行：

```bash
python -m pytest -q
python -m compileall -q .
```

当前基线：**139 个测试全部通过**（Python 3.14 / Windows 11）。

---

## 10. 扩展指南与故障排查

### 10.1 新增工具

1. 在 `tools/builtin.py` 写 handler：`def my_tool(args, ctx) -> str`；
2. 注册 `ToolSpec(name=..., input_schema=..., handler=..., profiles=...)`；
3. 如涉及高影响操作，在 `PermissionPolicy.HIGH_IMPACT_TOOLS` 中加入名称；
4. 写测试：成功、错误、profile 可见性。

### 10.2 新增 hook

```python
runtime.hooks.register("PreToolUse", my_hook)
```

返回字符串即阻断。注意 hook 在每轮工具执行时同步调用，不要做长阻塞操作。

### 10.3 新增技能

创建 `skills/<name>/SKILL.md`：

```markdown
---
name: my-skill
description: 一句话说明何时使用
tags: tag1, tag2
---

# 正文
...
```

`SkillLoader` 启动时扫描；新增文件后重启或重新实例化。

### 10.4 新增 workflow

```python
def my_flow(ctx, args):
    ctx.phase("Plan")
    plan = ctx.agent("Make a plan", label="plan")
    ctx.phase("Execute")
    results = ctx.parallel([
        lambda: ctx.agent(f"Execute: {plan}", label=f"step-{i}")
        for i in range(3)
    ])
    return {"plan": plan, "results": results}

manager.register("my-flow", "What it does", my_flow, phases=["Plan", "Execute"])
```

遵守约束：步骤尽量使用稳定 label/prompt（journal 键依赖它们）；不要在
workflow 里直接写文件或发网络请求，副作用应通过 agent 工具完成。

### 10.5 接入 MCP 服务器

```json
{
  "mcpServers": {
    "files": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]}
  }
}
```

放行策略在 `agent/mcp.py:MCP_HOST_POLICY` 中维护（白名单工具名），其余工具
需要交互确认。stdio 服务器必须在超时内响应，退出时会被清理进程树。

### 10.6 常见故障

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 400 thinking block 错误 | 历史中的 assistant 被重建/伪造 | 检查扩展是否修改了 assistant 消息 |
| 400 孤立 tool_result | 有 tool_use 没产生结果，或压缩拆散了组 | 确保每个 block 都返回结果；检查 snip 是否按 turn 组 |
| 权限一直拒绝 | 非主线程/非交互 + 未授权 | 用 `/goal`/`/workflow` 时的交互限制；改为 `PERMISSION_MODE=auto` 仅用于测试 |
| MCP connect 超时 | 服务器未响应 initialize/tools/list | 检查命令、args、cwd、env；适当提高 `timeout` |
| cron 到点不执行 | coordinator 未启动或 pump 被阻塞 | 检查 `RuntimeCoordinator.start()`；看 `/crons` 是否 pending |
| workflow 续跑报错 | 名称/args 不一致或 journal 损坏 | 用原始 args；损坏 journal 会被拒绝 |
| goal 停不下来 | block cap 设置过大或 evaluator 持续返回 continue | 调小 `GOAL_BLOCK_CAP`；检查 evaluator 输出 |
| worktree 删除失败 | 任务未完成或有未提交改动 | 先完成/释放任务；手动检查 `git status`；必要时显式 `discard_changes` |
| 队友一直等待 | 没有 ready 任务/未批准计划 | `/teammates` 查看状态；`review_plan`；创建任务 |
| 会话上下文暴涨 | 单个工具输出过大且未落盘 | 检查 L0/L1 配置；用 `read_file` 分页，不要 `cat` 大文件 |

### 10.7 安全边界

- `bash` 的 shell=True 意味着命令字符串交给系统 shell；安全命令 allowlist
  只对**无元字符**的简单命令生效。
- 路径沙箱防止文件工具逃逸，但不能阻止 `bash` 通过任意程序访问系统。
- 环境变量（包括密钥）会传给 MCP stdio 子进程；只连接可信服务器。
- 生产环境应使用容器/低权限用户/网络隔离，不能把本项目的权限层当作完整
  安全沙箱。

---

## 11. 十七课验收清单

- [ ] **s01**：模型连续两次工具调用，历史中 assistant 原样保留、每次调用
      恰好一个 tool_result。
- [ ] **s02**：只改 `builtin.py` 就能新增工具，子代理看不到 lead 专属工具。
- [ ] **s03**：`rm -rf /` 被硬拒，`cat` 放行，越界写入被拒，非交互时询问
      一律 fail closed。
- [ ] **s04**：PreToolUse 返回字符串可阻断；Stop hook 能追加一轮。
- [ ] **s05**：todo 最多一个 in_progress；3 轮不更新出现提醒。
- [ ] **s06**：子代理有独立历史，父历史只多最终摘要。
- [ ] **s07**：系统提示只有技能目录，`load_skill` 返回正文。
- [ ] **s08**：超长输出依次触发落盘、占位符、摘要；压缩后历史仍可被 API
      接受。
- [ ] **s09**：跨会话召回有效；临时任务信息不被写入。
- [ ] **s10**：依赖阻止 claim；完成 A 后 B 可 claim；重启后任务仍在。
- [ ] **s11**：后台任务完成通知在下一轮出现。
- [ ] **s12**：cron 按分钟触发；成功后一次性任务消失；失败可恢复。
- [ ] **s13**：队友独立工作；计划审批/关闭协议可用；worktree 隔离且受保护。
- [ ] **s14**：MCP 工具动态出现、可调用、受宿主策略约束、退出无残留进程。
- [ ] **s15**：cron/团队/后台/user 四个来源串行进入同一历史；prompt 每轮更新。
- [ ] **s16**：失败的 workflow 能用 runId 续跑，已完成步骤不重复调用模型。
- [ ] **s17**：未达标时自动续跑，达成/不可能/超限/错误都能交还控制权。

---

## 附录 A：工具清单

### 所有 profile 可见

`bash`、`read_file`、`write_file`、`edit_file`、`glob`、`load_skill`、
`compact`

### lead 专属

`task`（子代理）、`todo_write`、`create_task`、`update_task`、
`schedule_cron`、`list_crons`、`cancel_cron`、`spawn_teammate`、
`list_teammates`、`request_shutdown`、`request_plan`、`review_plan`、
`create_worktree`、`remove_worktree`、`connect_mcp`、`run_workflow`

### lead + teammate

`list_tasks`、`get_task`、`claim_task`、`complete_task`、`send_message`、
`list_mcp_tools`、`list_workflows`

### teammate 专属

`submit_plan`

> MCP 工具在连接后动态加入 `lead` 和 `teammate` 工具池，不在上表中。

---

## 附录 B：配置项

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MODEL_ID` | `deepseek-chat` | 模型 ID |
| `ANTHROPIC_BASE_URL` | 空 | Anthropic 兼容端点；设置后移除 auth token |
| `ANTHROPIC_API_KEY` | 空 | API 密钥 |
| `AGENT_WORKDIR` | 当前目录 | 工作区根 |
| `MAX_TOKENS` | 8000 | 单次回复上限 |
| `MAX_AGENT_TURNS` | 50 | lead 单 turn 最大循环轮数 |
| `SUBAGENT_MAX_TURNS` | 30 | 子代理/队友上限 |
| `LLM_RETRIES` | 2 | 可重试错误的次数 |
| `KEEP_RECENT_TOOL_RESULTS` | 3 | 微压缩保留的最近结果数 |
| `CONTEXT_TOKEN_THRESHOLD` | 5000 | L4 摘要触发阈值（估算 token） |
| `CONTEXT_CHAR_LIMIT` | 200000 | 工具结果总字符预算 |
| `SINGLE_TOOL_RESULT_LIMIT` | 6000 | L0 单条截断字符数 |
| `TOOL_RESULT_PERSIST_LIMIT` | 30000 | L1 落盘阈值 |
| `GOAL_BLOCK_CAP` | 8 | goal 连续阻断上限 |
| `PERMISSION_MODE` | `allowlist` | `allowlist` / `strict` / `auto` |
| `MEMORY_ENABLED` | 1 | 长期记忆 |
| `CRON_ENABLED` | 1 | 定时任务 |
| `BACKGROUND_ENABLED` | 1 | 后台 bash |
| `TEAMS_ENABLED` | 1 | 队友与 worktree |
| `MCP_ENABLED` | 1 | MCP |
| `WORKFLOW_ENABLED` | 1 | 工作流 |
| `LANGSMITH_TRACING` | 空 | 可选追踪 |
| `LANGSMITH_API_KEY` | 空 | 可选追踪密钥 |
| `LANGSMITH_PROJECT` | `agent-learning` | 追踪项目名 |

---

## 参考与署名

- 课程结构参考：`shareAI-lab/learn-claude-code`（MIT License）。
- 本项目代码为适配 Python 3.14、Windows 11、DeepSeek Anthropic 兼容端点
  的重新实现，并非逐行复制。
- 原始博客 `https://beatai.org/learn-ai/learn-claude-code/` 在写作时已无法
  访问，本文以开源仓库为基准。
