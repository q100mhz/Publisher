"""Защита от запросов во внутреннюю сеть (SSRF) и прочая гигиена ввода.

Любой пользователь может указать адрес сайта-источника, адрес API нейросети или вебхук Битрикс24.
Без проверки он мог бы заставить сервер обратиться к внутренним адресам — базе данных, служебным
портам, метаданным облака — и увидеть ответ. Поэтому каждый внешний адрес перед запросом
разрешается в IP и пропускается, только если все его IP — публичные адреса интернета.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import time
from urllib.parse import urlparse

# Только для локальной разработки и тестов: разрешить обращения к 127.0.0.1 и т. п.
ALLOW_PRIVATE = os.getenv("ALLOW_PRIVATE_URLS", "").strip().lower() in ("1", "true", "yes")
_cache: dict[tuple[str, int], tuple[bool, str, float]] = {}
CACHE_SECONDS = 300


class UnsafeURL(ValueError):
    pass


def _ip_is_public(value: str) -> bool:
    ip = ipaddress.ip_address(value.split("%")[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_url(url: str) -> str:
    """Возвращает адрес, если он безопасен, иначе бросает UnsafeURL с понятным текстом."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURL("Адрес должен начинаться с http:// или https://")
    if not parsed.hostname:
        raise UnsafeURL("В адресе нет имени сайта")
    if parsed.username or parsed.password:
        raise UnsafeURL("Адреса с логином и паролем внутри не поддерживаются")
    if ALLOW_PRIVATE:
        return url
    host = parsed.hostname.lower()
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise UnsafeURL("В адресе неверный номер порта")

    cached = _cache.get((host, port))
    if cached and time.time() - cached[2] < CACHE_SECONDS:
        ok, reason = cached[0], cached[1]
    else:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = {info[4][0] for info in infos}
            ok = bool(addresses) and all(_ip_is_public(a) for a in addresses)
            reason = "" if ok else "Адрес ведёт во внутреннюю сеть — такие адреса запрещены"
        except socket.gaierror:
            ok, reason = False, f"Сайт «{host}» не найден (ошибка DNS)"
        except ValueError:
            ok, reason = False, "Некорректный адрес"
        _cache[(host, port)] = (ok, reason, time.time())
    if not ok:
        raise UnsafeURL(reason)
    return url


def is_safe_url(url: str) -> bool:
    try:
        check_url(url)
        return True
    except UnsafeURL:
        return False


def escape_like(value: str) -> str:
    """Экранирует % и _ для поиска через LIKE, чтобы они искались как обычные символы."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(value: str | None, limit: int) -> str:
    """Убирает управляющие символы (кроме переводов строк и табуляции) и ограничивает длину."""
    return _CONTROL.sub("", value or "")[:limit]


def safe_filename(name: str, default: str = "file") -> str:
    """Имя файла без путей, кавычек и управляющих символов — для заголовка скачивания."""
    name = os.path.basename((name or "").replace("\\", "/"))
    name = re.sub(r'[\x00-\x1f\x7f"<>:|?*;]', "_", name).strip(" .")
    return name[:150] or default
