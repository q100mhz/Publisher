"""Парсинг источников и загрузка новостей в реестр.

Сколько статей берём:
  * Сайт только что добавлен (мы с него ещё ничего не брали) — последние 5 статей.
  * Повторный парсинг — все новые статьи, вплоть до последней уже загруженной.
    «Граница» определяется по ссылкам: всё, что на странице-списке стоит выше уже известных
    статей, — новое. Дополнительно сверяемся с датой статьи, если она есть: статьи старше
    последней загруженной не берём (защита от «перемешанных» лент).
  * Не больше PARSER_MAX_PER_RUN за один запуск.

Одну и ту же ссылку парсер никогда не загружает дважды (таблица seen_urls), даже если
новость потом ушла в архив. Дубли одной новости на разных сайтах помечает пользователь.
"""
from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from sqlalchemy import select, update

from ..config import PARSER_FIRST_RUN_LIMIT, PARSER_MAX_PER_RUN, PARSER_USE_BROWSER
from ..db import session_scope
from ..models import News, NewsHistory, NewsImage, ParserRun, SeenUrl, Source, now
from . import fetcher
from .extractor import Article, extract_article, extract_text, find_article_links, next_page_url
from .images import ImageError, save_image

MIN_TEXT_LEN = 300  # если текста меньше — пробуем открыть статью через браузер

executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="worker")
_auto_locks: dict[int, threading.Lock] = {}  # по одному обходу рубильника на пользователя
_locks_guard = threading.Lock()


