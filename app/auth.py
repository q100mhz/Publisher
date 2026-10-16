"""Вход по логину и паролю. Самостоятельной регистрации нет: пользователей создаёт администратор."""
import time
from collections import defaultdict

import bcrypt
from fastapi import HTTPException, Request
from sqlalchemy import func, select

from .config import ADMIN_LOGIN, ADMIN_PASSWORD
from .db import SessionLocal, session_scope
from .models import Prompt, User, get_setting

MAX_FAILS = 5          # неудачных попыток подряд
LOCK_SECONDS = 15 * 60  # на сколько блокируется IP после MAX_FAILS
_fails: dict[str, list[float]] = defaultdict(list)

DEFAULT_PROMPT_NAME = "Основной: SEO-статья"


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
    """Стартовый набор нового кабинета: основной промпт."""
    from .services.generator import DEFAULT_PROMPT

    if not db.scalar(select(Prompt.id).where(Prompt.user_id == user.id).limit(1)):
        db.add(Prompt(user_id=user.id, name=DEFAULT_PROMPT_NAME, text=DEFAULT_PROMPT, is_default=True))


def create_user(db, login: str, password: str, is_admin: bool = False) -> User:
    user = User(login=login, password_hash=hash_password(password), is_admin=is_admin)
    db.add(user)
    db.flush()
    seed_user(db, user)
    return user


def ensure_admin() -> None:
    """При первом запуске создаёт администратора из ADMIN_LOGIN / ADMIN_PASSWORD."""
    with session_scope() as db:
        if db.scalar(select(func.count(User.id))):
            for user in db.scalars(select(User)):
                seed_user(db, user)
            return
        if not (ADMIN_LOGIN and ADMIN_PASSWORD):
            print("[auth] Пользователь не создан: задайте ADMIN_LOGIN и ADMIN_PASSWORD в .env "
                  "или выполните: python -m app.cli set-password <логин>")
            return
        create_user(db, ADMIN_LOGIN, ADMIN_PASSWORD, is_admin=True)
        print(f"[auth] Создан администратор «{ADMIN_LOGIN}». Пароль из .env можно удалить.")


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


def require_admin(request: Request) -> int:
    user_id = require_user(request)
    if not request.state.user.is_admin:
        raise HTTPException(403, "Раздел доступен только администратору")
    return user_id
