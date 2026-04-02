from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api.routes_admin import router as admin_router
from .api.routes_inbox import router as inbox_router
from .api.routes_mailboxes import router as mailboxes_router
from .config import get_settings
from .db import close_db_pool, get_connection, open_db_pool, run_startup_migrations
from .storage import ensure_storage_dirs

settings = get_settings()


class CacheControlledStaticFiles(StaticFiles):
    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        query_string = scope.get("query_string", b"").decode("latin-1")
        if "v=" in query_string:
            response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
        else:
            response.headers.setdefault("Cache-Control", "public, max-age=3600")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

app = FastAPI(
    title=settings.app_name,
    version=__version__,
)

app.add_middleware(GZipMiddleware, minimum_size=1024)
app.mount(
    "/static",
    CacheControlledStaticFiles(directory=Path(__file__).resolve().parent / "static"),
    name="static",
)

app.include_router(mailboxes_router)
app.include_router(inbox_router)
app.include_router(admin_router)


@app.on_event("startup")
def startup() -> None:
    open_db_pool()
    ensure_storage_dirs()
    run_startup_migrations()


@app.on_event("shutdown")
def shutdown() -> None:
    close_db_pool()


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            row = cur.fetchone()
    return {"status": "ready", "db": row["ok"]}


@app.get("/")
def root() -> dict:
    return {
        "name": settings.app_name,
        "version": __version__,
        "create_mailbox_endpoint": "/api/v1/mailboxes",
    }
