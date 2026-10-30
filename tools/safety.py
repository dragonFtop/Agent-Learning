"""Command classification shared by the bash tool and the permission layer.

This is a fuse, not a security boundary. Regex matching cannot stop a
determined caller from obfuscating a command; real isolation needs a container
or an OS sandbox. The goal here is to stop obvious accidents and to give the
permission hook enough information to decide when to ask the user.
"""

from __future__ import annotations

import re

DENY_PATTERNS = [
    re.compile(r"\brm\s+-\S+\s+/{1,2}(?![A-Za-z])"),  # rm -rf / but not /tmp
    re.compile(r"\bsudo\b"),
    re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"),
    re.compile(r"\b(mkfs|fdisk|parted)\b"),
    re.compile(r"\bdd\s+if="),
    re.compile(r"\bformat\s+[A-Za-z]:"),
    re.compile(r"\b(del|rd|rmdir)\s+/[sq]\b[^\n]*[A-Za-z]:\\", re.IGNORECASE),
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"),  # fork bomb
    re.compile(r"\bchmod\s+(-R\s+)?777\s+/{1,2}"),
    re.compile(r">\s*/dev/sd[a-z]"),
]

# Shell metacharacters mean the command is more than a single simple command.
# Even an allowlisted first word cannot make `cat x && rm -rf y` safe.
SHELL_METACHARACTERS = re.compile(r"[;&|<>`$%\n\r]")

SAFE_COMMAND = re.compile(
    r"""^(?:
        ls|dir|pwd|cat|type|head|tail|wc|find|grep|rg|which|where|echo|
        git\s+(?:status|diff|log|show|branch|rev-parse|ls-files|remote\s+-v|config\s+--get)|
        (?:python|py|node|npm|pip)\s+--version|
        (?:py\s+-m\s+)?pytest(?:\s|$)
    )(?:\s|$)""",
    re.IGNORECASE | re.VERBOSE,
)


def is_dangerous_command(command: str) -> bool:
    return any(pattern.search(command) for pattern in DENY_PATTERNS)


def first_token(command: str) -> str:
    stripped = command.strip()
    if not stripped:
        return ""
    return stripped.split()[0].lower()


def classify_command(command: str) -> str:
    """Return ``deny``, ``safe`` or ``ask`` for a bash command string."""
    if is_dangerous_command(command):
        return "deny"
    if SHELL_METACHARACTERS.search(command):
        return "ask"
    if SAFE_COMMAND.match(command.strip()):
        return "safe"
    return "ask"
