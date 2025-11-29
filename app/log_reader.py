import json
import os
import subprocess
from datetime import datetime, timedelta
from typing import Tuple


class LogReadError(Exception):
    pass


def _enforce_window(log_text: str, max_lines: int | None, max_chars: int | None) -> str:
    lines = log_text.splitlines()
    if max_lines is not None and len(lines) > max_lines:
        lines = lines[-max_lines:]
    truncated = "\n".join(lines)
    if max_chars is not None and len(truncated) > max_chars:
        truncated = truncated[-max_chars:]
    return truncated


def read_file_logs(path: str, cursor_state: dict | None, max_lines: int | None, max_chars: int | None) -> Tuple[str, dict | None]:
    """Read new data from a log file based on byte offset cursor."""
    if not os.path.exists(path):
        raise LogReadError(f"Log file not found: {path}")

    offset = cursor_state.get("offset", 0) if cursor_state else 0
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(offset)
        content = f.read()
        new_offset = f.tell()
    content = _enforce_window(content, max_lines, max_chars)
    return content, {"offset": new_offset}


def read_docker_logs(container_name: str, cursor_state: dict | None, max_lines: int | None, max_chars: int | None) -> Tuple[str, dict | None]:
    """Read docker logs using `docker logs`. Uses --since when available."""
    since = None
    if cursor_state and cursor_state.get("last_read_at"):
        since = cursor_state["last_read_at"]
    else:
        since = (datetime.utcnow() - timedelta(minutes=5)).isoformat()

    try:
        args = [
            "docker",
            "logs",
            "--since",
            since,
            container_name,
        ]
        result = subprocess.check_output(args, text=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as exc:
        raise LogReadError(f"Docker logs failed: {exc.output}") from exc
    except FileNotFoundError as exc:
        raise LogReadError("docker command not available") from exc

    content = _enforce_window(result, max_lines, max_chars)
    return content, {"last_read_at": datetime.utcnow().isoformat()}


def read_logs(mode: str, config: dict, cursor_state: dict | None, window_config: dict | None) -> Tuple[str, dict | None]:
    window_config = window_config or {}
    max_lines = window_config.get("max_lines")
    max_chars = window_config.get("max_chars")

    if mode == "file":
        return read_file_logs(config["path"], cursor_state, max_lines, max_chars)
    if mode == "docker_logs":
        return read_docker_logs(config["container_name"], cursor_state, max_lines, max_chars)

    raise LogReadError(f"Unsupported log source mode: {mode}")
