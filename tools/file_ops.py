from pathlib import Path

from config import WORKDIR

# 定义的路径沙箱，防止路径逃逸
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"{p} is not a valid path")
    return path


# 定义文件操作工具的函数
def run_read(path: str, limit: int = None) -> str:
    try:
        text = safe_path(path).read_text(encoding="utf-8")
        lines = text.splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {str(e)}"

# 写文件时，如果父目录不存在则创建，写入内容后返回成功信息
def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytesto {path} successfully"
    except Exception as e:
        return f"Error: {str(e)}"

# 编辑文件时，如果文件不存在则创建，并返回成功信息
def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text(encoding="utf-8")
        if old_text not in content:
            return f"Error: '{old_text}' not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edit {path} successfully!"
    except Exception as e:
        return f"Error: {str(e)}"
