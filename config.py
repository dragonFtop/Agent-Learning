import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

from tools import SkillLoader

load_dotenv(override=True)

# 加载配置中的 ANTHROPIC_BASE_URL 后，删除 ANTHROPIC_AUTH_TOKEN 以避免冲突
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
MODEL = os.environ["MODEL_ID"]
PARENTAGENT_SYSTEM = f"You are a parent agent. Talk with the user or command subagent use tools to solve tasks."
SUBAGENT_SYSTEM = f"You are a subagent. help the parentagent to solve tasks."

SKILL_DIR = WORKDIR / "skills"
SKILL_LOADER = SkillLoader(SKILL_DIR)
SKILL_SYSTEM = f"""You are a coding agent at {WORKDIR}.Use load_skill to access specialized knowledge before tackling unfamiliar topics.
Skills available:
{SKILL_LOADER.get_descriptions()}"""

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
