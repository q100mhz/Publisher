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
import html as html_lib
import json
import re
import time
import traceback
from datetime import date

from urllib.parse import unquote

import httpx
from bs4 import BeautifulSoup, Comment

from ..config import MEDIA_DIR
from ..crypto import decrypt
from ..db import session_scope
from ..models import News, NewsHistory, NewsStatus, User, get_setting, now, set_setting
from ..security import UnsafeURL, check_url
from . import generator

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


def _seo(meta_html: str) -> dict:
    """SEO-данные и служебный комментарий из META-раздела новости."""
    soup = BeautifulSoup(meta_html or "", "html.parser")
    title = soup.find("title")
    seo = {"title": title.get_text(" ", strip=True) if title else ""}
    for name in ("description", "keywords"):
        tag = soup.find("meta", attrs={"name": name})
        seo[name] = (tag.get("content") or "").strip() if tag else ""
    notes = [c for c in soup.find_all(string=lambda t: isinstance(t, Comment)) if "Служебная часть" in c]
    seo["comment"] = f"<!--{notes[0]}-->" if notes else ""
    return seo


CSS_START, CSS_END = "/* publisher:start — стили статьи, обновляются сервисом */", "/* publisher:end */"


def _merge_css(existing: str, css: str) -> str:
    """Свой CSS страницы: стили шаблона сохраняются, стили статьи заменяются между метками."""
    existing = re.sub(re.escape(CSS_START) + r".*?" + re.escape(CSS_END), "", existing or "", flags=re.S).strip()
    block = f"{CSS_START}\n{css}\n{CSS_END}" if css else ""
    return "\n\n".join(p for p in (existing, block) if p)


def prepare_article(html_code: str, meta_html: str) -> dict:
    """Готовит статью к публикации: убирает обрывки markdown, отделяет служебную часть ответа нейросети
    (Meta Title, анализ, ключи, описание картинки) и стили. Битрикс24 вырезает тег <style> из HTML-блока,
    а текст стилей оставляет на странице, поэтому CSS уходит в собственный CSS страницы.
    Возвращает статью, CSS, SEO и очищенные поля новости, если в них что-то поменялось."""
    original = html_code or ""
    article, service = generator.split_service(generator.clean_html(original))
    meta = meta_html or ""
    if service:
        extra = generator.service_meta(service, meta)
        meta = "\n".join(p for p in (meta, extra) if p)
    article, css = generator.extract_css(article)
    css = css.replace("</", "<\\/")
    # В карточке оставляем стили в начале статьи — так предпросмотр выглядит как на сайте
    tidy = (f"<style>\n{css}\n</style>\n" if css else "") + article
    changed = service or tidy.strip() != original.strip()
    return {"article": article, "css": css, "seo": _seo(meta), "service": bool(service),
            "cleaned": (tidy, meta) if changed else None}


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


# ---------- Поиск места для статьи на странице ----------
#
# У блока «HTML-код» сам код хранится не в тексте блока, а в атрибуте элемента (тип поля «html»
# в манифесте блока). В режиме редактирования getcontent вместо кода отдаёт заглушку «HTML-код будет
# виден только на опубликованной странице», поэтому метку ищем так:
#   1) по манифесту блока находим его HTML-поле (селектор + имя атрибута) и читаем значение атрибута;
#   2) если прочитать не удалось — смотрим опубликованную версию шаблона, где код уже отрисован,
#      и берём блок с меткой по его порядковому номеру;
#   3) если HTML-блок на странице один — используем его.
# Статья записывается методом landing.block.updateattrs в найденный атрибут.

# Служебные ключи манифеста — это не селекторы элементов
MANIFEST_KEYS = {"attrs", "nodes", "style", "block", "cards", "additional", "items", "menu", "assets", "callbacks",
                 "lang", "lang_original", "fields", "group", "type", "name", "attribute", "value", "dynamic", "subtype",
                 "subtype_params", "namespace", "code", "preview", "timestamp", "sections", "manifest", "children", "root"}


