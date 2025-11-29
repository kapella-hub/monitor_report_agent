import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable

from .storage import storage

logger = logging.getLogger(__name__)


async def monitor_dispatcher(run_monitor: Callable[[dict], Awaitable[None]], tick_seconds: int = 1) -> None:
    """Simple scheduler that runs monitors based on interval_seconds."""
    running: set[str] = set()

    async def _run_and_release(monitor: dict) -> None:
        monitor_id = monitor["id"]
        running.add(monitor_id)
        try:
            await run_monitor(monitor)
        finally:
            running.discard(monitor_id)

    try:
        while True:
            try:
                now = datetime.utcnow()
                monitors = storage.list_monitors()
                for monitor in monitors:
                    last_run_at = monitor.get("last_run_at")
                    due = False
                    if not last_run_at:
                        due = True
                    else:
                        last_run_dt = datetime.fromisoformat(last_run_at)
                        elapsed = (now - last_run_dt).total_seconds()
                        due = elapsed >= monitor["interval_seconds"]
                    if due and monitor["id"] in running:
                        logger.info("Monitor %s still running; skipping new run", monitor["name"])
                        continue
                    if due:
                        logger.info("Scheduling monitor %s", monitor["name"])
                        asyncio.create_task(_run_and_release(monitor))
                        storage.touch_monitor_last_run(monitor["id"])
            except Exception:
                logger.exception("Scheduler loop error")

            await asyncio.sleep(tick_seconds)
    except asyncio.CancelledError:
        logger.info("Scheduler task cancelled; shutting down")
        raise
