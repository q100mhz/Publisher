"""Админка сервиса: publisher-admin.itb2bhost.ru.

Внутри приложения живёт по адресу /admin/ — nginx админского домена проксирует туда все запросы.
Вход — отдельными учётными записями администраторов (создаются только из терминала:
python -m app.cli add-admin <логин>). Содержимое кабинетов (новости, тексты) админка не показывает —
только учётные данные пользователей и статистику.
"""
from __future__ import annotations

import secrets
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import client_ip, hash_password, is_locked, normalize_email, register_fail, reset_fails, verify_password
from .config import ADMIN_PREFIX, COOKIE_SECURE, DATABASE_URL, DEEPSEEK_API_KEY, MEDIA_DIR, SECRET_KEY
from .db import get_db
from .models import (Admin, AdminAudit, AIUsage, News, ParserRun, Source, User, get_global, now, set_global,
                     set_setting)
from .security import clean_text, escape_like
from .web import templates

router = APIRouter(prefix="/admin", include_in_schema=False)
COOKIE = "publisher_admin"
SESSION_HOURS = 12
_signer = URLSafeTimedSerializer(SECRET_KEY, salt="publisher-admin-session")


def aurl(path: str = "/") -> str:
    return ADMIN_PREFIX + path


templates.env.globals["aurl"] = aurl


def human_bytes(value) -> str:
    if value is None:
        return "—"
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{value:.0f} {unit}" if unit in ("Б", "КБ") else f"{value:.1f} {unit}"
        value /= 1024


templates.env.filters["bytes"] = human_bytes
templates.env.tests["email"] = lambda value: normalize_email(value or "") is not None


class AdminLoginRequired(Exception):
    pass


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(aurl(path), status_code=303)


def current_admin(request: Request, db: Session = Depends(get_db)) -> Admin:
    token = request.cookies.get(COOKIE, "")
    try:
        data = _signer.loads(token, max_age=SESSION_HOURS * 3600)
    except BadSignature:
        raise AdminLoginRequired()
    admin = db.get(Admin, data.get("id"))
    # Смена пароля администратора завершает его старые сессии
    if not admin or data.get("pw") != admin.password_hash[-12:]:
        raise AdminLoginRequired()
    request.state.admin = admin
    return admin


def audit(db: Session, admin: Admin, action: str, user: User | None = None) -> None:
    db.add(AdminAudit(admin_login=admin.login, user_id=user.id if user else None, action=action))


def page(request: Request, name: str, **context):
    flashes = []
    if request.cookies.get("admin_flash"):
        try:
            flashes = [_signer.loads(request.cookies["admin_flash"], max_age=60)]
        except BadSignature:
            pass
    response = templates.TemplateResponse(request, f"admin/{name}", {
        "admin": getattr(request.state, "admin", None), "flashes": flashes, "today": date.today(),
        "theme": "auto", "user": None, **context})
    response.delete_cookie("admin_flash", path="/")
    return response


def flash_redirect(path: str, text: str, kind: str = "ok") -> RedirectResponse:
    response = _redirect(path)
    response.set_cookie("admin_flash", _signer.dumps({"text": text, "kind": kind}), max_age=60, path="/",
                        httponly=True, samesite="lax", secure=COOKIE_SECURE)
    return response


# ---------- Вход ----------

@router.get("/login")
def login_page(request: Request):
    return page(request, "login.html")


@router.post("/login")
def login(request: Request, login: str = Form(""), password: str = Form(""), db: Session = Depends(get_db)):
    key = "admin:" + client_ip(request)
    wait = is_locked(key)
    if wait:
        return page(request, "login.html", error=f"Слишком много попыток. Попробуйте через {wait // 60 + 1} мин.")
    admin = db.scalar(select(Admin).where(Admin.login == login.strip()))
    if not admin or not verify_password(password, admin.password_hash):
        register_fail(key)
        return page(request, "login.html", error="Неверный логин или пароль", login=login)
    reset_fails(key)
    admin.last_login_at = now()
    audit(db, admin, "Вход в админку")
    db.commit()
    response = _redirect("/")
    response.set_cookie(COOKIE, _signer.dumps({"id": admin.id, "pw": admin.password_hash[-12:]}),
                        max_age=SESSION_HOURS * 3600, path="/", httponly=True, samesite="lax", secure=COOKIE_SECURE)
    return response