def _is_html_field(field: dict) -> bool:
    kind = str(field.get("type", "")).lower()
    attribute = str(field.get("attribute", "")).lower()
    name = str(field.get("name", "")).lower()
    return bool(attribute) and (kind in ("html", "htmlcode", "html-code", "code", "textarea")
                                or "html" in attribute or "html" in name)


def _html_fields(node, selector: str | None = None):
    """(селектор, атрибут) полей «HTML код» из манифеста блока (где бы в манифесте они ни были описаны)."""
    if isinstance(node, dict):
        if selector and _is_html_field(node):
            yield selector, node["attribute"]
        for key, value in node.items():
            is_selector = isinstance(key, str) and key not in MANIFEST_KEYS and not key.isdigit()
            yield from _html_fields(value, key if is_selector else selector)
    elif isinstance(node, list):
        for item in node:
            yield from _html_fields(item, selector)


def _manifest_excerpt(manifest) -> str:
    """Короткая выжимка манифеста для отчёта «Проверить шаблон»."""
    if not isinstance(manifest, dict):
        return json.dumps(manifest, ensure_ascii=False)[:600]
    part = {k: manifest[k] for k in ("block", "attrs", "nodes", "style", "cards") if k in manifest}
    if isinstance(part.get("block"), dict):
        part["block"] = {k: v for k, v in part["block"].items() if k in ("name", "type", "subtype", "subtype_params", "html")}
    return json.dumps(part or {"keys": list(manifest)[:20]}, ensure_ascii=False)[:1500]


def _decoded(value: str) -> str:
    """Значение атрибута во всех вероятных кодировках, склеенное в одну строку для поиска метки."""
    variants = [value, html_lib.unescape(value), unquote(value), unquote(unquote(value))]
    if re.fullmatch(r"[A-Za-z0-9+/=\s]{8,}", value or ""):
        try:
            variants.append(base64.b64decode(value).decode("utf-8", "ignore"))
        except ValueError:
            pass
    return "\n".join(variants)


def _blocks(webhook: str, lid: int, *, edit: bool = True, content: bool = False) -> list[dict]:
    params = {"edit_mode": 1} if edit else {}
    if content:
        params["get_content"] = True
    return call(webhook, "landing.block.getlist", {"lid": lid, "params": params}) or []


def _block_info(webhook: str, lid: int, block: dict) -> dict:
    block_id = int(block.get("id") or block.get("ID"))
    manifest = call(webhook, "landing.block.getmanifest", {"lid": lid, "block": block_id, "params": {"edit_mode": 1}}) or {}
    # wrapper_show=false — HTML самого блока, без служебной обёртки редактора: его можно вернуть в updatecontent
    content = (call(webhook, "landing.block.getcontent", {"lid": lid, "block": block_id, "editMode": 1,
                                                           "params": {"wrapper_show": False}}) or {}).get("content", "")
    fields = list(dict.fromkeys(_html_fields(manifest)))
    values, raw = {}, {}
    soup = BeautifulSoup(content, "html.parser")
    for selector, attribute in fields:
        try:
            elements = soup.select(selector)
        except Exception:  # нестандартный селектор
            elements = []
        attr_values = [str(el.get(attribute, "")) for el in elements]
        values[(selector, attribute)] = "\n".join(_decoded(v) for v in attr_values)
        raw[(selector, attribute)] = html_lib.unescape(attr_values[0]) if attr_values else ""
    return {"id": block_id, "code": block.get("code") or block.get("CODE") or "", "content": content,
            "fields": fields, "values": values, "raw": raw, "manifest": _manifest_excerpt(manifest)}


def _find_article_target(webhook: str, page_id: int, template_id: int, infos: list[dict]):
    """(блок, селектор, атрибут), куда вставлять статью, или None."""
    for info in infos:
        for field, value in info["values"].items():
            if MARKERS["article"] in value or "publisher-article" in value:
                return info, *field
    # Значение поля не читается — ищем метку в опубликованных версиях (шаблона и самой страницы), где код отрисован
    for lid in dict.fromkeys((template_id, page_id)):
        try:
            published = _blocks(webhook, lid, edit=False, content=True)
        except BitrixError:
            continue
        for index, block in enumerate(published):
            if MARKERS["article"] in (block.get("content") or "") and index < len(infos) and infos[index]["fields"]:
                return infos[index], *infos[index]["fields"][0]
    html_blocks = [info for info in infos if info["fields"]]
    if len(html_blocks) == 1:
        return html_blocks[0], *html_blocks[0]["fields"][0]
    return None


