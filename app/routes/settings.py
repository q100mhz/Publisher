"""Справочник промптов, личные настройки и управление пользователями (для администратора)."""
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..auth import create_user, hash_password, require_admin, require_user, verify_password
from ..crypto import encrypt
from ..db import get_db
from ..models import News, Prompt, Source, User, get_setting, set_setting
from ..services import ai, generator
from ..web import flash, redirect, render

router = APIRouter()
THEMES = {"auto": "Как в системе", "light": "Светлая", "dark": "Тёмная"}


# ---------- Промпты ----------

def _own_prompt(db: Session, prompt_id: int, user_id: int) -> Prompt:
    prompt = db.get(Prompt, prompt_id)
    if not prompt or prompt.user_id != user_id:
        raise HTTPException(404, "Промпт не найден")
    return prompt


@router.get("/prompts")
def prompts_page(request: Request, edit: int | None = None, user_id: int = Depends(require_user),
                 db: Session = Depends(get_db)):
    prompts = db.scalars(select(Prompt).where(Prompt.user_id == user_id)
                         .order_by(Prompt.is_default.desc(), Prompt.name)).all()
    current = _own_prompt(db, edit, user_id) if edit else None
    return render(request, "prompts.html", active="prompts", prompts=prompts, current=current,
                  placeholders=generator.PLACEHOLDERS)


