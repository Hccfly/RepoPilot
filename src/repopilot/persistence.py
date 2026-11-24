"""P1 JSON patch ledger and the P2 SQLite checkpoint store."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .patching import PatchRunRecord

_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
)
"""


class RunStoreError(RuntimeError):
    pass


class PatchRunStore(Protocol):
    def save(self, record: PatchRunRecord) -> None: ...

    def load(self, run_id: str) -> PatchRunRecord: ...

    def list_records(self) -> tuple[PatchRunRecord, ...]: ...


def default_run_store_root() -> Path:
    override = os.getenv("REPOPILOT_RUNS_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data).resolve() / "RepoPilot" / "runs"
    return Path.home().resolve() / ".local" / "share" / "RepoPilot" / "runs"


class JsonPatchRunStore:
    """Atomic, project-external P1 ledger keyed by run_id."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve() if root else default_run_store_root()

    def save(self, record: PatchRunRecord) -> None:
        _validate_run_id(record.run_id)
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path(record.run_id)
        temporary = self.root / f".{record.run_id}.{uuid4().hex}.tmp"
        payload = json.dumps(record.to_dict(), ensure_ascii=False, indent=2)
        try:
            temporary.write_text(payload + "\n", encoding="utf-8")
            temporary.replace(target)
        except OSError as exc:
            raise RunStoreError(f"保存运行台账失败: {exc}") from exc
        finally:
            if temporary.exists():
                temporary.unlink()

    def load(self, run_id: str) -> PatchRunRecord:
        _validate_run_id(run_id)
        path = self._path(run_id)
        if not path.is_file():
            raise RunStoreError(f"找不到运行台账: {run_id}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            record = PatchRunRecord.from_dict(payload)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RunStoreError(f"运行台账损坏: {run_id}") from exc
        if record.run_id != run_id:
            raise RunStoreError(f"运行台账 ID 不匹配: {run_id}")
        return record

    def list_records(self) -> tuple[PatchRunRecord, ...]:
        if not self.root.is_dir():
            return ()
        records: list[PatchRunRecord] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                records.append(self.load(path.stem))
            except RunStoreError:
                continue
        return tuple(records)

    def _path(self, run_id: str) -> Path:
        return self.root / f"{run_id}.json"


def _validate_run_id(run_id: str) -> None:
    if not _RUN_ID.fullmatch(run_id):
        raise RunStoreError("run_id 必须是 32 位小写十六进制字符串")


class SqlitePatchRunStore:
    """P2 checkpoint ledger. One row per run; the payload is the full record."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve() if root else default_run_store_root()
        self.path = self.root / "repopilot.sqlite3"

    def save(self, record: PatchRunRecord) -> None:
        _validate_run_id(record.run_id)
        payload = json.dumps(record.to_dict(), ensure_ascii=False)
        try:
            with closing(self._connect()) as connection:
                connection.execute(
                    "INSERT INTO runs (run_id, repository, status, phase, updated_at, payload)"
                    " VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(run_id) DO UPDATE SET"
                    " repository = excluded.repository, status = excluded.status,"
                    " phase = excluded.phase, updated_at = excluded.updated_at,"
                    " payload = excluded.payload",
                    (
                        record.run_id,
                        record.repository,
                        record.status,
                        str(record.state.get("phase", "")),
                        datetime.now(UTC).isoformat(),
                        payload,
                    ),
                )
                connection.commit()
        except sqlite3.Error as exc:
            raise RunStoreError(f"写入 checkpoint 失败: {exc}") from exc

    def load(self, run_id: str) -> PatchRunRecord:
        _validate_run_id(run_id)
        if not self.path.is_file():
            raise RunStoreError(f"找不到运行台账: {run_id}")
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT payload FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
        except sqlite3.Error as exc:
            raise RunStoreError(f"读取 checkpoint 失败: {exc}") from exc
        if row is None:
            raise RunStoreError(f"找不到运行台账: {run_id}")
        return _record_from_payload(run_id, row[0])

    def list_records(self) -> tuple[PatchRunRecord, ...]:
        if not self.path.is_file():
            return ()
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT run_id, payload FROM runs ORDER BY updated_at DESC"
                ).fetchall()
        except sqlite3.Error as exc:
            raise RunStoreError(f"读取 checkpoint 失败: {exc}") from exc
        records: list[PatchRunRecord] = []
        for run_id, payload in rows:
            try:
                records.append(_record_from_payload(str(run_id), payload))
            except RunStoreError:
                continue
        return tuple(records)

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.execute(_SCHEMA)
        return connection


def _record_from_payload(run_id: str, payload: str) -> PatchRunRecord:
    try:
        record = PatchRunRecord.from_dict(json.loads(payload))
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RunStoreError(f"运行台账损坏: {run_id}") from exc
    if record.run_id != run_id:
        raise RunStoreError(f"运行台账 ID 不匹配: {run_id}")
    return record


def open_patch_store(kind: str, root: str | Path | None = None) -> PatchRunStore:
    if kind == "json":
        return JsonPatchRunStore(root)
    if kind == "sqlite":
        return SqlitePatchRunStore(root)
    raise RunStoreError(f"未知的台账类型: {kind}")
