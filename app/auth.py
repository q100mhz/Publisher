"""Вход по логину и паролю. Регистрации нет: пользователей создаёт владелец сервера из терминала."""
import time
from collections import defaultdict

import bcrypt
from fastapi import HTTPException, Request
from sqlalchemy import select

from .db import SessionLocal, session_scope
from .models import Prompt, User, get_setting

MAX_FAILS = 5          # неудачных попыток подряд
LOCK_SECONDS = 15 * 60  # на сколько блокируется IP после MAX_FAILS
_fails: dict[str, list[float]] = defaultdict(list)

DEFAULT_PROMPT_NAME = "Основной: SEO-статья"
DEFAULT_IMAGE_PROMPT_NAME = "Основной: иллюстрация к статье"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "?"


def is_locked(ip: str) -> int:
    """Сколько секунд осталось до разблокировки (0 — не заблокирован)."""
    attempts = [t for t in _fails[ip] if time.time() - t < LOCK_SECONDS]
    _fails[ip] = attempts
    if len(attempts) >= MAX_FAILS:
        return int(LOCK_SECONDS - (time.time() - attempts[0]))
    return 0


def register_fail(ip: str) -> None:
    _fails[ip].append(time.time())


def reset_fails(ip: str) -> None:
    _fails.pop(ip, None)


def seed_user(db, user: User) -> None:
    """Стартовый набор кабинета: основной промпт для статьи и основной промпт для картинки."""
    from .services.generator import DEFAULT_IMAGE_PROMPT, DEFAULT_PROMPT

    for kind, name, text_ in (("text", DEFAULT_PROMPT_NAME, DEFAULT_PROMPT),
                              ("image", DEFAULT_IMAGE_PROMPT_NAME, DEFAULT_IMAGE_PROMPT)):
        if not db.scalar(select(Prompt.id).where(Prompt.user_id == user.id, Prompt.kind == kind).limit(1)):
            db.add(Prompt(user_id=user.id, kind=kind, name=name, text=text_, is_default=True))


def create_user(db, login: str, password: str) -> User:
    user = User(login=login, password_hash=hash_password(password))
    db.add(user)
    db.flush()
    seed_user(db, user)
    return user


def ensure_users_ready() -> None:
    """У каждого пользователя должны быть стартовые промпты. Подсказывает, как создать первого пользователя."""
    with session_scope() as db:
        users = db.scalars(select(User)).all()
        for user in users:
            seed_user(db, user)
    if not users:
        print("[auth] Пользователей нет. Создайте: docker compose exec app python -m app.cli add-user <логин>")


class LoginRequired(Exception):
    pass


def _deny(request: Request):
    request.session.clear()
    if request.headers.get("x-requested-with"):
        raise HTTPException(401, "Требуется вход")
    raise LoginRequired()


def require_user(request: Request) -> int:
    """Проверяет вход и возвращает id пользователя. Пользователь и его тема кладутся в request.state."""
    user_id = request.session.get("user_id")
    if not user_id:
        _deny(request)
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user or not user.is_active:
            _deny(request)
        request.state.user = user
        request.state.theme = get_setting(db, user.id, "theme", "auto")
    return user.id
