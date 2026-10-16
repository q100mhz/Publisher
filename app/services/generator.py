"""Генерация переработанной статьи нейросетью, выбранной пользователем в настройках."""
from __future__ import annotations

import re
import traceback

from ..db import session_scope
from ..models import News, NewsHistory, NewsImage, NewsStatus, NewsVersion, Prompt, User, now
from . import ai
from .images import ImageError, save_image

SYSTEM_MESSAGE = (
    "Ты — редактор и SEO-копирайтер. Отвечай только готовым HTML-кодом статьи, "
    "без пояснений до или после, без markdown и без обрамления ```."
)

PLACEHOLDERS = {
    "{title}": "Заголовок исходной новости",
    "{original_text}": "Текст исходной новости",
    "{source_url}": "Ссылка на оригинал",
    "{source_name}": "Название сайта-источника",
    "{date}": "Дата публикации оригинала",
}

DEFAULT_PROMPT = """Перепиши новость ниже в уникальную статью для сайта бухгалтерской компании.

Требования:
- полностью уникальный текст, сохраняющий факты, цифры и даты оригинала;
- деловой, но живой стиль, понятный предпринимателю;
- структура: <h1>, вводный абзац, 3–5 разделов с <h2>, списки <ul> где уместно, вывод;
- в начале добавь <title> и <meta name="description"> (до 160 символов) для SEO;
- не упоминай сайт-источник и не ставь на него ссылки;
- верни только HTML-код.

Заголовок: {title}

Текст оригинала:
{original_text}
"""


IMAGE_PLACEHOLDERS = {
    "{title}": "Заголовок новости",
    "{original_text}": "Начало исходного текста (до 1500 символов)",
    "{generated_text}": "Начало переделанной статьи без HTML (до 1500 символов)",
}

DEFAULT_IMAGE_PROMPT = """Иллюстрация для статьи на сайте бухгалтерской компании.

Тема статьи: {title}
О чём статья: {original_text}

Стиль: объёмная «пластилиновая» 3D-иллюстрация, мягкий свет, светлый однотонный фон,
яркие тёплые цвета (оранжевый, жёлтый). Без текста, букв и цифр на изображении.
"""

IMAGE_PROMPT_LIMIT = 3800  # у некоторых моделей картинок жёсткий лимит длины промпта


def _plain(text_: str, limit: int) -> str:
    text_ = re.sub(r"<(script|style|title)[^>]*>.*?</\1>", " ", text_ or "", flags=re.S | re.I)
    text_ = re.sub(r"<[^>]+>|[#*_`>]+", " ", text_)
    text_ = re.sub(r"\s+", " ", text_).strip()
    return text_[:limit].rsplit(" ", 1)[0] + "…" if len(text_) > limit else text_


def render_image_prompt(template: str, news: News) -> str:
    values = {
        "{title}": news.title or "",
        "{original_text}": _plain(news.original_text, 1500),
        "{generated_text}": _plain(news.html_code, 1500),
    }
    text_ = template
    for key, value in values.items():
        text_ = text_.replace(key, value)
    if "{title}" not in template:
        text_ += f"\n\nТема статьи: {values['{title}']}"
    return text_[:IMAGE_PROMPT_LIMIT]


def generate_image(news_id: int, prompt_id: int) -> None:
    """Фоновая генерация картинки: результат кладётся в колонку «Сгенерировано ИИ»."""
    try:
        with session_scope() as db:
            news = db.get(News, news_id)
            prompt = db.get(Prompt, prompt_id)
            cfg = ai.image_config(db, db.get(User, news.user_id))
            image_prompt = render_image_prompt(prompt.text, news)
            prompt_name = prompt.name
            number = len(news.generated_images) + 1

        data = ai.generate_image(cfg, image_prompt)
        info = save_image(data, f"ai-{news_id}-{number}.png")

        with session_scope() as db:
            news = db.get(News, news_id)
            db.add(NewsImage(news_id=news_id, kind="generated", **info))
            news.img_state, news.img_error = "idle", ""
            db.add(NewsHistory(news_id=news_id, action=f"Сгенерирована картинка (промпт «{prompt_name}», "
                                                       f"{cfg.label}: {cfg.model})"))
    except Exception as e:
        if not isinstance(e, (ai.AIError, ImageError)):
            traceback.print_exc()
        with session_scope() as db:
            news = db.get(News, news_id)
            news.img_state = "error"
            news.img_error = (str(e) if isinstance(e, (ai.AIError, ImageError))
                              else f"{e.__class__.__name__}: {e}")[:1000]
            db.add(NewsHistory(news_id=news_id, action="Ошибка генерации картинки"))


def render_prompt(template: str, news: News) -> str:
    values = {
        "{title}": news.title or "",
        "{original_text}": news.original_text or "",
        "{source_url}": news.source_url or "",
        "{source_name}": news.source.name if news.source else "",
        "{date}": news.original_published_at.strftime("%d.%m.%Y") if news.original_published_at else "",
    }
    text = template
    for key, value in values.items():
        text = text.replace(key, value)
    if "{original_text}" not in template:
        text += f"\n\nЗаголовок: {values['{title}']}\n\nТекст оригинала:\n{values['{original_text}']}"
    return text


def clean_html(answer: str) -> str:
    answer = answer.strip()
    fenced = re.search(r"```(?:html)?\s*\n(.*?)```", answer, re.S | re.I)
    if fenced:
        answer = fenced.group(1).strip()
    return answer


def generate(news_id: int, prompt_id: int) -> None:
    try:
        with session_scope() as db:
            news = db.get(News, news_id)
            prompt = db.get(Prompt, prompt_id)
            user = db.get(User, news.user_id)
            cfg = ai.user_config(db, user)
            user_message = render_prompt(prompt.text, news)
            prompt_name = prompt.name

        html = clean_html(ai.complete(cfg, SYSTEM_MESSAGE, user_message))
        if not html:
            raise ai.AIError("Нейросеть вернула пустой ответ")

        with session_scope() as db:
            news = db.get(News, news_id)
            news.html_code = html
            if news.status == NewsStatus.NEW:
                news.status = NewsStatus.GENERATED
            news.gen_state = "idle"
            news.gen_error = ""
            model = f"{cfg.label}: {cfg.model}"[:100]
            db.add(NewsVersion(news_id=news_id, html_code=html, prompt_name=prompt_name, model=model))
            db.add(NewsHistory(news_id=news_id, action=f"Сгенерирован текст (промпт «{prompt_name}», {model})"))
    except Exception as e:
        if not isinstance(e, ai.AIError):
            traceback.print_exc()
        with session_scope() as db:
            news = db.get(News, news_id)
            news.gen_state = "error"
            news.gen_error = (str(e) if isinstance(e, ai.AIError) else f"{e.__class__.__name__}: {e}")[:1000]
            db.add(NewsHistory(news_id=news_id, action="Ошибка генерации"))


def mark_started(db, news: News) -> None:
    news.gen_state = "running"
    news.gen_error = ""
    news.gen_started_at = now()
