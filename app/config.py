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

# Учётная запись создаётся при первом запуске, если пользователей в базе ещё нет
ADMIN_LOGIN = os.getenv("ADMIN_LOGIN", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "./data/media")).resolve()
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

# DeepSeek (OpenAI-совместимый API)
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# Парсер
AUTO_PARSE_INTERVAL_HOURS = float(os.getenv("AUTO_PARSE_INTERVAL_HOURS", "4"))
PARSER_DELAY_MIN = float(os.getenv("PARSER_DELAY_MIN", "2"))  # секунды между запросами к одному сайту
PARSER_DELAY_MAX = float(os.getenv("PARSER_DELAY_MAX", "5"))
PARSER_FIRST_RUN_LIMIT = int(os.getenv("PARSER_FIRST_RUN_LIMIT", "5"))
PARSER_MAX_PER_RUN = int(os.getenv("PARSER_MAX_PER_RUN", "30"))
PARSER_USE_BROWSER = _bool("PARSER_USE_BROWSER", True)
