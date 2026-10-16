"""Генерация переработанной статьи через DeepSeek API (OpenAI-совместимый)."""
from __future__ import annotations

import re
import traceback

from openai import OpenAI

from ..config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
from ..db import session_scope
from ..models import News, NewsHistory, NewsStatus, NewsVersion, Prompt, get_float_setting, get_setting, now

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


def is_configured() -> bool:
    return bool(DEEPSEEK_API_KEY)


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
            user_message = render_prompt(prompt.text, news)
            prompt_name = prompt.name
            model = get_setting(db, "deepseek_model", DEEPSEEK_MODEL) or DEEPSEEK_MODEL
            temperature = get_float_setting(db, "deepseek_temperature", 1.0)

        client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL, timeout=300, max_retries=1)
        response = client.chat.completions.create(
            model=model,
            temperature=temperature,
            messages=[{"role": "system", "content": SYSTEM_MESSAGE},
                      {"role": "user", "content": user_message}],
        )
        html = clean_html(response.choices[0].message.content or "")
        if not html:
            raise RuntimeError("Нейросеть вернула пустой ответ")

        with session_scope() as db:
            news = db.get(News, news_id)
            news.html_code = html
            if news.status == NewsStatus.NEW:
                news.status = NewsStatus.GENERATED
            news.gen_state = "idle"
            news.gen_error = ""
            db.add(NewsVersion(news_id=news_id, html_code=html, prompt_name=prompt_name, model=model))
            db.add(NewsHistory(news_id=news_id, action=f"Сгенерирован текст (промпт «{prompt_name}», {model})"))
    except Exception as e:
        traceback.print_exc()
        with session_scope() as db:
            news = db.get(News, news_id)
            news.gen_state = "error"
            news.gen_error = f"{e.__class__.__name__}: {e}"[:1000]
            db.add(NewsHistory(news_id=news_id, action="Ошибка генерации"))


def mark_started(db, news: News) -> None:
    news.gen_state = "running"
    news.gen_error = ""
    news.gen_started_at = now()
