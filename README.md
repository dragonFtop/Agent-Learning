# Coding Agent（模块化版本）

这是一个基于 Anthropic SDK 的命令行 Coding Agent。  
当前版本已从单文件重构为模块化结构，支持通过工具调用来执行命令和读写文件。

## 当前功能

- 交互式对话循环（CLI）
- 基于模型的工具调用（tool use）
- 内置 4 个工具：
  - `bash`：执行 shell 命令（带基础危险命令拦截）
  - `read_file`：读取文件内容（支持行数限制）
  - `write_file`：写入文件（自动创建父目录）
  - `edit_file`：基于字符串替换编辑文件（单次替换）
- 文件路径沙箱限制：默认只允许操作当前工作目录内的路径

## 项目结构

```text
your_project/
├── main.py
├── config.py
├── tools/
│   ├── __init__.py
│   ├── bash.py
│   ├── file_ops.py
│   └── registry.py
├── agent/
│   ├── __init__.py
│   └── loop.py
└── .env
```

## 模块职责

- `main.py`：程序入口，负责命令行交互与会话历史维护
- `config.py`：环境变量加载、模型与客户端初始化、系统提示词配置
- `agent/loop.py`：核心 agent 循环，处理模型响应与工具执行结果回传
- `tools/bash.py`：命令执行工具
- `tools/file_ops.py`：文件读写编辑与路径安全校验
- `tools/registry.py`：工具描述（schema）与处理函数注册

## 环境准备

建议使用 Python 3.10+。

安装依赖：

```bash
pip install anthropic python-dotenv
```

在项目根目录创建 `.env`（按你的实际服务配置填写）：

```env
# 必填：模型 ID
MODEL_ID=your-model-id

# 可选：兼容 API 网关地址（例如代理或第三方兼容端点）
ANTHROPIC_BASE_URL=https://your-base-url

# 常见鉴权变量（按你接入方式二选一或同时配置）
ANTHROPIC_API_KEY=your-api-key
ANTHROPIC_AUTH_TOKEN=your-auth-token
```

说明：

- 当设置了 `ANTHROPIC_BASE_URL` 时，程序会移除 `ANTHROPIC_AUTH_TOKEN` 以避免冲突。
- 实际需要的鉴权变量取决于你接入的服务端实现。

## 启动方式

在项目根目录执行：

```bash
python main.py
```

交互退出方式：

- 输入 `q`
- 输入 `exit`
- 直接回车
- 或使用 `Ctrl+C` / `Ctrl+D`

## 运行流程（简述）

1. 用户输入问题
2. `agent_loop` 调用模型并携带工具定义
3. 若模型返回 `tool_use`，按工具名分发到本地处理函数
4. 将 `tool_result` 回传模型继续推理
5. 非 `tool_use` 时结束本轮

## 已知限制

- `bash` 只做了简单关键字拦截，不等同于完整安全沙箱
- `edit_file` 为字符串匹配替换，不支持结构化 AST 级编辑
- 默认工作目录取 `Path.cwd()`，请确保从项目根目录启动
- 当前没有测试与日志分级，适合学习和小规模原型

## 后续可扩展方向

- 增加 `requirements.txt` 或 `pyproject.toml`
- 增加单元测试（尤其是 `safe_path` 与工具分发）
- 增加更细粒度权限控制（例如命令白名单）
- 增加流式输出和更完善的错误处理

