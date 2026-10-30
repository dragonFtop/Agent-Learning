"""Central configuration: environment, model client, paths and limits.

Everything that is process-wide lives here. Per-agent state (history, cwd,
tool pool) lives in ``agent.runtime.AgentRuntime`` so subagents and teammates
can run with independent state in the same process.
"""

import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from langsmith.wrappers import wrap_anthropic

load_dotenv(override=True)

# DeepSeek's Anthropic-compatible endpoint expects ANTHROPIC_API_KEY. When a
# custom base URL is used, drop the legacy auth token so the SDK does not send
# two competing credentials.
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path(os.getenv("AGENT_WORKDIR", str(Path.cwd()))).resolve()
MODEL = os.getenv("MODEL_ID", "deepseek-chat")

# ---- runtime limits -------------------------------------------------------
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "8000"))
MAX_AGENT_TURNS = int(os.getenv("MAX_AGENT_TURNS", "50"))
SUBAGENT_MAX_TURNS = int(os.getenv("SUBAGENT_MAX_TURNS", "30"))
KEEP_RECENT_TOOL_RESULTS = int(os.getenv("KEEP_RECENT_TOOL_RESULTS", "3"))
CONTEXT_TOKEN_THRESHOLD = int(os.getenv("CONTEXT_TOKEN_THRESHOLD", "5000"))
CONTEXT_CHAR_LIMIT = int(os.getenv("CONTEXT_CHAR_LIMIT", "200000"))
SINGLE_TOOL_RESULT_LIMIT = int(os.getenv("SINGLE_TOOL_RESULT_LIMIT", "6000"))
TOOL_RESULT_PERSIST_LIMIT = int(os.getenv("TOOL_RESULT_PERSIST_LIMIT", "30000"))
GOAL_BLOCK_CAP = int(os.getenv("GOAL_BLOCK_CAP", "8"))
LLM_RETRIES = int(os.getenv("LLM_RETRIES", "2"))

# ---- feature switches -----------------------------------------------------
PERMISSION_MODE = os.getenv("PERMISSION_MODE", "allowlist").strip().lower()
MEMORY_ENABLED = os.getenv("MEMORY_ENABLED", "1").lower() not in ("0", "false", "no")
CRON_ENABLED = os.getenv("CRON_ENABLED", "1").lower() not in ("0", "false", "no")
BACKGROUND_ENABLED = os.getenv("BACKGROUND_ENABLED", "1").lower() not in ("0", "false", "no")
TEAMS_ENABLED = os.getenv("TEAMS_ENABLED", "1").lower() not in ("0", "false", "no")
WORKFLOW_ENABLED = os.getenv("WORKFLOW_ENABLED", "1").lower() not in ("0", "false", "no")
MCP_ENABLED = os.getenv("MCP_ENABLED", "1").lower() not in ("0", "false", "no")

# ---- persistent state paths ----------------------------------------------
SKILL_DIR = WORKDIR / "skills"
MEMORY_DIR = WORKDIR / ".memory"
TASKS_DIR = WORKDIR / ".tasks"
MAILBOX_DIR = WORKDIR / ".mailboxes"
WORKTREE_DIR = WORKDIR / ".worktrees"
RUNTIME_DIR = WORKDIR / ".runtime"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_OUTPUT_DIR = WORKDIR / ".tool_outputs"
CRON_FILE = WORKDIR / ".scheduled_tasks.json"
MCP_CONFIG_FILE = WORKDIR / ".mcp.json"

from tools.skill_loader import SkillLoader  # noqa: E402  (after paths exist)

SKILL_LOADER = SkillLoader(SKILL_DIR)

# Legacy prompt constants kept so older scripts keep importing.
SKILL_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Use load_skill to access specialized knowledge before tackling unfamiliar topics.\n"
    f"Skills available:\n{SKILL_LOADER.get_descriptions()}"
)
PARENTAGENT_SYSTEM = f"You are a coding agent. Use tools to solve tasks.\n\n{SKILL_SYSTEM}"
SUBAGENT_SYSTEM = f"You are a focused subagent. Complete the assigned subtask.\n\n{SKILL_SYSTEM}"

# LangSmith tracing is attached to the client itself. ``wrap_anthropic`` is a
# no-op when LANGSMITH_TRACING is not enabled.
client = wrap_anthropic(Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL") or None))


def ensure_runtime_dirs() -> None:
    """Create directories used by persistent harness features."""
    for path in (
        MEMORY_DIR,
        TASKS_DIR,
        MAILBOX_DIR,
        WORKTREE_DIR,
        RUNTIME_DIR,
        TRANSCRIPT_DIR,
        TOOL_OUTPUT_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
