"""s12: cron scheduler.

The scheduler thread never runs the agent. When a 5-field cron expression
matches the current minute it enqueues the job. The coordinator drains the
queue and runs one ordinary turn under the same lock as user input. Jobs are
acknowledged only after a successful turn; a failure puts them back.

Durable jobs live in ``.scheduled_tasks.json`` as a JSON list. Every write is
atomic, so a crash cannot leave a half-written schedules file.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Callable

CRON_ID_PATTERN = re.compile(r"^cron_[0-9a-f]{8}$")
FIELD_RULES = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day-of-month", 1, 31),
    ("month", 1, 12),
    ("day-of-week", 0, 6),
)


def _default_path() -> Path:
    from config import CRON_FILE

    return CRON_FILE


@dataclass
class CronJob:
    id: str
    cron: str
    prompt: str
    recurring: bool
    durable: bool
    pending_delivery: bool = False
    last_fired: str | None = None


def _field_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        return value % int(field[2:]) == 0
    if "," in field:
        return any(_field_matches(part.strip(), value) for part in field.split(","))
    if "-" in field:
        start, end = field.split("-", 1)
        return int(start) <= value <= int(end)
    return value == int(field)


def cron_matches(cron_expr: str, moment: datetime) -> bool:
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, day, month, weekday = fields
    cron_weekday = (moment.weekday() + 1) % 7
    try:
        if not (
            _field_matches(minute, moment.minute)
            and _field_matches(hour, moment.hour)
            and _field_matches(month, moment.month)
        ):
            return False
        day_matches = _field_matches(day, moment.day)
        weekday_matches = _field_matches(weekday, cron_weekday)
    except (ValueError, ZeroDivisionError):
        return False
    if day == "*" and weekday == "*":
        return True
    if day == "*":
        return weekday_matches
    if weekday == "*":
        return day_matches
    return day_matches or weekday_matches


def _validate_field(field: str, minimum: int, maximum: int) -> str | None:
    if field == "*":
        return None
    if field.startswith("*/"):
        step = field[2:]
        if not step.isdigit() or int(step) <= 0:
            return f"Invalid step: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            error = _validate_field(part.strip(), minimum, maximum)
            if error:
                return error
        return None
    if "-" in field:
        start, end = field.split("-", 1)
        if not start.isdigit() or not end.isdigit():
            return f"Invalid range: {field}"
        start_value, end_value = int(start), int(end)
        if start_value > end_value:
            return f"Range start is greater than end: {field}"
        if start_value < minimum or end_value > maximum:
            return f"Range {field} is outside [{minimum}-{maximum}]"
        return None
    if not field.isdigit():
        return f"Invalid field: {field}"
    value = int(field)
    if value < minimum or value > maximum:
        return f"Value {value} is outside [{minimum}-{maximum}]"
    return None


def validate_cron(cron_expr: str) -> str | None:
    fields = str(cron_expr).strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"
    for field, (name, minimum, maximum) in zip(fields, FIELD_RULES):
        error = _validate_field(field, minimum, maximum)
        if error:
            return f"{name}: {error}"
    return None


class CronScheduler:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        interval: float = 1.0,
        autoload: bool = True,
        console=None,
    ) -> None:
        self.path = Path(path) if path is not None else _default_path()
        self.jobs: dict[str, CronJob] = {}
        self.queue: list[CronJob] = []
        self._lock = threading.RLock()
        self._clock = clock or datetime.now
        self.interval = max(0.05, interval)
        self._console = console
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loaded = False
        if autoload:
            self.load_durable()

    # ------------------------------------------------------------ storage
    def _atomic_write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def save_durable(self) -> None:
        with self._lock:
            self._save_durable_locked()

    def _save_durable_locked(self) -> None:
        payload = [asdict(job) for job in self.jobs.values() if job.durable]
        self._atomic_write(json.dumps(payload, indent=2) + "\n")

    def load_durable(self) -> int:
        if self._loaded:
            return 0
        self._loaded = True
        if not self.path.exists():
            return 0
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise ValueError("expected a JSON list")
        except (OSError, json.JSONDecodeError, ValueError) as error:
            self._warn(f"[cron] could not load {self.path.name}: {error}")
            return 0

        loaded = 0
        with self._lock:
            for item in payload:
                try:
                    if not isinstance(item, dict):
                        raise ValueError("job is not an object")
                    known = {field.name for field in fields(CronJob)}
                    job = CronJob(**{key: value for key, value in item.items() if key in known})
                    error = validate_cron(job.cron)
                    if error:
                        raise ValueError(error)
                    if not CRON_ID_PATTERN.fullmatch(job.id):
                        raise ValueError("invalid job ID")
                    if not str(job.prompt).strip():
                        raise ValueError("prompt cannot be empty")
                except (TypeError, ValueError) as error:
                    self._warn(f"[cron] skipped invalid saved job: {error}")
                    continue
                if job.id in self.jobs:
                    continue
                self.jobs[job.id] = job
                if job.pending_delivery:
                    self.queue.append(job)
                loaded += 1
        return loaded

    # ------------------------------------------------------------ mutation
    def _new_id_locked(self) -> str:
        for _ in range(100):
            job_id = f"cron_{secrets.token_hex(4)}"
            if job_id not in self.jobs:
                return job_id
        raise RuntimeError("Could not allocate a cron job ID")

    def schedule(
        self,
        cron: str,
        prompt: str,
        recurring: bool = True,
        durable: bool = True,
    ) -> CronJob | str:
        error = validate_cron(cron)
        if error:
            return error
        prompt = str(prompt).strip()
        if not prompt:
            return "Prompt cannot be empty"
        with self._lock:
            job = CronJob(
                id=self._new_id_locked(),
                cron=str(cron).strip(),
                prompt=prompt,
                recurring=bool(recurring),
                durable=bool(durable),
            )
            self.jobs[job.id] = job
            try:
                if job.durable:
                    self._save_durable_locked()
            except Exception:
                self.jobs.pop(job.id, None)
                raise
            return job

    def cancel(self, job_id: str) -> str:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                return f"Job {job_id} not found"
            previous_queue = list(self.queue)
            self.jobs.pop(job_id, None)
            self.queue[:] = [queued for queued in self.queue if queued.id != job_id]
            try:
                if job.durable:
                    self._save_durable_locked()
            except Exception:
                self.jobs[job_id] = job
                self.queue[:] = previous_queue
                raise
            return f"Cancelled {job_id}"

    # ------------------------------------------------------------- polling
    def poll_due_jobs(self, moment: datetime | None = None) -> list[CronJob]:
        moment = moment or self._clock()
        minute_marker = moment.strftime("%Y-%m-%d %H:%M")
        enqueued: list[CronJob] = []
        with self._lock:
            for job in list(self.jobs.values()):
                if self.jobs.get(job.id) is not job:
                    continue
                if job.pending_delivery or job.last_fired == minute_marker:
                    continue
                if not cron_matches(job.cron, moment):
                    continue
                previous_pending = job.pending_delivery
                previous_fired = job.last_fired
                job.pending_delivery = True
                job.last_fired = minute_marker
                try:
                    if job.durable:
                        self._save_durable_locked()
                except Exception as error:
                    job.pending_delivery = previous_pending
                    job.last_fired = previous_fired
                    self._warn(f"[cron] could not enqueue {job.id}: {error}")
                    continue
                self.queue.append(job)
                enqueued.append(job)
        return enqueued

    def consume_queue(self) -> list[CronJob]:
        with self._lock:
            jobs = list(self.queue)
            self.queue.clear()
            return jobs

    def acknowledge(self, jobs: list[CronJob]) -> None:
        changed: list[tuple[CronJob, bool]] = []
        removed: list[CronJob] = []
        with self._lock:
            for delivered in jobs:
                current = self.jobs.get(delivered.id)
                if current is None:
                    continue
                changed.append((current, current.pending_delivery))
                if current.recurring:
                    current.pending_delivery = False
                else:
                    removed.append(current)
                    self.jobs.pop(current.id, None)
            try:
                if any(job.durable for job, _ in changed):
                    self._save_durable_locked()
            except Exception:
                for job in removed:
                    self.jobs[job.id] = job
                for job, pending in changed:
                    job.pending_delivery = pending
                queued_ids = {job.id for job in self.queue}
                for job, _ in changed:
                    if job.id not in queued_ids:
                        self.queue.append(job)
                raise

    def restore(self, jobs: list[CronJob]) -> None:
        with self._lock:
            queued_ids = {job.id for job in self.queue}
            for delivered in jobs:
                current = self.jobs.get(delivered.id)
                if current is None:
                    continue
                current.pending_delivery = True
                if current.id not in queued_ids:
                    self.queue.append(current)
                    queued_ids.add(current.id)

    def has_queued_jobs(self) -> bool:
        with self._lock:
            return bool(self.queue)

    # -------------------------------------------------------------- thread
    def start(self) -> None:
        self.load_durable()
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="cron-scheduler")
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.poll_due_jobs()
            except Exception as error:  # noqa: BLE001 - keep the scheduler alive
                self._warn(f"[cron] poll failed: {type(error).__name__}: {error}")

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---------------------------------------------------------------- views
    def list_text(self) -> str:
        with self._lock:
            jobs = list(self.jobs.values())
        if not jobs:
            return "No cron jobs."
        lines = []
        for job in jobs:
            frequency = "recurring" if job.recurring else "one-shot"
            storage = "durable" if job.durable else "session"
            pending = ", pending" if job.pending_delivery else ""
            lines.append(
                f"{job.id}: {job.cron} -> {job.prompt[:60]} "
                f"[{frequency}, {storage}{pending}]"
            )
        return "\n".join(lines)

    def _warn(self, message: str) -> None:
        if self._console is not None:
            self._console.warn(message)
