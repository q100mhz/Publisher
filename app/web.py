"""Общие вещи для страниц: шаблоны, адреса с учётом /publisher/, flash-сообщения."""
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import BASE_PATH

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]


def url(path: str = "/") -> str:
    return BASE_PATH + path


def fmt_dt(value: datetime | None) -> str:
    return value.strftime("%d.%m.%Y %H:%M") if value else "—"


def fmt_date(value: date | datetime | None) -> str:
    return value.strftime("%d.%m.%Y") if value else "—"


def human_date(value: date | None) -> str:
    return f"{value.day} {MONTHS[value.month - 1]} {value.year}" if value else ""


def filter_url(filters: dict, **changes) -> str:
    """Ссылка на реестр с текущими фильтрами и изменениями (пустые значения отбрасываются)."""
    params = {**filters, **changes}
    query = urlencode({k: v for k, v in params.items() if v not in ("", None) and not (k == "period" and v == "all")})
    return url("/" + (f"?{query}" if query else ""))


templates.env.globals.update(url=url, BASE_PATH=BASE_PATH, filter_url=filter_url)
templates.env.filters.update(dt=fmt_dt, d=fmt_date, human_date=human_date)


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append({"text": message, "kind": kind})


def pop_flash(request: Request) -> list[dict]:
    return request.session.pop("flash", [])


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(url(path), status_code=303)


def render(request: Request, name: str, **context):
    context.setdefault("active", "")
    context.setdefault("user", getattr(request.state, "user", None))
    context.setdefault("theme", getattr(request.state, "theme", "auto"))
    return templates.TemplateResponse(request, name, {"flashes": pop_flash(request), "today": date.today(), **context})