@router.post("/logout")
def logout():
    response = _redirect("/login")
    response.delete_cookie(COOKIE, path="/")
    return response


# ---------- Дашборд ----------

def _daily(db: Session, column, since: datetime, *conditions) -> list[tuple[date, int]]:
    """Количество записей по дням за период (с нулями в пустые дни)."""
    rows = db.execute(select(func.date(column), func.count()).where(column >= since, *conditions)
                      .group_by(func.date(column))).all()
    counts = {(r[0] if isinstance(r[0], date) else date.fromisoformat(str(r[0]))): r[1] for r in rows}
    days = [(since + timedelta(days=i)).date() for i in range((now() - since).days + 1)]
    return [(d, counts.get(d, 0)) for d in days]


def _chart(points: list[tuple[date, int]]) -> dict:
    """Геометрия столбчатой диаграммы (SVG рисует шаблон)."""
    width, height, pad_left, pad_bottom, pad_top = 640, 170, 30, 22, 12
    top = max([v for _, v in points] + [1])
    # «Красивая» верхняя граница шкалы
    for step in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000):
        if top <= step * 4:
            scale_max = step * ((top + step - 1) // step)
            break
    else:
        scale_max = top
    plot_w, plot_h = width - pad_left - 4, height - pad_bottom - pad_top
    slot = plot_w / len(points)
    bars = []
    for i, (day, value) in enumerate(points):
        h = 0 if not value else max(2, plot_h * value / scale_max)
        x = pad_left + i * slot + 1
        w, y, base = max(slot - 2, 2), pad_top + plot_h - h, pad_top + plot_h
        r = min(4, w / 2, h / 2) if h else 0
        # Столбик со скруглённым верхом и прямым основанием
        path = (f"M{x:.1f},{base:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} H{x + w - r:.1f} "
                f"Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{base:.1f} Z") if h else ""
        bars.append({"path": path, "x": round(x, 1), "w": round(w, 1), "y": round(y, 1),
                     "h": round(h, 1), "hit_x": round(pad_left + i * slot, 1), "hit_w": round(slot, 1),
                     "label": f"{day:%d.%m}: {value}", "day": day, "value": value,
                     "tick": (i % 7 == 0 and i <= len(points) - 4) or i == len(points) - 1})
    return {"w": width, "h": height, "left": pad_left, "top": pad_top, "plot_h": plot_h, "base": pad_top + plot_h,
            "max": scale_max, "mid": scale_max // 2 if scale_max % 2 == 0 else None, "bars": bars,
            "total": sum(v for _, v in points)}


def _size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


def _db_size(db: Session) -> int | None:
    if DATABASE_URL.startswith("sqlite"):
        file = Path(DATABASE_URL.split("///", 1)[1])
        return file.stat().st_size if file.exists() else None
    try:
        return db.scalar(select(func.pg_database_size(func.current_database())))
    except Exception:
        return None


@router.get("/")
def dashboard(request: Request, admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    t = now()
    d7, d30 = t - timedelta(days=7), t - timedelta(days=30)
    since = (t - timedelta(days=29)).replace(hour=0, minute=0, second=0)
    month_start = t.replace(day=1, hour=0, minute=0, second=0)
    count = lambda q: db.scalar(q) or 0  # noqa: E731

    users = {
        "total": count(select(func.count(User.id))),
        "new7": count(select(func.count(User.id)).where(User.created_at >= d7)),
        "new30": count(select(func.count(User.id)).where(User.created_at >= d30)),
        "active7": count(select(func.count(User.id)).where(User.last_seen_at >= d7)),
        "blocked": count(select(func.count(User.id)).where(User.is_active.is_(False))),
        "base_ai": count(select(func.count(User.id)).where(User.base_ai_allowed.is_(True))),
        "bitrix": count(select(func.count(User.id)).where(User.bitrix_allowed.is_(True))),
    }
    content = {
        "sources": count(select(func.count(Source.id))),
        "parsed": count(select(func.count(News.id)).where(News.origin == "parser")),
        "parsed7": count(select(func.count(News.id)).where(News.origin == "parser", News.created_at >= d7)),
        "news": count(select(func.count(News.id))),
        "published": count(select(func.count(News.id)).where(News.status == "published")),
        "bitrix": count(select(func.count(News.id)).where(News.bitrix_page_id.is_not(None))),
    }
    gen30 = select(func.count(AIUsage.id)).where(AIUsage.created_at >= d30, AIUsage.kind != "test")
    ai = {
        "today": count(gen30.where(AIUsage.created_at >= t.replace(hour=0, minute=0, second=0))),
        "total30": count(gen30),
        "errors30": count(gen30.where(AIUsage.ok.is_(False))),
        "base_month": count(select(func.count(AIUsage.id)).where(
            AIUsage.via_base.is_(True), AIUsage.ok.is_(True), AIUsage.kind != "test", AIUsage.created_at >= month_start)),
        "by_kind": db.execute(select(AIUsage.kind, func.count()).where(AIUsage.created_at >= d30, AIUsage.kind != "test")
                              .group_by(AIUsage.kind)).all(),
        "by_provider": db.execute(
            select(AIUsage.provider, AIUsage.via_base, func.count()).where(AIUsage.created_at >= d30, AIUsage.kind != "test")
            .group_by(AIUsage.provider, AIUsage.via_base).order_by(func.count().desc())).all(),
    }
    ai["error_share"] = round(100 * ai["errors30"] / ai["total30"]) if ai["total30"] else 0
    top_users = db.execute(
        select(User.login, func.count(AIUsage.id)).join(AIUsage, AIUsage.user_id == User.id)
        .where(AIUsage.created_at >= d30, AIUsage.kind != "test").group_by(User.login)
        .order_by(func.count(AIUsage.id).desc()).limit(5)).all()

    disk = shutil.disk_usage(MEDIA_DIR)
    health = {
        "disk_free": disk.free, "disk_total": disk.total, "disk_pct": round(100 * disk.used / disk.total),
        "db_size": _db_size(db), "media_size": _size(MEDIA_DIR),
        "base_key": bool(DEEPSEEK_API_KEY),
        "running": count(select(func.count(Source.id)).where(Source.is_running.is_(True))),
        "failing_sources": db.execute(
            select(Source.url, Source.last_message, Source.last_run_at, User.login)
            .join(User, User.id == Source.user_id).where(Source.last_status == "error")
            .order_by(Source.last_run_at.desc()).limit(10)).all(),
        "ai_errors": db.execute(
            select(AIUsage.created_at, AIUsage.provider, AIUsage.kind, AIUsage.error, User.login)
            .join(User, User.id == AIUsage.user_id).where(AIUsage.ok.is_(False))
            .order_by(AIUsage.id.desc()).limit(8)).all(),
        "runs_failed7": count(select(func.count(ParserRun.id)).where(ParserRun.status == "error",
                                                                    ParserRun.started_at >= d7)),
    }
    charts = {
        "signups": _chart(_daily(db, User.created_at, since)),
        "generations": _chart(_daily(db, AIUsage.created_at, since, AIUsage.kind != "test")),
    }
    return page(request, "dashboard.html", active="dashboard", users=users, content=content, ai=ai,
                top_users=top_users, health=health, charts=charts)


# ---------- Пользователи ----------

@router.get("/users")
def users_page(request: Request, q: str = "", status: str = "", admin: Admin = Depends(current_admin),
               db: Session = Depends(get_db)):
    d30 = now() - timedelta(days=30)
    query = select(User).order_by(User.created_at.desc())
    if q.strip():
        query = query.where(User.login.ilike(f"%{escape_like(clean_text(q, 200).strip())}%", escape="\\"))
    if status == "blocked":
        query = query.where(User.is_active.is_(False))
    elif status == "active":
        query = query.where(User.is_active.is_(True))
    elif status == "base_ai":
        query = query.where(User.base_ai_allowed.is_(True))
    elif status == "bitrix":
        query = query.where(User.bitrix_allowed.is_(True))
    users = db.scalars(query).all()

    def per_user(stmt):
        return dict(db.execute(stmt).all())
    stats = {
        "sources": per_user(select(Source.user_id, func.count(Source.id)).group_by(Source.user_id)),
        "parsed": per_user(select(News.user_id, func.count(News.id)).where(News.origin == "parser").group_by(News.user_id)),
        "news": per_user(select(News.user_id, func.count(News.id)).group_by(News.user_id)),
        "gen30": per_user(select(AIUsage.user_id, func.count(AIUsage.id))
                          .where(AIUsage.created_at >= d30, AIUsage.kind != "test").group_by(AIUsage.user_id)),
    }
    return page(request, "users.html", active="users", users=users, stats=stats, q=q, status=status)


def _user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    return user


def _user_context(db: Session, user: User) -> dict:
    from .services.ai import base_usage_this_month

    d30 = now() - timedelta(days=30)
    info = {
        "sources": db.execute(select(Source.name, Source.url, Source.is_active, Source.last_status, Source.last_run_at)
                              .where(Source.user_id == user.id).order_by(Source.id)).all(),
        "parsed": db.scalar(select(func.count(News.id)).where(News.user_id == user.id, News.origin == "parser")) or 0,
        "news": db.scalar(select(func.count(News.id)).where(News.user_id == user.id)) or 0,
        "published": db.scalar(select(func.count(News.id)).where(News.user_id == user.id,
                                                                News.status == "published")) or 0,
        "gen30": db.execute(select(AIUsage.kind, AIUsage.ok, func.count()).where(
            AIUsage.user_id == user.id, AIUsage.created_at >= d30, AIUsage.kind != "test")
            .group_by(AIUsage.kind, AIUsage.ok)).all(),
        "base_used": base_usage_this_month(db, user.id),
        "audit": db.scalars(select(AdminAudit).where(AdminAudit.user_id == user.id)
                            .order_by(AdminAudit.id.desc()).limit(20)).all(),
    }
    return {"active": "users", "u": user, "info": info, "base_key": bool(DEEPSEEK_API_KEY)}


@router.get("/users/{user_id}")
def user_page(request: Request, user_id: int, admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    return page(request, "user.html", **_user_context(db, _user(db, user_id)))


@router.post("/users/{user_id}/login")
def change_login(user_id: int, login: str = Form(...), admin: Admin = Depends(current_admin),
                 db: Session = Depends(get_db)):
    """Смена e-mail пользователя (он же логин для входа — другие логины не допускаются)."""
    user = _user(db, user_id)
    email = normalize_email(login)
    if not email:
        return flash_redirect(f"/users/{user_id}", "Укажите корректный e-mail — пользователи входят только по e-mail", "warn")
    if db.scalar(select(User.id).where(func.lower(User.login) == email, User.id != user.id)):
        return flash_redirect(f"/users/{user_id}", f"E-mail «{email}» уже занят", "warn")
    old = user.login
    user.login = email
    audit(db, admin, f"E-mail изменён: «{old}» → «{email}»", user)
    db.commit()
    return flash_redirect(f"/users/{user_id}", "E-mail изменён")


@router.post("/users/{user_id}/password")
def change_password(request: Request, user_id: int, password: str = Form(""), generate: str = Form(""),
                    admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    user = _user(db, user_id)
    if generate:
        password = secrets.token_urlsafe(9)
    elif len(password) < 8:
        return flash_redirect(f"/users/{user_id}", "Пароль — минимум 8 символов", "warn")
    user.password_hash = hash_password(password)
    user.session_version += 1  # старые сессии пользователя больше не действуют
    audit(db, admin, "Задан новый пароль" + (" (временный)" if generate else ""), user)
    db.commit()
    if generate:
        # Временный пароль показываем один раз — прямо на странице, нигде не сохраняя в открытом виде
        return page(request, "user.html", temp_password=password, **_user_context(db, user))
    return flash_redirect(f"/users/{user_id}", "Пароль изменён, все сессии пользователя завершены")


@router.post("/users/{user_id}/toggle-active")
def toggle_active(user_id: int, admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    user = _user(db, user_id)
    user.is_active = not user.is_active
    if not user.is_active:
        user.session_version += 1
        set_setting(db, user.id, "auto_enabled", "0")
    audit(db, admin, "Доступ разрешён" if user.is_active else "Пользователь заблокирован", user)
    db.commit()
    if not user.is_active:
        from .services import scheduler
        scheduler.set_enabled(user.id, False)
    return flash_redirect(f"/users/{user_id}", "Доступ разрешён" if user.is_active else "Пользователь заблокирован")


@router.post("/users/{user_id}/sessions")
def end_sessions(user_id: int, admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    user = _user(db, user_id)
    user.session_version += 1
    audit(db, admin, "Завершены все сессии", user)
    db.commit()
    return flash_redirect(f"/users/{user_id}", "Все сессии пользователя завершены")


@router.post("/users/{user_id}/access")
def change_access(user_id: int, base_ai: str = Form(""), limit: str = Form(""), bitrix: str = Form(""),
                  admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    user = _user(db, user_id)
    changes = []
    new_base, new_bitrix = bool(base_ai), bool(bitrix)
    new_limit = int(limit) if limit.strip().isdigit() else None
    if new_base != user.base_ai_allowed:
        changes.append("базовый DeepSeek: " + ("разрешён" if new_base else "запрещён"))
    if new_limit != user.ai_monthly_limit:
        changes.append(f"лимит в месяц: {new_limit if new_limit is not None else 'без лимита'}")
    if new_bitrix != user.bitrix_allowed:
        changes.append("Битрикс24: " + ("разрешён" if new_bitrix else "запрещён"))
    user.base_ai_allowed, user.ai_monthly_limit, user.bitrix_allowed = new_base, new_limit, new_bitrix
    if changes:
        audit(db, admin, "Доступы: " + ", ".join(changes), user)
        db.commit()
        return flash_redirect(f"/users/{user_id}", "Доступы сохранены")
    return flash_redirect(f"/users/{user_id}", "Изменений нет", "info")


@router.post("/users/bulk-access")
async def bulk_access(request: Request, admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    form = await request.form()
    ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
    action = form.get("action", "")
    values = {"base_on": ("base_ai_allowed", True), "base_off": ("base_ai_allowed", False),
              "bitrix_on": ("bitrix_allowed", True), "bitrix_off": ("bitrix_allowed", False)}
    if not ids or action not in values:
        return flash_redirect("/users", "Ничего не выбрано", "warn")
    field, value = values[action]
    labels = {"base_ai_allowed": "базовый DeepSeek", "bitrix_allowed": "Битрикс24"}
    for user in db.scalars(select(User).where(User.id.in_(ids))):
        setattr(user, field, value)
        audit(db, admin, f"Доступы (массово): {labels[field]} — {'разрешён' if value else 'запрещён'}", user)
    db.commit()
    return flash_redirect("/users", f"Обновлено пользователей: {len(ids)}")


# ---------- Журнал и объявление ----------

@router.get("/audit")
def audit_page(request: Request, admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    rows = db.execute(select(AdminAudit, User.login).outerjoin(User, User.id == AdminAudit.user_id)
                      .order_by(AdminAudit.id.desc()).limit(300)).all()
    return page(request, "audit.html", active="audit", rows=rows)


@router.get("/announcement")
def announcement_page(request: Request, admin: Admin = Depends(current_admin), db: Session = Depends(get_db)):
    return page(request, "announcement.html", active="announcement",
                text=get_global(db, "announcement", ""), kind=get_global(db, "announcement_kind", "info"))


@router.post("/announcement")
def save_announcement(text: str = Form(""), kind: str = Form("info"), admin: Admin = Depends(current_admin),
                      db: Session = Depends(get_db)):
    text = text.strip()[:500]
    set_global(db, "announcement", text)
    set_global(db, "announcement_kind", kind if kind in ("info", "warn") else "info")
    audit(db, admin, f"Объявление: «{text}»" if text else "Объявление снято")
    db.commit()
    return flash_redirect("/announcement", "Объявление опубликовано" if text else "Объявление снято")