class RunLog:
    def __init__(self, run_id: int) -> None:
        self.run_id = run_id
        self.lines: list[str] = []

    def __call__(self, message: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {message}"
        self.lines.append(line)
        with session_scope() as db:
            run = db.get(ParserRun, self.run_id)
            run.log = "\n".join(self.lines)


def _keywords(source: Source) -> list[str]:
    return [k.strip().lower() for k in (source.keywords or "").replace("\n", ",").split(",") if k.strip()]


def _boundary_index(links: list[str], known: set[str]) -> int:
    """Позиция, выше которой на странице только новые статьи.

    Одиночная известная ссылка в самом верху — скорее всего закреплённая статья, её пропускаем:
    границей считаем первую известную ссылку, за которой идёт ещё одна известная.
    """
    flags = [u in known for u in links]
    for i, is_known in enumerate(flags):
        if is_known and (i + 1 >= len(flags) or flags[i + 1] or i > 0):
            return i
    return len(links)


def _load_article(url: str, log: RunLog) -> Article:
    article = extract_article(fetcher.fetch_html(url), url)
    if len(article.text) < MIN_TEXT_LEN and PARSER_USE_BROWSER:
        log(f"  текст короткий ({len(article.text)} симв.), открываю через браузер")
        html = fetcher.render_html(url, ready=lambda h: len(extract_text(h)) >= MIN_TEXT_LEN, max_wait=10)
        rendered = extract_article(html, url)
        if len(rendered.text) > len(article.text):
            article = rendered
    return article


def _discover(source: Source, log: RunLog) -> list[str]:
    links: list[str] = []
    try:
        links = find_article_links(fetcher.fetch_html(source.url), source.url)
        log(f"Страница-список загружена, найдено ссылок на статьи: {len(links)}")
    except fetcher.FetchError as e:
        log(f"Не удалось загрузить страницу напрямую: {e}")
    if len(links) < 3 and PARSER_USE_BROWSER:
        log("Статей мало — открываю страницу через браузер (сайт подгружает ленту скриптом)")
        html = fetcher.render_html(source.url, ready=lambda h: len(find_article_links(h, source.url)) >= 3)
        rendered = find_article_links(html, source.url)
        log(f"Через браузер найдено ссылок: {len(rendered)}")
        if len(rendered) > len(links):
            links = rendered
    return links


OLDER_BATCH = 5      # сколько старых статей добавлять за одно нажатие
OLDER_MAX_PAGES = 15  # сколько страниц ленты листать в поисках старых статей


def _older_candidates(links: list[str], known: set[str]) -> list[str]:
    """Ещё не загруженные статьи, которые стоят в ленте ниже уже загруженных (то есть старше их)."""
    if not known:
        return list(links)
    boundary = _boundary_index(links, known)
    return [u for u in links[boundary:] if u not in known]


def _discover_older(source: Source, known: set[str], want: int, log: RunLog) -> list[str]:
    """Листает ленту (страницы ?page=2, /page/2/ … или кнопку «Загрузить ещё»), пока не наберёт want старых статей."""
    all_links: list[str] = []
    url, visited = source.url, set()
    for page_no in range(1, OLDER_MAX_PAGES + 1):
        visited.add(url)
        try:
            html = fetcher.fetch_html(url)
        except fetcher.FetchError as e:
            log(f"Страница {page_no}: не загрузилась ({e})")
            break
        links = find_article_links(html, url)
        all_links += [u for u in links if u not in all_links]
        older = _older_candidates(all_links, known)
        log(f"Страница {page_no}: ссылок {len(links)}, старых незагруженных пока {len(older)}")
        if len(older) >= want or (page_no == 1 and len(links) < 3):
            break
        nxt = next_page_url(html, url)
        if not nxt or nxt in visited:
            log("Следующей страницы ленты нет")
            break
        url = nxt

    older = _older_candidates(all_links, known)
    if len(older) < want and PARSER_USE_BROWSER:
        log("Открываю ленту в браузере и нажимаю «Загрузить ещё»")
        html = fetcher.render_html(
            source.url, click_more=True, max_wait=60,
            ready=lambda h: len(_older_candidates(find_article_links(h, source.url), known)) >= want,
        )
        rendered = find_article_links(html, source.url)
        rendered_older = _older_candidates(rendered, known)
        log(f"Через браузер: ссылок {len(rendered)}, старых незагруженных {len(rendered_older)}")
        if len(rendered_older) > len(older):
            older = rendered_older
    return older


def _save_article(source_id: int, article: Article, created_at: datetime, log: RunLog) -> int:
    with session_scope() as db:
        news = News(
            user_id=db.get(Source, source_id).user_id,
            title=article.title[:1000] or article.url,
            created_at=created_at,
            origin="parser",
            source_id=source_id,
            source_url=article.url,
            original_text=article.text,
            original_published_at=article.published_at,
        )
        db.add(news)
        db.flush()
        db.add(NewsHistory(news_id=news.id, action="Загружена парсером"))
        db.add(SeenUrl(source_id=source_id, url=article.url, accepted=True))
        news_id = news.id

    saved = 0
    for img_url in article.image_urls:
        try:
            resp = fetcher.fetch(img_url)
            name = img_url.rsplit("/", 1)[-1].split("?")[0][:200] or "image"
            info = save_image(resp.content, name, min_side=150)
        except (fetcher.FetchError, ImageError):
            continue
        with session_scope() as db:
            db.add(NewsImage(news_id=news_id, source_url=img_url, **info))
        saved += 1
    if article.image_urls:
        log(f"  картинок сохранено: {saved} из {len(article.image_urls)}")
    return news_id


def run_source(source_id: int, trigger: str = "manual", mode: str = "new") -> None:
    """mode: "new" — свежие статьи, "older" — следующие 5 статей старше уже загруженных."""
    # Захватываем источник атомарно: один сайт никогда не парсится в двух потоках сразу
    with session_scope() as db:
        got = db.execute(
            update(Source).where(Source.id == source_id, Source.is_running.is_(False))
            .values(is_running=True)
        ).rowcount
        if not got:
            return
        source = db.get(Source, source_id)
        run = ParserRun(source_id=source_id, user_id=source.user_id, trigger="older" if mode == "older" else trigger)
        db.add(run)
        db.flush()
        run_id = run.id
        known = set(db.scalars(select(SeenUrl.url).where(SeenUrl.source_id == source_id)))
        known |= set(u for u in db.scalars(select(News.source_url).where(News.source_id == source_id)) if u)
        db.expunge(source)

    log = RunLog(run_id)
    # Все статьи одного запуска получают одно время создания, а порядок внутри запуска — как на сайте
    run_started = now()
    found = added = skipped = 0
    status, message = "ok", ""
    first_run = not known
    older_mode = mode == "older" and not first_run
    keywords = _keywords(source)
    last_date = source.last_article_date
    newest_date = last_date

    try:
        log(f"Старт: {source.name} — {source.url}" + (
            f" (загружаем ещё {OLDER_BATCH} статей старше уже загруженных)" if older_mode
            else f" (первый запуск: берём последние {PARSER_FIRST_RUN_LIMIT})" if first_run else ""))
        if older_mode:
            # С фильтром по словам часть статей отсеется — берём кандидатов с запасом
            want = OLDER_BATCH * 3 if keywords else OLDER_BATCH
            links = _discover_older(source, known, want, log)
        else:
            links = _discover(source, log)
        found = len(links)
        if not links and older_mode:
            status, message = "empty", "Более старых статей не найдено"
        elif not links:
            status, message = "error", "Не найдено ни одной ссылки на статью"
            log(message)
        else:
            if older_mode:
                targets = links  # уже только старые незагруженные
            elif first_run:
                targets = links[:PARSER_FIRST_RUN_LIMIT]
            else:
                boundary = _boundary_index(links, known)
                targets = [u for u in links[:boundary] if u not in known][:PARSER_MAX_PER_RUN]
            log(f"Кандидатов: {len(targets)}, загружаем до {OLDER_BATCH}" if older_mode else f"К загрузке: {len(targets)}")

            older_in_row = 0
            for url in targets:
                if older_mode and added >= OLDER_BATCH:
                    break
                try:
                    log(f"→ {url}")
                    article = _load_article(url, log)
                    if (not first_run and not older_mode and last_date and article.published_at
                            and article.published_at.date() < last_date.date()):
                        older_in_row += 1
                        skipped += 1
                        log(f"  статья от {article.published_at:%d.%m.%Y} старше последней загруженной — пропуск")
                        with session_scope() as db:
                            db.add(SeenUrl(source_id=source_id, url=url, accepted=False))
                        if older_in_row >= 3:
                            log("Три старые статьи подряд — дальше только старые, останавливаюсь")
                            break
                        continue
                    older_in_row = 0
                    if keywords:
                        haystack = f"{article.title}\n{article.text}".lower()
                        if not any(k in haystack for k in keywords):
                            skipped += 1
                            log("  нет ключевых слов — пропуск")
                            with session_scope() as db:
                                db.add(SeenUrl(source_id=source_id, url=url, accepted=False))
                            continue
                    if len(article.text) < 50:
                        log("  не удалось извлечь текст статьи — запись создана с пустым текстом")
                    _save_article(source_id, article, run_started, log)
                    added += 1
                    log(f"  ✓ {article.title[:120]}")
                    if article.published_at and (not newest_date or article.published_at > newest_date):
                        newest_date = article.published_at
                except fetcher.FetchError as e:
                    log(f"  ошибка загрузки: {e}")
                except Exception as e:  # одна битая статья не должна ронять весь запуск
                    log(f"  ошибка обработки: {e}")
            message = f"Найдено {found}, добавлено {added}" + (f", пропущено {skipped}" if skipped else "")
            if not added and not skipped:
                status = "empty"
                message = "Более старых статей не найдено" if older_mode else "Новых статей нет"
    except Exception as e:
        status, message = "error", str(e)[:500]
        log("Ошибка: " + message)
        traceback.print_exc()
    finally:
        log("Готово: " + message)
        with session_scope() as db:
            src = db.get(Source, source_id)
            src.is_running = False
            src.last_run_at = now()
            src.last_status = status
            src.last_message = message
            if newest_date and (not src.last_article_date or newest_date > src.last_article_date):
                src.last_article_date = newest_date
            run = db.get(ParserRun, run_id)
            run.status = "error" if status == "error" else "ok"
            run.found, run.added, run.skipped = found, added, skipped
            run.finished_at = now()


def run_all(user_id: int, trigger: str = "auto") -> None:
    """Обход всех активных источников пользователя (используется рубильником)."""
    with _locks_guard:
        lock = _auto_locks.setdefault(user_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return
    try:
        with session_scope() as db:
            ids = list(db.scalars(select(Source.id).where(Source.user_id == user_id, Source.is_active.is_(True))
                                  .order_by(Source.id)))
        for source_id in ids:
            run_source(source_id, trigger)
    finally:
        lock.release()


def submit_source(source_id: int, mode: str = "new") -> None:
    executor.submit(run_source, source_id, "manual", mode)


def reset_stale_flags() -> None:
    """После перезапуска контейнера снимаем «зависшие» флаги выполнения."""
    with session_scope() as db:
        db.execute(update(Source).values(is_running=False))
        db.execute(update(ParserRun).where(ParserRun.status == "running")
                   .values(status="error", finished_at=now(), log=ParserRun.log + "\nПрервано перезапуском"))
        db.execute(update(News).where(News.gen_state == "running")
                   .values(gen_state="error", gen_error="Прервано перезапуском сервера"))
        db.execute(update(News).where(News.img_state == "running")
                   .values(img_state="error", img_error="Прервано перезапуском сервера"))
        db.execute(update(News).where(News.bitrix_state == "running")
                   .values(bitrix_state="error", bitrix_error="Прервано перезапуском сервера"))

