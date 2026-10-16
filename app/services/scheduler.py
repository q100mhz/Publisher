"""Рубильник автопарсинга: при включении парсит сразу, дальше — каждые N часов (по умолчанию 4)."""
from __future__ import annotations

from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from ..config import AUTO_PARSE_INTERVAL_HOURS
from ..db import session_scope
from ..models import get_setting, set_setting
from .parser import run_all

JOB_ID = "auto_parse"
# Часовой пояс — системный (в контейнере его задаёт TZ=Europe/Moscow), все даты — с явным поясом
scheduler = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600})


def _now() -> datetime:
    return datetime.now().astimezone()


def _job() -> None:
    with session_scope() as db:
        set_setting(db, "auto_last_run", datetime.now().isoformat(timespec="seconds"))
    run_all("auto")


def _schedule(first_run: datetime) -> None:
    scheduler.add_job(_job, "interval", hours=AUTO_PARSE_INTERVAL_HOURS, id=JOB_ID,
                      next_run_time=first_run, replace_existing=True)


def start() -> None:
    scheduler.start()
    with session_scope() as db:
        enabled = get_setting(db, "auto_enabled") == "1"
        last = get_setting(db, "auto_last_run")
    if enabled:
        # После перезапуска сервера продолжаем прежний ритм, а не парсим всё заново
        next_run = _now() + timedelta(minutes=1)
        if last:
            planned = datetime.fromisoformat(last).astimezone() + timedelta(hours=AUTO_PARSE_INTERVAL_HOURS)
            next_run = max(planned, next_run)
        _schedule(next_run)


def shutdown() -> None:
    scheduler.shutdown(wait=False)


def is_enabled() -> bool:
    return scheduler.get_job(JOB_ID) is not None


def next_run_time() -> datetime | None:
    job = scheduler.get_job(JOB_ID)
    return job.next_run_time.astimezone().replace(tzinfo=None) if job and job.next_run_time else None


def set_enabled(enabled: bool) -> None:
    with session_scope() as db:
        set_setting(db, "auto_enabled", "1" if enabled else "0")
    if enabled:
        _schedule(_now())  # первый обход — сразу
    elif scheduler.get_job(JOB_ID):
        scheduler.remove_job(JOB_ID)
