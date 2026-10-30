"""File tools with a workspace sandbox.

``safe_path`` resolves every path against the current agent cwd and rejects
anything that escapes it. The cwd is explicit so a teammate can work inside a
git worktree while the lead stays in the main checkout.
"""

from __future__ import annotations

import glob as glob_module
from pathlib import Path

MAX_READ_CHARS = 50000
MAX_GLOB_RESULTS = 200


def _root(cwd: str | Path | None = None) -> Path:
    if cwd is not None:
        return Path(cwd).resolve()
    # Lazy import avoids a config <-> tools import cycle at module load time.
    from config import WORKDIR

    return WORKDIR


def safe_path(path: str, cwd: str | Path | None = None) -> Path:
    root = _root(cwd)
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"{path} is outside the workspace")
    return candidate


def run_read(
    path: str,
    limit: int | None = None,
    offset: int = 0,
    cwd: str | Path | None = None,
) -> str:
    try:
        text = safe_path(path, cwd).read_text(encoding="utf-8")
        lines = text.splitlines()
        start = max(0, int(offset or 0))
        selected = lines[start:]
        if limit and int(limit) < len(selected):
            selected = selected[: int(limit)]
            selected.append(f"... ({len(lines) - start - int(limit)} more lines)")
        output = "\n".join(selected)
        if len(output) > MAX_READ_CHARS:
            output = output[:MAX_READ_CHARS] + "\n... (output truncated)"
        return output
    except Exception as error:  # noqa: BLE001 - returned to the model
        return f"Error: {error}"


def run_write(path: str, content: str, cwd: str | Path | None = None) -> str:
    try:
        file_path = safe_path(path, cwd)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} characters to {path}"
    except Exception as error:  # noqa: BLE001
        return f"Error: {error}"


def run_edit(
    path: str,
    old_text: str,
    new_text: str,
    cwd: str | Path | None = None,
) -> str:
    try:
        file_path = safe_path(path, cwd)
        content = file_path.read_text(encoding="utf-8")
        if old_text not in content:
            return f"Error: '{old_text[:80]}' not found in {path}"
        file_path.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as error:  # noqa: BLE001
        return f"Error: {error}"


def run_glob(
    pattern: str,
    cwd: str | Path | None = None,
    max_results: int = MAX_GLOB_RESULTS,
) -> str:
    try:
        root = _root(cwd)
        if Path(pattern).is_absolute():
            matches = glob_module.glob(pattern, recursive=True)
        else:
            matches = glob_module.glob(pattern, root_dir=root, recursive=True)
        safe_matches = []
        for match in matches:
            candidate = Path(match) if Path(match).is_absolute() else root / match
            if candidate.resolve().is_relative_to(root):
                safe_matches.append(match)
        matches = sorted(set(safe_matches))
        shown = matches[:max_results]
        if len(matches) > max_results:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) if shown else "(no matches)"
    except Exception as error:  # noqa: BLE001
        return f"Error: {error}"
