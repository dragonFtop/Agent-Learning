from datetime import datetime

from agent.cron import CronScheduler, cron_matches, validate_cron

MONDAY_0930 = datetime(2026, 10, 19, 9, 30)


def _scheduler(tmp_path, **kwargs):
    return CronScheduler(tmp_path / ".scheduled_tasks.json", autoload=False, **kwargs)


def test_validate_cron():
    assert validate_cron("0 9 * * 1-5") is None
    assert validate_cron("*/15 * * * *") is None
    assert validate_cron("0 9 * * 1,3,5") is None
    assert "Expected 5 fields" in validate_cron("* * *")
    assert "minute" in validate_cron("60 * * * *")
    assert "Invalid step" in validate_cron("*/0 * * * *")
    assert "Range start" in validate_cron("5-1 * * * *")
    assert "day-of-week" in validate_cron("* * * * 8")


def test_cron_matches_fields_and_weekday():
    assert cron_matches("30 9 * * *", MONDAY_0930)
    assert cron_matches("*/5 * * * *", datetime(2026, 10, 19, 9, 30))
    assert not cron_matches("*/5 * * * *", datetime(2026, 10, 19, 9, 31))
    assert cron_matches("30 9 * * 1", MONDAY_0930)  # cron Monday = 1
    assert not cron_matches("30 9 * * 0", MONDAY_0930)
    assert cron_matches("30 9 19 * *", MONDAY_0930)
    assert not cron_matches("30 9 20 * *", MONDAY_0930)
    assert not cron_matches("bad cron", MONDAY_0930)


def test_schedule_and_durable_roundtrip(tmp_path):
    path = tmp_path / ".scheduled_tasks.json"
    scheduler = _scheduler(tmp_path)
    job = scheduler.schedule("0 9 * * *", "run tests", recurring=True, durable=True)

    assert not isinstance(job, str)
    payload = path.read_text(encoding="utf-8")
    assert job.id in payload

    reloaded = CronScheduler(path, autoload=True)
    assert job.id in reloaded.jobs
    assert reloaded.jobs[job.id].prompt == "run tests"


def test_poll_enqueues_once_per_minute(tmp_path):
    scheduler = _scheduler(tmp_path)
    job = scheduler.schedule("* * * * *", "tick", durable=False)
    assert not isinstance(job, str)

    assert [item.id for item in scheduler.poll_due_jobs(MONDAY_0930)] == [job.id]
    assert scheduler.poll_due_jobs(MONDAY_0930) == []
    assert scheduler.has_queued_jobs()
    assert len(scheduler.queue) == 1


def test_recurring_job_ack_keeps_job_for_next_minute(tmp_path):
    scheduler = _scheduler(tmp_path)
    job = scheduler.schedule("* * * * *", "tick", durable=False)
    scheduler.poll_due_jobs(MONDAY_0930)

    delivered = scheduler.consume_queue()
    assert [item.id for item in delivered] == [job.id]
    scheduler.acknowledge(delivered)

    assert job.id in scheduler.jobs
    assert scheduler.jobs[job.id].pending_delivery is False
    assert scheduler.poll_due_jobs(datetime(2026, 10, 19, 9, 31))


def test_one_shot_job_is_removed_after_ack(tmp_path):
    scheduler = _scheduler(tmp_path)
    job = scheduler.schedule("* * * * *", "once", recurring=False, durable=False)
    scheduler.poll_due_jobs(MONDAY_0930)

    scheduler.acknowledge(scheduler.consume_queue())
    assert job.id not in scheduler.jobs


def test_cancel_job_updates_queue_and_file(tmp_path):
    scheduler = _scheduler(tmp_path)
    job = scheduler.schedule("* * * * *", "tick", durable=True)
    scheduler.poll_due_jobs(MONDAY_0930)

    assert "Cancelled" in scheduler.cancel(job.id)
    assert job.id not in scheduler.jobs
    assert scheduler.queue == []
    assert job.id not in (tmp_path / ".scheduled_tasks.json").read_text(encoding="utf-8")
    assert "not found" in scheduler.cancel(job.id)


def test_pending_delivery_is_restored_on_reload(tmp_path):
    scheduler = _scheduler(tmp_path)
    job = scheduler.schedule("* * * * *", "tick", durable=True)
    scheduler.poll_due_jobs(MONDAY_0930)
    assert scheduler.jobs[job.id].pending_delivery is True

    reloaded = CronScheduler(tmp_path / ".scheduled_tasks.json", autoload=True)
    assert reloaded.jobs[job.id].pending_delivery is True
    assert [item.id for item in reloaded.queue] == [job.id]


def test_scheduler_thread_starts_and_stops(tmp_path):
    scheduler = _scheduler(tmp_path, interval=0.05)
    scheduler.start()
    assert scheduler.is_running()
    scheduler.stop()
    assert not scheduler.is_running()
