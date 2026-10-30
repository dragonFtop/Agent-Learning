"""s08 Layer 0: cap a single tool result before it enters the context.

Oversized output is spilled to ``<workspace>/.tool_outputs`` and the model
gets the head plus the file path, so the full text is still recoverable with
read_file instead of re-running the command.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from config import SINGLE_TOOL_RESULT_LIMIT

MAX_CHARS = SINGLE_TOOL_RESULT_LIMIT


def cap_output(
    tool_name: str,
    output: str,
    max_chars: int = MAX_CHARS,
    workdir: str | Path | None = None,
) -> str:
    if len(output) <= max_chars:
        return output

    if workdir is None:
        from config import WORKDIR

        root = WORKDIR
    else:
        root = Path(workdir).resolve()

    spill_dir = root / ".tool_outputs"
    spill_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w.-]", "_", tool_name)
    path = spill_dir / f"{safe_name}_{int(time.time() * 1000)}.txt"
    path.write_text(output, encoding="utf-8")
    try:
        shown_path = path.relative_to(root)
    except ValueError:
        shown_path = path
    return (
        f"{output[:max_chars]}\n\n"
        f"[输出过长已截断：共 {len(output)} 字符，仅保留前 {max_chars} 字符。"
        f"完整内容已保存到 {shown_path}，需要时用 read_file 查看，不要重复执行同一命令]"
    )