def _code_from_rendered(content: str, selector: str) -> str | None:
    """Код HTML-блока из его отрисованной (опубликованной) версии: внутренность самого глубокого элемента,
    в котором есть все метки блока. Так из разметки Битрикс24 вынимается ровно то, что вписано в поле «HTML код»."""
    present = [m for m in MARKERS.values() if m in content]
    if MARKERS["article"] not in present:
        return None
    soup = BeautifulSoup(content, "html.parser")
    element = None
    if not _is_component(selector):
        try:
            element = soup.select_one(selector)
        except Exception:
            element = None
    if element is not None and all(m in element.decode_contents() for m in present):
        return element.decode_contents().strip()
    nodes = [el for el in soup.find_all(True) if all(m in el.decode_contents() for m in present)]
    return (nodes[-1].decode_contents() if nodes else content).strip()


def _template_code(webhook: str, template_id: int, index: int, selector: str, attribute: str,
                   page_id: int | None = None) -> str | None:
    """Исходный код HTML-блока с метками — его заполняем при каждой публикации, поэтому повторное
    обновление тоже работает. Источники: поле блока в шаблоне, опубликованный шаблон, опубликованная копия."""
    sources = []
    try:
        template_blocks = _blocks(webhook, template_id)
        if index < len(template_blocks):
            raw = _block_info(webhook, template_id, template_blocks[index])["raw"].get((selector, attribute), "")
            if MARKERS["article"] in raw:
                return raw
        sources.append(_blocks(webhook, template_id, edit=False, content=True))
    except BitrixError:
        pass
    if page_id:
        try:
            sources.append(_blocks(webhook, page_id, edit=False, content=True))
        except BitrixError:
            pass
    for published in sources:
        for block in ([published[index]] if index < len(published) else []) + published:
            code = _code_from_rendered(block.get("content") or "", selector)
            if code:
                return code
    return None


def _fill_code(code: str, article: str, title: str, image_html: str) -> str:
    article_html = f'<div class="publisher-article">{article}</div>'
    # Метка статьи одна в абзаце — заменяем абзац целиком: блочные теги внутри <p> браузеры ломают
    code = re.sub(r"<p[^>]*>\s*" + re.escape(MARKERS["article"]) + r"\s*</p>", lambda _: article_html, code)
    if not image_html:  # картинки нет — убираем и метку, и опустевший абзац вокруг неё
        code = re.sub(r"<p[^>]*>\s*" + re.escape(MARKERS["image"]) + r"\s*</p>", "", code)
    return (code.replace(MARKERS["article"], article_html)
                .replace(MARKERS["title"], html_lib.escape(title))
                .replace(MARKERS["image"], image_html))


def _markers_left(webhook: str, page_id: int) -> bool:
    """Остались ли метки %%…%% на опубликованной странице."""
    try:
        published = _blocks(webhook, page_id, edit=False, content=True)
    except BitrixError:
        return False  # проверить не удалось — не считаем это ошибкой публикации
    return any(m in (b.get("content") or "") for b in published for m in MARKERS.values())


def _write_by_content(webhook: str, page_id: int, info: dict, selector: str, attribute: str, code: str) -> None:
    """Запасной способ: код записывается и в атрибут элемента, и внутрь него — через замену содержимого блока."""
    soup = BeautifulSoup(info["content"], "html.parser")
    try:
        element = soup.select_one(selector)
    except Exception:
        element = None
    if element is None:
        raise BitrixError(f"В блоке не найден элемент {selector} для записи кода")
    element[attribute] = code
    element.clear()
    element.append(BeautifulSoup(code, "html.parser"))
    call(webhook, "landing.block.updatecontent", {"lid": page_id, "block": info["id"], "content": str(soup)})


