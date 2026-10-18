"""Управление пользователями и администраторами из терминала сервера.

Пользователи входят только по e-mail — он же их логин.

  python -m app.cli add-user <e-mail>             — создать пользователя (пароль спросит скрыто)
  python -m app.cli set-password <e-mail>         — сменить пароль
  python -m app.cli set-email <старый> <e-mail>   — заменить логин или e-mail пользователя на новый e-mail
  python -m app.cli disable-user <e-mail>         — запретить вход (данные сохраняются)
  python -m app.cli enable-user <e-mail>          — снова разрешить вход
  python -m app.cli users                         — список пользователей

  python -m app.cli add-admin <логин>      — создать администратора (вход в publisher-admin.itb2bhost.ru)
  python -m app.cli admin-password <логин> — сменить пароль администратора
  python -m app.cli admins                 — список администраторов

У каждого пользователя свой отдельный кабинет: источники, реестр, промпты и ключи не пересекаются.
"""
import getpass
import re
import sys

from sqlalchemy import func, select

from . import migrate
from .auth import create_user, hash_password, normalize_email
from .db import Base, engine, session_scope
from .models import Admin, News, Source, User

LOGIN_RE = re.compile(r"^[A-Za-z0-9._@-]{3,50}$")  # только для логинов администраторов


def ask_password() -> str:
    password = getpass.getpass("Пароль: ")
    if len(password) < 8:
        sys.exit("Пароль должен быть не короче 8 символов")
    if password != getpass.getpass("Повторите пароль: "):
        sys.exit("Пароли не совпадают")
    return password


def find(db, login: str) -> User:
    user = db.scalar(select(User).where(func.lower(User.login) == login.strip().lower()))
    if not user:
        sys.exit(f"Пользователь «{login}» не найден. Список: python -m app.cli users")
    return user


def require_email(value: str) -> str:
    email = normalize_email(value)
    if not email:
        sys.exit(f"«{value}» — не e-mail. Пользователи создаются и входят только по e-mail.")
    return email


def add_user(value: str) -> None:
    email = require_email(value)
    with session_scope() as db:
        if db.scalar(select(User.id).where(func.lower(User.login) == email)):
            sys.exit(f"Пользователь «{email}» уже есть. Сменить пароль: python -m app.cli set-password {email}")
    password = ask_password()
    with session_scope() as db:
        create_user(db, email, password)
    print(f"Пользователь «{email}» создан. У него свой пустой кабинет.")


def set_email(old: str, new: str) -> None:
    email = require_email(new)
    with session_scope() as db:
        user = find(db, old)
        if db.scalar(select(User.id).where(func.lower(User.login) == email, User.id != user.id)):
            sys.exit(f"E-mail «{email}» уже занят другим пользователем")
        previous, user.login = user.login, email
    print(f"«{previous}» → «{email}». Теперь вход по этому e-mail, данные кабинета сохранены.")


def set_password(login: str) -> None:
    with session_scope() as db:
        find(db, login)
    password = ask_password()
    with session_scope() as db:
        find(db, login).password_hash = hash_password(password)
    print(f"Пароль пользователя «{login}» изменён")


def set_active(login: str, active: bool) -> None:
    with session_scope() as db:
        user = find(db, login)
        user.is_active = active
        user_id = user.id
    if not active:
        # Рубильник автопарсинга отключённого пользователя тоже выключаем
        from .models import set_setting
        with session_scope() as db:
            set_setting(db, user_id, "auto_enabled", "0")
    print(f"«{login}»: " + ("вход разрешён" if active else "вход запрещён, данные сохранены"))


def list_users() -> None:
    with session_scope() as db:
        news = dict(db.execute(select(News.user_id, func.count(News.id)).group_by(News.user_id)).all())
        sources = dict(db.execute(select(Source.user_id, func.count(Source.id)).group_by(Source.user_id)).all())
        users = db.scalars(select(User).order_by(User.id)).all()
        if not users:
            print("Пользователей нет. Создайте: python -m app.cli add-user <e-mail>")
        for u in users:
            state = "" if u.is_active else "  [вход запрещён]"
            if not normalize_email(u.login):
                state += "  [не e-mail — войти не сможет: python -m app.cli set-email " + u.login + " <e-mail>]"
            print(f"{u.login:<30} источников: {sources.get(u.id, 0):<4} новостей: {news.get(u.id, 0)}{state}")


def add_admin(login: str) -> None:
    if not LOGIN_RE.match(login):
        sys.exit("Логин: 3–50 символов — латиница, цифры и . _ @ -")
    with session_scope() as db:
        if db.scalar(select(Admin.id).where(Admin.login == login)):
            sys.exit(f"Администратор «{login}» уже есть. Сменить пароль: python -m app.cli admin-password {login}")
    password = ask_password()
    with session_scope() as db:
        db.add(Admin(login=login, password_hash=hash_password(password)))
    print(f"Администратор «{login}» создан. Вход: https://publisher-admin.itb2bhost.ru")


def admin_password(login: str) -> None:
    with session_scope() as db:
        if not db.scalar(select(Admin.id).where(Admin.login == login)):
            sys.exit(f"Администратор «{login}» не найден")
    password = ask_password()
    with session_scope() as db:
        db.scalar(select(Admin).where(Admin.login == login)).password_hash = hash_password(password)
    print(f"Пароль администратора «{login}» изменён, его старые сессии завершены")


def main() -> None:
    Base.metadata.create_all(engine)
    migrate.run()
    args = sys.argv[1:]
    commands = {"add-user": add_user, "set-password": set_password,
                "disable-user": lambda login: set_active(login, False),
                "enable-user": lambda login: set_active(login, True),
                "add-admin": add_admin, "admin-password": admin_password}
    if len(args) == 2 and args[0] in commands:
        commands[args[0]](args[1])
    elif len(args) == 3 and args[0] == "set-email":
        set_email(args[1], args[2])
    elif args == ["users"]:
        list_users()
    elif args == ["admins"]:
        with session_scope() as db:
            for a in db.scalars(select(Admin).order_by(Admin.id)):
                print(a.login, f"(последний вход: {a.last_login_at:%d.%m.%Y %H:%M})" if a.last_login_at else "")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
