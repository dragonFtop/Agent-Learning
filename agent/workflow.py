"""s16: host-registered workflows with a journaled resume.

A workflow is trusted Python registered by the host. The model can only pick a
registered name and pass JSON args; it cannot supply code. Every ``agent()``
step is cached in an append-only JSONL journal keyed by its semantic content,
so resuming a failed run replays finished steps instead of paying for them
again.

The runtime is synchronous like the rest of the harness. ``parallel`` and
``pipeline`` use short-lived thread pools; a run-wide semaphore caps concurrent
model calls and a run-wide counter caps total agent calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:  # POSIX cross-process run lock
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None

try:  # Windows cross-process run lock
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None

from llm import call_llm

AGENT_CAP = 1000
CONCURRENCY = 8
MAX_SCHEMA_RETRIES = 1
WORKFLOW_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RUN_ID_RE = re.compile(r"^wf_[A-Za-z0-9][A-Za-z0-9._-]{0,63}_[0-9a-f]{16}$")
MISS = object()

WORKFLOW_AGENT_SYSTEM = (
    "You are a focused workflow agent. Complete only the supplied step. "
    "Do not claim access to files or tools you were not given. "
    "When a JSON schema is requested, return only one JSON object."
)


class WorkflowInputError(Exception):
    """Bad workflow, metadata, schema, journal, or resume input."""


def validate_workflow_name(name: str) -> str | None:
    if not isinstance(name, str) or not WORKFLOW_NAME_RE.fullmatch(name):
        return (
            "workflow name must be 1-64 letters, digits, dots, underscores, or "
            "dashes, and start with a letter or digit"
        )
    return None


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise WorkflowInputError("invalid workflow runId")
    return run_id


def _stable_hash(text: str) -> int:
    # Python's hash() is salted per process, which would break resume keys.
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)


def _parse_json(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:] if lines else lines
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for position, character in enumerate(stripped):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(stripped[position:])
            except json.JSONDecodeError:
                continue
            return value
    return None


class SimpleJsonSchema:
    """Tiny validator backing ``agent(..., schema=...)``."""

    def __init__(self, schema: dict) -> None:
        self.schema = schema

    def validate(self, value: Any, schema: dict | None = None) -> tuple[bool, str | None]:
        schema = self.schema if schema is None else schema
        if "enum" in schema and value not in schema["enum"]:
            return False, f"expected one of {schema['enum']}"
        kind = schema.get("type")
        if kind == "object":
            if not isinstance(value, dict):
                return False, "expected object"
            for key in schema.get("required", []):
                if key not in value:
                    return False, f"missing required key '{key}'"
            for key, sub in schema.get("properties", {}).items():
                if key in value:
                    ok, error = self.validate(value[key], sub)
                    if not ok:
                        return False, f"{key}: {error}"
            return True, None
        if kind == "array":
            if not isinstance(value, list):
                return False, "expected array"
            items = schema.get("items")
            if items:
                for index, item in enumerate(value):
                    ok, error = self.validate(item, items)
                    if not ok:
                        return False, f"[{index}]: {error}"
            return True, None
        if kind == "string":
            return isinstance(value, str), None if isinstance(value, str) else "expected string"
        if kind == "boolean":
            return isinstance(value, bool), None if isinstance(value, bool) else "expected boolean"
        if kind in ("number", "integer"):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            return ok, None if ok else "expected number"
        return True, None


@dataclass(frozen=True)
class RunnerOutput:
    value: Any
    tokens: int


class AnthropicAgentRunner:
    """Run workflow steps through the shared LLM wrapper."""

    def __init__(self, client: Any, model: str, max_tokens: int = 2000) -> None:
        self.client = client
        self.model = model
        self.max_tokens = max_tokens

    def run(self, prompt: str, schema: dict | None = None, label: str | None = None) -> RunnerOutput:
        request = prompt
        if schema is not None:
            request += (
                "\n\nReturn only one JSON object matching this schema:\n"
                + json.dumps(schema, ensure_ascii=True, sort_keys=True)
            )
        result = call_llm(
            self.client,
            model=self.model,
            system=WORKFLOW_AGENT_SYSTEM,
            messages=[{"role": "user", "content": request}],
            max_tokens=self.max_tokens,
        )
        value: Any = result.text
        if schema is not None:
            parsed = _parse_json(result.text)
            if parsed is not None:
                value = parsed
        return RunnerOutput(value, result.input_tokens + result.output_tokens)


class WorkflowJournal:
    """Append-only ``<runId>.journal.jsonl`` with replay on resume."""

    def __init__(self, run_id: str, resume: bool, store: Path) -> None:
        store.mkdir(parents=True, exist_ok=True)
        self.path = store / f"{run_id}.journal.jsonl"
        self._lock = threading.RLock()
        self.cache: dict[str, Any] = {}
        if resume:
            if not self.path.exists():
                raise WorkflowInputError(f"resume journal not found for {run_id}")
            for line_number, line in enumerate(
                self.path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    if (
                        not isinstance(record, dict)
                        or not isinstance(record.get("key"), str)
                        or "value" not in record
                    ):
                        raise ValueError("expected key/value record")
                except (json.JSONDecodeError, ValueError) as error:
                    raise WorkflowInputError(
                        f"invalid resume journal record at line {line_number}"
                    ) from error
                self.cache[record["key"]] = record["value"]
            self._handle = self.path.open("a", encoding="utf-8")
        else:
            self._handle = self.path.open("w", encoding="utf-8")

    def key(self, kind: str, label: str | None, prompt: str, schema: dict | None) -> str:
        basis = f"{kind}|{label}|{prompt}|{json.dumps(schema, sort_keys=True)}"
        return f"{kind}-{_stable_hash(basis) % 10**10:010d}"

    def cached(self, key: str) -> Any:
        with self._lock:
            return self.cache.get(key, MISS)

    def record(self, key: str, value: Any) -> None:
        with self._lock:
            self._handle.write(json.dumps({"key": key, "value": value}, default=str) + "\n")
            self._handle.flush()
            self.cache[key] = value

    def close(self) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.close()


class Budget:
    def __init__(self, total: int | None = None) -> None:
        self.total = total
        self._spent = 0
        self._lock = threading.Lock()

    def add(self, tokens: int) -> None:
        with self._lock:
            if self.total is not None and self._spent + tokens > self.total:
                raise WorkflowInputError(
                    f"token budget exceeded ({self._spent + tokens} > {self.total})"
                )
            self._spent += tokens

    def spent(self) -> int:
        with self._lock:
            return self._spent

    def remaining(self) -> float:
        with self._lock:
            if self.total is None:
                return float("inf")
            return max(0, self.total - self._spent)


class WorkflowTask:
    def __init__(self, task_id: str, run_id: str, meta: dict, console: Any = None) -> None:
        self.task_id = task_id
        self.run_id = run_id
        self.meta = meta
        self.console = console
        self.status = "running"
        self.usage = {"agents": 0, "tokens": 0}
        self.progress: list[dict] = []
        self._lock = threading.Lock()

    def event(self, name: str, **data: Any) -> None:
        if self.console is not None:
            details = " ".join(f"{key}={value}" for key, value in data.items())
            self.console.info(f"  [workflow] {name:<16} {details}".rstrip())

    def progress_event(self, event_type: str, **data: Any) -> None:
        with self._lock:
            self.progress.append({"type": event_type, **data})
        if self.console is not None:
            details = " ".join(f"{key}={value}" for key, value in data.items())
            self.console.info(f"  [workflow] {event_type:<16} {details}".rstrip())

    def add_usage(self, agents: int, tokens: int) -> None:
        with self._lock:
            self.usage["agents"] += agents
            self.usage["tokens"] += tokens

    def serialize(self) -> dict:
        with self._lock:
            return {
                "taskId": self.task_id,
                "taskType": "local_workflow",
                "runId": self.run_id,
                "workflowName": self.meta["name"],
                "status": self.status,
                "usage": dict(self.usage),
                "progress": list(self.progress),
            }


class ExecutionLimits:
    def __init__(self, agent_cap: int = AGENT_CAP, concurrency: int = CONCURRENCY) -> None:
        self.agent_cap = agent_cap
        self.concurrency = max(1, concurrency)
        self.agents = 0
        self.semaphore = threading.BoundedSemaphore(self.concurrency)
        self._lock = threading.Lock()

    def claim_agent(self) -> None:
        with self._lock:
            self.agents += 1
            if self.agents > self.agent_cap:
                raise WorkflowInputError(f"agent() cap reached ({self.agent_cap})")


class ExecutionState:
    """Injected into workflow code: phase/log/agent/parallel/pipeline/workflow."""

    def __init__(
        self,
        task: WorkflowTask,
        journal: WorkflowJournal,
        runner: Any,
        budget: Budget,
        args: dict,
        *,
        depth: int = 0,
        limits: ExecutionLimits | None = None,
        workflows: dict[str, tuple[dict, Callable]] | None = None,
    ) -> None:
        self.task = task
        self.journal = journal
        self.runner = runner
        self.budget = budget
        self.args = args
        self._depth = depth
        self._phase: str | None = None
        self._phases_seen: set[str] = set()
        self._limits = limits or ExecutionLimits()
        self._workflows = workflows or {}

    def phase(self, title: str) -> None:
        self._phase = title
        if title not in self._phases_seen:
            self._phases_seen.add(title)
            self.task.progress_event("workflow_phase", title=title)

    def log(self, message: str) -> None:
        self.task.progress_event("workflow_log", message=str(message))

    def agent(
        self,
        prompt: str,
        schema: dict | None = None,
        label: str | None = None,
        phase: str | None = None,
    ) -> Any:
        prompt = str(prompt)
        label = label or (prompt[:24] + "...")
        self._limits.claim_agent()
        if self.budget.remaining() <= 0:
            raise WorkflowInputError("token budget exceeded")

        key = self.journal.key("agent", label, prompt, schema)
        cached = self.journal.cached(key)
        if cached is not MISS:
            if schema is not None:
                ok, error = SimpleJsonSchema(schema).validate(cached)
                if not ok:
                    raise WorkflowInputError(
                        f"cached agent output failed schema validation: {error}"
                    )
            self.task.progress_event(
                "workflow_agent", label=label, phase=phase or self._phase, status="cached"
            )
            return cached

        with self._limits.semaphore:
            output = self.runner.run(prompt, schema, label)
            result = output.value
            tokens = output.tokens

            if schema is not None:
                ok, error = SimpleJsonSchema(schema).validate(result)
                for _ in range(MAX_SCHEMA_RETRIES):
                    if ok:
                        break
                    retry = self.runner.run(prompt + "\n\nReturn valid JSON.", schema, label)
                    result = retry.value
                    tokens += retry.tokens
                    ok, error = SimpleJsonSchema(schema).validate(result)
                if not ok:
                    raise WorkflowInputError(f"agent({{schema}}) invalid output: {error}")

        self.budget.add(tokens)
        self.task.add_usage(1, tokens)
        self.journal.record(key, result)
        self.task.progress_event(
            "workflow_agent", label=label, phase=phase or self._phase, status="done"
        )
        return result

    def parallel(self, thunks: list[Callable[[], Any]]) -> list:
        thunks = list(thunks)
        if not thunks:
            return []
        workers = min(len(thunks), self._limits.concurrency)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wf-parallel") as pool:
            futures = [pool.submit(thunk) for thunk in thunks]
            return [future.result() for future in futures]

    def pipeline(self, items: list, *stages: Callable) -> list:
        items = list(items)
        if not items:
            return []

        def run_item(item: Any, index: int) -> Any:
            value = item
            for stage in stages:
                value = stage(value, item, index)
            return value

        workers = min(len(items), self._limits.concurrency)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wf-pipeline") as pool:
            futures = [pool.submit(run_item, item, index) for index, item in enumerate(items)]
            return [future.result() for future in futures]

    def workflow(self, name: str, args: dict | None = None) -> Any:
        if self._depth >= 1:
            raise WorkflowInputError("workflow() nesting is one level only")
        if name not in self._workflows:
            raise WorkflowInputError(f"unknown workflow '{name}'")
        _meta, function = self._workflows[name]
        child = ExecutionState(
            self.task,
            self.journal,
            self.runner,
            self.budget,
            args or {},
            depth=self._depth + 1,
            limits=self._limits,
            workflows=self._workflows,
        )
        return function(child, args or {})


FINDINGS_SCHEMA = {
    "type": "object",
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title", "severity"],
                "properties": {
                    "title": {"type": "string"},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                },
            },
        }
    },
}
VERDICT_SCHEMA = {
    "type": "object",
    "required": ["isReal", "reason"],
    "properties": {"isReal": {"type": "boolean"}, "reason": {"type": "string"}},
}
DIMENSIONS = ["correctness", "security", "performance", "style"]


def review_changes(ctx: ExecutionState, args: dict) -> dict:
    """Review dimensions in a pipeline, verifying each finding concurrently."""
    ctx.phase("Review")
    changes = args.get("changes", "")
    if not isinstance(changes, str):
        raise WorkflowInputError("args.changes must be a string")
    review_input = changes.strip() or "No change context was supplied."

    def audit(_value: Any, dimension: str, _index: int) -> dict:
        output = ctx.agent(
            f"Review this change context for {dimension} issues. "
            "Report only issues supported by the supplied text.\n\n"
            f"{review_input}",
            schema=FINDINGS_SCHEMA,
            label=f"audit:{dimension}",
            phase="Review",
        )
        return {"dimension": dimension, "findings": output["findings"]}

    def verify(audited: dict, dimension: str, _index: int) -> dict:
        ctx.phase("Verify")
        verdicts = ctx.parallel(
            [
                lambda finding=finding: ctx.agent(
                    f"Adversarially verify this {dimension} finding against the "
                    "supplied change context.\n\n"
                    f"Change context:\n{review_input}\n\n"
                    f"Finding:\n{json.dumps(finding, ensure_ascii=True)}",
                    schema=VERDICT_SCHEMA,
                    label=f"verify:{dimension}:{finding['title']}",
                    phase="Verify",
                )
                for finding in audited["findings"]
            ]
        )
        confirmed = [
            finding
            for finding, verdict in zip(audited["findings"], verdicts)
            if isinstance(verdict, dict) and verdict.get("isReal")
        ]
        return {"dimension": dimension, "confirmed": confirmed}

    results = ctx.pipeline(DIMENSIONS, audit, verify)
    confirmed = [
        {"dimension": result["dimension"], **finding}
        for result in results
        if result
        for finding in result["confirmed"]
    ]
    confirmed.sort(key=lambda finding: {"high": 0, "medium": 1, "low": 2}.get(finding["severity"], 3))
    ctx.log(f"confirmed {len(confirmed)} real finding(s)")
    return {"confirmed": confirmed}


class WorkflowManager:
    def __init__(
        self,
        workspace: str | Path,
        client: Any,
        model: str,
        *,
        store: str | Path | None = None,
        runner: Any = None,
        console: Any = None,
        agent_cap: int = AGENT_CAP,
        concurrency: int = CONCURRENCY,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.client = client
        self.model = model
        self.store = (
            Path(store) if store is not None else self.workspace / ".runtime" / "workflows"
        )
        self.store = self.store.resolve()
        self.console = console
        self.runner = runner if runner is not None else AnthropicAgentRunner(client, model)
        self.agent_cap = agent_cap
        self.concurrency = max(1, concurrency)
        self.workflows: dict[str, tuple[dict, Callable]] = {}
        self._run_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.register(
            "review-changes",
            "Review changes across dimensions, then adversarially verify each finding.",
            review_changes,
            phases=["Review", "Verify"],
        )

    # ------------------------------------------------------------ registry
    def register(
        self,
        name: str,
        description: str,
        function: Callable[[ExecutionState, dict], Any],
        *,
        phases: list[str] | None = None,
        replace: bool = False,
    ) -> None:
        error = validate_workflow_name(name)
        if error:
            raise ValueError(error)
        if not callable(function):
            raise ValueError("workflow function must be callable")
        if name in self.workflows and not replace:
            raise ValueError(f"Workflow already registered: {name}")
        meta = {
            "name": name,
            "description": str(description),
            "phases": [str(phase) for phase in (phases or [])],
        }
        self.workflows[name] = (meta, function)

    def catalog_text(self) -> str:
        if not self.workflows:
            return "No workflows registered."
        lines = []
        for name, (meta, _function) in sorted(self.workflows.items()):
            phases = ", ".join(meta["phases"]) or "-"
            lines.append(f"- {name}: {meta['description']} (phases: {phases})")
        return "\n".join(lines)

    def list_text(self) -> str:
        lines = [self.catalog_text(), f"Store: {self.store}"]
        last = self.last_run_id()
        if last:
            lines.append(f"Last run: {last}")
        return "\n".join(lines)

    # --------------------------------------------------------------- paths
    def _snapshot_path(self, run_id: str) -> Path:
        return self.store / f"{run_id}.json"

    def _output_path(self, run_id: str) -> Path:
        return self.store / f"{run_id}.output.json"

    def _reserve_run_id(self, name: str) -> str:
        self.store.mkdir(parents=True, exist_ok=True)
        for _ in range(32):
            run_id = validate_run_id(f"wf_{name}_{secrets.token_hex(8)}")
            path = self._snapshot_path(run_id)
            try:
                handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            os.close(handle)
            return run_id
        raise WorkflowInputError("could not allocate a unique workflow runId")

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(value, indent=2, default=str, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_snapshot(self, run_id: str) -> dict:
        path = self._snapshot_path(run_id)
        if not path.exists():
            raise WorkflowInputError(f"resume snapshot not found for {run_id}")
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkflowInputError(f"invalid resume snapshot for {run_id}") from error
        if not isinstance(snapshot, dict):
            raise WorkflowInputError(f"invalid resume snapshot for {run_id}")
        return snapshot

    def _save_snapshot(self, run_id: str, meta: dict, args: dict, task: WorkflowTask) -> None:
        self._write_json(
            self._snapshot_path(run_id),
            {
                "runId": run_id,
                "workflowName": meta["name"],
                "args": args,
                "task": task.serialize(),
            },
        )

    def _save_last_run(self, run_id: str) -> None:
        try:
            (self.store / "last_run.txt").write_text(run_id, encoding="utf-8")
        except OSError:
            pass

    def last_run_id(self) -> str | None:
        path = self.store / "last_run.txt"
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return text or None

    # ---------------------------------------------------------------- lock
    @contextmanager
    def _run_lock(self, run_id: str):
        with self._locks_guard:
            lock = self._run_locks.setdefault(run_id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise WorkflowInputError(f"workflow run {run_id} is already active")
        handle = None
        try:
            self.store.mkdir(parents=True, exist_ok=True)
            handle = (self.store / f"{run_id}.lock").open("a+b")
            if msvcrt is not None and os.name == "nt":
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    pass  # best effort; the thread lock is authoritative here
            elif fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    pass
            yield
        finally:
            if handle is not None:
                try:
                    if msvcrt is not None and os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    elif fcntl is not None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                handle.close()
            lock.release()
            with self._locks_guard:
                if not lock.locked() and self._run_locks.get(run_id) is lock:
                    self._run_locks.pop(run_id, None)

    # ----------------------------------------------------------------- run
    def run(
        self,
        name: str,
        args: dict | None = None,
        resume_from_run_id: str | None = None,
    ) -> str:
        try:
            if not isinstance(name, str) or name not in self.workflows:
                raise WorkflowInputError(f"unknown workflow '{name}'")
            if args is not None and not isinstance(args, dict):
                raise WorkflowInputError("workflow args must be an object")
            meta, function = self.workflows[name]
            resuming = resume_from_run_id is not None
            if resuming:
                run_id = validate_run_id(resume_from_run_id)
                snapshot = self._read_snapshot(run_id)
                if snapshot.get("workflowName") != name:
                    raise WorkflowInputError("resume runId does not match workflow name")
                saved_args = snapshot.get("args", {})
                if not isinstance(saved_args, dict):
                    raise WorkflowInputError("resume snapshot args are invalid")
                if args is None:
                    args = saved_args
                elif args != saved_args:
                    raise WorkflowInputError("resume args do not match the original run")
            else:
                args = args or {}
                run_id = self._reserve_run_id(name)
            budget_total = args.get("budget")
            if budget_total is not None and (
                isinstance(budget_total, bool)
                or not isinstance(budget_total, int)
                or budget_total < 0
            ):
                raise WorkflowInputError("args.budget must be a non-negative integer")
        except WorkflowInputError as error:
            return f"Error: {error}"

        try:
            with self._run_lock(run_id):
                return self._execute(meta, function, args, run_id, resuming, budget_total)
        except WorkflowInputError as error:
            return f"Error: {error}"

    def _execute(
        self,
        meta: dict,
        function: Callable,
        args: dict,
        run_id: str,
        resuming: bool,
        budget_total: int | None,
    ) -> str:
        try:
            journal = WorkflowJournal(run_id, resuming, self.store)
        except WorkflowInputError as error:
            return f"Error: {error}"

        task = WorkflowTask(f"local_workflow_{run_id}", run_id, meta, console=self.console)
        task.event("task_started", workflow=meta["name"], resume=resuming)
        self._save_snapshot(run_id, meta, args, task)
        try:
            limits = ExecutionLimits(self.agent_cap, self.concurrency)
            state = ExecutionState(
                task,
                journal,
                self.runner,
                Budget(budget_total),
                args,
                limits=limits,
                workflows=self.workflows,
            )
            try:
                result = function(state, args)
                task.status = "completed"
            except Exception as error:  # noqa: BLE001 - failures are resumable output
                task.status = "failed"
                result = {"error": str(error)}
        finally:
            journal.close()

        self._write_json(self._output_path(run_id), result)
        self._save_snapshot(run_id, meta, args, task)
        self._save_last_run(run_id)
        task.event(
            "task_notification",
            status=task.status,
            agents=task.usage["agents"],
            tokens=task.usage["tokens"],
        )
        return json.dumps(
            {
                "runId": run_id,
                "workflowName": meta["name"],
                "status": task.status,
                "usage": dict(task.usage),
                "result": result,
            },
            default=str,
            ensure_ascii=False,
        )
