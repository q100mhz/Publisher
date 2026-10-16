"""Управление пользователями — только из терминала сервера. На сайте пользователя завести нельзя.

  python -m app.cli add-user <логин>       — создать пользователя (пароль спросит скрыто)
  python -m app.cli set-password <логин>   — сменить пароль
  python -m app.cli disable-user <логин>   — запретить вход (данные сохраняются)
  python -m app.cli enable-user <логин>    — снова разрешить вход
  python -m app.cli users                  — список пользователей

У каждого пользователя свой отдельный кабинет: источники, реестр, промпты и ключи не пересекаются.
"""
import getpass
import re
import sys

from sqlalchemy import func, select

from . import migrate
from .auth import create_user, hash_password
from .db import Base, engine, session_scope
from .models import News, Source, User

LOGIN_RE = re.compile(r"^[A-Za-z0-9._@-]{3,50}$")


def ask_password() -> str:
    password = getpass.getpass("Пароль: ")
    if len(password) < 8:
        sys.exit("Пароль должен быть не короче 8 символов")
    if password != getpass.getpass("Повторите пароль: "):
        sys.exit("Пароли не совпадают")
    return password


def find(db, login: str) -> User:
    user = db.scalar(select(User).where(User.login == login))
    if not user:
        sys.exit(f"Пользователь «{login}» не найден. Список: python -m app.cli users")
    return user


def add_user(login: str) -> None:
    if not LOGIN_RE.match(login):
        sys.exit("Логин: 3–50 символов — латиница, цифры и . _ @ -")
    with session_scope() as db:
        if db.scalar(select(User.id).where(User.login == login)):
            sys.exit(f"Пользователь «{login}» уже есть. Сменить пароль: python -m app.cli set-password {login}")
    password = ask_password()
    with session_scope() as db:
        create_user(db, login, password)
    print(f"Пользователь «{login}» создан. У него свой пустой кабинет.")


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
            print("Пользователей нет. Создайте: python -m app.cli add-user <логин>")
        for u in users:
            state = "" if u.is_active else "  [вход запрещён]"
            print(f"{u.login:<25} источников: {sources.get(u.id, 0):<4} новостей: {news.get(u.id, 0)}{state}")


def main() -> None:
    Base.metadata.create_all(engine)
    migrate.run()
    args = sys.argv[1:]
    commands = {"add-user": add_user, "set-password": set_password,
                "disable-user": lambda login: set_active(login, False),
                "enable-user": lambda login: set_active(login, True)}
    if len(args) == 2 and args[0] in commands:
        commands[args[0]](args[1])
    elif args == ["users"]:
        list_users()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
