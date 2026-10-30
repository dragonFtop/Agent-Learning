"""Minimal frontmatter reader shared by skills and memory records.

Only the YAML subset the harness writes is supported: ``key: value`` lines,
quoted strings, and ``>`` / ``|`` block scalars. Keeping this in the standard
library avoids a PyYAML dependency for two small parsers.
"""

from __future__ import annotations


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return ``(metadata, body)``; text without frontmatter returns ``({}, text)``."""
    normalized = text.lstrip("﻿")
    lines = normalized.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, normalized

    closing = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            closing = index
            break
    if closing is None:
        return {}, normalized

    metadata = _parse_header(lines[1:closing])
    body = "".join(lines[closing + 1 :]).strip()
    return metadata, body


def _parse_header(lines: list[str]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    index = 0
    while index < len(lines):
        raw = lines[index].rstrip("\r\n")
        index += 1
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if value[:1] in (">", "|") and set(value) <= {">", "|", "+", "-"}:
            style = value[0]
            collected: list[str] = []
            while index < len(lines):
                follow = lines[index].rstrip("\r\n")
                if follow.strip() and not follow.startswith((" ", "\t")):
                    break
                collected.append(follow.strip())
                index += 1
            while collected and not collected[-1]:
                collected.pop()
            if style == ">":
                metadata[key] = " ".join(part for part in collected if part)
            else:
                metadata[key] = "\n".join(collected)
            continue

        metadata[key] = _unquote(value)
    return metadata


def dump_frontmatter(metadata: dict[str, str], body: str) -> str:
    lines = ["---"]
    for key, value in metadata.items():
        flat = " ".join(str(value).split())
        lines.append(f"{key}: {flat}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.strip() + "\n"
