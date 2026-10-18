"""Публикация статей в Битрикс24 → «Сайты» через входящий вебхук (REST, права «Сайты» — landing).

Как это работает:
  1. На портале заранее создана страница-шаблон статьи (шапка, подвал, оформление).
     В блоке «HTML-код» на ней написано %%ARTICLE%% — сюда встанет текст статьи.
     Можно добавить %%TITLE%% (заголовок) и %%IMAGE%% (картинка) в любые блоки шаблона.
  2. Публикация: шаблон копируется в тот же сайт и папку (landing.landing.copy), метки заменяются
     содержимым новости (landing.block.updatecontent), странице задаются название, адрес и SEO
     (landing.landing.update), затем она публикуется (landing.landing.publication).
  3. Повторная публикация обновляет ту же страницу: вставленный текст обёрнут в элементы
     с классами publisher-article / publisher-title / publisher-image, их содержимое заменяется.
"""
from __future__ import annotations

import base64
import re
import time
import traceback
from datetime import date

import httpx
from bs4 import BeautifulSoup

from ..config import MEDIA_DIR
from ..crypto import decrypt
from ..db import session_scope
from ..models import News, NewsHistory, NewsStatus, User, get_setting, now
from ..security import UnsafeURL, check_url

MARKERS = {"article": "%%ARTICLE%%", "title": "%%TITLE%%", "image": "%%IMAGE%%"}
WEBHOOK_RE = re.compile(r"^https://[A-Za-z0-9.-]+/rest/\d+/[A-Za-z0-9]+/?$")
TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                     "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


class BitrixError(Exception):
    pass


def normalize_webhook(url: str) -> str:
    url = url.strip()
    if not WEBHOOK_RE.match(url):
        raise BitrixError("Адрес вебхука должен выглядеть так: https://ваш-портал.bitrix24.ru/rest/1/xxxxxxxxxxxx/")
    try:
        check_url(url)
    except UnsafeURL as e:
        raise BitrixError(f"Адрес вебхука: {e}")
    return url.rstrip("/") + "/"


def call(webhook: str, method: str, params: dict | None = None):
    """Вызов метода REST. Битрикс24 пускает ~2 запроса в секунду — делаем паузу между вызовами."""
    time.sleep(0.6)
    try:
        check_url(webhook)
    except UnsafeURL as e:
        raise BitrixError(f"Адрес вебхука: {e}")
    try:
        response = httpx.post(f"{webhook}{method}.json", json=params or {}, timeout=60, follow_redirects=False)
    except httpx.HTTPError as e:
        raise BitrixError(f"Портал Битрикс24 недоступен: {e}")
    try:
        data = response.json()
    except ValueError:
        raise BitrixError(f"Битрикс24 вернул непонятный ответ на {method} (HTTP {response.status_code})")
    if "error" in data:
        description = data.get("error_description") or data["error"]
        if data["error"] in ("insufficient_scope", "INSUFFICIENT_SCOPE"):
            description = "у вебхука нет права «Сайты (landing)» — добавьте его в настройках вебхука"
        elif data["error"] in ("NO_AUTH_FOUND", "invalid_token", "expired_token"):
            description = "вебхук недействителен — проверьте адрес или создайте вебхук заново"
        raise BitrixError(f"{method}: {description}")
    return data.get("result")


def list_sites(webhook: str) -> list[dict]:
    result = call(webhook, "landing.site.getList", {"params": {"select": ["ID", "TITLE", "TYPE"], "order": {"ID": "DESC"}}})
    return [{"id": int(s["ID"]), "title": s.get("TITLE") or f"Сайт {s['ID']}", "type": s.get("TYPE", "")}
            for s in (result or [])]


def list_pages(webhook: str, site_id: int) -> list[dict]:
    result = call(webhook, "landing.landing.getList", {"params": {
        "select": ["ID", "TITLE", "FOLDER_ID"], "filter": {"SITE_ID": site_id}, "order": {"ID": "DESC"}}})
    return [{"id": int(p["ID"]), "title": p.get("TITLE") or f"Страница {p['ID']}", "folder_id": p.get("FOLDER_ID")}
            for p in (result or [])]


def user_settings(db, user: User) -> dict:
    encrypted = get_setting(db, user.id, "bitrix_webhook", "")
    webhook = decrypt(encrypted) if encrypted else ""
    return {
        "webhook": webhook or "",
        "webhook_broken": bool(encrypted and not webhook),
        "site_id": get_setting(db, user.id, "bitrix_site_id", ""),
        "template_id": get_setting(db, user.id, "bitrix_template_id", ""),
        "site_title": get_setting(db, user.id, "bitrix_site_title", ""),
        "template_title": get_setting(db, user.id, "bitrix_template_title", ""),
        "mark_published": get_setting(db, user.id, "bitrix_mark_published", "1") == "1",
    }


def ready(db, user: User) -> str:
    """Пустая строка — можно публиковать, иначе — что настроить."""
    if not user.bitrix_allowed:
        return "Публикация в Битрикс24 не разрешена администратором"
    cfg = user_settings(db, user)
    if cfg["webhook_broken"]:
        return "Сохранённый вебхук не читается — укажите его заново в «Настройках» → «Битрикс24»"
    if not (cfg["webhook"] and cfg["site_id"] and cfg["template_id"]):
        return "Настройте Битрикс24: «Настройки» → «Битрикс24» (вебхук, сайт и страница-шаблон)"
    return ""


def slugify(text: str) -> str:
    text = "".join(TRANSLIT.get(ch, ch) for ch in text.lower())
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:80].rstrip("-") or f"article-{int(time.time())}"


