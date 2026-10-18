"""Настройки приложения. Всё берётся из переменных окружения (файл .env)."""
import os
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# Путь, по которому открывается сайт: https://itb2bhost.ru/publisher/
BASE_PATH = "/" + os.getenv("BASE_PATH", "/publisher").strip("/")
if BASE_PATH == "/":
    BASE_PATH = ""

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/publisher.db")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
COOKIE_SECURE = _bool("COOKIE_SECURE", False)

MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "./data/media")).resolve()
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

# Базовый ключ DeepSeek сервиса. Им пользуются только те, кому это разрешено в админке,
# и только пока у пользователя нет своего ключа DeepSeek.
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# Регистрация по e-mail на странице входа
REGISTRATION_ENABLED = _bool("REGISTRATION_ENABLED", True)

# Админка: открывается на своём адресе (publisher-admin.itb2bhost.ru), nginx отдаёт её из /admin/.
# ADMIN_HOST — если задан, админка отвечает только на этом адресе.
ADMIN_HOST = os.getenv("ADMIN_HOST", "").strip().lower()
# Префикс ссылок внутри админки: "" — когда у неё свой домен, "/admin" — для локальной разработки
ADMIN_PREFIX = "/" + os.getenv("ADMIN_PREFIX", "").strip("/") if os.getenv("ADMIN_PREFIX", "").strip("/") else ""

# Парсер
AUTO_PARSE_INTERVAL_HOURS = float(os.getenv("AUTO_PARSE_INTERVAL_HOURS", "4"))
PARSER_DELAY_MIN = float(os.getenv("PARSER_DELAY_MIN", "2"))  # секунды между запросами к одному сайту
PARSER_DELAY_MAX = float(os.getenv("PARSER_DELAY_MAX", "5"))
PARSER_FIRST_RUN_LIMIT = int(os.getenv("PARSER_FIRST_RUN_LIMIT", "5"))
PARSER_MAX_PER_RUN = int(os.getenv("PARSER_MAX_PER_RUN", "30"))
PARSER_USE_BROWSER = _bool("PARSER_USE_BROWSER", True)
