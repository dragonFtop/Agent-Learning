"""Tool registry: the s02 dispatch map with an explicit execution context.

A tool is a ``ToolSpec`` (schema + handler + visibility). Handlers receive a
``ToolContext`` instead of reading globals, which is what lets the same tool
run in the main workspace, inside a subagent, or inside a teammate's worktree.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

Handler = Callable[[dict, "ToolContext"], str]


@dataclass
class ToolContext:
    """Everything a handler may need beyond its own arguments."""

    runtime: Any = None
    cwd: Path = field(default_factory=Path.cwd)
    profile: str = "lead"  # lead | subagent | teammate
    agent_name: str = "lead"
    task_id: str | None = None
    interactive: bool = True
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Handler
    source: str = "builtin"
    # Which agent profiles may see this tool. Keeping this on the spec means
    # adding a tool never requires editing the loop.
    profiles: tuple[str, ...] = ("lead", "subagent", "teammate")

    def to_anthropic(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        # MCP tools are registered while teammate threads may already be
        # building their tool pools, so pool mutation and reads share a lock.
        self._lock = threading.RLock()

    def register(self, spec: ToolSpec, *, replace: bool = False) -> None:
        with self._lock:
            if spec.name in self._tools and not replace:
                raise ValueError(f"Tool already registered: {spec.name}")
            self._tools[spec.name] = spec

    def unregister(self, name: str) -> None:
        with self._lock:
            self._tools.pop(name, None)

    def get(self, name: str) -> ToolSpec | None:
        with self._lock:
            return self._tools.get(name)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._tools)

    def subset(self, profile: str) -> "ToolRegistry":
        registry = ToolRegistry()
        with self._lock:
            specs = list(self._tools.values())
        for spec in specs:
            if profile in spec.profiles:
                registry.register(spec)
        return registry

    def to_anthropic_tools(self) -> list[dict]:
        with self._lock:
            specs = list(self._tools.values())
        return [spec.to_anthropic() for spec in specs]

    def dispatch(self, name: str, args: dict, ctx: ToolContext) -> str:
        spec = self.get(name)
        if spec is None:
            return f"Error: unknown tool '{name}'"
        if not isinstance(args, dict):
            return f"Error: tool '{name}' expects an object input"
        try:
            result = spec.handler(args, ctx)
        except Exception as error:  # noqa: BLE001 - model-facing tools return text
            return f"Error: {type(error).__name__}: {error}"
        return result if isinstance(result, str) else str(result)

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None
