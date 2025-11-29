import asyncio
import logging
from typing import List

from fastapi import FastAPI, HTTPException

from .config import settings
from . import log_reader
from .scheduler import monitor_dispatcher
from .schemas import (
    LogSource,
    LogSourceCreate,
    LogSourceUpdate,
    MonitorRun,
    PromptMonitor,
    PromptMonitorCreate,
    PromptMonitorUpdate,
    Target,
    TargetCreate,
    TargetUpdate,
)
from .service import run_monitor
from .storage import storage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AI Log Monitor")


@app.on_event("startup")
async def startup_event() -> None:
    _ensure_default_target()
    # Kick off scheduler
    app.state.scheduler_task = asyncio.create_task(
        monitor_dispatcher(run_monitor, settings.scheduler_tick_seconds)
    )
    logger.info("Scheduler started with tick=%s", settings.scheduler_tick_seconds)


@app.on_event("shutdown")
async def shutdown_event() -> None:
    task = getattr(app.state, "scheduler_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            logger.info("Scheduler task cancelled cleanly")
    storage.close()


def _ensure_default_target() -> None:
    """Create a default local target when none exist for quicker setup."""
    existing = storage.list_targets()
    if existing:
        return
    default_target = Target(name=settings.default_target_name, type="local")
    storage.create_target(default_target.dict())
    logger.info("Created default target '%s'", settings.default_target_name)


@app.get("/health")
async def health() -> dict:
    scheduler_task = getattr(app.state, "scheduler_task", None)
    scheduler_running = bool(scheduler_task) and not scheduler_task.cancelled()
    db_ok = storage.ping()
    status = "ok" if scheduler_running and db_ok else "degraded"
    return {
        "status": status,
        "scheduler_running": scheduler_running,
        "database": db_ok,
    }


# Targets
@app.post("/targets", response_model=Target)
async def create_target(payload: TargetCreate) -> Target:
    target = Target(**payload.dict())
    storage.create_target(target.dict())
    return target


@app.get("/targets", response_model=List[Target])
async def list_targets() -> List[Target]:
    return [Target(**t) for t in storage.list_targets()]


@app.get("/targets/{target_id}", response_model=Target)
async def get_target(target_id: str) -> Target:
    target = storage.get_target(target_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target not found")
    return Target(**target)


@app.put("/targets/{target_id}", response_model=Target)
async def update_target(target_id: str, payload: TargetUpdate) -> Target:
    target = storage.get_target(target_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target not found")
    updates = {k: v for k, v in payload.dict(exclude_unset=True).items()}
    updated = storage.update_target(target_id, updates)
    return Target(**(updated or target))


@app.delete("/targets/{target_id}")
async def delete_target(target_id: str) -> dict:
    if not storage.get_target(target_id):
        raise HTTPException(status_code=404, detail="Target not found")
    try:
        deleted = storage.delete_target(target_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"deleted": deleted}


# Log sources
@app.post("/log-sources", response_model=LogSource)
async def create_log_source(payload: LogSourceCreate) -> LogSource:
    if not storage.get_target(payload.target_id):
        raise HTTPException(status_code=400, detail="Target not found")
    source = LogSource(**payload.dict())
    storage.create_log_source(source.dict())
    return source


@app.get("/log-sources", response_model=List[LogSource])
async def list_log_sources() -> List[LogSource]:
    return [LogSource(**s) for s in storage.list_log_sources()]


@app.get("/log-sources/{source_id}", response_model=LogSource)
async def get_log_source(source_id: str) -> LogSource:
    source = storage.get_log_source(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Log source not found")
    return LogSource(**source)


@app.put("/log-sources/{source_id}", response_model=LogSource)
async def update_log_source(source_id: str, payload: LogSourceUpdate) -> LogSource:
    source = storage.get_log_source(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Log source not found")
    updates = {k: v for k, v in payload.dict(exclude_unset=True).items()}
    if updates.get("target_id") and not storage.get_target(updates["target_id"]):
        raise HTTPException(status_code=400, detail="Target not found")
    updated = storage.update_log_source(source_id, updates)
    return LogSource(**(updated or source))


@app.delete("/log-sources/{source_id}")
async def delete_log_source(source_id: str) -> dict:
    if not storage.get_log_source(source_id):
        raise HTTPException(status_code=404, detail="Log source not found")
    try:
        deleted = storage.delete_log_source(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"deleted": deleted}


# Monitors
@app.post("/monitors", response_model=PromptMonitor)
async def create_monitor(payload: PromptMonitorCreate) -> PromptMonitor:
    if not storage.get_target(payload.target_id):
        raise HTTPException(status_code=400, detail="Target not found")
    if payload.log_source_id and not storage.get_log_source(payload.log_source_id):
        raise HTTPException(status_code=400, detail="Log source not found")
    monitor = PromptMonitor(**payload.dict())
    storage.create_monitor(monitor.dict())
    return monitor


@app.get("/monitors", response_model=List[PromptMonitor])
async def list_monitors() -> List[PromptMonitor]:
    return [PromptMonitor(**m) for m in storage.list_monitors()]


@app.get("/monitors/{monitor_id}", response_model=PromptMonitor)
async def get_monitor(monitor_id: str) -> PromptMonitor:
    monitor = storage.get_monitor(monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    return PromptMonitor(**monitor)


@app.put("/monitors/{monitor_id}", response_model=PromptMonitor)
async def update_monitor(monitor_id: str, payload: PromptMonitorUpdate) -> PromptMonitor:
    monitor = storage.get_monitor(monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    updates = {
        k: v
        for k, v in payload.dict(exclude_unset=True).items()
        if v is not None or isinstance(v, bool)
    }
    if updates.get("target_id") and not storage.get_target(updates["target_id"]):
        raise HTTPException(status_code=400, detail="Target not found")
    if updates.get("log_source_id") and not storage.get_log_source(updates["log_source_id"]):
        raise HTTPException(status_code=400, detail="Log source not found")
    updated = storage.update_monitor(monitor_id, updates)
    if not updated:
        raise HTTPException(status_code=404, detail="Monitor not found")
    return PromptMonitor(**updated)


@app.delete("/monitors/{monitor_id}")
async def delete_monitor(monitor_id: str) -> dict:
    if not storage.get_monitor(monitor_id):
        raise HTTPException(status_code=404, detail="Monitor not found")
    deleted = storage.delete_monitor(monitor_id)
    return {"deleted": deleted}


@app.post("/monitors/{monitor_id}/run-once", response_model=MonitorRun)
async def run_monitor_once(monitor_id: str) -> MonitorRun:
    monitor = storage.get_monitor(monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    storage.touch_monitor_last_run(monitor_id)
    run = await run_monitor(monitor)
    return MonitorRun(**run)


@app.post("/log-sources/{source_id}/test")
async def test_log_source(source_id: str) -> dict:
    source = storage.get_log_source(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Log source not found")

    try:
        preview, _ = log_reader.read_logs(
            source["mode"],
            source["config"],
            source.get("cursor_state"),
            {"max_lines": 20, "max_chars": 4000},
        )
    except Exception as exc:  # surface log read errors to caller
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"preview": preview}


# Runs
@app.get("/monitors/{monitor_id}/runs", response_model=List[MonitorRun])
async def list_runs(monitor_id: str, limit: int = 50, offset: int = 0) -> List[MonitorRun]:
    if not storage.get_monitor(monitor_id):
        raise HTTPException(status_code=404, detail="Monitor not found")
    runs = storage.list_monitor_runs(monitor_id, limit=limit, offset=offset)
    return [MonitorRun(**r) for r in runs]


@app.get("/monitors/{monitor_id}/runs/latest", response_model=MonitorRun)
async def get_latest_run(monitor_id: str) -> MonitorRun:
    if not storage.get_monitor(monitor_id):
        raise HTTPException(status_code=404, detail="Monitor not found")
    run = storage.latest_monitor_run(monitor_id)
    if not run:
        raise HTTPException(status_code=404, detail="No runs found for monitor")
    return MonitorRun(**run)


@app.get("/runs/{run_id}", response_model=MonitorRun)
async def get_run(run_id: str) -> MonitorRun:
    run = storage.get_monitor_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return MonitorRun(**run)
