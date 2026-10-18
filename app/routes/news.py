"""Карточка новости: создание, редактирование, статусы, картинки, генерация."""
from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..auth import require_user
from ..db import get_db
from ..models import News, NewsHistory, NewsImage, NewsStatus, NewsVersion, Prompt, now
from ..services import ai, bitrix, generator
from ..services.images import ImageError, media_path, remove_files, save_image
from ..security import clean_text, safe_filename
from ..services.parser import executor
from ..web import flash, redirect, render

router = APIRouter()
LIMITS = {"text": 300_000, "html": 500_000, "meta": 20_000}  # символов
MAX_UPLOAD = 25 * 1024 * 1024  # 25 МБ на файл


def _ai_error(db: Session, user) -> str:
    """Пустая строка — нейросеть настроена; иначе — что нужно сделать."""
    try:
        ai.user_config(db, user)
        return ""
    except ai.AIError as e:
        return str(e)


def _img_error(db: Session, user) -> str:
    try:
        ai.image_config(db, user)
        return ""
    except ai.AIError as e:
        return str(e)


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _parse_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _get(db: Session, news_id: int, user_id: int) -> News:
    """Новость текущего пользователя. Чужая запись выглядит так же, как несуществующая."""
    news = db.get(News, news_id, options=[selectinload(News.images), selectinload(News.source)])
    if not news or news.user_id != user_id:
        raise HTTPException(404, "Запись не найдена")
    return news


async def _store_uploads(db: Session, news: News, files: list[UploadFile]) -> list[str]:
    errors = []
    for f in files:
        if not f or not f.filename:
            continue
        data = await f.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            errors.append(f"«{f.filename}» больше 25 МБ")
            continue
        try:
            info = save_image(data, safe_filename(f.filename, "image"))
        except ImageError as e:
            errors.append(str(e))
            continue
        db.add(NewsImage(news_id=news.id, **info))
    return errors


@router.get("/news/new")
def new_form(request: Request, user_id: int = Depends(require_user)):
    return render(request, "news_new.html", active="registry", now=now())


@router.post("/news/new")
async def create(
    request: Request,
    title: str = Form(...),
    created_at: str = Form(""),
    original_text: str = Form(""),
    html_code: str = Form(""),
    planned_publish_date: str = Form(""),
    images: list[UploadFile] = File(default=[]),
    user_id: int = Depends(require_user),
    db: Session = Depends(get_db),
):
    original_text, html_code = clean_text(original_text, LIMITS["text"]), clean_text(html_code, LIMITS["html"])
    news = News(
        user_id=user_id,
        title=clean_text(title, 1000).strip() or "Без заголовка",
        created_at=_parse_datetime(created_at) or now(),
        original_text=original_text,
        html_code=html_code,
        status=NewsStatus.GENERATED if html_code.strip() else NewsStatus.NEW,
        planned_publish_date=_parse_date(planned_publish_date),
        origin="manual",
    )
    db.add(news)
    db.flush()
    db.add(NewsHistory(news_id=news.id, action="Создана вручную"))
    for err in await _store_uploads(db, news, images):
        flash(request, err, "warn")
    db.commit()
    flash(request, "Новость добавлена в реестр")
    return redirect(f"/news/{news.id}")


