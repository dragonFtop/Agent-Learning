"""s15 event pump: one lead history, one turn lock.

User input, cron firings, teammate messages and background notifications all
have to reach the same ``AgentRuntime``. The coordinator owns that boundary:

* ``run_user_input`` and ``pump_once`` take the same RLock, so a scheduled
  turn can never interleave with a user turn.
* The cron scheduler only enqueues; a job is acknowledged after its turn
  succeeds and restored if the model call fails.
* The background manager is drained by the runtime itself at the top of its
  loop, so the pump only wakes the runtime when a notification is waiting.

``ConsoleBroker`` serializes stdin reads. Tools running off the main thread
still fail closed in ``PermissionPolicy``; the broker exists for the team
protocol in s13, where a teammate may ask a question through the lead's CLI.
"""

from __future__ import annotations

import threading
from typing import Any, Callable


class ConsoleBroker:
    def __init__(self, reader: Callable[[str], str] | None = None) -> None:
        self._lock = threading.Lock()
        self._reader = reader

    def ask(self, prompt: str = "") -> str:
        with self._lock:
            reader = self._reader or input
            return reader(prompt)


class RuntimeCoordinator:
    def __init__(
        self,
        runtime: Any,
        *,
        console: Any = None,
        broker: ConsoleBroker | None = None,
        poll_interval: float = 1.0,
        on_result: Callable[[str], None] | None = None,
    ) -> None:
        self.runtime = runtime
        self.console = console if console is not None else getattr(runtime, "console", None)
        self.broker = broker or ConsoleBroker()
        self.poll_interval = max(0.05, poll_interval)
        self.on_result = on_result
        self._turn_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_cron = False

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        cron = getattr(self.runtime, "cron", None)
        if cron is not None and not cron.is_running():
            cron.start()
            self._started_cron = True
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="runtime-coordinator")
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        if self._started_cron:
            cron = getattr(self.runtime, "cron", None)
            if cron is not None:
                cron.stop()
            self._started_cron = False
        teams = getattr(self.runtime, "teams", None)
        if teams is not None:
            try:
                teams.stop_all()
            except Exception as error:  # noqa: BLE001 - shutdown must continue
                self._warn(f"[coordinator] team shutdown failed: {type(error).__name__}: {error}")
        mcp = getattr(self.runtime, "mcp", None)
        if mcp is not None:
            try:
                mcp.close_all()
            except Exception as error:  # noqa: BLE001 - shutdown must continue
                self._warn(f"[coordinator] MCP shutdown failed: {type(error).__name__}: {error}")

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ----------------------------------------------------------- user turns
    def run_user_input(self, text: str, *, source: str = "user") -> str:
        with self._turn_lock:
            result = self.runtime.run_turn(text, source=source)
        self._emit(result)
        return result

    def run_locked(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a state-changing command under the same turn lock."""
        with self._turn_lock:
            return fn(*args, **kwargs)

    # ---------------------------------------------------------- event pump
    def pump_once(self) -> str | None:
        """Run one batch of pending scheduled/team events, if any.

        Returns the final assistant text, or ``None`` when nothing was waiting.
        """
        with self._turn_lock:
            cron = getattr(self.runtime, "cron", None)
            cron_jobs = cron.consume_queue() if cron is not None else []
            prompts = [f"[Scheduled task {job.id}] {job.prompt}" for job in cron_jobs]
            team_prompt = self._collect_team_prompt()
            background = getattr(self.runtime, "background", None)
            background_wake = (
                background is not None
                and background.has_pending()
                and not prompts
                and not team_prompt
            )
            if not prompts and not team_prompt and not background_wake:
                return None

            joined = "\n\n".join(prompts + ([team_prompt] if team_prompt else []))
            try:
                if joined:
                    source = "cron" if prompts else "team"
                    text = self.runtime.run_turn(joined, source=source)
                else:
                    text = self.runtime.run(source="background")
            except Exception as error:  # noqa: BLE001 - keep the pump alive
                if cron is not None and cron_jobs:
                    cron.restore(cron_jobs)
                self._warn(f"[coordinator] turn failed: {type(error).__name__}: {error}")
                return None

            if cron is not None and cron_jobs:
                if text.startswith("[Agent error"):
                    cron.restore(cron_jobs)
                else:
                    try:
                        cron.acknowledge(cron_jobs)
                    except Exception as error:  # noqa: BLE001
                        self._warn(f"[coordinator] cron ack failed: {type(error).__name__}: {error}")

        self._emit(text)
        return text

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_interval):
            try:
                self.pump_once()
            except Exception as error:  # noqa: BLE001 - the pump must not die
                self._warn(f"[coordinator] pump error: {type(error).__name__}: {error}")

    # ------------------------------------------------------------ team hook
    def _collect_team_prompt(self) -> str | None:
        teams = getattr(self.runtime, "teams", None)
        if teams is None:
            return None
        drain = getattr(teams, "drain_lead_inbox", None) or getattr(teams, "drain_inbox", None)
        if drain is None:
            return None
        try:
            events = drain()
        except Exception as error:  # noqa: BLE001
            self._warn(f"[coordinator] team inbox failed: {type(error).__name__}: {error}")
            return None
        if not events:
            return None
        formatter = getattr(teams, "format_events", None)
        if formatter is not None:
            try:
                return formatter(events)
            except Exception:  # noqa: BLE001
                pass
        return "[Team events]\n" + "\n".join(str(event) for event in events)

    # ---------------------------------------------------------------- output
    def _emit(self, text: str) -> None:
        if self.on_result is not None and text:
            self.on_result(text)

    def _warn(self, message: str) -> None:
        if self.console is not None:
            self.console.warn(message)
