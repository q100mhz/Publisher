"""Вход по заранее заданному логину и паролю. Регистрации нет."""
import time
from collections import defaultdict

import bcrypt
from fastapi import HTTPException, Request
from sqlalchemy import select

from .config import ADMIN_LOGIN, ADMIN_PASSWORD
from .db import session_scope
from .models import User

MAX_FAILS = 5          # неудачных попыток подряд
LOCK_SECONDS = 15 * 60  # на сколько блокируется IP после MAX_FAILS
_fails: dict[str, list[float]] = defaultdict(list)


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


def ensure_admin() -> None:
    """При первом запуске создаёт пользователя из ADMIN_LOGIN / ADMIN_PASSWORD."""
    with session_scope() as db:
        if db.scalar(select(User).limit(1)):
            return
        if not (ADMIN_LOGIN and ADMIN_PASSWORD):
            print("[auth] Пользователь не создан: задайте ADMIN_LOGIN и ADMIN_PASSWORD в .env "
                  "или выполните: python -m app.cli set-password <логин>")
            return
        db.add(User(login=ADMIN_LOGIN, password_hash=hash_password(ADMIN_PASSWORD)))
        print(f"[auth] Создан пользователь «{ADMIN_LOGIN}». Пароль из .env можно удалить.")


class LoginRequired(Exception):
    pass


def require_user(request: Request) -> int:
    user_id = request.session.get("user_id")
    if not user_id:
        if request.url.path.endswith((".json",)) or request.headers.get("x-requested-with"):
            raise HTTPException(401, "Требуется вход")
        raise LoginRequired()
    return user_id
