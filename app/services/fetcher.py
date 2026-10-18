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
import re
import threading
import time
from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx

from ..config import PARSER_DELAY_MAX, PARSER_DELAY_MIN, PARSER_USE_BROWSER
from ..security import UnsafeURL, check_url, is_safe_url

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
# Перенаправления проходим вручную: каждый новый адрес проверяется на «внутреннюю сеть»
_client = httpx.Client(headers=HEADERS, follow_redirects=False, timeout=httpx.Timeout(30, connect=15))
MAX_REDIRECTS = 5


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _get_checked(url: str) -> httpx.Response:
    """GET с проверкой адреса и каждого перенаправления."""
    for _ in range(MAX_REDIRECTS + 1):
        try:
            check_url(url)
        except UnsafeURL as e:
            raise FetchError(f"{url}: {e}") from e
        resp = _client.get(url)
        if resp.is_redirect and resp.headers.get("location"):
            url = urljoin(str(resp.url), resp.headers["location"])
            continue
        return resp
    raise FetchError(f"{url}: слишком много перенаправлений")


def fetch(url: str, *, binary: bool = False) -> httpx.Response:
    domain = _domain(url)
    with throttle.lock_for(domain):
        backoffs = [10, 30]
        while True:
            throttle.wait(domain)
            try:
                resp = _get_checked(url)
            except httpx.HTTPError as e:
                throttle.mark(domain)
                raise FetchError(f"{url}: {e.__class__.__name__}: {e}") from e
            except FetchError:
                throttle.mark(domain)
                raise
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


# Текст кнопки подгрузки ленты: «Загрузить ещё», «Показать ещё», «Показать больше», «Ещё», «Load more»
LOAD_MORE = re.compile(r"^\s*((загрузить|показать|смотреть)\s+(ещё|еще|больше)|ещё|еще|load more|show more)\s*$", re.I)


def render_html(url: str, ready: Callable[[str], bool] | None = None, max_wait: float = 20,
                click_more: bool = False) -> str:
    """Открывает страницу в настоящем (headless) браузере — для сайтов, где статьи подгружаются скриптами.

    ready(html) — проверка «нужное уже появилось на странице». Пока она не выполнена, браузер
    прокручивает страницу и ждёт, но не дольше max_wait секунд.
    click_more — дополнительно нажимать кнопку «Загрузить ещё» (для лент без постраничной навигации).
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
                    # Браузер не должен ходить во внутреннюю сеть ни за страницей, ни за её ресурсами
                    page.route("**/*", lambda route: route.continue_() if is_safe_url(route.request.url)
                               or route.request.url.startswith(("data:", "blob:")) else route.abort())
                    check_url(url)
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
                        if click_more:
                            button = page.locator("button:visible, a:visible").filter(has_text=LOAD_MORE).last
                            try:
                                if button.count():
                                    button.click(timeout=3000)
                                    page.wait_for_timeout(1200)
                            except Exception:
                                pass
                        page.wait_for_timeout(800)
                        html = page.content()
                    return html
                finally:
                    browser.close()
        except Exception as e:
            raise FetchError(f"{url}: браузер не смог открыть страницу: {e}") from e
        finally:
            throttle.mark(domain)
