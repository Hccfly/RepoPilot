"""Persistent request audit log and quota counters for the sandbox service."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .persistence import default_run_store_root

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    tenant TEXT NOT NULL,
    action TEXT NOT NULL,
    ok INTEGER NOT NULL,
    error TEXT NOT NULL,
    exit_code INTEGER,
    duration_seconds REAL NOT NULL,
    remote TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quota (
    tenant TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    used INTEGER NOT NULL,
    PRIMARY KEY (tenant, window_start)
);
"""


@dataclass(frozen=True, slots=True)
class AuditRecord:
    timestamp: str
    tenant: str
    action: str
    ok: bool
    error: str
    exit_code: int | None
    duration_seconds: float
    remote: str


@dataclass(frozen=True, slots=True)
class QuotaDecision:
    allowed: bool
    used: int
    limit: int
    reset_at: str


@dataclass(frozen=True, slots=True)
class QuotaUsage:
    tenant: str
    used: int
    reset_at: str


def default_sandbox_db_path() -> Path:
    override = os.getenv("REPOPILOT_SANDBOX_DB", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return default_run_store_root() / "sandbox.db"


class SandboxLedger:
    """SQLite-backed audit trail plus fixed-window quota counters.

    Rows never contain credentials or command output; only the tenant, action
    name, outcome, exit code, duration and peer address.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser().resolve() if path else default_sandbox_db_path()
        with closing(self._connect()) as connection:
            connection.executescript(_SCHEMA)
            connection.commit()

    def record(self, record: AuditRecord) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO audit"
                " (ts, tenant, action, ok, error, exit_code, duration_seconds, remote)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.timestamp,
                    record.tenant,
                    record.action,
                    1 if record.ok else 0,
                    record.error,
                    record.exit_code,
                    record.duration_seconds,
                    record.remote,
                ),
            )

    def recent(self, limit: int = 50) -> tuple[AuditRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT ts, tenant, action, ok, error, exit_code, duration_seconds, remote"
                " FROM audit ORDER BY id DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return tuple(
            AuditRecord(ts, tenant, action, bool(ok), error, exit_code, duration, remote)
            for ts, tenant, action, ok, error, exit_code, duration, remote in rows
        )

    def reserve(
        self,
        tenant: str,
        limit: int,
        window_seconds: int,
        *,
        now: float | None = None,
    ) -> QuotaDecision:
        """Atomically check and consume one slot in the current fixed window."""
        moment = time.time() if now is None else now
        window = max(1, int(window_seconds))
        start = int(moment // window) * window
        reset_at = datetime.fromtimestamp(start + window, UTC).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT OR IGNORE INTO quota (tenant, window_start, used)"
                    " VALUES (?, ?, 0)",
                    (tenant, start),
                )
                used = connection.execute(
                    "SELECT used FROM quota WHERE tenant = ? AND window_start = ?",
                    (tenant, start),
                ).fetchone()[0]
                if limit > 0 and used >= limit:
                    connection.execute("COMMIT")
                    return QuotaDecision(False, used, limit, reset_at)
                connection.execute(
                    "UPDATE quota SET used = used + 1"
                    " WHERE tenant = ? AND window_start = ?",
                    (tenant, start),
                )
                connection.execute(
                    "DELETE FROM quota WHERE window_start < ?", (start - 24 * window,)
                )
                connection.execute("COMMIT")
                return QuotaDecision(True, used + 1, limit, reset_at)
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def usage(
        self, window_seconds: int, *, now: float | None = None
    ) -> tuple[QuotaUsage, ...]:
        moment = time.time() if now is None else now
        window = max(1, int(window_seconds))
        start = int(moment // window) * window
        reset_at = datetime.fromtimestamp(start + window, UTC).isoformat()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT t.tenant, COALESCE(q.used, 0) FROM ("
                "  SELECT tenant FROM quota WHERE window_start = ?"
                "  UNION SELECT DISTINCT tenant FROM audit"
                ") AS t LEFT JOIN quota AS q"
                "  ON q.tenant = t.tenant AND q.window_start = ?"
                " ORDER BY t.tenant",
                (start, start),
            ).fetchall()
        return tuple(QuotaUsage(tenant, used, reset_at) for tenant, used in rows)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
