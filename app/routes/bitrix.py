"""Битрикс24: настройки пользователя и публикация статей. Доступно, только если разрешил администратор."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..auth import require_user
from ..crypto import encrypt
from ..db import get_db
from ..models import News, set_setting
from ..services import bitrix
from ..services.parser import executor
from ..web import flash, redirect

router = APIRouter()


def _allowed(request: Request) -> None:
    if not request.state.user.bitrix_allowed:
        raise HTTPException(403, "Публикация в Битрикс24 не разрешена администратором")


def _webhook(db: Session, request: Request, value: str) -> str:
    """Новый адрес из формы или сохранённый."""
    if value.strip():
        return bitrix.normalize_webhook(value)
    saved = bitrix.user_settings(db, request.state.user)["webhook"]
    if not saved:
        raise bitrix.BitrixError("Вставьте адрес входящего вебхука")
    return saved


@router.post("/settings/bitrix/sites")
def sites(request: Request, webhook: str = Form(""), user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    _allowed(request)
    try:
        items = bitrix.list_sites(_webhook(db, request, webhook))
    except bitrix.BitrixError as e:
        return JSONResponse({"ok": False, "error": str(e)}, 400)
    if not items:
        return JSONResponse({"ok": False, "error": "На портале не найдено ни одного сайта"}, 400)
    return {"ok": True, "sites": items}


@router.post("/settings/bitrix/pages")
def pages(request: Request, site_id: int = Form(...), webhook: str = Form(""), user_id: int = Depends(require_user),
          db: Session = Depends(get_db)):
    _allowed(request)
    try:
        items = bitrix.list_pages(_webhook(db, request, webhook), site_id)
    except bitrix.BitrixError as e:
        return JSONResponse({"ok": False, "error": str(e)}, 400)
    return {"ok": True, "pages": items}


@router.post("/settings/bitrix/check")
def check(request: Request, user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    """Проверка страницы-шаблона (только чтение — на портале ничего не меняется)."""
    _allowed(request)
    cfg = bitrix.user_settings(db, request.state.user)
    if not (cfg["webhook"] and cfg["template_id"]):
        return JSONResponse({"ok": False, "error": "Сначала сохраните вебхук, сайт и страницу-шаблон"}, 400)
    try:
        report = bitrix.check_template(cfg["webhook"], int(cfg["template_id"]))
    except bitrix.BitrixError as e:
        return JSONResponse({"ok": False, "error": str(e)}, 400)
    if str(cfg["list_id"]).isdigit():
        try:
            report["list"] = bitrix.check_news_list(cfg["webhook"], int(cfg["list_id"]))
            report["list_ok"] = True
        except bitrix.BitrixError as e:
            report["list"], report["list_ok"] = str(e), False
    return report


@router.post("/settings/bitrix")
def save(request: Request, webhook: str = Form(""), site_id: str = Form(""), template_id: str = Form(""),
         site_title: str = Form(""), template_title: str = Form(""), mark_published: str = Form(""),
         image_place: str = Form("after_h1"), list_id: str = Form(""), list_title: str = Form(""),
         remove: str = Form(""), user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    _allowed(request)
    if remove:
        for key in ("bitrix_webhook", "bitrix_site_id", "bitrix_template_id", "bitrix_site_title", "bitrix_template_title",
                    "bitrix_list_id", "bitrix_list_title"):
            set_setting(db, user_id, key, "")
        db.commit()
        flash(request, "Подключение к Битрикс24 удалено")
        return redirect("/settings#bitrix")
    if webhook.strip():
        try:
            set_setting(db, user_id, "bitrix_webhook", encrypt(bitrix.normalize_webhook(webhook)))
        except bitrix.BitrixError as e:
            flash(request, str(e), "warn")
            return redirect("/settings#bitrix")
    if site_id.isdigit():
        set_setting(db, user_id, "bitrix_site_id", site_id)
        set_setting(db, user_id, "bitrix_site_title", site_title.strip()[:200])
    if template_id.isdigit():
        set_setting(db, user_id, "bitrix_template_id", template_id)
        set_setting(db, user_id, "bitrix_template_title", template_title.strip()[:200])
    set_setting(db, user_id, "bitrix_mark_published", "1" if mark_published else "0")
    if list_id.isdigit() or list_id == "":
        set_setting(db, user_id, "bitrix_list_id", list_id)
        set_setting(db, user_id, "bitrix_list_title", list_title.strip()[:200] if list_id else "")
    if image_place in bitrix.IMAGE_PLACES:
        set_setting(db, user_id, "bitrix_image_place", image_place)
    db.commit()
    flash(request, "Настройки Битрикс24 сохранены")
    return redirect("/settings#bitrix")


def _own_news(db: Session, news_id: int, user_id: int) -> News:
    news = db.get(News, news_id)
    if not news or news.user_id != user_id:
        raise HTTPException(404, "Запись не найдена")
    return news


@router.post("/news/{news_id}/bitrix")
def publish(request: Request, news_id: int, user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    news = _own_news(db, news_id, user_id)
    problem = bitrix.ready(db, request.state.user)
    if problem:
        return JSONResponse({"ok": False, "error": problem}, 400)
    if not news.html_code.strip():
        return JSONResponse({"ok": False, "error": "Нет текста статьи — сначала сгенерируйте её"}, 400)
    if news.bitrix_state == "running":
        return JSONResponse({"ok": False, "error": "Публикация уже идёт"}, 409)
    news.bitrix_state, news.bitrix_error = "running", ""
    db.commit()
    executor.submit(bitrix.publish, news_id)
    return {"ok": True}


@router.get("/news/{news_id}/bitrix-state")
def state(news_id: int, user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    news = _own_news(db, news_id, user_id)
    return {"state": news.bitrix_state, "error": news.bitrix_error, "url": news.bitrix_url or ""}
