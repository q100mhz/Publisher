"""Консольные команды.

  python -m app.cli set-password <логин>   — создать пользователя или сменить ему пароль
                                            (первый созданный пользователь становится администратором)
  python -m app.cli make-admin <логин>     — выдать права администратора
  python -m app.cli users                  — список пользователей
"""
import getpass
import sys

from sqlalchemy import select

from . import migrate
from .auth import create_user, hash_password
from .db import Base, engine, session_scope
from .models import User


def set_password(login: str) -> None:
    password = getpass.getpass("Новый пароль: ")
    if len(password) < 8:
        sys.exit("Пароль должен быть не короче 8 символов")
    if password != getpass.getpass("Повторите пароль: "):
        sys.exit("Пароли не совпадают")
    with session_scope() as db:
        user = db.scalar(select(User).where(User.login == login))
        if user:
            user.password_hash = hash_password(password)
            user.is_active = True
            print(f"Пароль пользователя «{login}» изменён")
        else:
            first_admin = not db.scalar(select(User.id).where(User.is_admin.is_(True)).limit(1))
            create_user(db, login, password, is_admin=first_admin)
            print(f"Пользователь «{login}» создан" + (" (администратор)" if first_admin else ""))


def make_admin(login: str) -> None:
    with session_scope() as db:
        user = db.scalar(select(User).where(User.login == login))
        if not user:
            sys.exit(f"Пользователь «{login}» не найден")
        user.is_admin = True
        print(f"«{login}» теперь администратор")


def main() -> None:
    Base.metadata.create_all(engine)
    migrate.run()
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "set-password":
        set_password(args[1])
    elif len(args) == 2 and args[0] == "make-admin":
        make_admin(args[1])
    elif args == ["users"]:
        with session_scope() as db:
            for user in db.scalars(select(User).order_by(User.id)):
                flags = [f for f, on in (("администратор", user.is_admin), ("отключён", not user.is_active)) if on]
                print(user.login + (f"  ({', '.join(flags)})" if flags else ""))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
