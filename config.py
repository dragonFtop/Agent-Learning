import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv


load_dotenv(override=True)

# 加载配置中的 ANTHROPIC_BASE_URL 后，删除 ANTHROPIC_AUTH_TOKEN 以避免冲突
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
MODEL = os.environ["MODEL_ID"]
SYSTEM = f"You are a coding agent at {WORKDIR}. Use bash to solve tasks. Act, don't explain."

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
