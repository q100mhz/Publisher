"""Справочник промптов и личные настройки пользователя.

Пользователей на сайте завести нельзя — только из терминала сервера (python -m app.cli).
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..auth import hash_password, require_user, verify_password
from ..crypto import encrypt
from ..security import UnsafeURL, check_url, clean_text
from ..db import get_db
from ..models import Prompt, User, get_setting, set_setting
from ..services import ai, generator
from ..services import bitrix as bitrix_service
from ..web import flash, redirect, render

router = APIRouter()
THEMES = {"auto": "Как в системе", "light": "Светлая", "dark": "Тёмная"}


# ---------- Промпты ----------

def _own_prompt(db: Session, prompt_id: int, user_id: int) -> Prompt:
    prompt = db.get(Prompt, prompt_id)
    if not prompt or prompt.user_id != user_id:
        raise HTTPException(404, "Промпт не найден")
    return prompt


PROMPT_KINDS = {"text": "Для статьи", "image": "Для картинки"}


@router.get("/prompts")
def prompts_page(request: Request, edit: int | None = None, kind: str = "text", user_id: int = Depends(require_user),
                 db: Session = Depends(get_db)):
    prompts = db.scalars(select(Prompt).where(Prompt.user_id == user_id)
                         .order_by(Prompt.is_default.desc(), Prompt.name)).all()
    current = _own_prompt(db, edit, user_id) if edit else None
    kind = current.kind if current else (kind if kind in PROMPT_KINDS else "text")
    placeholders = {"image": generator.IMAGE_PLACEHOLDERS}.get(
        kind, generator.PLACEHOLDERS)
    return render(request, "prompts.html", active="prompts", prompts=prompts, current=current, kind=kind,
                  kinds=PROMPT_KINDS, placeholders=placeholders)


@router.post("/prompts")
def save_prompt(request: Request, prompt_id: str = Form(""), name: str = Form(...), text: str = Form(...),
                kind: str = Form("text"), user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    text, name = clean_text(text, 20_000), clean_text(name, 200)
    if not text.strip():
        flash(request, "Текст промпта не может быть пустым", "warn")
        return redirect("/prompts")
    if prompt_id.isdigit():
        prompt = _own_prompt(db, int(prompt_id), user_id)
        prompt.name, prompt.text = name.strip() or prompt.name, text
    else:
        kind = kind if kind in PROMPT_KINDS else "text"
        has_any = db.scalar(select(Prompt.id).where(Prompt.user_id == user_id, Prompt.kind == kind).limit(1)) is not None
        prompt = Prompt(user_id=user_id, kind=kind, name=name.strip() or "Новый промпт", text=text,
                        is_default=not has_any)
        db.add(prompt)
    db.commit()
    flash(request, "Промпт сохранён")
    return redirect(f"/prompts?edit={prompt.id}")


@router.post("/prompts/{prompt_id}/default")
def make_default(request: Request, prompt_id: int, user_id: int = Depends(require_user),
                 db: Session = Depends(get_db)):
    prompt = _own_prompt(db, prompt_id, user_id)
    db.execute(update(Prompt).where(Prompt.user_id == user_id, Prompt.kind == prompt.kind).values(is_default=False))
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
        cfg = ai.user_config(db, user)
        ai_status = f"Готово: {cfg.label}" + (" — через базовый ключ сервиса" if cfg.via_base else "")
    except ai.AIError as e:
        ai_status = str(e)
    try:
        cfg = ai.image_config(db, user)
        img_status = f"Готово: {cfg.label}, модель {cfg.model}" + ("" if cfg.separate else " (ключ основной нейросети)")
    except ai.AIError as e:
        img_status = str(e)
    return render(request, "settings.html", active="settings", themes=THEMES, providers=ai.PROVIDERS,
                  ai_settings=ai.describe(db, user), ai_status=ai_status,
                  img_providers=ai.IMAGE_PROVIDERS, img_models=ai.IMAGE_MODEL_HINTS, img_sizes=ai.IMAGE_SIZES,
                  img_settings=ai.describe_images(db, user), img_status=img_status, bitrix=_bitrix_view(db, user),
                  image_places=bitrix_service.IMAGE_PLACES)


def _bitrix_view(db: Session, user) -> dict | None:
    """Настройки Битрикс24 для страницы (адрес вебхука — только маской)."""
    if not user.bitrix_allowed:
        return None
    from ..crypto import mask
    from ..services import bitrix
    cfg = bitrix.user_settings(db, user)
    cfg["webhook_mask"] = mask(cfg.pop("webhook")) if cfg.get("webhook") else ""
    return cfg


@router.post("/settings/theme")
def save_theme(request: Request, theme: str = Form("auto"), user_id: int = Depends(require_user),
               db: Session = Depends(get_db)):
    set_setting(db, user_id, "theme", theme if theme in THEMES else "auto")
    db.commit()
    flash(request, f"Тема: {THEMES.get(theme, THEMES['auto']).lower()}")
    return redirect("/settings")


def _check_base(value: str) -> tuple[str, str]:
    """Адрес API нейросети: пустой, либо публичный http(s)-адрес (не внутренняя сеть сервера)."""
    value = clean_text(value, 500).strip()
    if not value:
        return "", ""
    try:
        return check_url(value), ""
    except UnsafeURL as e:
        return "", str(e)


@router.post("/settings/ai")
def save_ai(request: Request, provider: str = Form(...), model: str = Form("", alias="ai_model_name"), api_key: str = Form(""),
            base_url: str = Form(""), temperature: str = Form("1.0"), remove_key: str = Form(""),
            user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    if provider not in ai.PROVIDERS:
        flash(request, "Неизвестный провайдер", "warn")
        return redirect("/settings#ai")
    previous = get_setting(db, user_id, "ai_provider", "deepseek")
    set_setting(db, user_id, "ai_provider", provider)
    set_setting(db, user_id, "ai_model", model.strip())
    base_url, problem = _check_base(base_url)
    if problem:
        flash(request, f"Адрес API не сохранён: {problem}", "warn")
    set_setting(db, user_id, "ai_base_url", base_url)
    try:
        temp = min(max(float(temperature.replace(",", ".")), 0.0), 2.0)
    except ValueError:
        temp = 1.0
    set_setting(db, user_id, "ai_temperature", str(temp))

    key_name = f"ai_api_key:{provider}"  # у каждого сервиса свой ключ
    if remove_key:
        set_setting(db, user_id, key_name, "")
    elif api_key.strip():
        set_setting(db, user_id, key_name, encrypt(api_key.strip()))
    elif provider != previous and not get_setting(db, user_id, key_name):
        if not (provider == "deepseek" and ai.base_available(request.state.user)):
            flash(request, "Вы выбрали другую нейросеть — вставьте ключ от неё", "info")
    db.commit()
    flash(request, "Настройки нейросети сохранены")
    return redirect("/settings#ai")


@router.post("/settings/images")
def save_images(request: Request, provider: str = Form(""), model: str = Form("", alias="img_model_name"), api_key: str = Form(""),
                base_url: str = Form(""), size: str = Form(""), remove_key: str = Form(""),
                user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    provider = provider if provider in ai.IMAGE_PROVIDERS else ""
    set_setting(db, user_id, "img_provider", provider)
    set_setting(db, user_id, "img_model", model.strip())
    base_url, problem = _check_base(base_url)
    if problem:
        flash(request, f"Адрес API для картинок не сохранён: {problem}", "warn")
    set_setting(db, user_id, "img_base_url", base_url)
    set_setting(db, user_id, "img_size", size if size in ai.IMAGE_SIZES else ai.IMAGE_SIZES[0])
    if provider:
        key_name = f"img_api_key:{provider}"
        if remove_key:
            set_setting(db, user_id, key_name, "")
        elif api_key.strip():
            set_setting(db, user_id, key_name, encrypt(api_key.strip()))
    db.commit()
    flash(request, "Настройки картинок сохранены")
    return redirect("/settings#images")


@router.post("/settings/ai/test")
def test_ai(request: Request, user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    """Короткий пробный запрос к нейросети с сохранёнными настройками."""
    try:
        cfg = ai.user_config(db, request.state.user)
        answer = ai.complete(cfg, "Отвечай кратко.", "Ответь одним словом: работает?", quick_test=True)
        ai.log_usage(user_id, "test", cfg.provider, cfg.model, cfg.via_base, True)
    except ai.AIError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    source = " (базовый ключ сервиса)" if cfg.via_base else ""
    return {"ok": True, "message": f"{cfg.label}{source}, модель {cfg.model} — ответ: "
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
