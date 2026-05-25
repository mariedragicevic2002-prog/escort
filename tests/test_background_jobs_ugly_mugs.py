from __future__ import annotations

import sys
import types

from services import background_jobs
from services.ugly_mugs_sync_service import UglyMugsSyncSchedule


class _FakeScheduler:
    def __init__(self, daemon: bool = True):
        self.daemon = daemon
        self.jobs: list[dict] = []
        self.started = False

    def add_job(self, func, trigger, **kwargs):
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})

    def start(self):
        self.started = True

    def shutdown(self):
        self.started = False


class _FakeCronTrigger:
    def __init__(self, hour, minute, timezone=None):
        self.hour = hour
        self.minute = minute
        self.timezone = timezone


def test_init_scheduler_registers_ugly_mugs_job(monkeypatch):
    monkeypatch.setattr(background_jobs, "_scheduler", None)
    monkeypatch.setattr(background_jobs, "HAS_APSCHEDULER", True)
    monkeypatch.setattr(background_jobs, "BackgroundScheduler", _FakeScheduler)
    monkeypatch.setattr(background_jobs, "CronTrigger", _FakeCronTrigger)
    monkeypatch.setattr(background_jobs, "IntervalTrigger", lambda **kwargs: ("interval", kwargs))
    monkeypatch.setitem(
        sys.modules,
        "config",
        types.ModuleType("config"),
    )
    fake_config = sys.modules["config"]
    fake_config.AUTO_BACKUP_ENABLED = False
    fake_config.BACKUP_HOUR_UTC = 3
    fake_config.BACKUP_MINUTE_UTC = 15
    fake_config.BACKUP_DIR = "backups"
    monkeypatch.setattr(
        "services.ugly_mugs_sync_service.get_ugly_mugs_sync_schedule",
        lambda: UglyMugsSyncSchedule(hour=9, minute=0, timezone="Australia/Adelaide", enabled=True),
    )

    background_jobs.init_scheduler(state_manager=object(), db_service=object())
    scheduler = background_jobs.get_scheduler()

    assert scheduler is not None
    assert scheduler.started is True
    job_ids = {job["id"] for job in scheduler.jobs}
    assert "ugly_mugs_sync_daily" in job_ids

    ugly_job = next(job for job in scheduler.jobs if job["id"] == "ugly_mugs_sync_daily")
    assert ugly_job["name"] == "Daily ugly-mugs safety watchlist sync"
    assert isinstance(ugly_job["trigger"], _FakeCronTrigger)
    assert ugly_job["trigger"].hour == 9
    assert ugly_job["trigger"].minute == 0
    assert ugly_job["func"] == background_jobs.ugly_mugs_sync_job

    background_jobs.shutdown_scheduler()


def test_ugly_mugs_sync_job_skipped(monkeypatch):
    monkeypatch.setattr(
        "services.ugly_mugs_sync_service.run_ugly_mugs_sync",
        lambda: {"status": "skipped", "reason": "disabled", "inserted": 0},
    )

    background_jobs.ugly_mugs_sync_job()
