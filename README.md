# Claude Code 式 Agent Harness
一个不依赖 LangChain / LlamaIndex 等框架、直接用 Python + Anthropic SDK 手写的
命令行 Coding Agent。项目把 Claude Code 的核心机制拆成 17 个可独立理解的模块，
再合并进同一个可运行的 CLI：主循环、工具注册、权限、Hooks、Todo、子代理、Skill、
分层上下文压缩、长期记忆、持久任务、后台任务、Cron、Agent 团队、MCP、工作流、
目标闭环。

完整机制讲解见 **[docs/CLAUDE_CODE_HARNESS.md](docs/CLAUDE_CODE_HARNESS.md)**。
该文档以开源项目 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)
（MIT）的 s01–s17 课程结构为复现基准，并针对本仓库的 Python 3.14 / Windows 11 /
DeepSeek 兼容端点架构做了重写。

## 功能概览

- **Agent Loop**：模型 → `tool_use` → 本地执行 → `tool_result` 回传，直到模型停止
- **工具注册表**：`ToolSpec` 描述 schema + handler + 可见 profile，新增工具不改主循环
- **权限策略**：硬拒规则 → 路径沙箱 → 安全命令白名单 → 交互确认；非主线程 fail-closed
- **Hooks**：`UserPromptSubmit / PreToolUse / PostToolUse / Stop` 四类事件
- **Todo**：会话级任务清单，单一 `in_progress`，长时间未更新会提醒
- **子代理**：独立上下文与工具子集，最多 30 轮，只把最终报告返回主 Agent
- **Skill**：系统提示只放技能目录，`load_skill` 按需加载 `skills/*/SKILL.md` 全文
- **上下文压缩**：L0 单条截断 → L1 大结果落盘 → L2 整轮裁剪 → L3 微压缩 →
  L4 模型摘要；另有超长请求的 reactive 压缩与 `/compact` 手动压缩
- **长期记忆**：`.memory/*.md` + `MEMORY.md` 索引，回合结束抽取、相关记忆召回
- **持久任务板**：`.tasks/task_<8hex>.json`，支持依赖图、环检测、claim / complete
- **后台任务**：`bash(run_in_background=true)`，完成通知在后续 turn 注入
- **Cron**：5 字段本地时间表达式，持久化到 `.scheduled_tasks.json`
- **Agent 团队**：文件邮箱 + 线程队友 + 任务认领 + plan approval / shutdown 协议 +
  Git worktree 隔离
- **MCP**：内置 mock 服务器（`docs` / `deploy`）与可选 stdio JSON-RPC 服务器；
  工具以 `mcp__<server>__<tool>` 动态注册
- **工作流**：宿主注册的受信编排代码，模型只能选择 `name` + `args`；JSONL journal
  支持失败后续跑
- **目标闭环**：`/goal <条件>` 后由独立 evaluator 判定是否达成，未达成自动续跑

## 环境要求

- Python 3.10+（开发与验证环境为 Python 3.14）
- Windows 11 / Linux / macOS
- 支持 Anthropic Messages API 的模型服务；推荐 DeepSeek Anthropic 兼容端点

## 快速开始

```bash
pip install -r requirements.txt

# 复制配置模板并填写
cp .env.example .env

# 启动
python main.py
```

DeepSeek 的典型 `.env`：

```env
MODEL_ID=deepseek-chat
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_API_KEY=your-api-key
```

`config.py` 检测到 `ANTHROPIC_BASE_URL` 时会移除 `ANTHROPIC_AUTH_TOKEN`，
避免 SDK 同时发送两套凭据。使用 LiteLLM 网关接 OpenAI 协议模型时，可参考
`scripts/litellm_config.yaml` 与 `scripts/start_litellm.bat`。

更多配置项（功能开关、上下文阈值、目标 block cap 等）见 `.env.example`。

## 交互命令

| 命令 | 作用 |
|---|---|
| `/help` | 显示命令列表 |
| `/tasks` | 查看持久任务板 |
| `/teammates` | 查看活跃队友 |
| `/crons` | 查看定时任务 |
| `/mcp` | 查看 MCP 服务器、工具与 host policy |
| `/workflows` | 查看已注册工作流与最近一次运行 |
| `/workflow NAME [JSON]` | 直接运行工作流 |
| `/goal [条件\|clear]` | 查看、设置或清除目标闭环 |
| `/memory` | 查看长期记忆目录 |
| `/compact [focus]` | 手动压缩上下文 |
| `/quit`、`q`、`exit` | 退出（空行、`Ctrl+C`、`Ctrl+D` 也可退出） |

## 内置工具

Lead Agent 默认可见（子代理 / 队友按 profile 取子集）：

| 类别 | 工具 |
|---|---|
| 文件与命令 | `bash`（可后台）、`read_file`、`write_file`、`edit_file`、`glob` |
| 技能与上下文 | `load_skill`、`compact` |
| 子代理 | `task` |
| 计划 | `todo_write` |
| 任务板 | `create_task`、`update_task`、`list_tasks`、`get_task`、`claim_task`、`complete_task` |
| 定时 | `schedule_cron`、`list_crons`、`cancel_cron` |
| 团队 | `spawn_teammate`、`list_teammates`、`send_message`、`submit_plan`、`request_shutdown`、`request_plan`、`review_plan` |
| Worktree | `create_worktree`、`remove_worktree` |
| MCP | `connect_mcp`、`list_mcp_tools`，以及运行时发现的 `mcp__<server>__<tool>` |
| 工作流 | `run_workflow`、`list_workflows` |

