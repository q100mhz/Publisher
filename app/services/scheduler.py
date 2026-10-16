"""Рубильник автопарсинга — свой у каждого пользователя.

При включении сразу обходит все активные сайты пользователя, дальше — каждые N часов (по умолчанию 4).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from ..config import AUTO_PARSE_INTERVAL_HOURS
from ..db import session_scope
from ..models import User, UserSetting, get_setting, set_setting
from .parser import run_all

# Часовой пояс — системный (в контейнере его задаёт TZ=Europe/Moscow), все даты — с явным поясом
scheduler = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600})


def _now() -> datetime:
    return datetime.now().astimezone()


def _job_id(user_id: int) -> str:
    return f"auto_parse_{user_id}"


def _job(user_id: int) -> None:
    with session_scope() as db:
        user = db.get(User, user_id)
        if not user or not user.is_active or get_setting(db, user_id, "auto_enabled") != "1":
            # Пользователя отключили (или рубильник выключили из терминала) — снимаем задачу
            if scheduler.get_job(_job_id(user_id)):
                scheduler.remove_job(_job_id(user_id))
            return
        set_setting(db, user_id, "auto_last_run", datetime.now().isoformat(timespec="seconds"))
    run_all(user_id, "auto")


def _schedule(user_id: int, first_run: datetime) -> None:
    scheduler.add_job(_job, "interval", args=[user_id], hours=AUTO_PARSE_INTERVAL_HOURS, id=_job_id(user_id),
                      next_run_time=first_run, replace_existing=True)


def start() -> None:
    scheduler.start()
    with session_scope() as db:
        enabled = db.scalars(
            select(UserSetting.user_id).join(User, User.id == UserSetting.user_id)
            .where(UserSetting.key == "auto_enabled", UserSetting.value == "1", User.is_active.is_(True))
        ).all()
        last_runs = {uid: get_setting(db, uid, "auto_last_run") for uid in enabled}
    for user_id in enabled:
        # После перезапуска сервера продолжаем прежний ритм, а не парсим всё заново
        next_run = _now() + timedelta(minutes=1)
        if last_runs[user_id]:
            planned = datetime.fromisoformat(last_runs[user_id]).astimezone() + timedelta(hours=AUTO_PARSE_INTERVAL_HOURS)
            next_run = max(planned, next_run)
        _schedule(user_id, next_run)


def shutdown() -> None:
    scheduler.shutdown(wait=False)


def is_enabled(user_id: int) -> bool:
    return scheduler.get_job(_job_id(user_id)) is not None


def next_run_time(user_id: int) -> datetime | None:
    job = scheduler.get_job(_job_id(user_id))
    return job.next_run_time.astimezone().replace(tzinfo=None) if job and job.next_run_time else None


def set_enabled(user_id: int, enabled: bool) -> None:
    with session_scope() as db:
        set_setting(db, user_id, "auto_enabled", "1" if enabled else "0")
    if enabled:
        _schedule(user_id, _now())  # первый обход — сразу
    elif scheduler.get_job(_job_id(user_id)):
        scheduler.remove_job(_job_id(user_id))
