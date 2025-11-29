import json
import sqlite3
import threading
from datetime import datetime
from typing import Any, Iterable

from .config import settings


class Storage:
    """Lightweight storage with JSON helpers and optional Postgres connector."""

    def __init__(self, db_path: str | None = None, backend: str | None = None, dsn: str | None = None):
        self.backend = (backend or settings.database_backend).lower()
        self._lock = threading.Lock()
        self._placeholder = "?" if self.backend == "sqlite" else "%s"

        if self.backend == "postgres":
            try:
                import psycopg
                from psycopg.rows import dict_row
            except Exception as exc:  # pragma: no cover - optional dependency
                raise RuntimeError("psycopg is required for postgres backend") from exc

            self.dsn = dsn or settings.database_url
            if not self.dsn:
                raise RuntimeError("DATABASE_URL must be set for postgres backend")

            self._conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)
        else:
            self.db_path = db_path or settings.database_path
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row

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
                    target_id TEXT,
                    log_source_id TEXT,
                    interval_seconds INTEGER NOT NULL,
                    prompt TEXT NOT NULL,
                    inputs TEXT,
                    window_config TEXT,
                    notification_config TEXT,
                    last_run_at TEXT,
                    enabled INTEGER DEFAULT 1,
                    FOREIGN KEY(log_source_id) REFERENCES log_sources(id),
                    FOREIGN KEY(target_id) REFERENCES targets(id)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS monitor_runs (
                    id TEXT PRIMARY KEY,
                    monitor_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT,
                    llm_provider TEXT,
                    llm_provider_metadata TEXT,
                    llm_raw_input TEXT,
                    llm_raw_output TEXT,
                    summary TEXT,
                    details TEXT,
                    error_message TEXT,
                    FOREIGN KEY(monitor_id) REFERENCES monitors(id)
                );
                """
            )
            # Ensure columns exist when upgrading across releases
            self._ensure_column(cur, "targets", "connection_config", "TEXT")
            self._ensure_column(cur, "log_sources", "cursor_state", "TEXT")
            self._ensure_column(cur, "monitors", "inputs", "TEXT")
            self._ensure_column(cur, "monitors", "window_config", "TEXT")
            self._ensure_column(cur, "monitors", "notification_config", "TEXT")
            self._ensure_column(cur, "monitors", "last_run_at", "TEXT")
            self._ensure_column(cur, "monitors", "target_id", "TEXT")
            self._ensure_column(cur, "monitors", "enabled", "INTEGER DEFAULT 1")
            self._ensure_column(cur, "monitor_runs", "llm_provider", "TEXT")
            self._ensure_column(cur, "monitor_runs", "llm_provider_metadata", "TEXT")
            self._ensure_column(cur, "monitor_runs", "finished_at", "TEXT")
            self._ensure_column(cur, "monitor_runs", "status", "TEXT")
            self._ensure_column(cur, "monitor_runs", "llm_raw_input", "TEXT")
            self._ensure_column(cur, "monitor_runs", "llm_raw_output", "TEXT")
            self._ensure_column(cur, "monitor_runs", "summary", "TEXT")
            self._ensure_column(cur, "monitor_runs", "details", "TEXT")
            self._ensure_column(cur, "monitor_runs", "error_message", "TEXT")
            # Add indexes for common lookups
            self._ensure_index(cur, "idx_log_sources_target", "log_sources", ["target_id"])
            self._ensure_index(cur, "idx_monitors_log_source", "monitors", ["log_source_id"])
            self._ensure_index(cur, "idx_monitor_runs_monitor", "monitor_runs", ["monitor_id"])
            self._ensure_index(cur, "idx_monitor_runs_started", "monitor_runs", ["started_at"])
            if self.backend == "sqlite":
                self._conn.commit()

    # region Helpers
    def _execute(self, query: str, params: Iterable[Any]) -> Any:
        query = query.replace("?", self._placeholder)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(query, params)
            if self.backend == "sqlite":
                self._conn.commit()
            return cur

    def _fetchvalue(self, query: str, params: Iterable[Any]) -> Any:
        query = query.replace("?", self._placeholder)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(query, params)
            row = cur.fetchone()
            if not row:
                return None
            if isinstance(row, dict):
                return next(iter(row.values()))
            return row[0]

    def _fetchone(self, query: str, params: Iterable[Any]) -> Any:
        query = query.replace("?", self._placeholder)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(query, params)
            return cur.fetchone()

    def _fetchall(self, query: str, params: Iterable[Any]) -> list[Any]:
        query = query.replace("?", self._placeholder)
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

    def _ensure_column(self, cur: Any, table: str, column: str, definition: str) -> None:
        """Add a column if it does not exist (SQLite and Postgres)."""

        if self.backend == "sqlite":
            cur.execute(f"PRAGMA table_info({table})")
            existing = {row[1] for row in cur.fetchall()}
            if column not in existing:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            return

        cur.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_name = %s AND column_name = %s
            """,
            (table, column),
        )
        if cur.fetchone():
            return
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _ensure_index(self, cur: Any, name: str, table: str, columns: list[str]) -> None:
        """Create an index if missing for both SQLite and Postgres."""

        cols = ", ".join(columns)
        if self.backend == "sqlite":
            cur.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})")
            return

        cur.execute(
            """
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND indexname = %s
            """,
            (name,),
        )
        if cur.fetchone():
            return
        cur.execute(f"CREATE INDEX {name} ON {table} ({cols})")

    def ping(self) -> bool:
        """Simple health check to verify the database connection is usable."""
        try:
            self._fetchone("SELECT 1", ())
            return True
        except Exception:
            return False

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

    def delete_target(self, target_id: str) -> bool:
        dependent_sources = self._fetchvalue(
            "SELECT COUNT(1) FROM log_sources WHERE target_id = ?", (target_id,)
        )
        if dependent_sources:
            raise ValueError("Cannot delete target with existing log sources")

        cur = self._execute("DELETE FROM targets WHERE id = ?", (target_id,))
        return cur.rowcount > 0

    def update_target(self, target_id: str, updates: dict) -> dict | None:
        target = self.get_target(target_id)
        if not target:
            return None
        target.update(updates)
        self._execute(
            "UPDATE targets SET name = ?, type = ?, connection_config = ? WHERE id = ?",
            (
                target["name"],
                target["type"],
                self._to_json(target.get("connection_config")),
                target_id,
            ),
        )
        return target

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

    def delete_log_source(self, source_id: str) -> bool:
        dependent_monitors = self._fetchvalue(
            "SELECT COUNT(1) FROM monitors WHERE log_source_id = ?", (source_id,)
        )
        if dependent_monitors:
            raise ValueError("Cannot delete log source with existing monitors")

        cur = self._execute("DELETE FROM log_sources WHERE id = ?", (source_id,))
        return cur.rowcount > 0

    def update_log_source(self, source_id: str, updates: dict) -> dict | None:
        source = self.get_log_source(source_id)
        if not source:
            return None
        source.update(updates)
        self._execute(
            """
            UPDATE log_sources
            SET target_id = ?, name = ?, mode = ?, config = ?, cursor_state = ?
            WHERE id = ?
            """,
            (
                source["target_id"],
                source["name"],
                source["mode"],
                self._to_json(source.get("config")),
                self._to_json(source.get("cursor_state")),
                source_id,
            ),
        )
        return source

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
                id, name, target_id, log_source_id, interval_seconds, prompt, inputs, window_config,
                notification_config, last_run_at, enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                monitor["id"],
                monitor["name"],
                monitor.get("target_id"),
                monitor.get("log_source_id"),
                monitor["interval_seconds"],
                monitor["prompt"],
                self._to_json(monitor.get("inputs")),
                self._to_json(monitor.get("window_config")),
                self._to_json(monitor.get("notification_config")),
                monitor.get("last_run_at"),
                1 if monitor.get("enabled", True) else 0,
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
            SET name = ?, target_id = ?, log_source_id = ?, interval_seconds = ?, prompt = ?, inputs = ?,
                window_config = ?, notification_config = ?, last_run_at = ?, enabled = ?
            WHERE id = ?
            """,
            (
                monitor["name"],
                monitor.get("target_id"),
                monitor.get("log_source_id"),
                monitor["interval_seconds"],
                monitor["prompt"],
                self._to_json(monitor.get("inputs")),
                self._to_json(monitor.get("window_config")),
                self._to_json(monitor.get("notification_config")),
                monitor.get("last_run_at"),
                1 if monitor.get("enabled", True) else 0,
                monitor_id,
            ),
        )
        return monitor

    def delete_monitor(self, monitor_id: str) -> bool:
        # Remove runs first to avoid orphaned history
        self._execute("DELETE FROM monitor_runs WHERE monitor_id = ?", (monitor_id,))
        cur = self._execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
        return cur.rowcount > 0

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
            "target_id": row["target_id"],
            "log_source_id": row["log_source_id"],
            "interval_seconds": row["interval_seconds"],
            "prompt": row["prompt"],
            "inputs": Storage._from_json(row["inputs"]),
            "window_config": Storage._from_json(row["window_config"]),
            "notification_config": Storage._from_json(row["notification_config"]),
            "last_run_at": row["last_run_at"],
            "enabled": bool(row["enabled"] if row["enabled"] is not None else 1),
        }

    # endregion

    # region Monitor runs
    def create_monitor_run(self, run: dict) -> dict:
        self._execute(
            """
            INSERT INTO monitor_runs (
                id, monitor_id, started_at, finished_at, status, llm_provider,
                llm_provider_metadata, llm_raw_input, llm_raw_output, summary, details, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run["id"],
                run["monitor_id"],
                run["started_at"],
                run.get("finished_at"),
                run.get("status"),
                run.get("llm_provider"),
                run.get("llm_provider_metadata"),
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
            SET finished_at = ?, status = ?, llm_provider = ?, llm_provider_metadata = ?,
                llm_raw_input = ?, llm_raw_output = ?, summary = ?, details = ?, error_message = ?
            WHERE id = ?
            """,
            (
                run.get("finished_at"),
                run.get("status"),
                run.get("llm_provider"),
                run.get("llm_provider_metadata"),
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

    def latest_monitor_run(self, monitor_id: str) -> dict | None:
        row = self._fetchone(
            """
            SELECT * FROM monitor_runs
            WHERE monitor_id = ?
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (monitor_id,),
        )
        return self._row_to_monitor_run(row) if row else None

    def prune_monitor_runs(self, monitor_id: str, keep: int) -> int:
        """Delete older runs beyond the keep threshold for a monitor."""

        if keep <= 0:
            return 0

        cur = self._execute(
            """
            DELETE FROM monitor_runs
            WHERE monitor_id = ? AND id NOT IN (
                SELECT id FROM monitor_runs
                WHERE monitor_id = ?
                ORDER BY started_at DESC
                LIMIT ?
            )
            """,
            (monitor_id, monitor_id, keep),
        )
        return cur.rowcount or 0

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
            "llm_provider": row["llm_provider"],
            "llm_provider_metadata": Storage._from_json(row["llm_provider_metadata"]),
            "llm_raw_input": row["llm_raw_input"],
            "llm_raw_output": row["llm_raw_output"],
            "summary": row["summary"],
            "details": row["details"],
            "error_message": row["error_message"],
        }

    # endregion


storage = Storage()
