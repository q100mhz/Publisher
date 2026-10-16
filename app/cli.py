"""Консольные команды.

  python -m app.cli set-password <логин>   — создать пользователя или сменить ему пароль
  python -m app.cli users                  — список пользователей
"""
import getpass
import sys

from sqlalchemy import select

from .auth import hash_password
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
            print(f"Пароль пользователя «{login}» изменён")
        else:
            db.add(User(login=login, password_hash=hash_password(password)))
            print(f"Пользователь «{login}» создан")


def main() -> None:
    Base.metadata.create_all(engine)
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "set-password":
        set_password(args[1])
    elif args == ["users"]:
        with session_scope() as db:
            for user in db.scalars(select(User)):
                print(user.login)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
