from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .auth import LoginRequired, ensure_users_ready
from .config import BASE_PATH, COOKIE_SECURE, SECRET_KEY
from . import migrate
from .db import Base, engine
from .routes import news, pages, settings, sources
from .services import parser, scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    migrate.run()
    ensure_users_ready()
    parser.reset_stale_flags()
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def same_origin_posts(request: Request, call_next):
    """Защита от CSRF: изменяющие запросы принимаются только со своего же сайта."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin:
            host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
            if urlparse(origin).netloc != host.split(",")[0].strip():
                return PlainTextResponse("Запрос с чужого сайта отклонён", status_code=403)
    return await call_next(request)


app.add_middleware(
    SessionMiddleware, secret_key=SECRET_KEY, session_cookie="publisher_session",
    max_age=30 * 24 * 3600, same_site="lax", https_only=COOKIE_SECURE, path=BASE_PATH or "/",
)


@app.exception_handler(LoginRequired)
async def login_required(request: Request, exc: LoginRequired):
    return RedirectResponse(BASE_PATH + "/login", status_code=303)


app.mount(BASE_PATH + "/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
for module in (pages, news, sources, settings):
    app.include_router(module.router, prefix=BASE_PATH)

if BASE_PATH:
    @app.get(BASE_PATH, include_in_schema=False)
    async def no_slash():
        return RedirectResponse(BASE_PATH + "/", status_code=301)


@app.get(BASE_PATH + "/health", include_in_schema=False)
async def health():
    return {"ok": True}