所有文件工具都以 `ToolContext.cwd` 为沙箱根目录，因此主 Agent、子代理和
worktree 中的队友可以共享同一套工具实现。

## 项目结构

```text
Agent-Learning/
├── main.py                     # REPL、slash 命令、RuntimeCoordinator 启动
├── config.py                   # 环境变量、客户端、路径、限额与功能开关
├── llm.py                      # 统一 LLM 调用：重试、usage、超长识别
├── agent/
│   ├── runtime.py              # 唯一主循环（s01/s02/s15）
│   ├── coordinator.py          # turn 锁 + 事件泵（用户/cron/团队/后台）
│   ├── hooks.py                # HookBus（s04）
│   ├── permissions.py          # 权限策略（s03）
│   ├── prompts.py              # 动态 system prompt（s15）
│   ├── sub_agent.py            # 子代理（s06）
│   ├── todo.py                 # 会话 Todo（s05）
│   ├── tasks.py                # 持久任务板（s10）
│   ├── background.py           # 后台 bash（s11）
│   ├── cron.py                 # Cron 调度（s12）
│   ├── teams.py                # 团队消息/协议（s13）
│   ├── teammate.py             # 队友运行线程（s13）
│   ├── worktree.py             # Git worktree（s13）
│   ├── mcp.py                  # MCP mock + stdio 客户端（s14）
│   ├── workflow.py             # 固定编排 + journal 续跑（s16）
│   ├── goal.py                 # 目标闭环（s17）
│   └── parent_agent.py         # 兼容旧脚本的 agent_loop 包装
├── tools/
│   ├── registry.py             # ToolContext / ToolSpec / ToolRegistry
│   ├── builtin.py              # 内置工具注册
│   ├── bash.py、file_ops.py    # 工具实现
│   ├── output_limit.py         # L0 输出截断
│   ├── frontmatter.py          # SKILL.md frontmatter 解析
│   ├── safety.py               # 危险命令规则
│   └── skill_loader.py         # 技能加载（s07）
├── memory/
│   ├── context_compact.py      # 分层压缩管线（s08）
│   └── longterm.py             # 长期记忆（s09）
├── skills/weather/SKILL.md     # 示例技能
├── tests/                      # pytest，单测不访问真实 API
├── scripts/                    # 手动调试脚本与 LiteLLM 配置
├── docs/CLAUDE_CODE_HARNESS.md # 17 课完整讲解
├── requirements.txt
├── requirements-dev.txt
└── .env.example
```

## 持久化目录

| 路径 | 内容 |
|---|---|
| `.memory/` | 长期记忆 Markdown 与 `MEMORY.md` 索引 |
| `.tasks/` | 持久任务 JSON |
| `.mailboxes/` | 团队消息邮箱 |
| `.worktrees/` | Git worktree |
| `.runtime/workflows/` | 工作流 journal 与快照 |
| `.transcripts/` | 压缩前的完整历史存档 |
| `.tool_outputs/` | 大工具结果溢写文件 |
| `.scheduled_tasks.json` | Cron 任务 |

这些目录均在运行时创建；`.transcripts/`、`.tool_outputs/` 与 `.env` 已加入
`.gitignore`。

## 运行测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

测试使用 fake client / fake evaluator / fake runner，**不发起真实 API 请求**，
覆盖主循环配对约束、权限与 Hooks、压缩管线、记忆、任务、后台、Cron、团队、
MCP、工作流续跑、目标闭环与端到端集成。

静态语法检查：

```bash
python -m compileall -q .
```

## 安全与权限

默认 `PERMISSION_MODE=allowlist`：

1. 命中硬拒规则（如 `rm -rf /`、磁盘格式化）直接拒绝；
2. 文件路径必须位于工作目录内；
3. 只读命令与安全命令自动放行；
4. 写文件、删除、网络请求、未知命令需要交互确认；
5. 非主线程（cron / 队友 / 后台事件触发的 turn）或无交互环境一律 fail-closed。

`PERMISSION_MODE=strict` 会要求确认所有工具调用；`auto` 仅建议在受控环境中使用。
权限检查本身是一个 `PreToolUse` Hook，拒绝也会生成对应的 `tool_result`，
保证 `tool_use` / `tool_result` 严格配对。

## Windows 与 DeepSeek 注意事项

- 不使用 `fcntl` / `select` / `killpg`；文件锁按平台走 `msvcrt.locking` 或
  `fcntl` 的 best-effort 分支，进程内以 `RLock` 为准。
- 后台进程使用独立进程组，清理时走 `taskkill /F /T`（Windows）或进程组信号。
- 持久化写入采用“写临时文件 + `os.replace`”的原子替换。
- DeepSeek thinking 模式要求历史中的 assistant 消息原样回放：运行时只追加模型
  返回的 content，不做改写；摘要、提醒、通知等注入一律使用 user 消息。

## 已知限制

- `bash` 的危险命令拦截与路径沙箱是尽力而为，不等同于操作系统级安全沙箱。
- `edit_file` 是精确字符串替换，不支持 AST 级结构化编辑。
- 记忆抽取、目标评估、工作流都会产生额外 LLM 调用，可用功能开关关闭。
- 团队与 worktree 依赖 Git；无 Git 环境会明确报错而不是静默降级删除。
- Token 估算基于字符数，只用于触发压缩，不代表精确用量。

## 参考与署名

- 课程结构参考：[shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)（MIT）
- 原始博客 `https://beatai.org/learn-ai/learn-claude-code/` 已失效，本仓库以其对应
  开源项目为复现基准
- 本项目代码为适配自身架构的独立实现，仅用于学习与研究
