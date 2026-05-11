import os
import subprocess

# 定义运行bash命令的函数，包含危险命令过滤和超时处理
def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
            errors="ignore",
        )
        out = (result.stdout + result.stderr).strip()
        return out[:50000] if out else "继续中ing"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
