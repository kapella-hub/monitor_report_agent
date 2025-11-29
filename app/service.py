import asyncio
import json
import logging
import subprocess
from datetime import datetime
from typing import Any, Iterable

from .config import settings
from .llm_client import analyze_logs_with_llm
from .notifications import send_email, send_sms
from .storage import storage

logger = logging.getLogger(__name__)


async def run_monitor(monitor: dict) -> dict:
    """Execute a monitor once and persist run history."""
    run = {
        "id": monitor.get("run_id") or monitor["id"] + "-" + datetime.utcnow().strftime("%Y%m%d%H%M%S%f"),
        "monitor_id": monitor["id"],
        "started_at": datetime.utcnow().isoformat(),
        "status": None,
        "llm_raw_input": None,
        "llm_raw_output": None,
        "summary": None,
        "details": None,
        "error_message": None,
    }
    storage.create_monitor_run(run)

    try:
        log_source = storage.get_log_source(monitor["log_source_id"])
        if not log_source:
            raise RuntimeError("Monitor references missing log source")

        logs_text, success_count, total_inputs = await _collect_monitor_logs(monitor)
        if total_inputs == 0:
            raise RuntimeError("No log input configured for this monitor")
        if success_count == 0:
            raise RuntimeError("All log inputs failed to collect")

        prompt = monitor["prompt"]
        llm_input = json.dumps({
            "system": (
                "You are a monitoring assistant reviewing logs grouped by labels. "
                "Use the provided labeled sections to make your decision."
            ),
            "prompt": prompt,
            "logs": logs_text,
        })
        llm_output = await analyze_logs_with_llm(prompt, logs_text)

        run_updates: dict[str, Any] = {
            "finished_at": datetime.utcnow().isoformat(),
            "status": llm_output.get("status", "error"),
            "llm_raw_input": llm_input,
            "llm_raw_output": json.dumps(llm_output),
            "summary": llm_output.get("summary"),
            "details": llm_output.get("details"),
        }
        storage.update_monitor_run(run["id"], run_updates)
        run.update(run_updates)

        storage.prune_monitor_runs(monitor["id"], settings.max_run_history_per_monitor)

        await _maybe_notify(monitor, run, log_source)
        return run
    except Exception as exc:
        logger.exception("Monitor %s failed", monitor["id"])
        finished_at = datetime.utcnow().isoformat()
        updates = {
            "finished_at": finished_at,
            "status": "error",
            "error_message": str(exc),
        }
        storage.update_monitor_run(run["id"], updates)
        run.update(updates)
        storage.prune_monitor_runs(monitor["id"], settings.max_run_history_per_monitor)
        return run


def _should_notify(notify_on: str, status: str) -> bool:
    if notify_on == "all":
        return True
    if notify_on == "warn_and_alert" and status in {"warn", "alert"}:
        return True
    if notify_on == "alert_only" and status == "alert":
        return True
    return False


async def _maybe_notify(monitor: dict, run: dict, log_source: dict) -> None:
    config = monitor.get("notification_config") or {}
    notify_on = config.get("notify_on", "alert_only")
    status = run.get("status") or ""
    if not _should_notify(notify_on, status):
        return

    subject = f"Monitor {monitor['name']} status: {status}"
    body_lines = [
        f"Monitor: {monitor['name']}",
        f"Log source: {log_source['name']}",
        f"Status: {status}",
    ]
    if run.get("summary"):
        body_lines.append(f"Summary: {run['summary']}")
    if run.get("details"):
        body_lines.append(f"Details: {run['details']}")
    body_lines.append(f"Run ID: {run['id']}")
    body_lines.append(f"Started at: {run['started_at']}")
    body_lines.append(f"Finished at: {run.get('finished_at')}")
    body = "\n".join(body_lines)

    email_recipients = config.get("email_recipients", [])
    sms_recipients = config.get("sms_recipients", [])

    if not email_recipients and not sms_recipients:
        return

    loop = asyncio.get_event_loop()
    if email_recipients:
        await loop.run_in_executor(None, send_email, email_recipients, subject, body)
    if sms_recipients:
        await loop.run_in_executor(None, send_sms, sms_recipients, body)


async def _collect_monitor_logs(monitor: dict) -> tuple[str, int, int]:
    inputs: Iterable[dict] = monitor.get("inputs") or []
    window_config = monitor.get("window_config") or {}
    logs_by_label: dict[str, str] = {}

    success_count = 0

    loop = asyncio.get_running_loop()
    semaphore = asyncio.Semaphore(settings.max_command_workers)

    async def _collect(label: str, command: str) -> str:
        async with semaphore:
            return await loop.run_in_executor(
                None, _run_command, command, settings.command_timeout_seconds
            )

    pending: list[tuple[str, asyncio.Task[str]]] = []
    for input_def in inputs:
        mode = input_def.get("mode")
        label = input_def.get("label") or "UNLABELED"
        if mode != "command":
            logger.warning("Unsupported input mode %s for monitor %s", mode, monitor.get("id"))
            continue

        command = input_def.get("command")
        if not command:
            logger.warning("Missing command for monitor %s input %s", monitor.get("id"), label)
            continue

        pending.append((label, asyncio.create_task(_collect(label, command))))

    for label, task in pending:
        try:
            output = await task
            logs_by_label[label] = _truncate_output(output, window_config)
            success_count += 1
        except Exception as exc:  # capture failures per input without aborting others
            logger.exception("Failed to collect input %s for monitor %s", label, monitor.get("id"))
            logs_by_label[label] = f"[collection_error] {exc}"

    if not logs_by_label:
        return "", success_count, len(pending)

    sections = []
    for label, text in logs_by_label.items():
        sections.append(f"[{label}]\n{text}")

    combined = "\n\n".join(sections)
    combined = _truncate_output(combined, window_config)
    return combined, success_count, len(pending)


def _run_command(command: str, timeout_seconds: int = 60) -> str:
    completed = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Command failed (exit {completed.returncode}): {completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout


def _truncate_output(text: str, window_config: dict) -> str:
    max_lines = window_config.get("max_lines")
    max_chars = window_config.get("max_chars")

    lines = text.splitlines()
    if max_lines and len(lines) > max_lines:
        lines = lines[-max_lines:]
    truncated = "\n".join(lines)

    if max_chars and len(truncated) > max_chars:
        truncated = truncated[-max_chars:]
    return truncated
