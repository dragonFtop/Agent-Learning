"""LLM call wrapper: retries, text extraction and context-overflow detection.

The rest of the harness must never call ``client.messages.create`` directly.
Keeping a single entry point makes retries, usage accounting and the s08
reactive-compaction path consistent everywhere (main loop, subagents,
workflow runners and the goal evaluator).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from config import LLM_RETRIES


class PromptTooLongError(RuntimeError):
    """Raised when the provider rejects a request for exceeding context."""


@dataclass
class LLMResult:
    response: Any
    text: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int


def block_type(block: Any) -> str | None:
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)


def block_get(block: Any, key: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def extract_text(content: Any) -> str:
    """Return concatenated text blocks from an assistant content payload."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if block_type(block) == "text":
                text = block_get(block, "text", "")
                if text:
                    parts.append(text)
    return "".join(parts)


def _usage_tokens(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return 0, 0
    if isinstance(usage, dict):
        return int(usage.get("input_tokens", 0) or 0), int(usage.get("output_tokens", 0) or 0)
    return int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0)


_PROMPT_TOO_LONG_MARKERS = (
    "prompt too long",
    "prompt is too long",
    "context length",
    "context_length_exceeded",
    "maximum context",
    "too many tokens",
    "request too large",
    "tokens exceed",
    "413",
)


def is_prompt_too_long(error: BaseException) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in _PROMPT_TOO_LONG_MARKERS)


def _is_retryable(error: BaseException) -> bool:
    name = type(error).__name__.lower()
    if is_prompt_too_long(error):
        return False
    if any(part in name for part in ("authentication", "permission", "badrequest", "notfound")):
        return False
    # Connection resets, rate limits and 5xx responses are worth retrying.
    return True


def call_llm(
    client: Any,
    *,
    model: str,
    system: str,
    messages: list,
    tools: list | None = None,
    max_tokens: int = 8000,
    retries: int | None = None,
) -> LLMResult:
    """Call the model once with bounded retries.

    ``PromptTooLongError`` is raised immediately so the caller can run the
    reactive compaction path instead of retrying the same oversized request.
    """
    attempts = LLM_RETRIES if retries is None else max(0, retries)
    last_error: BaseException | None = None
    for attempt in range(attempts + 1):
        kwargs: dict[str, Any] = {
            "model": model,
            "system": system,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
        try:
            response = client.messages.create(**kwargs)
        except Exception as error:  # noqa: BLE001 - provider errors vary by SDK
            last_error = error
            if is_prompt_too_long(error):
                raise PromptTooLongError(str(error)) from error
            if attempt >= attempts or not _is_retryable(error):
                raise
            time.sleep(min(8.0, 1.0 * (2**attempt)))
            continue

        input_tokens, output_tokens = _usage_tokens(response)
        return LLMResult(
            response=response,
            text=extract_text(getattr(response, "content", [])),
            stop_reason=getattr(response, "stop_reason", None),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    assert last_error is not None
    raise last_error
