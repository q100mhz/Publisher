"""Обновление структуры уже существующей базы (без потери данных).

Выполняется при каждом запуске и ничего не делает, если всё уже обновлено.
Новая база создаётся сразу правильной через create_all — здесь только «догоняем» старые.

Переход к многопользовательскому режиму:
  * в таблицы добавляется владелец (user_id);
  * всё, что было создано раньше, закрепляется за первым пользователем (владельцем сервера);
  * общие настройки (рубильник, модель DeepSeek) переносятся в его личные настройки.
"""
from sqlalchemy import inspect, text

from .db import engine

NEW_COLUMNS = [
    ("users", "is_active", "BOOLEAN NOT NULL DEFAULT TRUE"),
    ("sources", "user_id", "INTEGER"),
    ("news", "user_id", "INTEGER"),
    ("news", "source_label", "VARCHAR(200)"),
    ("prompts", "user_id", "INTEGER"),
    ("parser_runs", "user_id", "INTEGER"),
    ("prompts", "kind", "VARCHAR(20) NOT NULL DEFAULT 'text'"),
    ("news_images", "kind", "VARCHAR(20) NOT NULL DEFAULT 'original'"),
    ("news", "img_state", "VARCHAR(20) NOT NULL DEFAULT 'idle'"),
    ("news", "img_error", "TEXT NOT NULL DEFAULT ''"),
    ("news", "new_title", "VARCHAR(1000)"),
    ("news", "meta_html", "TEXT NOT NULL DEFAULT ''"),
    ("news_versions", "title", "VARCHAR(1000)"),
    ("news_versions", "meta_html", "TEXT NOT NULL DEFAULT ''"),
    ("users", "registered_via", "VARCHAR(20) NOT NULL DEFAULT 'cli'"),
    ("users", "last_login_at", "TIMESTAMP"),
    ("users", "last_seen_at", "TIMESTAMP"),
    ("users", "session_version", "INTEGER NOT NULL DEFAULT 1"),
    ("users", "base_ai_allowed", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("users", "ai_monthly_limit", "INTEGER"),
    ("users", "bitrix_allowed", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("news", "bitrix_page_id", "INTEGER"),
    ("news", "bitrix_url", "VARCHAR(1000)"),
    ("news", "bitrix_state", "VARCHAR(20) NOT NULL DEFAULT 'idle'"),
    ("news", "bitrix_error", "TEXT NOT NULL DEFAULT ''"),
    ("news", "bitrix_published_at", "TIMESTAMP"),
]
OWNED_TABLES = ["sources", "news", "prompts", "parser_runs"]
# Старый общий ключ настройки → личный ключ
OLD_SETTINGS = {
    "auto_enabled": "auto_enabled",
    "auto_last_run": "auto_last_run",
    "deepseek_model": "ai_model",
    "deepseek_temperature": "ai_temperature",
}


def _add_setting(conn, user_id: int, key: str, value: str) -> None:
    """Добавляет личную настройку, если её ещё нет.

    Отдельные SELECT и INSERT ... VALUES вместо INSERT ... SELECT ... WHERE NOT EXISTS:
    PostgreSQL не может вывести тип параметра, использованного в таком запросе дважды.
    """
    exists = conn.execute(text("SELECT 1 FROM user_settings WHERE user_id = :u AND key = :k"),
                          {"u": user_id, "k": key}).scalar()
    if not exists:
        conn.execute(text("INSERT INTO user_settings (user_id, key, value) VALUES (:u, :k, :v)"),
                     {"u": user_id, "k": key, "v": value})


def run() -> None:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, column, ddl in NEW_COLUMNS:
            if table in tables and column not in {c["name"] for c in insp.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                print(f"[migrate] {table}.{column} добавлена")
        for table in OWNED_TABLES:
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_user_id ON {table} (user_id)"))

        # Ключи API теперь хранятся отдельно для каждой нейросети: ai_api_key → ai_api_key:<сервис>
        if "user_settings" in tables:
            for prefix, provider_key, default in (("ai_api_key", "ai_provider", "deepseek"),
                                                  ("img_api_key", "img_provider", "")):
                rows = conn.execute(text("SELECT user_id, value FROM user_settings WHERE key = :k"),
                                    {"k": prefix}).all()
                for user_id, value in rows:
                    provider = conn.execute(text("SELECT value FROM user_settings WHERE user_id = :u AND key = :k"),
                                            {"u": user_id, "k": provider_key}).scalar() or default
                    if value and provider:
                        _add_setting(conn, user_id, f"{prefix}:{provider}", value)
                    conn.execute(text("DELETE FROM user_settings WHERE user_id = :u AND key = :k"),
                                 {"u": user_id, "k": prefix})

        owner = conn.execute(text("SELECT id FROM users ORDER BY id LIMIT 1")).scalar()
        if owner is None:
            return  # пользователей ещё нет — значит, и данных без владельца нет

        for table in OWNED_TABLES:
            moved = conn.execute(text(f"UPDATE {table} SET user_id = :o WHERE user_id IS NULL"), {"o": owner}).rowcount
            if moved:
                print(f"[migrate] {table}: {moved} записей закреплено за пользователем #{owner}")

        if "settings" in tables:
            for old_key, new_key in OLD_SETTINGS.items():
                value = conn.execute(text("SELECT value FROM settings WHERE key = :k"), {"k": old_key}).scalar()
                if value is not None:
                    _add_setting(conn, owner, new_key, value)
                    if new_key == "ai_model":
                        _add_setting(conn, owner, "ai_provider", "deepseek")
            conn.execute(text("DELETE FROM settings WHERE key IN ('auto_enabled', 'auto_last_run', "
                              "'deepseek_model', 'deepseek_temperature')"))
