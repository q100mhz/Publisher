"""Шифрование API-ключей пользователей в базе.

Ключ шифрования выводится из SECRET_KEY: без файла .env содержимое базы не позволит
прочитать ключи. Если SECRET_KEY сменить, сохранённые ключи придётся ввести заново.
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .config import SECRET_KEY

_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(("publisher-api-keys:" + SECRET_KEY).encode()).digest()))


def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def decrypt(token: str) -> str | None:
    """None — ключ был зашифрован другим SECRET_KEY и прочитать его нельзя."""
    try:
        return _fernet.decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def mask(value: str) -> str:
    return f"{value[:3]}…{value[-4:]}" if len(value) > 10 else "…"
