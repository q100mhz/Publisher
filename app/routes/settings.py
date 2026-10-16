"""Справочник промптов и настройки."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..auth import hash_password, require_user, verify_password
from ..config import DEEPSEEK_MODEL
from ..db import get_db
from ..models import Prompt, User, get_setting, set_setting
from ..services import generator
from ..web import flash, redirect, render

router = APIRouter()


@router.get("/prompts")
def prompts_page(request: Request, edit: int | None = None, user_id: int = Depends(require_user),
                 db: Session = Depends(get_db)):
    prompts = db.scalars(select(Prompt).order_by(Prompt.is_default.desc(), Prompt.name)).all()
    current = db.get(Prompt, edit) if edit else None
    return render(request, "prompts.html", active="prompts", prompts=prompts, current=current,
                  placeholders=generator.PLACEHOLDERS)


@router.post("/prompts")
def save_prompt(request: Request, prompt_id: str = Form(""), name: str = Form(...), text: str = Form(...),
                user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    if not text.strip():
        flash(request, "Текст промпта не может быть пустым", "warn")
        return redirect("/prompts")
    if prompt_id.isdigit():
        prompt = db.get(Prompt, int(prompt_id))
        if not prompt:
            raise HTTPException(404)
        prompt.name, prompt.text = name.strip() or prompt.name, text
    else:
        has_any = db.scalar(select(Prompt).limit(1)) is not None
        prompt = Prompt(name=name.strip() or "Новый промпт", text=text, is_default=not has_any)
        db.add(prompt)
    db.commit()
    flash(request, "Промпт сохранён")
    return redirect(f"/prompts?edit={prompt.id}")


@router.post("/prompts/{prompt_id}/default")
def make_default(request: Request, prompt_id: int, user_id: int = Depends(require_user),
                 db: Session = Depends(get_db)):
    prompt = db.get(Prompt, prompt_id)
    if not prompt:
        raise HTTPException(404)
    db.execute(update(Prompt).values(is_default=False))
    prompt.is_default = True
    db.commit()
    flash(request, f"«{prompt.name}» теперь основной промпт")
    return redirect("/prompts")


@router.post("/prompts/{prompt_id}/delete")
def delete_prompt(request: Request, prompt_id: int, user_id: int = Depends(require_user),
                  db: Session = Depends(get_db)):
    prompt = db.get(Prompt, prompt_id)
    if not prompt:
        raise HTTPException(404)
    if prompt.is_default:
        flash(request, "Основной промпт удалить нельзя — сначала назначьте основным другой", "warn")
        return redirect("/prompts")
    db.delete(prompt)
    db.commit()
    flash(request, "Промпт удалён")
    return redirect("/prompts")


@router.get("/settings")
def settings_page(request: Request, user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    return render(request, "settings.html", active="settings", ai_ready=generator.is_configured(),
                  model=get_setting(db, "deepseek_model", DEEPSEEK_MODEL),
                  temperature=get_setting(db, "deepseek_temperature", "1.0"))


@router.post("/settings/ai")
def save_ai(request: Request, model: str = Form(...), temperature: str = Form("1.0"),
            user_id: int = Depends(require_user), db: Session = Depends(get_db)):
    try:
        temp = min(max(float(temperature.replace(",", ".")), 0.0), 2.0)
    except ValueError:
        temp = 1.0
    set_setting(db, "deepseek_model", model.strip() or DEEPSEEK_MODEL)
    set_setting(db, "deepseek_temperature", str(temp))
    db.commit()
    flash(request, "Настройки генерации сохранены")
    return redirect("/settings")


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
