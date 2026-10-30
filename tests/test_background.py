import os
import time

from agent.background import BackgroundManager


def _wait_for_notifications(manager, timeout=15.0):
    deadline = time.time() + timeout
    collected = []
    while time.time() < deadline:
        collected.extend(manager.drain_notifications())
        if collected:
            return collected
        time.sleep(0.02)
    return collected


def test_background_command_completes_and_notifies(tmp_path):
    manager = BackgroundManager(workdir=tmp_path)
    started = manager.start("echo bg-ok")

    assert "Background task bg_0001 started" in started
    assert "bg_0001" in manager.snapshot()
    assert not manager.has_pending()

    notifications = _wait_for_notifications(manager)
    assert notifications
    message = notifications[0]
    assert "<task_notification>" in message
    assert "<status>completed</status>" in message
    assert "bg-ok" in message
    assert not manager.has_pending()
    assert not manager.running_ids()


def test_background_failure_reports_failed_status(tmp_path):
    manager = BackgroundManager(workdir=tmp_path)
    command = "exit /b 3" if os.name == "nt" else "exit 3"
    manager.start(command)

    notifications = _wait_for_notifications(manager)
    assert notifications
    assert "<status>failed</status>" in notifications[0]


def test_empty_and_dangerous_commands(tmp_path):
    manager = BackgroundManager(workdir=tmp_path)
    try:
        manager.start("   ")
    except ValueError as error:
        assert "empty" in str(error)
    else:
        raise AssertionError("empty command must raise")

    result = manager.start("rm -rf /")
    assert "blocked" in result
    assert manager.snapshot() == {}


def test_drain_is_destructive(tmp_path):
    manager = BackgroundManager(workdir=tmp_path)
    manager.start("echo once")
    assert _wait_for_notifications(manager)
    assert manager.drain_notifications() == []
