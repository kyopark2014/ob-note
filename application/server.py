"""ob-note FastAPI server — Obsidian-like vault at site root."""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from application.api.routes_auth import bind_request_vault_user, router as auth_router
from application.api.routes_agent import router as agent_router
from application.api.routes_documents import router as documents_router
from application.api.routes_files import router as files_router
from application.api.routes_graph import router as graph_router
from application.api.routes_meeting_log import router as meeting_log_router
from application.api.routes_prefs import router as prefs_router
from application.api.routes_search import router as search_router
from application.api.routes_share import api_router as share_api_router
from application.api.routes_share import public_router as share_public_router
from application.security_headers import SecurityHeadersMiddleware
from application import vault_backend

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("server")

_APP_DIR = Path(__file__).resolve().parent
_ROOT = _APP_DIR.parent
_WEB_DIST = _ROOT / "web" / "dist"

_ENABLE_API_DOCS = os.environ.get("ENABLE_API_DOCS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    vault_backend.ensure_seed_vault()
    mode = vault_backend.backend_mode()
    logger.info("Vault backend mode: %s base=%s", mode, vault_backend.vault_base())
    try:
        from application import app_data_backend, vault_db_persistence

        logger.info(
            "App-data persistence: mode=%s mount=%s enabled=%s",
            app_data_backend.backend_mode(),
            app_data_backend.mount_dir(),
            vault_db_persistence.persistence_enabled(),
        )
    except Exception:
        logger.debug("App-data backend probe failed", exc_info=True)
    try:
        from application import note_store

        if note_store.enabled():
            note_store.start_background_mirror()
            logger.info(
                "Notes: DynamoDB table=%s is the source of truth; S3 mirrors from it",
                note_store.table_name(),
            )
    except Exception:
        logger.exception("Failed to start DynamoDB S3 mirror")
    if mode == "s3":
        try:
            from application import vault_sync

            result = vault_sync.startup_sync()
            logger.info("Startup vault sync: %s", result)
        except Exception:
            logger.exception("Initial vault S3 sync setup failed")
    # Index rebuild is per-user on first authenticated access / sync.
    try:
        yield
    finally:
        try:
            from application import vault_db_persistence

            vault_db_persistence.flush_persist()
            logger.info("notes.db shutdown persist complete")
        except Exception:
            logger.exception("notes.db shutdown persist failed")

class VaultUserMiddleware(BaseHTTPMiddleware):
    """Bind vault_backend paths to the signed-in user for each request."""

    async def dispatch(self, request: Request, call_next):
        user_id = None
        try:
            user_id = bind_request_vault_user(request)
        except Exception:
            logger.debug("Vault user bind skipped", exc_info=True)
        token = None
        if user_id:
            token = vault_backend.set_current_user_id(user_id)
        try:
            return await call_next(request)
        finally:
            if token is not None:
                vault_backend.reset_current_user_id(token)
            else:
                vault_backend.set_current_user_id(None)


app = FastAPI(
    title="ob-note",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs" if _ENABLE_API_DOCS else None,
    redoc_url="/api/redoc" if _ENABLE_API_DOCS else None,
    openapi_url="/api/openapi.json" if _ENABLE_API_DOCS else None,
)

# Must allow same-origin iframe for Notes Graph despite CloudFront XFO DENY.
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(VaultUserMiddleware)

app.include_router(auth_router)
app.include_router(files_router)
app.include_router(prefs_router)
app.include_router(agent_router)
app.include_router(documents_router)
app.include_router(share_api_router)
app.include_router(share_public_router)
app.include_router(search_router)
app.include_router(graph_router)
app.include_router(meeting_log_router)


@app.get("/api/health")
def health() -> dict:
    from application import app_data_backend

    return {
        "status": "ok",
        "service": "ob-note",
        "backend": vault_backend.backend_mode(),
        "app_data": app_data_backend.backend_mode(),
    }


@app.get("/favicon.ico")
@app.get("/favicon.svg")
def favicon():
    icon = _WEB_DIST / "favicon.svg"
    if icon.is_file():
        return FileResponse(icon, media_type="image/svg+xml")
    return HTMLResponse("", status_code=204)


# Legacy /vault/* bookmarks → root paths
@app.get("/vault")
@app.get("/vault/")
def legacy_vault_root():
    return RedirectResponse(url="/", status_code=302)


@app.get("/vault/{full_path:path}")
def legacy_vault_get(full_path: str):
    target = f"/{full_path}" if full_path else "/"
    return RedirectResponse(url=target, status_code=302)


@app.head("/vault/{full_path:path}")
def legacy_vault_head(full_path: str):
    target = f"/{full_path}" if full_path else "/"
    return RedirectResponse(url=target, status_code=302)


if _WEB_DIST.is_dir():
    assets = _WEB_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/")
    def spa_index():
        index = _WEB_DIST / "index.html"
        if index.is_file():
            return FileResponse(index)
        return HTMLResponse("<h1>ob-note</h1><p>Build web/ first.</p>", status_code=503)

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        # API / public share / legacy vault handled by earlier routes.
        if full_path.startswith("api/") or full_path.startswith("s/"):
            return HTMLResponse("Not Found", status_code=404)
        candidate = _WEB_DIST / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        index = _WEB_DIST / "index.html"
        if index.is_file():
            return FileResponse(index)
        return HTMLResponse("Build web/ first", status_code=503)
else:

    @app.get("/")
    def spa_missing():
        return HTMLResponse(
            "<h1>ob-note</h1><p>Frontend not built. Run <code>cd web && npm run build</code>.</p>",
            status_code=503,
        )
