import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import List

from fastapi import Depends, FastAPI, HTTPException, Header

from .config import settings
from . import log_reader
from .scheduler import monitor_dispatcher
from .schemas import (
    LogSource,
    LogSourceCreate,
    LogSourceUpdate,
    MonitorCollection,
    MonitorRun,
    MonitorStatus,
    PromptMonitor,
    PromptMonitorCreate,
    PromptMonitorUpdate,
    Target,
    TargetCreate,
    TargetUpdate,
)
from .service import collect_monitor_inputs, run_monitor
from .storage import storage
from .llm_client import validate_llm_provider_config, supported_llm_providers

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """Simple header-based API token guard when API_TOKEN is configured."""

    token = settings.api_token
    if not token:
        return
    if x_api_key == token:
        return
    raise HTTPException(status_code=401, detail="Invalid or missing API token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_default_target()
    if settings.scheduler_enabled:
        app.state.scheduler_task = asyncio.create_task(
            monitor_dispatcher(run_monitor, settings.scheduler_tick_seconds)
        )
        logger.info("Scheduler started with tick=%s", settings.scheduler_tick_seconds)
    else:
        app.state.scheduler_task = None
        logger.info("Scheduler disabled via SCHEDULER_ENABLED=false")

    try:
        yield
    finally:
        task = getattr(app.state, "scheduler_task", None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.info("Scheduler task cancelled cleanly")
        storage.close()


app = FastAPI(title="AI Log Monitor", dependencies=[Depends(require_api_key)], lifespan=lifespan)


def _ensure_default_target() -> None:
    """Create a default local target when none exist for quicker setup."""
    existing = storage.list_targets()
    if existing:
        return
    default_target = Target(name=settings.default_target_name, type="local")
    storage.create_target(default_target.model_dump())
    logger.info("Created default target '%s'", settings.default_target_name)


@app.get("/health")
async def health() -> dict:
    scheduler_task = getattr(app.state, "scheduler_task", None)
    scheduler_running = bool(scheduler_task) and not scheduler_task.cancelled()
    db_ok = storage.ping()
    expected_scheduler = settings.scheduler_enabled
    scheduler_ok = scheduler_running or not expected_scheduler
    llm_status = validate_llm_provider_config()
    status = "ok" if scheduler_ok and db_ok and llm_status.get("ready") else "degraded"
    return {
        "status": status,
        "scheduler_enabled": expected_scheduler,
        "scheduler_running": scheduler_running,
        "database": db_ok,
        "database_backend": storage.backend,
        "llm_provider": llm_status.get("provider"),
        "llm_ready": llm_status.get("ready"),
        "llm_message": llm_status.get("message"),
        "supported_llm_providers": supported_llm_providers(),
    }


@app.get("/llm/providers")
async def llm_providers(provider: str | None = None) -> dict:
    """Return readiness details for all supported LLM providers or a specific one."""

    if provider:
        return {"providers": [validate_llm_provider_config(provider)]}

    statuses = [validate_llm_provider_config(p) for p in supported_llm_providers()]
    return {"providers": statuses}


# Targets
@app.post("/targets", response_model=Target)
async def create_target(payload: TargetCreate) -> Target:
    target = Target(**payload.model_dump())
    storage.create_target(target.model_dump())
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
    updates = {k: v for k, v in payload.model_dump(exclude_unset=True).items()}
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
    source = LogSource(**payload.model_dump())
    storage.create_log_source(source.model_dump())
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
    updates = {k: v for k, v in payload.model_dump(exclude_unset=True).items()}
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
    monitor = PromptMonitor(**payload.model_dump())
    storage.create_monitor(monitor.model_dump())
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
        for k, v in payload.model_dump(exclude_unset=True).items()
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


@app.post("/monitors/{monitor_id}/collect", response_model=MonitorCollection)
async def collect_monitor(monitor_id: str) -> MonitorCollection:
    monitor = storage.get_monitor(monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")

    try:
        logs_text, success, total, log_source = await collect_monitor_inputs(monitor)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return MonitorCollection(
        logs_text=logs_text,
        success_count=success,
        total_inputs=total,
        log_source_id=log_source.get("id") if log_source else None,
        log_source_name=log_source.get("name") if log_source else None,
    )


@app.get("/monitors/{monitor_id}/status", response_model=MonitorStatus)
async def monitor_status(monitor_id: str) -> MonitorStatus:
    monitor = storage.get_monitor(monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    return _build_monitor_status(monitor)


@app.get("/monitors/status", response_model=List[MonitorStatus])
async def all_monitor_statuses() -> List[MonitorStatus]:
    monitors = storage.list_monitors()
    return [_build_monitor_status(m) for m in monitors]


def _build_monitor_status(monitor: dict) -> MonitorStatus:
    last_run_at = monitor.get("last_run_at")
    interval_seconds = monitor.get("interval_seconds")
    enabled = monitor.get("enabled", True)

    now = datetime.utcnow()
    next_run_at: str | None = None
    due_in: float | None = None

    if enabled and settings.scheduler_enabled:
        if last_run_at:
            last = datetime.fromisoformat(last_run_at)
            next_dt = last + timedelta(seconds=interval_seconds)
        else:
            next_dt = now
        next_run_at = next_dt.isoformat()
        due_in = max(0.0, (next_dt - now).total_seconds())

    latest = storage.latest_monitor_run(monitor["id"])

    return MonitorStatus(
        monitor_id=monitor["id"],
        enabled=enabled,
        interval_seconds=interval_seconds,
        last_run_at=last_run_at,
        next_run_at=next_run_at,
        due_in_seconds=due_in,
        scheduler_enabled=settings.scheduler_enabled,
        latest_run=MonitorRun(**latest) if latest else None,
    )


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


@app.post("/monitors/{monitor_id}/enable")
async def enable_monitor(monitor_id: str) -> dict:
    monitor = storage.get_monitor(monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    storage.update_monitor(monitor_id, {"enabled": True})
    return {"monitor_id": monitor_id, "enabled": True}


@app.post("/monitors/{monitor_id}/disable")
async def disable_monitor(monitor_id: str) -> dict:
    monitor = storage.get_monitor(monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    storage.update_monitor(monitor_id, {"enabled": False})
    return {"monitor_id": monitor_id, "enabled": False}
