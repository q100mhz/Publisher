"""Вход по логину и паролю. Регистрации нет: пользователей создаёт владелец сервера из терминала."""
import re
import time
from collections import defaultdict

import bcrypt
from fastapi import HTTPException, Request
from sqlalchemy import select

from .db import SessionLocal, session_scope
from .models import Prompt, User, get_setting, now

MAX_FAILS = 5          # неудачных попыток подряд
LOCK_SECONDS = 15 * 60  # на сколько блокируется IP после MAX_FAILS
_fails: dict[str, list[float]] = defaultdict(list)

DEFAULT_PROMPT_NAME = "Основной: SEO-статья"
DEFAULT_IMAGE_PROMPT_NAME = "Основной: иллюстрация к статье"
DEFAULT_TITLE_PROMPT_NAME = "Основной: новый заголовок"


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
    """Стартовый набор кабинета: основные промпты для статьи, заголовка и картинки."""
    from .services.generator import DEFAULT_IMAGE_PROMPT, DEFAULT_PROMPT, DEFAULT_TITLE_PROMPT

    for kind, name, text_ in (("text", DEFAULT_PROMPT_NAME, DEFAULT_PROMPT),
                              ("title", DEFAULT_TITLE_PROMPT_NAME, DEFAULT_TITLE_PROMPT),
                              ("image", DEFAULT_IMAGE_PROMPT_NAME, DEFAULT_IMAGE_PROMPT)):
        if not db.scalar(select(Prompt.id).where(Prompt.user_id == user.id, Prompt.kind == kind).limit(1)):
            db.add(Prompt(user_id=user.id, kind=kind, name=name, text=text_, is_default=True))


def create_user(db, login: str, password: str, via: str = "cli") -> User:
    user = User(login=login, password_hash=hash_password(password), registered_via=via)
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
        print("[auth] Пользователей нет. Зарегистрируйтесь на сайте или: python -m app.cli add-user <e-mail>")
    for user in users:
        if not normalize_email(user.login):
            print(f"[auth] «{user.login}» — не e-mail, войти не сможет. "
                  f"Замените: python -m app.cli set-email {user.login} <e-mail>")


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
        # Заблокирован или администратор завершил все его сессии — выходим
        if not user or not user.is_active or request.session.get("sv", 1) != user.session_version:
            _deny(request)
        if not user.last_seen_at or (now() - user.last_seen_at).total_seconds() > 300:
            user.last_seen_at = now()
            db.commit()
        request.state.user = user
        request.state.theme = get_setting(db, user.id, "theme", "auto")
    return user.id


def start_session(request: Request, user: User) -> None:
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["login"] = user.login
    request.session["sv"] = user.session_version


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")


def normalize_email(value: str) -> str | None:
    """E-mail в нижнем регистре или None, если это не e-mail. Пользователи входят только по e-mail."""
    value = (value or "").strip().lower()
    return value if EMAIL_RE.match(value) and len(value) <= 100 else None


_signups: dict[str, list[float]] = defaultdict(list)
SIGNUPS_PER_HOUR = 5


def signup_allowed(ip: str) -> bool:
    """Не больше 5 регистраций в час с одного IP — защита от автоматической регистрации."""
    recent = [t for t in _signups[ip] if time.time() - t < 3600]
    _signups[ip] = recent
    return len(recent) < SIGNUPS_PER_HOUR


def register_signup(ip: str) -> None:
    _signups[ip].append(time.time())
