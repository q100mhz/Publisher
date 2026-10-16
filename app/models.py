"""Модель данных.

Все даты хранятся в локальном времени сервера (TZ=Europe/Moscow в контейнере).
Новости никогда не удаляются из базы — только помечаются архивными.
"""
from datetime import date, datetime

from sqlalchemy import (
    Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


class NewsStatus:
    NEW = "new"              # только что спарсена / создана
    GENERATED = "generated"  # есть переработанный текст
    PUBLISHED = "published"  # опубликована на сайте
    POSTPONED = "postponed"  # отложена «на потом» (бэклог)

    LABELS = {
        NEW: "Новая",
        GENERATED: "Сгенерирована",
        PUBLISHED: "Опубликована",
        POSTPONED: "Отложена",
    }


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Source(Base):
    """Справочник сайтов, с которых парсятся статьи."""
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(1000))
    keywords: Mapped[str] = mapped_column(Text, default="")  # через запятую; пусто = брать всё
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)  # участвует в автопарсинге
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    is_running: Mapped[bool] = mapped_column(Boolean, default=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(20), default="")  # ok / error / empty
    last_message: Mapped[str] = mapped_column(Text, default="")
    last_article_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    news: Mapped[list["News"]] = relationship(back_populates="source")


class SeenUrl(Base):
    """Все ссылки, которые парсер уже обработал (включая отфильтрованные по ключевым словам).

    Нужна, чтобы не загружать одну статью дважды и понимать, где «граница» прошлого парсинга.
    """
    __tablename__ = "seen_urls"
    __table_args__ = (UniqueConstraint("source_id", "url"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    url: Mapped[str] = mapped_column(String(1000))
    accepted: Mapped[bool] = mapped_column(Boolean, default=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ParserRun(Base):
    __tablename__ = "parser_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    trigger: Mapped[str] = mapped_column(String(20))  # auto / manual
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running / ok / error
    found: Mapped[int] = mapped_column(Integer, default=0)
    added: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    log: Mapped[str] = mapped_column(Text, default="")

    source: Mapped[Source | None] = relationship()


class Prompt(Base):
    """Справочник промптов для генерации."""
    __tablename__ = "prompts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class News(Base):
    """Запись реестра публикаций."""
    __tablename__ = "news"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    origin: Mapped[str] = mapped_column(String(20), default="manual")  # manual / parser

    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True, index=True)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True, index=True)
    original_published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    original_text: Mapped[str] = mapped_column(Text, default="")
    # HTML-код переделанной новости (ответ нейросети или ручной ввод).
    # «Текст переделанной новости» в карточке — это он же, показанный так, как увидит читатель.
    html_code: Mapped[str] = mapped_column(Text, default="")

    status: Mapped[str] = mapped_column(String(20), default=NewsStatus.NEW, index=True)
    planned_publish_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    published_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Фоновая генерация: idle / running / error
    gen_state: Mapped[str] = mapped_column(String(20), default="idle")
    gen_error: Mapped[str] = mapped_column(Text, default="")
    gen_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    source: Mapped[Source | None] = relationship(back_populates="news")
    images: Mapped[list["NewsImage"]] = relationship(
        back_populates="news", order_by="NewsImage.id", cascade="all, delete-orphan"
    )
    versions: Mapped[list["NewsVersion"]] = relationship(
        back_populates="news", order_by="NewsVersion.id.desc()", cascade="all, delete-orphan"
    )
    history: Mapped[list["NewsHistory"]] = relationship(
        back_populates="news", order_by="NewsHistory.id.desc()", cascade="all, delete-orphan"
    )

    @property
    def status_label(self) -> str:
        return NewsStatus.LABELS.get(self.status, self.status)

    def publish_flag(self, today: date) -> str:
        """'today' — надо опубликовать сегодня, 'overdue' — дата прошла, '' — всё в порядке."""
        if self.archived or self.status == NewsStatus.PUBLISHED or not self.planned_publish_date:
            return ""
        if self.planned_publish_date == today:
            return "today"
        if self.planned_publish_date < today:
            return "overdue"
        return ""


class NewsImage(Base):
    __tablename__ = "news_images"
    id: Mapped[int] = mapped_column(primary_key=True)
    news_id: Mapped[int] = mapped_column(ForeignKey("news.id"), index=True)
    filename: Mapped[str] = mapped_column(String(300))        # оригинал
    thumb_filename: Mapped[str] = mapped_column(String(300))  # миниатюра
    original_name: Mapped[str] = mapped_column(String(300))
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    news: Mapped[News] = relationship(back_populates="images")


class NewsVersion(Base):
    """История генераций: каждый ответ нейросети сохраняется."""
    __tablename__ = "news_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    news_id: Mapped[int] = mapped_column(ForeignKey("news.id"), index=True)
    html_code: Mapped[str] = mapped_column(Text)
    prompt_name: Mapped[str] = mapped_column(String(200), default="")
    model: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    news: Mapped[News] = relationship(back_populates="versions")


class NewsHistory(Base):
    """Журнал изменений записи."""
    __tablename__ = "news_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    news_id: Mapped[int] = mapped_column(ForeignKey("news.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    action: Mapped[str] = mapped_column(Text)

    news: Mapped[News] = relationship(back_populates="history")


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


def get_setting(db, key: str, default: str = "") -> str:
    row = db.get(Setting, key)
    return row.value if row else default


def set_setting(db, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


def get_float_setting(db, key: str, default: float) -> float:
    try:
        return float(get_setting(db, key, str(default)))
    except ValueError:
        return default