def _is_component(selector: str) -> bool:
    """«bitrix:landing.blocks.html» — это блок-компонент, а не CSS-селектор."""
    return ":" in selector and not selector.startswith((".", "#", "["))


def _write_attempts(webhook: str, page_id: int, info: dict, selector: str, attribute: str, code: str):
    """Способы записать код в HTML-блок — по очереди, пока на опубликованной странице не пропадут метки.

    У блока-компонента (bitrix:landing.blocks.html) код — параметр компонента, он меняется через
    landing.block.updatenodes с ключом attrs. У обычного блока — атрибут элемента (updateattrs)
    или само содержимое блока (updatecontent).
    """
    nodes = ("параметр компонента", lambda: call(webhook, "landing.block.updatenodes", {
        "lid": page_id, "block": info["id"], "data": {selector: {"attrs": {attribute: code}}}}))
    attrs = ("атрибут блока", lambda: call(webhook, "landing.block.updateattrs", {
        "lid": page_id, "block": info["id"], "data": {selector: {attribute: code}}}))
    content = ("содержимое блока", lambda: _write_by_content(webhook, page_id, info, selector, attribute, code))
    return [nodes, attrs] if _is_component(selector) else [attrs, content, nodes]


def describe_blocks(infos: list[dict]) -> str:
    parts = []
    for n, info in enumerate(infos, 1):
        fields = ", ".join(f"{sel} [{attr}]" for sel, attr in info["fields"]) or "нет HTML-поля"
        found = any(MARKERS["article"] in v for v in info["values"].values()) or MARKERS["article"] in info["content"]
        parts.append(f"№{n} {info['code'] or 'блок'} — {fields}{' — метка найдена' if found else ''}")
    return "; ".join(parts)


