"""Автоизвлечение: поиск ссылок на статьи на странице-списке и извлечение текста статьи.

Настраивать разметку под каждый сайт не нужно:
  * Ссылки на статьи находятся по общим признакам: тот же домен, «человекочитаемый» адрес
    (slug вида /club/article-knowledge/kak-otkryt-ip), осмысленный текст ссылки, и главное —
    у статей общий «родительский» раздел, и таких ссылок на странице много. Меню, рубрики,
    пагинация и служебные страницы отбрасываются.
  * Текст, заголовок, дата и картинки извлекаются библиотекой trafilatura, которая сама
    находит основной контент страницы и отбрасывает меню, рекламу и подвал.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlparse, urlunparse

import trafilatura
from bs4 import BeautifulSoup

BAD_SEGMENTS = {
    "tag", "tags", "category", "categories", "rubric", "rubrics", "author", "authors", "page",
    "search", "login", "logout", "auth", "signin", "sign-in", "sign-up", "register", "cart",
    "basket", "contacts", "contact", "about", "privacy", "policy", "terms", "landingpage",
    "promo", "user", "users", "feed", "rss", "subscribe", "account", "cabinet", "lk", "vacancy",
    "vacancies", "jobs", "sitemap", "amp",
}
NAV_CLASS = re.compile(r"(^|[-_\s])(menu|nav|navbar|navigation|breadcrumbs?|pagination|pager|footer|header|sidebar|tags?)($|[-_\s])", re.I)
BAD_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|pdf|docx?|xlsx?|zip|rar|mp[34]|avi|ico|xml|css|js)$", re.I)


@dataclass
class Article:
    url: str
    title: str = ""
    text: str = ""
    published_at: datetime | None = None
    image_urls: list[str] = field(default_factory=list)


def _norm_domain(netloc: str) -> str:
    return netloc.lower().removeprefix("www.")


def normalize_url(url: str) -> str:
    p = urlparse(url)
    path = re.sub(r"/{2,}", "/", p.path) or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", "", ""))


def _segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def _slug_like(segment: str) -> bool:
    if len(segment) < 8:
        return False
    return "-" in segment or "_" in segment or sum(c.isdigit() for c in segment) >= 4


def _in_navigation(a) -> bool:
    """Ссылка лежит в меню / рубрикаторе / пагинации (по классам самой ссылки и 4 ближайших родителей)."""
    node, depth = a, 0
    while node is not None and depth < 5:
        classes = " ".join(node.get("class") or [])
        if classes and NAV_CLASS.search(classes):
            return True
        node, depth = node.parent, depth + 1
    return False


def find_article_links(html: str, page_url: str) -> list[str]:
    """Возвращает ссылки на статьи в порядке их появления на странице."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.select("header, nav, footer, aside, [role=navigation], .breadcrumbs, .breadcrumb"):
        tag.decompose()

    base = urlparse(page_url)
    base_domain = _norm_domain(base.netloc)
    listing_path = normalize_url(page_url)
    listing_segments = _segments(base.path)

    order: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(page_url, href)
        p = urlparse(absolute)
        if p.scheme not in ("http", "https") or _norm_domain(p.netloc) != base_domain:
            continue
        url = normalize_url(absolute)
        if url == listing_path or BAD_EXT.search(p.path):
            continue
        segs = _segments(p.path)
        if not segs or any(s.lower() in BAD_SEGMENTS for s in segs[-2:]):
            continue
        if not _slug_like(segs[-1]) or _in_navigation(a):
            continue
        if url not in order:
            order.append(url)

    if not order:
        return []

    # Ссылки, которые являются «родителем» других ссылок, — это рубрики, а не статьи
    paths = {u: urlparse(u).path for u in order}
    parents = {u for u in order if any(o != u and paths[o].startswith(paths[u] + "/") for o in order)}
    candidates = [u for u in order if u not in parents]

    # Группируем по родительскому разделу и оставляем крупные группы
    def parent(u: str) -> str:
        return "/".join(_segments(paths[u])[:-1])

    groups = Counter(parent(u) for u in candidates)
    listing_prefix = "/".join(listing_segments)

    def weight(g: str) -> float:
        bonus = 1.5 if listing_prefix and (g == listing_prefix or g.startswith(listing_prefix + "/")) else 1.0
        return groups[g] * bonus

    best = max(weight(g) for g in groups)
    keep = {g for g in groups if weight(g) >= max(2, best * 0.3)} or {max(groups, key=weight)}

    # Ссылки вида /tpost/x и /blog/category/y/tpost/x считаем одной группой (общий последний раздел)
    keep_tails = {g.rsplit("/", 1)[-1] for g in keep if g}
    return [u for u in candidates if parent(u) in keep or parent(u).rsplit("/", 1)[-1] in keep_tails]


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[: len(datetime.now().strftime(fmt))], fmt)
        except ValueError:
            continue
    return None


def extract_text(html: str) -> str:
    """Быстрая проверка: сколько основного текста удаётся извлечь со страницы."""
    return trafilatura.extract(html, include_comments=False, favor_recall=True) or ""


def extract_article(html: str, url: str) -> Article:
    article = Article(url=url)
    data = trafilatura.extract(
        html, url=url, output_format="json", with_metadata=True, include_comments=False,
        include_tables=True, include_images=False, favor_recall=True,
    )
    if data:
        meta = json.loads(data)
        article.title = (meta.get("title") or "").strip()
        article.published_at = _parse_date(meta.get("date"))
        if meta.get("image"):
            article.image_urls.append(urljoin(url, meta["image"]))

    # Текст с сохранением абзацев, заголовков и списков (markdown читается и человеком, и нейросетью)
    article.text = (trafilatura.extract(
        html, url=url, output_format="markdown", include_comments=False, include_tables=True,
        include_images=False, include_links=False, favor_recall=True,
    ) or "").strip()

    # Картинки из тела статьи
    xml = trafilatura.extract(
        html, url=url, output_format="xml", include_images=True, include_comments=False, favor_recall=True,
    )
    if xml:
        for src in re.findall(r'<graphic[^>]*\bsrc="([^"]+)"', xml):
            article.image_urls.append(urljoin(url, src.replace("&amp;", "&")))

    if not article.title:
        soup = BeautifulSoup(html, "lxml")
        h1 = soup.find("h1")
        article.title = h1.get_text(" ", strip=True) if h1 else (soup.title.string or "").strip() if soup.title else ""

    seen: set[str] = set()
    article.image_urls = [u for u in article.image_urls
                          if u.startswith("http") and not u.lower().endswith((".svg", ".gif"))
                          and not (u in seen or seen.add(u))][:10]
    return article
