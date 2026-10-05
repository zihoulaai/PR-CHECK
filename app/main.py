"""FastAPI 应用工厂。

挂载路由、统一异常中间件、静态单页 UI；启动时初始化 SQLite。
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.errors import register_exception_handlers
from app.storage.sqlite import init_db

logger = logging.getLogger("pr_check")

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
INDEX_HTML = os.path.join(STATIC_DIR, "index.html")


def create_app() -> FastAPI:
    app = FastAPI(title="PR 提交前置自检 Agent", version="0.1.0")

    register_exception_handlers(app)

    from app.api.routes_settings import router as settings_router
    from app.api.routes_projects import router as projects_router
    from app.api.routes_check import router as check_router
    from app.api.routes_kb import router as kb_router

    app.include_router(settings_router)
    app.include_router(projects_router)
    app.include_router(check_router)
    app.include_router(kb_router)

    @app.on_event("startup")
    def _startup() -> None:
        try:
            init_db()
        except Exception as exc:  # 不阻断启动，但记录
            logger.error("init_db_failed type=%s", type(exc).__name__)

    # 静态资源（若存在）
    if os.path.isdir(STATIC_DIR):
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", response_class=HTMLResponse)
        def index() -> HTMLResponse:
            if os.path.exists(INDEX_HTML):
                return FileResponse(INDEX_HTML)
            return HTMLResponse("<h1>PR 提交前置自检 Agent</h1><p>前端未构建。</p>")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
