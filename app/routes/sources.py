"""Справочник сайтов-источников, рубильник и журнал парсера."""
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import require_user
from ..db import get_db
from ..models import News, ParserRun, Source
from ..services import parser, scheduler
from ..web import flash, fmt_dt, redirect, render

router = APIRouter()


def _clean_url(value: str) -> str | None:
    value = value.strip()
    if value and "://" not in value:
        value = "https://" + value
    p = urlparse(value)
    return value if p.scheme in ("http", "https") and "." in p.netloc else None


def _status_payload(db: Session) -> dict:
    sources = db.scalars(select(Source)).all()
    next_run = scheduler.next_run_time()
    return {
        "auto": scheduler.is_enabled(),
        "next_run": fmt_dt(next_run) if next_run else "",
        "sources": {
            s.id: {"running": s.is_running, "status": s.last_status, "message": s.last_message,
                   "last_run": fmt_dt(s.last_run_at) if s.last_run_at else ""} for s in sources
        },
    }


@router.get("/sources")
def sources_page(request: Request, user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    sources = db.scalars(select(Source).order_by(Source.id)).all()
    counts = dict(db.execute(select(News.source_id, func.count(News.id)).group_by(News.source_id)).all())
    return render(request, "sources.html", active="sources", sources=sources, counts=counts,
                  auto_enabled=scheduler.is_enabled(), next_run=scheduler.next_run_time())


@router.post("/sources")
def create_source(request: Request, name: str = Form(""), url: str = Form(...), keywords: str = Form(""),
                  user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    clean = _clean_url(url)
    if not clean:
        flash(request, "Укажите корректный адрес страницы со списком статей", "warn")
        return redirect("/sources")
    db.add(Source(name=name.strip() or urlparse(clean).netloc.removeprefix("www."), url=clean,
                  keywords=keywords.strip()))
    db.commit()
    flash(request, "Сайт добавлен в справочник")
    return redirect("/sources")


@router.post("/sources/{source_id}")
def update_source(request: Request, source_id: int, name: str = Form(""), url: str = Form(...),
                  keywords: str = Form(""), user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    source = db.get(Source, source_id) or _404()
    clean = _clean_url(url)
    if not clean:
        flash(request, "Некорректный адрес", "warn")
        return redirect("/sources")
    source.name = name.strip() or source.name
    source.url = clean
    source.keywords = keywords.strip()
    db.commit()
    flash(request, "Сохранено")
    return redirect("/sources")


@router.post("/sources/{source_id}/toggle")
def toggle_source(request: Request, source_id: int, user_id: int = Depends(require_user),
                  db: Session = Depends(get_db)):
    source = db.get(Source, source_id) or _404()
    source.is_active = not source.is_active
    db.commit()
    flash(request, f"«{source.name}»: " + ("участвует в автопарсинге" if source.is_active else "исключён из автопарсинга"))
    return redirect("/sources")


@router.post("/sources/{source_id}/run")
def run_source(source_id: int, user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    source = db.get(Source, source_id) or _404()
    if scheduler.is_enabled():
        return JSONResponse({"ok": False, "error": "Включён автопарсинг — ручной запуск недоступен"}, 409)
    if source.is_running:
        return JSONResponse({"ok": False, "error": "Этот сайт уже парсится"}, 409)
    parser.submit_source(source_id)
    return {"ok": True}


@router.post("/sources/{source_id}/run-older")
def run_source_older(source_id: int, user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    """Догрузить 5 статей старше уже загруженных. Работает и при включённом рубильнике:
    один сайт всё равно никогда не парсится в двух потоках сразу."""
    source = db.get(Source, source_id) or _404()
    if source.is_running:
        return JSONResponse({"ok": False, "error": "Этот сайт сейчас парсится — дождитесь окончания"}, 409)
    parser.submit_source(source_id, mode="older")
    return {"ok": True}


@router.post("/auto")
def toggle_auto(request: Request, enabled: str = Form(""), back: str = Form("/sources"),
                user_id: int = Depends(require_user)):
    on = enabled == "1"
    scheduler.set_enabled(on)
    flash(request, "Автопарсинг включён: первый обход уже запущен, дальше — каждые 4 часа" if on
          else "Автопарсинг выключен")
    return redirect(back if back.startswith("/") and not back.startswith("//") else "/sources")


@router.get("/parser/status")
def status(user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    return _status_payload(db)


@router.get("/runs")
def runs(request: Request, source: int | None = None, user_id: int = Depends(require_user),
         db: Session = Depends(get_db)):
    query = select(ParserRun).order_by(ParserRun.id.desc()).limit(100)
    if source:
        query = query.where(ParserRun.source_id == source)
    return render(request, "runs.html", active="runs", runs=db.scalars(query).all(),
                  sources=db.scalars(select(Source).order_by(Source.name)).all(), source_filter=source)


@router.get("/runs/{run_id}")
def run_detail(request: Request, run_id: int, user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    run = db.get(ParserRun, run_id) or _404()
    return render(request, "run_detail.html", active="runs", run=run)


def _404():
    raise HTTPException(404, "Не найдено")