@router.post("/prompts")
def save_prompt(request: Request, prompt_id: str = Form(""), name: str = Form(...), text: str = Form(...),
                user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    if not text.strip():
        flash(request, "Текст промпта не может быть пустым", "warn")
        return redirect("/prompts")
    if prompt_id.isdigit():
        prompt = _own_prompt(db, int(prompt_id), user_id)
        prompt.name, prompt.text = name.strip() or prompt.name, text
    else:
        has_any = db.scalar(select(Prompt.id).where(Prompt.user_id == user_id).limit(1)) is not None
        prompt = Prompt(user_id=user_id, name=name.strip() or "Новый промпт", text=text, is_default=not has_any)
        db.add(prompt)
    db.commit()
    flash(request, "Промпт сохранён")
    return redirect(f"/prompts?edit={prompt.id}")


@router.post("/prompts/{prompt_id}/default")
def make_default(request: Request, prompt_id: int, user_id: int = Depends(require_user),
                 db: Session = Depends(get_db)):
    prompt = _own_prompt(db, prompt_id, user_id)
    db.execute(update(Prompt).where(Prompt.user_id == user_id).values(is_default=False))
    prompt.is_default = True
    db.commit()
    flash(request, f"«{prompt.name}» теперь основной промпт")
    return redirect("/prompts")


@router.post("/prompts/{prompt_id}/delete")
def delete_prompt(request: Request, prompt_id: int, user_id: int = Depends(require_user),
                  db: Session = Depends(get_db)):
    prompt = _own_prompt(db, prompt_id, user_id)
    if prompt.is_default:
        flash(request, "Основной промпт удалить нельзя — сначала назначьте основным другой", "warn")
        return redirect("/prompts")
    db.delete(prompt)
    db.commit()
    flash(request, "Промпт удалён")
    return redirect("/prompts")


# ---------- Личные настройки ----------

@router.get("/settings")
def settings_page(request: Request, user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    user = request.state.user
    try:
        ai_status = f"Готово: {ai.user_config(db, user).label}"
    except ai.AIError as e:
        ai_status = str(e)
    return render(request, "settings.html", active="settings", themes=THEMES, providers=ai.PROVIDERS,
                  ai_settings=ai.describe(db, user), ai_status=ai_status)


@router.post("/settings/theme")
def save_theme(request: Request, theme: str = Form("auto"), user_id: int = Depends(require_user),
               db: Session = Depends(get_db)):
    set_setting(db, user_id, "theme", theme if theme in THEMES else "auto")
    db.commit()
    flash(request, f"Тема: {THEMES.get(theme, THEMES['auto']).lower()}")
    return redirect("/settings")


@router.post("/settings/ai")
def save_ai(request: Request, provider: str = Form(...), model: str = Form(""), api_key: str = Form(""),
            base_url: str = Form(""), temperature: str = Form("1.0"), remove_key: str = Form(""),
            user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    if provider not in ai.PROVIDERS:
        flash(request, "Неизвестный провайдер", "warn")
        return redirect("/settings#ai")
    previous = get_setting(db, user_id, "ai_provider", "deepseek")
    set_setting(db, user_id, "ai_provider", provider)
    set_setting(db, user_id, "ai_model", model.strip())
    base_url = base_url.strip()
    if base_url and not re.match(r"^https?://", base_url):
        flash(request, "Адрес API должен начинаться с https://", "warn")
        base_url = ""
    set_setting(db, user_id, "ai_base_url", base_url)
    try:
        temp = min(max(float(temperature.replace(",", ".")), 0.0), 2.0)
    except ValueError:
        temp = 1.0
    set_setting(db, user_id, "ai_temperature", str(temp))

    if remove_key:
        set_setting(db, user_id, "ai_api_key", "")
    elif api_key.strip():
        set_setting(db, user_id, "ai_api_key", encrypt(api_key.strip()))
    elif provider != previous and get_setting(db, user_id, "ai_api_key"):
        flash(request, "Вы сменили нейросеть — не забудьте вставить ключ от нового сервиса", "info")
    db.commit()
    flash(request, "Настройки нейросети сохранены")
    return redirect("/settings#ai")


@router.post("/settings/ai/test")
def test_ai(request: Request, user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    """Короткий пробный запрос к нейросети с сохранёнными настройками."""
    try:
        cfg = ai.user_config(db, request.state.user)
        answer = ai.complete(cfg, "Отвечай кратко.", "Ответь одним словом: работает?", quick_test=True)
    except ai.AIError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    return {"ok": True, "message": f"{cfg.label}, модель {cfg.model} ({cfg.key_source} ключ) — ответ: "
                                   f"«{(answer or '').strip()[:60] or 'пустой, но соединение есть'}»"}


@router.post("/settings/password")
def change_password(request: Request, current: str = Form(...), new: str = Form(...), repeat: str = Form(...),
                    user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user or not verify_password(current, user.password_hash):
        flash(request, "Текущий пароль указан неверно", "warn")
    elif len(new) < 8:
        flash(request, "Новый пароль — минимум 8 символов", "warn")
    elif new != repeat:
        flash(request, "Пароли не совпадают", "warn")
    else:
        user.password_hash = hash_password(new)
        db.commit()
        flash(request, "Пароль изменён")
    return redirect("/settings")


# ---------- Пользователи (только администратор) ----------

LOGIN_RE = re.compile(r"^[A-Za-z0-9._@-]{3,50}$")


@router.get("/users")
def users_page(request: Request, user_id: int = Depends(require_admin), db: Session = Depends(get_db)):
    users = db.scalars(select(User).order_by(User.id)).all()
    news_counts = dict(db.execute(select(News.user_id, func.count(News.id)).group_by(News.user_id)).all())
    source_counts = dict(db.execute(select(Source.user_id, func.count(Source.id)).group_by(Source.user_id)).all())
    return render(request, "users.html", active="users", users=users, news_counts=news_counts,
                  source_counts=source_counts)


@router.post("/users")
def add_user(request: Request, login: str = Form(...), password: str = Form(...), is_admin: str = Form(""),
             user_id: int = Depends(require_admin), db: Session = Depends(get_db)):
    login = login.strip()
    if not LOGIN_RE.match(login):
        flash(request, "Логин: 3–50 символов, латиница, цифры и . _ @ -", "warn")
    elif len(password) < 8:
        flash(request, "Пароль — минимум 8 символов", "warn")
    elif db.scalar(select(User.id).where(User.login == login)):
        flash(request, f"Пользователь «{login}» уже есть", "warn")
    else:
        create_user(db, login, password, is_admin=bool(is_admin))
        db.commit()
        flash(request, f"Пользователь «{login}» создан. Передайте ему логин и пароль.")
    return redirect("/users")


def _other_user(db: Session, target_id: int, user_id: int) -> User:
    target = db.get(User, target_id)
    if not target:
        raise HTTPException(404, "Пользователь не найден")
    return target


@router.post("/users/{target_id}/password")
def reset_password(request: Request, target_id: int, password: str = Form(...),
                   user_id: int = Depends(require_admin), db: Session = Depends(get_db)):
    target = _other_user(db, target_id, user_id)
    if len(password) < 8:
        flash(request, "Пароль — минимум 8 символов", "warn")
    else:
        target.password_hash = hash_password(password)
        db.commit()
        flash(request, f"Пароль пользователя «{target.login}» изменён")
    return redirect("/users")


@router.post("/users/{target_id}/toggle")
def toggle_user(request: Request, target_id: int, field: str = Form(...),
                user_id: int = Depends(require_admin), db: Session = Depends(get_db)):
    target = _other_user(db, target_id, user_id)
    if target.id == user_id:
        flash(request, "Себя отключить или лишить прав нельзя", "warn")
        return redirect("/users")
    if field == "active":
        target.is_active = not target.is_active
        if not target.is_active:
            from ..services import scheduler
            scheduler.set_enabled(target.id, False)
        flash(request, f"«{target.login}»: " + ("вход разрешён" if target.is_active else "вход запрещён"))
    elif field == "admin":
        target.is_admin = not target.is_admin
        flash(request, f"«{target.login}»: " + ("теперь администратор" if target.is_admin else "больше не администратор"))
    db.commit()
    return redirect("/users")
