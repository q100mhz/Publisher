"""Загрузка страниц с паузами, чтобы сайты-источники нас не блокировали.

Как работают паузы:
  * Перед каждым запросом к домену проверяется, сколько прошло с прошлого запроса к нему же.
    Если меньше случайной паузы из диапазона PARSER_DELAY_MIN..PARSER_DELAY_MAX (по умолчанию 2–5 с),
    поток ждёт остаток. Запросы к разным сайтам друг друга не тормозят.
  * К одному домену в каждый момент идёт только один запрос (блокировка на домен).
  * На ответы 429 / 503 делается до 2 повторов с нарастающей паузой (10 с, 30 с) или столько,
    сколько попросил сайт в заголовке Retry-After (но не больше 60 с).
  * Запросы выглядят как обычный браузер: User-Agent Chrome, русский Accept-Language.

Скорость: 1 запрос на список + по 1 на каждую статью, в среднем 3,5 с между ними →
3 статьи ≈ 15 секунд, 5 статей ≈ 25 секунд. Требование «≥ 3 статьи в минуту» выполняется с запасом,
даже если страницу пришлось открывать через браузер (это добавляет 3–6 с на страницу).
"""
from __future__ import annotations

import random
import threading
import time
from typing import Callable
from urllib.parse import urlparse

import httpx

from ..config import PARSER_DELAY_MAX, PARSER_DELAY_MIN, PARSER_USE_BROWSER

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
}


class FetchError(Exception):
    pass


class DomainThrottle:
    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def lock_for(self, domain: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(domain, threading.Lock())

    def wait(self, domain: str) -> None:
        delay = random.uniform(PARSER_DELAY_MIN, PARSER_DELAY_MAX)
        elapsed = time.monotonic() - self._last.get(domain, 0)
        if elapsed < delay:
            time.sleep(delay - elapsed)

    def mark(self, domain: str) -> None:
        self._last[domain] = time.monotonic()


throttle = DomainThrottle()
_client = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=httpx.Timeout(30, connect=15))


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def fetch(url: str, *, binary: bool = False) -> httpx.Response:
    domain = _domain(url)
    with throttle.lock_for(domain):
        backoffs = [10, 30]
        while True:
            throttle.wait(domain)
            try:
                resp = _client.get(url)
            except httpx.HTTPError as e:
                throttle.mark(domain)
                raise FetchError(f"{url}: {e.__class__.__name__}: {e}") from e
            throttle.mark(domain)
            if resp.status_code in (429, 503) and backoffs:
                retry_after = resp.headers.get("retry-after", "")
                pause = min(int(retry_after), 60) if retry_after.isdigit() else backoffs[0]
                backoffs.pop(0)
                time.sleep(pause)
                continue
            if resp.status_code >= 400:
                raise FetchError(f"{url}: HTTP {resp.status_code}")
            return resp


def fetch_html(url: str) -> str:
    resp = fetch(url)
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype and "xml" not in ctype:
        raise FetchError(f"{url}: это не HTML-страница ({ctype})")
    return resp.text


def render_html(url: str, ready: Callable[[str], bool] | None = None, max_wait: float = 20) -> str:
    """Открывает страницу в настоящем (headless) браузере — для сайтов, где статьи подгружаются скриптами.

    ready(html) — проверка «нужное уже появилось на странице». Пока она не выполнена, браузер
    прокручивает страницу и ждёт, но не дольше max_wait секунд.
    """
    if not PARSER_USE_BROWSER:
        raise FetchError("Браузерный режим отключён (PARSER_USE_BROWSER=false)")
    from playwright.sync_api import sync_playwright

    domain = _domain(url)
    with throttle.lock_for(domain):
        throttle.wait(domain)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
                try:
                    page = browser.new_page(user_agent=USER_AGENT, locale="ru-RU",
                                            viewport={"width": 1366, "height": 900})
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                    # Прокручиваем, чтобы подгрузились «ленивые» ленты, и ждём появления нужного
                    deadline = time.monotonic() + max_wait
                    html = page.content()
                    for step in range(100):
                        if ready is None and step >= 3:
                            break
                        if ready is not None and ready(html):
                            break
                        if time.monotonic() > deadline:
                            break
                        page.mouse.wheel(0, 2500)
                        page.wait_for_timeout(800)
                        html = page.content()
                    return html
                finally:
                    browser.close()
        except Exception as e:
            raise FetchError(f"{url}: браузер не смог открыть страницу: {e}") from e
        finally:
            throttle.mark(domain)