def check_template(webhook: str, template_id: int) -> dict:
    """Проверка страницы-шаблона без изменений на портале: где стоят метки.

    Кроме таблицы возвращает подробный отчёт (манифест и содержимое HTML-блоков) — по нему видно,
    как конкретный портал хранит код HTML-блока, если автоматически найти метку не удалось.
    """
    infos = [_block_info(webhook, template_id, b) for b in _blocks(webhook, template_id)]
    target = _find_article_target(webhook, template_id, template_id, infos)
    try:
        published = _blocks(webhook, template_id, edit=False, content=True)
    except BitrixError as e:
        published = []
        published_error = str(e)
    else:
        published_error = "" if published else "опубликованной версии шаблона нет"
    rows, report = [], [f"Шаблон: страница {template_id}. Блоков: {len(infos)}. "
                        f"Опубликованная версия: {published_error or f'{len(published)} блоков'}."]
    for n, info in enumerate(infos, 1):
        text = info["content"]
        public = (published[n - 1].get("content") or "") if n <= len(published) else ""
        marker_where = [where for where, ok in (
            ("поле атрибута", any(MARKERS["article"] in v for v in info["values"].values())),
            ("черновик блока", MARKERS["article"] in text),
            ("опубликованная версия", MARKERS["article"] in public)) if ok]
        rows.append({
            "n": n, "code": info["code"],
            "html_fields": [f"{sel} [{attr}]" for sel, attr in info["fields"]],
            "article": bool(target and target[0]["id"] == info["id"]),
            "marker": ", ".join(marker_where),
            "title": MARKERS["title"] in text, "image": MARKERS["image"] in text,
        })
        report.append(f"\n--- Блок №{n} (id {info['id']}, код {info['code']}) ---\n"
                      f"Метка %%ARTICLE%%: {', '.join(marker_where) or 'не найдена'}\n"
                      f"HTML-поля: {', '.join(rows[-1]['html_fields']) or 'нет'}\n"
                      f"Манифест: {info['manifest']}\n"
                      f"Черновик: {text[:400]}\n"
                      f"Опубликовано: {public[:400] or '—'}")
    return {"ok": bool(target), "blocks": rows, "report": "\n".join(report)}


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
    page_id, published_now, webhook = None, False, ""
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
            prepared = prepare_article(news.html_code, news.meta_html)
            if prepared["cleaned"]:
                # Служебную часть из текста статьи переносим в META, чтобы в карточке было то же, что на сайте
                news.html_code, news.meta_html = prepared["cleaned"]
                if prepared["service"]:
                    db.add(NewsHistory(news_id=news_id, action="Служебная часть ответа нейросети (Meta, анализ, "
                                                               "описание картинки) перенесена из текста статьи в META"))
            article, css, seo = prepared["article"], prepared["css"], prepared["seo"]
            image = (news.generated_images or news.original_images or [None])[0]
            page_id = news.bitrix_page_id
            user_id = user.id
            saved_raw = get_setting(db, user.id, f"bitrix_tpl:{template_id}", "")
            first_time = not news.bitrix_url  # страница ещё ни разу не публиковалась успешно
            mark_published = cfg["mark_published"]
            if not article.strip():
                raise BitrixError("Нет текста статьи — сначала сгенерируйте или вставьте HTML-код")

        action = "опубликована" if first_time else "обновлена"
        published_now = False
        if not page_id:
            folder_id = next((p["folder_id"] for p in list_pages(webhook, site_id) if p["id"] == template_id), None)
            params = {"lid": template_id, "toSiteId": site_id}
            if folder_id:
                params["toFolderId"] = int(folder_id)
            page_id = int(call(webhook, "landing.landing.copy", params))
            # Сразу запоминаем копию: при ошибке ниже повторная попытка возьмёт её, а не создаст ещё одну
            with session_scope() as db:
                db.get(News, news_id).bitrix_page_id = page_id

        infos = [_block_info(webhook, page_id, b) for b in _blocks(webhook, page_id)]
        saved = json.loads(saved_raw) if saved_raw else {}
        target = _find_article_target(webhook, page_id, template_id, infos)
        if not target and saved and saved.get("index", -1) < len(infos):
            # Меток на странице уже нет (статья вставлена раньше) — берём блок, найденный при прошлой публикации
            target = (infos[saved["index"]], saved["selector"], saved["attribute"])
        if not target:
            raise BitrixError("Не удалось определить, в какой блок вставить статью: метка %%ARTICLE%% не найдена. "
                              "Нажмите «Настройки» → «Битрикс24» → «Проверить шаблон» и пришлите подробный отчёт. "
                              "Блоки страницы: "
                              + describe_blocks(infos))
        info, selector, attribute = target
        index = [i["id"] for i in infos].index(info["id"])
        code = (_template_code(webhook, template_id, index, selector, attribute, page_id)
                or saved.get("code") or MARKERS["article"])
        image_html = ""
        if MARKERS["image"] in code and image:
            src = _upload_image(webhook, info["id"], image)
            image_html = f'<img src="{src}" alt="{html_lib.escape(title)}">' if src else ""
        filled = _fill_code(code, article + ("\n" + seo["comment"] if seo["comment"] else ""), title, image_html)

        # %%TITLE%% и %%IMAGE%% в обычных текстовых блоках (если они там есть) — их видно в содержимом блока
        for other in infos:
            if other["id"] == target[0]["id"]:
                continue
            block_id, content = other["id"], other["content"]
            if not any(m in content for m in (MARKERS["title"], MARKERS["image"])) \
                    and "publisher-title" not in content and "publisher-image" not in content:
                continue
            image_html = None
            if MARKERS["image"] in content or "publisher-image" in content:
                image_html = ""  # картинки нет — метку просто убираем
                if image:
                    src = _upload_image(webhook, block_id, image)
                    image_html = f'<img src="{src}" alt="{title}">' if src else ""
            new_content, changed = _fill(content, {"article": None, "title": title, "image": image_html})
            if changed:
                call(webhook, "landing.block.updatecontent", {"lid": page_id, "block": block_id, "content": new_content})

        fields = {"TITLE": title[:255]}
        if first_time:
            fields["CODE"] = slugify(title)
        extra = {k: v for k, v in {"METAMAIN_TITLE": seo["title"] or title, "METAMAIN_DESCRIPTION": seo["description"],
                                    "METAMAIN_KEYWORDS": seo["keywords"], "METAOG_TITLE": seo["title"] or title,
                                    "METAOG_DESCRIPTION": seo["description"]}.items() if v}
        extra["METAMAIN_USE"] = "Y"
        current_css = ""
        try:
            current = call(webhook, "landing.landing.getadditionalfields", {"lid": page_id}) or {}
            current_css = current.get("CSSBLOCK_CODE") or "" if isinstance(current, dict) else ""
        except BitrixError:
            pass
        if css or CSS_START in current_css:
            extra["CSSBLOCK_USE"] = "Y"
            extra["CSSBLOCK_CODE"] = _merge_css(current_css, css)
        fields["ADDITIONAL_FIELDS"] = extra
        notes = []
        try:
            call(webhook, "landing.landing.update", {"lid": page_id, "fields": fields})
            if css:
                notes.append("стили статьи — в CSS страницы")
        except BitrixError as e:
            # Портал не принял часть дополнительных полей — сохраняем название и основное SEO без них
            for key in ("CSSBLOCK_USE", "CSSBLOCK_CODE", "METAMAIN_KEYWORDS"):
                extra.pop(key, None)
            call(webhook, "landing.landing.update", {"lid": page_id, "fields": fields})
            if css:
                notes.append(f"портал не принял CSS страницы ({e}) — оформление статьи не применено")
        if prepared["service"]:
            notes.append("служебная часть ответа нейросети на странице скрыта")

        # Записываем код HTML-блока, публикуем и проверяем, что меток на сайте не осталось
        tried = []
        for way, write in _write_attempts(webhook, page_id, info, selector, attribute, filled):
            try:
                write()
            except BitrixError as e:
                tried.append(f"{way}: {e}")
                continue
            call(webhook, "landing.landing.publication", {"lid": page_id})
            published_now = True
            if not _markers_left(webhook, page_id):
                break
            tried.append(f"{way}: портал не применил изменения")
        else:
            raise BitrixError("Битрикс24 не принял новый код HTML-блока (" + "; ".join(tried) + "). "
                              "Страница снята с публикации, чтобы на сайте не висели метки %%…%%. Нажмите «Настройки» → "
                              "«Битрикс24» → «Проверить шаблон», скопируйте подробный отчёт и пришлите разработчику.")
        # Запоминаем, куда и по какому шаблону вставлялась статья, — для следующих обновлений
        with session_scope() as db:
            set_setting(db, user_id, f"bitrix_tpl:{template_id}", json.dumps(
                {"index": index, "selector": selector, "attribute": attribute, "code": code}, ensure_ascii=False))
        url = call(webhook, "landing.landing.getpublicurl", {"lid": page_id})
        if isinstance(url, dict):
            url = url.get(str(page_id)) or next(iter(url.values()), "")

        with session_scope() as db:
            news = db.get(News, news_id)
            news.bitrix_page_id, news.bitrix_url = page_id, (url or "")[:1000] or None
            news.bitrix_state, news.bitrix_error, news.bitrix_published_at = "idle", "", now()
            if mark_published and news.status != NewsStatus.PUBLISHED:
                news.status, news.published_at = NewsStatus.PUBLISHED, date.today()
            db.add(NewsHistory(news_id=news_id, action=f"Битрикс24: страница {action}" + (f" — {url}" if url else "")
                                                       + (f" ({'; '.join(notes)})" if notes else "")))
    except Exception as e:
        if not isinstance(e, BitrixError):
            traceback.print_exc()
        # Если страница с метками успела попасть на сайт — снимаем её с публикации
        try:
            if page_id and published_now and _markers_left(webhook, page_id):
                call(webhook, "landing.landing.unpublic", {"lid": page_id})
        except Exception:
            pass
        with session_scope() as db:
            news = db.get(News, news_id)
            news.bitrix_state = "error"
            news.bitrix_error = (str(e) if isinstance(e, BitrixError) else f"{e.__class__.__name__}: {e}")[:1000]
            db.add(NewsHistory(news_id=news_id, action="Битрикс24: ошибка публикации"))