def _seo(meta_html: str) -> tuple[str, str]:
    soup = BeautifulSoup(meta_html or "", "html.parser")
    title = soup.find("title")
    desc = soup.find("meta", attrs={"name": "description"})
    return (title.get_text(" ", strip=True) if title else "",
            (desc.get("content") or "").strip() if desc else "")


def _fill(content: str, parts: dict[str, str]) -> tuple[str, bool]:
    """Вставляет части статьи в HTML блока: по меткам %%…%% или в уже вставленные ранее обёртки."""
    changed = False
    for name, html in parts.items():
        if html is None:
            continue
        wrapper_tag = "span" if name == "title" else "div"
        wrapped = f'<{wrapper_tag} class="publisher-{name}">{html}</{wrapper_tag}>'
        if MARKERS[name] in content:
            content = content.replace(MARKERS[name], wrapped)
            changed = True
            continue
        if f"publisher-{name}" in content:
            soup = BeautifulSoup(content, "html.parser")
            node = soup.find(class_=f"publisher-{name}")
            if node is not None:
                node.clear()
                node.append(BeautifulSoup(html, "html.parser"))
                content, changed = str(soup), True
    return content, changed


def _upload_image(webhook: str, block_id: int, image) -> str:
    """Загружает картинку в блок портала и возвращает её адрес на портале."""
    data = (MEDIA_DIR / image.filename).read_bytes()
    result = call(webhook, "landing.block.uploadfile", {
        "block": block_id, "picture": [image.original_name or "image.png", base64.b64encode(data).decode()]})
    if isinstance(result, dict):
        return result.get("src") or result.get("SRC") or ""
    return str(result or "")


def publish(news_id: int) -> None:
    """Фоновая публикация (или обновление) статьи на портале."""
    try:
        with session_scope() as db:
            news = db.get(News, news_id)
            user = db.get(User, news.user_id)
            problem = ready(db, user)
            if problem:
                raise BitrixError(problem)
            cfg = user_settings(db, user)
            webhook = cfg["webhook"]
            site_id, template_id = int(cfg["site_id"]), int(cfg["template_id"])
            title = (news.new_title or news.title).strip()
            article = news.html_code
            meta_title, meta_desc = _seo(news.meta_html)
            image = (news.generated_images or news.original_images or [None])[0]
            page_id = news.bitrix_page_id
            mark_published = cfg["mark_published"]
            if not article.strip():
                raise BitrixError("Нет текста статьи — сначала сгенерируйте или вставьте HTML-код")

        if page_id:
            action = "обновлена"
        else:
            folder_id = next((p["folder_id"] for p in list_pages(webhook, site_id) if p["id"] == template_id), None)
            params = {"lid": template_id, "toSiteId": site_id}
            if folder_id:
                params["toFolderId"] = int(folder_id)
            page_id = int(call(webhook, "landing.landing.copy", params))
            action = "опубликована"

        blocks = call(webhook, "landing.block.getlist", {"lid": page_id, "params": {"edit_mode": 1}}) or []
        inserted = False
        for block in blocks:
            block_id = int(block.get("id") or block.get("ID"))
            content = (call(webhook, "landing.block.getcontent",
                            {"lid": page_id, "block": block_id, "editMode": 1}) or {}).get("content", "")
            if not any(m in content for m in MARKERS.values()) and "publisher-" not in content:
                continue
            image_html = None
            if MARKERS["image"] in content or "publisher-image" in content:
                image_html = ""  # картинки нет — метку просто убираем
                if image:
                    src = _upload_image(webhook, block_id, image)
                    image_html = f'<img src="{src}" alt="{title}">' if src else ""
            new_content, changed = _fill(content, {"article": article, "title": title, "image": image_html})
            if changed:
                call(webhook, "landing.block.updatecontent", {"lid": page_id, "block": block_id, "content": new_content})
                inserted = inserted or "publisher-article" in new_content
        if not inserted:
            raise BitrixError("На странице-шаблоне не найдена метка %%ARTICLE%%. Добавьте в шаблон блок "
                              "«HTML-код» и впишите в него %%ARTICLE%%")

        fields = {"TITLE": title[:255]}
        if action == "опубликована":
            fields["CODE"] = slugify(title)
        extra = {k: v for k, v in {"METAMAIN_TITLE": meta_title or title, "METAMAIN_DESCRIPTION": meta_desc,
                                    "METAOG_TITLE": meta_title or title, "METAOG_DESCRIPTION": meta_desc}.items() if v}
        if extra:
            fields["ADDITIONAL_FIELDS"] = extra
        call(webhook, "landing.landing.update", {"lid": page_id, "fields": fields})
        call(webhook, "landing.landing.publication", {"lid": page_id})
        url = call(webhook, "landing.landing.getpublicurl", {"lid": page_id})
        if isinstance(url, dict):
            url = url.get(str(page_id)) or next(iter(url.values()), "")

        with session_scope() as db:
            news = db.get(News, news_id)
            news.bitrix_page_id, news.bitrix_url = page_id, (url or "")[:1000] or None
            news.bitrix_state, news.bitrix_error, news.bitrix_published_at = "idle", "", now()
            if mark_published and news.status != NewsStatus.PUBLISHED:
                news.status, news.published_at = NewsStatus.PUBLISHED, date.today()
            db.add(NewsHistory(news_id=news_id, action=f"Битрикс24: страница {action}" + (f" — {url}" if url else "")))
    except Exception as e:
        if not isinstance(e, BitrixError):
            traceback.print_exc()
        with session_scope() as db:
            news = db.get(News, news_id)
            news.bitrix_state = "error"
            news.bitrix_error = (str(e) if isinstance(e, BitrixError) else f"{e.__class__.__name__}: {e}")[:1000]
            db.add(NewsHistory(news_id=news_id, action="Битрикс24: ошибка публикации"))