@router.get("/news/{news_id}")
def card(request: Request, news_id: int, user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    news = _get(db, news_id, user_id)
    prompts = db.scalars(select(Prompt).where(Prompt.user_id == user_id)
                         .order_by(Prompt.is_default.desc(), Prompt.name)).all()
    image_prompts = [p for p in prompts if p.kind == "image"]
    title_prompts = [p for p in prompts if p.kind == "title"]
    prompts = [p for p in prompts if p.kind == "text"]
    return render(
        request, "news_card.html", active="registry", news=news, prompts=prompts,
        statuses=NewsStatus.LABELS, versions=news.versions[:20], history=news.history[:50],
        ai_error=_ai_error(db, request.state.user), image_prompts=image_prompts, title_prompts=title_prompts,
        privacy=generator.privacy_report(news),
        bitrix_problem=bitrix.ready(db, request.state.user) if request.state.user.bitrix_allowed else None,
        img_error=_img_error(db, request.state.user),
    )


@router.post("/news/{news_id}")
def save(
    request: Request,
    news_id: int,
    title: str = Form(...),
    created_at: str = Form(""),
    original_text: str = Form(""),
    html_code: str = Form(""),
    new_title: str = Form(""),
    meta_html: str = Form(""),
    planned_publish_date: str = Form(""),
    published_at: str = Form(""),
    user_id: int = Depends(require_user),
    db: Session = Depends(get_db),
):
    news = _get(db, news_id, user_id)
    changes = []
    new_values = {
        "title": (clean_text(title, 1000).strip() or news.title, "заголовок"),
        "created_at": (_parse_datetime(created_at) or news.created_at, "дата создания"),
        "original_text": (clean_text(original_text, LIMITS["text"]), "текст оригинала"),
        "html_code": (clean_text(html_code, LIMITS["html"]), "HTML-код"),
        "new_title": (clean_text(new_title, 1000).strip() or None, "изменённый заголовок"),
        "meta_html": (clean_text(meta_html, LIMITS["meta"]).strip(), "META"),
        "planned_publish_date": (_parse_date(planned_publish_date), "плановая дата публикации"),
        "published_at": (_parse_date(published_at), "фактическая дата публикации"),
    }
    for field, (value, label) in new_values.items():
        if getattr(news, field) != value:
            setattr(news, field, value)
            changes.append(label)
    if "HTML-код" in changes and news.html_code.strip() and news.status == NewsStatus.NEW:
        news.status = NewsStatus.GENERATED
    if news.published_at and news.status != NewsStatus.PUBLISHED:
        news.status = NewsStatus.PUBLISHED
        changes.append("статус → Опубликована")
    if changes:
        db.add(NewsHistory(news_id=news.id, action="Изменено: " + ", ".join(changes)))
        db.commit()
        flash(request, "Сохранено")
    else:
        flash(request, "Изменений нет", "info")
    return redirect(f"/news/{news_id}")


ACTIONS = {
    "publish": "Статус: Опубликована",
    "postpone": "Статус: Отложена",
    "to_work": "Возвращена в работу",
    "archive": "Отправлена в архив",
    "unarchive": "Возвращена из архива",
    "duplicate_on": "Помечена как дубль",
    "duplicate_off": "Снята отметка «дубль»",
}


@router.post("/news/{news_id}/action")
def action(request: Request, news_id: int, action: str = Form(...), user_id: int = Depends(require_user),
           db: Session = Depends(get_db)):
    news = _get(db, news_id, user_id)
    if action not in ACTIONS:
        raise HTTPException(400, "Неизвестное действие")
    if action == "publish":
        news.status = NewsStatus.PUBLISHED
        news.published_at = news.published_at or date.today()
    elif action == "postpone":
        news.status = NewsStatus.POSTPONED
    elif action == "to_work":
        news.status = NewsStatus.GENERATED if news.html_code.strip() else NewsStatus.NEW
        news.published_at = None
    elif action == "archive":
        news.archived, news.archived_at = True, now()
    elif action == "unarchive":
        news.archived, news.archived_at = False, None
    elif action == "duplicate_on":
        news.is_duplicate = True
    elif action == "duplicate_off":
        news.is_duplicate = False
    db.add(NewsHistory(news_id=news.id, action=ACTIONS[action]))
    db.commit()
    flash(request, ACTIONS[action])
    return redirect(f"/news/{news_id}")


QUICK_STATUSES = {**NewsStatus.LABELS, "archive": "В архив", "unarchive": "Вернуть из архива"}


@router.post("/news/{news_id}/quick")
def quick_update(news_id: int, status: str | None = Form(None), planned_publish_date: str | None = Form(None),
                 user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    """Изменение статуса или плановой даты прямо из реестра (без перехода в карточку)."""
    news = _get(db, news_id, user_id)
    if status is not None:
        if status not in QUICK_STATUSES:
            return JSONResponse({"ok": False, "error": "Неизвестный статус"}, 400)
        if status == "archive":
            news.archived, news.archived_at = True, now()
        elif status == "unarchive":
            news.archived, news.archived_at = False, None
        elif status != news.status:
            news.status = status
            if status == NewsStatus.PUBLISHED:
                news.published_at = news.published_at or date.today()
            else:
                news.published_at = None
        db.add(NewsHistory(news_id=news.id, action=f"Из реестра: {QUICK_STATUSES[status]}"))
    if planned_publish_date is not None:
        value = _parse_date(planned_publish_date)
        if value != news.planned_publish_date:
            news.planned_publish_date = value
            db.add(NewsHistory(news_id=news.id, action="Из реестра: плановая дата публикации "
                                                        + (value.strftime("%d.%m.%Y") if value else "снята")))
    db.commit()
    return {
        "ok": True,
        "status": news.status,
        "label": news.status_label,
        "archived": news.archived,
        "flag": news.publish_flag(date.today()),
        "published_at": news.published_at.strftime("%d.%m.%Y") if news.published_at else "",
        "planned": news.planned_publish_date.isoformat() if news.planned_publish_date else "",
    }


@router.post("/news/{news_id}/images")
async def upload_images(request: Request, news_id: int, images: list[UploadFile] = File(default=[]),
                        user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    news = _get(db, news_id, user_id)
    errors = await _store_uploads(db, news, images)
    db.add(NewsHistory(news_id=news.id, action="Добавлены картинки"))
    db.commit()
    for err in errors:
        flash(request, err, "warn")
    return redirect(f"/news/{news_id}#images")


@router.post("/news/{news_id}/images/{image_id}/delete")
def delete_image(request: Request, news_id: int, image_id: int, user_id: int = Depends(require_user),
                 db: Session = Depends(get_db)):
    _get(db, news_id, user_id)
    img = db.get(NewsImage, image_id)
    if not img or img.news_id != news_id:
        raise HTTPException(404)
    remove_files(img.filename, img.thumb_filename)
    db.delete(img)
    db.add(NewsHistory(news_id=news_id, action=f"Удалена картинка «{img.original_name}»"))
    db.commit()
    flash(request, "Картинка удалена")
    return redirect(f"/news/{news_id}#images")


@router.get("/media/{image_id}/{kind}")
def media(image_id: int, kind: str, user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    img = db.get(NewsImage, image_id)
    if not img or kind not in ("thumb", "original") or img.news.user_id != user_id:
        raise HTTPException(404)
    if kind == "thumb":
        return FileResponse(media_path(img.thumb_filename), headers={"Cache-Control": "private, max-age=86400"})
    return FileResponse(media_path(img.filename), filename=img.original_name)  # скачивание в оригинале


@router.post("/news/{news_id}/generate")
def generate(request: Request, news_id: int, prompt_id: int = Form(...), user_id: int = Depends(require_user),
             db: Session = Depends(get_db)):
    news = _get(db, news_id, user_id)
    error = _ai_error(db, request.state.user)
    if error:
        return JSONResponse({"ok": False, "error": error}, 400)
    prompt = db.get(Prompt, prompt_id)
    if not prompt or prompt.user_id != user_id or prompt.kind != "text":
        return JSONResponse({"ok": False, "error": "Промпт не найден"}, 400)
    if not news.original_text.strip():
        return JSONResponse({"ok": False, "error": "Нет текста оригинальной новости — нечего переделывать"}, 400)
    if news.gen_state == "running":
        return JSONResponse({"ok": False, "error": "Генерация уже идёт"}, 409)
    generator.mark_started(db, news)
    db.commit()
    executor.submit(generator.generate, news_id, prompt_id)
    return {"ok": True}


@router.post("/news/{news_id}/generate-title")
def generate_title(request: Request, news_id: int, prompt_id: int = Form(...), user_id: int = Depends(require_user),
                   db: Session = Depends(get_db)):
    news = _get(db, news_id, user_id)
    error = _ai_error(db, request.state.user)
    if error:
        return JSONResponse({"ok": False, "error": error}, 400)
    prompt = db.get(Prompt, prompt_id)
    if not prompt or prompt.user_id != user_id or prompt.kind != "title":
        return JSONResponse({"ok": False, "error": "Промпт для заголовка не найден"}, 400)
    if not (news.original_text.strip() or news.title.strip()):
        return JSONResponse({"ok": False, "error": "Нет текста новости — не из чего делать заголовок"}, 400)
    db.close()
    try:
        title = generator.generate_title(news_id, prompt_id)
    except ai.AIError as e:
        return JSONResponse({"ok": False, "error": str(e)}, 400)
    return {"ok": True, "title": title}


@router.post("/news/{news_id}/generate-image")
def generate_image(request: Request, news_id: int, prompt_id: int = Form(...), user_id: int = Depends(require_user),
                   db: Session = Depends(get_db)):
    news = _get(db, news_id, user_id)
    error = _img_error(db, request.state.user)
    if error:
        return JSONResponse({"ok": False, "error": error}, 400)
    prompt = db.get(Prompt, prompt_id)
    if not prompt or prompt.user_id != user_id or prompt.kind != "image":
        return JSONResponse({"ok": False, "error": "Промпт для картинки не найден"}, 400)
    if news.img_state == "running":
        return JSONResponse({"ok": False, "error": "Картинка уже создаётся"}, 409)
    news.img_state, news.img_error = "running", ""
    db.commit()
    executor.submit(generator.generate_image, news_id, prompt_id)
    return {"ok": True}


@router.get("/news/{news_id}/image-generation")
def image_generation_state(news_id: int, user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    news = _get(db, news_id, user_id)
    return {"state": news.img_state, "error": news.img_error}


@router.get("/news/{news_id}/generation")
def generation_state(news_id: int, user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    news = _get(db, news_id, user_id)
    return {"state": news.gen_state, "error": news.gen_error, "status": news.status}


@router.post("/news/{news_id}/versions/{version_id}/restore")
def restore_version(request: Request, news_id: int, version_id: int, user_id: int = Depends(require_user),
                    db: Session = Depends(get_db)):
    version = db.get(NewsVersion, version_id)
    if not version or version.news_id != news_id:
        raise HTTPException(404)
    news = _get(db, news_id, user_id)
    news.html_code = version.html_code
    news.meta_html = version.meta_html or ""
    if version.title:
        news.new_title = version.title
    db.add(NewsHistory(news_id=news_id, action=f"Восстановлена версия от {version.created_at:%d.%m.%Y %H:%M}"))
    db.commit()
    flash(request, "Версия восстановлена")
    return redirect(f"/news/{news_id}")
