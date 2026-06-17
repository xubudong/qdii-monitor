from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .configuration import load_config
from .database import Database
from .scheduler import create_scheduler
from .service import MonitorService
from .settings import CONFIG_FILE, DB_FILE, DISABLE_SCHEDULER, DISABLE_STARTUP_REFRESH, STATIC_DIR


def create_app(
    config_path: Path = CONFIG_FILE,
    db_path: Path = DB_FILE,
    service_overrides: dict[str, Any] | None = None,
    scheduler_enabled: bool | None = None,
) -> FastAPI:
    config = load_config(config_path)
    db = Database(db_path)
    db.initialize()
    service = MonitorService(config, db, **(service_overrides or {}))
    enable_scheduler = (not DISABLE_SCHEDULER) if scheduler_enabled is None else scheduler_enabled

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = create_scheduler(service, run_startup_refresh=not DISABLE_STARTUP_REFRESH) if enable_scheduler else None
        app.state.scheduler = scheduler
        yield
        if scheduler:
            scheduler.shutdown(wait=False)

    app = FastAPI(title="QDII ETF 溢价与公告监控台", version="1.0.0", lifespan=lifespan)
    app.state.service = service
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def home() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    @app.get("/api/dashboard")
    def dashboard(
        snapshot_at: str | None = Query(default=None),
        snapshot_mode: str = Query(default="day", pattern="^(latest|day|open|close)$"),
    ) -> dict[str, Any]:
        return service.dashboard(snapshot_at, snapshot_mode)

    @app.get("/api/groups")
    def groups() -> dict[str, Any]:
        return {"groups": service.groups_payload()}

    @app.get("/api/history")
    def history(
        limit: int = Query(default=240, ge=10, le=2000),
        daily_limit: int = Query(default=760, ge=20, le=1000),
    ) -> dict[str, Any]:
        return service.history_payload(limit, daily_limit)

    @app.post("/api/refresh/quotes")
    def refresh_quotes() -> dict[str, Any]:
        return _execute(service.refresh_quotes)

    @app.post("/api/refresh/premarket")
    def refresh_premarket() -> dict[str, Any]:
        return _execute(service.refresh_premarket_anchors)

    @app.post("/api/refresh/references")
    def refresh_references() -> dict[str, Any]:
        return _execute(service.refresh_references)

    @app.post("/api/refresh/notices")
    def refresh_notices() -> dict[str, Any]:
        return _execute(service.refresh_notices)

    @app.post("/api/refresh/history")
    def refresh_history(days: int = Query(default=1095, ge=30, le=1500)) -> dict[str, Any]:
        return _execute(lambda: service.refresh_daily_premiums(days))

    @app.post("/api/refresh/quota")
    def refresh_quota() -> dict[str, Any]:
        return _execute(service.refresh_quota)

    @app.post("/api/refresh/metadata")
    def refresh_metadata() -> dict[str, Any]:
        return _execute(service.refresh_metadata)

    @app.post("/api/holdings/upload")
    async def upload_holdings(file: UploadFile = File(...)) -> dict[str, Any]:
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="上传文件为空")
        try:
            return service.upload_holdings(content, file.filename or "table.xls")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "config_file": str(config_path),
            "database": str(db_path),
            "funds": len(config.funds),
            "groups": len(config.groups),
            "scheduler_enabled": enable_scheduler,
            "tasks": db.task_statuses(),
        }

    return app


def _execute(callback: Any) -> dict[str, Any]:
    try:
        return callback()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


app = create_app()
