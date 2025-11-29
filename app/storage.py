import json
import sqlite3
import threading
from datetime import datetime
from typing import Any, Iterable

from .config import settings


class Storage:
    """Lightweight SQLite storage with JSON helpers."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or settings.database_path
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._setup()

    def close(self) -> None:
        """Close the SQLite connection when shutting down the service."""
        with self._lock:
            self._conn.close()

    def _setup(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS targets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    connection_config TEXT
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS log_sources (
                    id TEXT PRIMARY KEY,
                    target_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    config TEXT NOT NULL,
                    cursor_state TEXT,
                    FOREIGN KEY(target_id) REFERENCES targets(id)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS monitors (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    log_source_id TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL,
                    prompt TEXT NOT NULL,
                    inputs TEXT,
                    window_config TEXT,
                    notification_config TEXT,
                    last_run_at TEXT,
                    FOREIGN KEY(log_source_id) REFERENCES log_sources(id)
                );
                """
            )
            self._ensure_column(cur, "monitors", "inputs", "TEXT")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS monitor_runs (
                    id TEXT PRIMARY KEY,
                    monitor_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT,
                    llm_raw_input TEXT,
                    llm_raw_output TEXT,
                    summary TEXT,
                    details TEXT,
                    error_message TEXT,
                    FOREIGN KEY(monitor_id) REFERENCES monitors(id)
                );
                """
            )
            self._conn.commit()

    # region Helpers
    def _execute(self, query: str, params: Iterable[Any]) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(query, params)
            self._conn.commit()
            return cur

    def _fetchone(self, query: str, params: Iterable[Any]) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(query, params)
            return cur.fetchone()

    def _fetchall(self, query: str, params: Iterable[Any]) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(query, params)
            return cur.fetchall()

    @staticmethod
    def _to_json(value: Any) -> str:
        return json.dumps(value) if value is not None else None

    @staticmethod
    def _from_json(value: str | None) -> Any:
        return json.loads(value) if value else None

    @staticmethod
    def _now() -> str:
        return datetime.utcnow().isoformat()

    @staticmethod
    def _ensure_column(cur: sqlite3.Cursor, table: str, column: str, definition: str) -> None:
        cur.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in cur.fetchall()}
        if column not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # endregion

    # region Targets
    def create_target(self, target: dict) -> dict:
        self._execute(
            "INSERT INTO targets (id, name, type, connection_config) VALUES (?, ?, ?, ?)",
            (
                target["id"],
                target["name"],
                target["type"],
                self._to_json(target.get("connection_config")),
            ),
        )
        return target

    def list_targets(self) -> list[dict]:
        rows = self._fetchall("SELECT * FROM targets", ())
        return [self._row_to_target(r) for r in rows]

    def get_target(self, target_id: str) -> dict | None:
        row = self._fetchone("SELECT * FROM targets WHERE id = ?", (target_id,))
        return self._row_to_target(row) if row else None

    @staticmethod
    def _row_to_target(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "type": row["type"],
            "connection_config": Storage._from_json(row["connection_config"]),
        }

    # endregion

    # region Log sources
    def create_log_source(self, source: dict) -> dict:
        self._execute(
            """
            INSERT INTO log_sources (id, target_id, name, mode, config, cursor_state)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                source["id"],
                source["target_id"],
                source["name"],
                source["mode"],
                self._to_json(source.get("config")),
                self._to_json(source.get("cursor_state")),
            ),
        )
        return source

    def list_log_sources(self) -> list[dict]:
        rows = self._fetchall("SELECT * FROM log_sources", ())
        return [self._row_to_log_source(r) for r in rows]

    def get_log_source(self, source_id: str) -> dict | None:
        row = self._fetchone("SELECT * FROM log_sources WHERE id = ?", (source_id,))
        return self._row_to_log_source(row) if row else None

    def update_log_source_cursor(self, source_id: str, cursor_state: dict | None) -> None:
        self._execute(
            "UPDATE log_sources SET cursor_state = ? WHERE id = ?",
            (self._to_json(cursor_state), source_id),
        )

    @staticmethod
    def _row_to_log_source(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "target_id": row["target_id"],
            "name": row["name"],
            "mode": row["mode"],
            "config": Storage._from_json(row["config"]),
            "cursor_state": Storage._from_json(row["cursor_state"]),
        }

    # endregion

    # region Monitors
    def create_monitor(self, monitor: dict) -> dict:
        self._execute(
            """
            INSERT INTO monitors (
                id, name, log_source_id, interval_seconds, prompt, inputs, window_config,
                notification_config, last_run_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                monitor["id"],
                monitor["name"],
                monitor["log_source_id"],
                monitor["interval_seconds"],
                monitor["prompt"],
                self._to_json(monitor.get("inputs")),
                self._to_json(monitor.get("window_config")),
                self._to_json(monitor.get("notification_config")),
                monitor.get("last_run_at"),
            ),
        )
        return monitor

    def list_monitors(self) -> list[dict]:
        rows = self._fetchall("SELECT * FROM monitors", ())
        return [self._row_to_monitor(r) for r in rows]

    def get_monitor(self, monitor_id: str) -> dict | None:
        row = self._fetchone("SELECT * FROM monitors WHERE id = ?", (monitor_id,))
        return self._row_to_monitor(row) if row else None

    def update_monitor(self, monitor_id: str, updates: dict) -> dict | None:
        monitor = self.get_monitor(monitor_id)
        if not monitor:
            return None
        monitor.update(updates)
        self._execute(
            """
            UPDATE monitors
            SET name = ?, log_source_id = ?, interval_seconds = ?, prompt = ?, inputs = ?,
                window_config = ?, notification_config = ?, last_run_at = ?
            WHERE id = ?
            """,
            (
                monitor["name"],
                monitor["log_source_id"],
                monitor["interval_seconds"],
                monitor["prompt"],
                self._to_json(monitor.get("inputs")),
                self._to_json(monitor.get("window_config")),
                self._to_json(monitor.get("notification_config")),
                monitor.get("last_run_at"),
                monitor_id,
            ),
        )
        return monitor

    def touch_monitor_last_run(self, monitor_id: str) -> None:
        self._execute(
            "UPDATE monitors SET last_run_at = ? WHERE id = ?",
            (self._now(), monitor_id),
        )

    @staticmethod
    def _row_to_monitor(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "log_source_id": row["log_source_id"],
            "interval_seconds": row["interval_seconds"],
            "prompt": row["prompt"],
            "inputs": Storage._from_json(row["inputs"]),
            "window_config": Storage._from_json(row["window_config"]),
            "notification_config": Storage._from_json(row["notification_config"]),
            "last_run_at": row["last_run_at"],
        }

    # endregion

    # region Monitor runs
    def create_monitor_run(self, run: dict) -> dict:
        self._execute(
            """
            INSERT INTO monitor_runs (
                id, monitor_id, started_at, finished_at, status, llm_raw_input,
                llm_raw_output, summary, details, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run["id"],
                run["monitor_id"],
                run["started_at"],
                run.get("finished_at"),
                run.get("status"),
                run.get("llm_raw_input"),
                run.get("llm_raw_output"),
                run.get("summary"),
                run.get("details"),
                run.get("error_message"),
            ),
        )
        return run

    def update_monitor_run(self, run_id: str, updates: dict) -> dict | None:
        run = self.get_monitor_run(run_id)
        if not run:
            return None
        run.update(updates)
        self._execute(
            """
            UPDATE monitor_runs
            SET finished_at = ?, status = ?, llm_raw_input = ?, llm_raw_output = ?,
                summary = ?, details = ?, error_message = ?
            WHERE id = ?
            """,
            (
                run.get("finished_at"),
                run.get("status"),
                run.get("llm_raw_input"),
                run.get("llm_raw_output"),
                run.get("summary"),
                run.get("details"),
                run.get("error_message"),
                run_id,
            ),
        )
        return run

    def list_monitor_runs(self, monitor_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
        rows = self._fetchall(
            """
            SELECT * FROM monitor_runs
            WHERE monitor_id = ?
            ORDER BY started_at DESC
            LIMIT ? OFFSET ?
            """,
            (monitor_id, limit, offset),
        )
        return [self._row_to_monitor_run(r) for r in rows]

    def get_monitor_run(self, run_id: str) -> dict | None:
        row = self._fetchone("SELECT * FROM monitor_runs WHERE id = ?", (run_id,))
        return self._row_to_monitor_run(row) if row else None

    @staticmethod
    def _row_to_monitor_run(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "monitor_id": row["monitor_id"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "status": row["status"],
            "llm_raw_input": row["llm_raw_input"],
            "llm_raw_output": row["llm_raw_output"],
            "summary": row["summary"],
            "details": row["details"],
            "error_message": row["error_message"],
        }

    # endregion


storage = Storage()
