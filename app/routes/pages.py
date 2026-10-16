"""Вход и главная страница (дашборд + реестр)."""
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..auth import client_ip, is_locked, register_fail, require_user, reset_fails, verify_password
from ..db import get_db
from ..models import News, NewsHistory, NewsStatus, ParserRun, Source, User, now
from ..services import scheduler
from ..web import flash, redirect, render

router = APIRouter()
PAGE_SIZE = 50
PERIODS = {"today": "Сегодня", "week": "7 дней", "month": "30 дней", "all": "Всё время"}


@router.get("/login")
def login_page(request: Request):
    if request.session.get("user_id"):
        return redirect("/")
    return render(request, "login.html")


@router.post("/login")
def login(request: Request, login: str = Form(""), password: str = Form(""), db: Session = Depends(get_db)):
    ip = client_ip(request)
    wait = is_locked(ip)
    if wait:
        return render(request, "login.html", error=f"Слишком много попыток. Попробуйте через {wait // 60 + 1} мин.",
                      login=login)
    user = db.scalar(select(User).where(User.login == login.strip()))
    if not user or not verify_password(password, user.password_hash):
        register_fail(ip)
        return render(request, "login.html", error="Неверный логин или пароль", login=login)
    if not user.is_active:
        return render(request, "login.html", error="Учётная запись отключена. Обратитесь к администратору.",
                      login=login)
    reset_fails(ip)
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["login"] = user.login
    return redirect("/")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


def _period_start(period: str) -> datetime | None:
    today = datetime.combine(date.today(), datetime.min.time())
    return {"today": today, "week": today - timedelta(days=6), "month": today - timedelta(days=29)}.get(period)


@router.get("/")
def index(
    request: Request,
    period: str = "all",
    status: str | None = None,  # не указан — показываем «в работе»
    source: str = "",
    q: str = "",
    archive: str = "",   # "" — без архива, "1" — только архив
    dup: str = "",       # "" — все, "1" — только дубли, "0" — без дублей
    publish: str = "",   # "today" — к публикации сегодня и просроченные
    page: int = 1,
    user_id: int = Depends(require_user),
    db: Session = Depends(get_db),
):
    period = period if period in PERIODS else "all"
    start = _period_start(period)
    today = date.today()
    if status is None:
        # По умолчанию — новости в работе. В архиве и в «опубликовать сегодня» — все статусы
        status = "all" if archive == "1" or publish == "today" else "work"

    # Дашборд: считается за выбранный период, архив — отдельной плиткой
    base = select(func.count(News.id)).where(News.user_id == user_id)
    if start:
        base = base.where(News.created_at >= start)
    live = base.where(News.archived.is_(False))
    stats = {
        "total": db.scalar(live),
        "new": db.scalar(live.where(News.status.in_([NewsStatus.NEW, NewsStatus.GENERATED]))),
        "published": db.scalar(live.where(News.status == NewsStatus.PUBLISHED)),
        "postponed": db.scalar(live.where(News.status == NewsStatus.POSTPONED)),
        "archived": db.scalar(base.where(News.archived.is_(True))),
        "due": db.scalar(select(func.count(News.id)).where(
            News.user_id == user_id, News.archived.is_(False), News.status != NewsStatus.PUBLISHED,
            News.planned_publish_date.is_not(None), News.planned_publish_date <= today)),
    }

    query = (select(News).options(selectinload(News.source), selectinload(News.images))
             .where(News.user_id == user_id))
    if start:
        query = query.where(News.created_at >= start)
    query = query.where(News.archived.is_(archive == "1"))
    if status in NewsStatus.LABELS:
        query = query.where(News.status == status)
    elif status == "work":
        query = query.where(News.status.in_([NewsStatus.NEW, NewsStatus.GENERATED]))
    if source == "manual":
        query = query.where(News.source_id.is_(None))
    elif source.isdigit():
        query = query.where(News.source_id == int(source))
    if dup in ("0", "1"):
        query = query.where(News.is_duplicate.is_(dup == "1"))
    if publish == "today":
        query = query.where(News.status != NewsStatus.PUBLISHED, News.planned_publish_date.is_not(None),
                            News.planned_publish_date <= today)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(News.title.ilike(like), News.original_text.ilike(like)))

    total = db.scalar(select(func.count()).select_from(query.subquery()))
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(1, page), pages)
    items = db.scalars(query.order_by(News.created_at.desc(), News.id.asc())
                       .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()

    last_run = db.scalar(select(ParserRun).where(ParserRun.user_id == user_id).order_by(ParserRun.id.desc()).limit(1))
    filters = dict(period=period, status=status, source=source, q=q, archive=archive, dup=dup, publish=publish)
    return render(
        request, "index.html", active="registry", stats=stats, items=items, total=total, page=page,
        pages=pages, filters=filters, periods=PERIODS, statuses=NewsStatus.LABELS,
        sources=db.scalars(select(Source).where(Source.user_id == user_id).order_by(Source.name)).all(),
        last_run=last_run, auto_enabled=scheduler.is_enabled(user_id), next_run=scheduler.next_run_time(user_id),
        running=db.scalar(select(func.count(Source.id)).where(Source.user_id == user_id, Source.is_running.is_(True))),
    )


@router.post("/news/bulk")
async def bulk(request: Request, user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    form = await request.form()
    ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
    action = form.get("action", "")
    back = str(form.get("back", "/"))
    if not back.startswith("/") or back.startswith("//"):
        back = "/"
    if not ids:
        flash(request, "Ничего не выбрано", "warn")
        return redirect(back)
    items = db.scalars(select(News).where(News.id.in_(ids), News.user_id == user_id)).all()
    for item in items:
        if action == "archive" and not item.archived:
            item.archived, item.archived_at = True, now()
            db.add(NewsHistory(news_id=item.id, action="Отправлена в архив (массово)"))
        elif action == "unarchive" and item.archived:
            item.archived, item.archived_at = False, None
            db.add(NewsHistory(news_id=item.id, action="Возвращена из архива (массово)"))
        elif action == "postpone" and item.status != NewsStatus.POSTPONED:
            item.status = NewsStatus.POSTPONED
            db.add(NewsHistory(news_id=item.id, action="Статус: Отложена (массово)"))
        elif action == "duplicate":
            item.is_duplicate = True
            db.add(NewsHistory(news_id=item.id, action="Помечена как дубль (массово)"))
    db.commit()
    flash(request, f"Обработано записей: {len(items)}")
    return redirect(back)
