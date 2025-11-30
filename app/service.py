import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime
from typing import Any, Iterable

from .config import settings
from .llm_client import get_llm_client
from .notifications import send_email, send_sms
from .storage import storage
from . import log_reader

logger = logging.getLogger(__name__)


async def run_monitor(monitor: dict) -> dict:
    """Execute a monitor once and persist run history."""
    provider = (monitor.get("llm_provider") or settings.llm_provider or "openai").lower()
    provider_metadata = _merge_provider_metadata(
        monitor.get("llm_provider_metadata"), _llm_provider_metadata(provider)
    )

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
        "llm_provider": provider,
        "llm_provider_metadata": json.dumps(provider_metadata) if provider_metadata else None,
    }
    storage.create_monitor_run(run)

    try:
        logs_text, success_count, total_inputs, log_source = await collect_monitor_inputs(monitor)

        prompt = monitor["prompt"]
        llm_input = json.dumps(
            {
                "system": (
                    "You are a monitoring assistant reviewing logs grouped by labels. "
                    "Use the provided labeled sections to make your decision."
                ),
                "prompt": prompt,
                "logs": logs_text,
            }
        )
        llm_input = _truncate_storage(llm_input, settings.run_record_max_chars)
        llm_client = get_llm_client(provider)
        llm_output = await llm_client.analyze_logs(prompt, logs_text, provider_metadata=provider_metadata)
        status = _map_llm_status(llm_output.get("status"))

        run_updates: dict[str, Any] = {
            "finished_at": datetime.utcnow().isoformat(),
            "status": status,
            "llm_provider": provider,
            "llm_provider_metadata": json.dumps(provider_metadata) if provider_metadata else None,
            "llm_raw_input": llm_input,
            "llm_raw_output": _truncate_storage(
                json.dumps(llm_output), settings.run_record_max_chars
            ),
            "summary": llm_output.get("summary"),
            "details": llm_output.get("report") or llm_output.get("details"),
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


async def collect_monitor_inputs(monitor: dict) -> tuple[str, int, int, dict | None]:
    """Collect monitor inputs or log source content without invoking the LLM."""

    target = storage.get_target(monitor.get("target_id")) if monitor.get("target_id") else None
    log_source = (
        storage.get_log_source(monitor.get("log_source_id")) if monitor.get("log_source_id") else None
    )
    if not target and log_source:
        target = storage.get_target(log_source.get("target_id")) if log_source else None
    if not target:
        raise RuntimeError("Monitor references missing target")

    logs_text, success_count, total_inputs, log_source = await _collect_monitor_logs(
        monitor, log_source
    )
    if total_inputs == 0:
        raise RuntimeError("No log input configured for this monitor")
    if success_count == 0:
        raise RuntimeError("All log inputs failed to collect")

    return logs_text, success_count, total_inputs, log_source


def _should_notify(notify_on: str, status: str) -> bool:
    if notify_on == "all":
        return True
    if notify_on == "warn_and_alert" and status in {"warn", "alert"}:
        return True
    if notify_on == "alert_only" and status == "alert":
        return True
    return False


def _map_llm_status(raw: str | None) -> str:
    normalized = (raw or "").strip().lower()
    if normalized in {"healthy", "ok", "normal"}:
        return "ok"
    if normalized in {"warning", "warn"}:
        return "warn"
    if normalized in {"critical", "alert", "error"}:
        return "alert"
    return "error"


def _llm_provider_metadata(provider: str) -> dict:
    if provider == "openai":
        return {"model": settings.openai_model or "gpt-4.1-mini"}
    if provider == "amazon_q":
        return {"application_id": settings.qbusiness_app_id, "region": settings.aws_region}
    if provider in {"stub", "dummy", "mock"}:
        return {"note": "stub provider; replace with a real LLM in production"}
    return {}


def _merge_provider_metadata(configured: Any, defaults: dict) -> dict:
    """Merge monitor-configured metadata with provider defaults."""

    parsed: dict = {}
    if isinstance(configured, str):
        try:
            parsed = json.loads(configured)
        except Exception:
            parsed = {}
    elif isinstance(configured, dict):
        parsed = configured

    merged = {**(defaults or {}), **parsed}
    return {k: v for k, v in merged.items() if v is not None}


async def _maybe_notify(monitor: dict, run: dict, log_source: dict | None) -> None:
    config = monitor.get("notification_config") or {}
    notify_on = config.get("notify_on", "alert_only")
    status = run.get("status") or ""
    if not _should_notify(notify_on, status):
        return

    subject = f"Monitor {monitor['name']} status: {status}"
    body_lines = [
        f"Monitor: {monitor['name']}",
        f"Target: {monitor.get('target_id') or 'n/a'}",
        f"Status: {status}",
    ]
    if log_source:
        body_lines.append(f"Log source: {log_source['name']}")
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


async def _collect_monitor_logs(
    monitor: dict, log_source: dict | None = None
) -> tuple[str, int, int, dict | None]:
    inputs: Iterable[dict] = monitor.get("inputs") or []
    window_config = monitor.get("window_config") or {}
    logs_by_label: dict[str, str] = {}

    success_count = 0

    loop = asyncio.get_running_loop()
    semaphore = asyncio.Semaphore(settings.max_command_workers)

    async def _collect(label: str, command: str, *, timeout: int | None, workdir: str | None, env: dict | None) -> str:
        async with semaphore:
            return await loop.run_in_executor(
                None,
                _run_command,
                command,
                timeout if timeout is not None else settings.command_timeout_seconds,
                workdir,
                env,
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

        timeout_override = input_def.get("timeout_seconds")
        workdir = input_def.get("workdir")
        env = input_def.get("env") if isinstance(input_def.get("env"), dict) else None

        pending.append(
            (
                label,
                asyncio.create_task(
                    _collect(label, command, timeout=timeout_override, workdir=workdir, env=env)
                ),
            )
        )

    total_inputs = len(pending) + (1 if log_source else 0)

    for label, task in pending:
        try:
            output = await task
            logs_by_label[label] = _truncate_output(output, window_config)
            success_count += 1
        except Exception as exc:  # capture failures per input without aborting others
            logger.exception("Failed to collect input %s for monitor %s", label, monitor.get("id"))
            logs_by_label[label] = f"[collection_error] {exc}"

    if log_source:
        source_label = log_source.get("name", "log_source")
        try:
            content, cursor = await loop.run_in_executor(
                None,
                log_reader.read_logs,
                log_source["mode"],
                log_source["config"],
                log_source.get("cursor_state"),
                window_config,
            )
            if cursor is not None:
                storage.update_log_source_cursor(log_source["id"], cursor)
            logs_by_label[source_label] = _truncate_output(content, window_config)
            success_count += 1
        except Exception as exc:
            logger.exception(
                "Failed to collect logs from source %s for monitor %s", log_source.get("id"), monitor.get("id")
            )
            logs_by_label[source_label] = f"[collection_error] {exc}"

    if logs_by_label:
        sections = []
        for label, text in logs_by_label.items():
            sections.append(f"[{label}]\n{text}")

        combined = "\n\n".join(sections)
        combined = _truncate_output(combined, window_config)
        return combined, success_count, total_inputs, log_source
    return "", success_count, total_inputs, log_source


def _run_command(
    command: str, timeout_seconds: int = 60, workdir: str | None = None, env: dict | None = None
) -> str:
    merged_env = os.environ.copy()
    if env:
        merged_env.update({str(k): str(v) for k, v in env.items()})

    completed = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        cwd=workdir,
        env=merged_env,
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


def _truncate_storage(text: str | None, max_chars: int | None) -> str | None:
    if text is None or max_chars is None:
        return text
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]
