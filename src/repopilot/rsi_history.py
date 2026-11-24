"""Persistent history for multi-round RSI attempts.

One JSON object per line so an interrupted run keeps everything written so far.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

STATUS_IMPROVED = "improved"
STATUS_REJECTED = "rejected"
STATUS_DUPLICATE = "duplicate"
STATUS_ERROR = "error"
_STATUSES = frozenset({STATUS_IMPROVED, STATUS_REJECTED, STATUS_DUPLICATE, STATUS_ERROR})


@dataclass(frozen=True, slots=True)
class RoundRecord:
    index: int
    run_id: str
    timestamp: str
    scenario: str
    patch_hash: str
    score: float | None
    best_score: float
    status: str
    reasons: tuple[str, ...]
    patch_path: str = ""

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "scenario": self.scenario,
            "patch_hash": self.patch_hash,
            "score": self.score,
            "best_score": self.best_score,
            "status": self.status,
            "reasons": list(self.reasons),
            "patch_path": self.patch_path,
        }

    @classmethod
    def from_dict(cls, value: dict) -> RoundRecord:
        status = str(value.get("status", STATUS_ERROR))
        if status not in _STATUSES:
            raise ValueError(f"unknown round status: {status}")
        score = value.get("score")
        return cls(
            index=int(value.get("index", 0)),
            run_id=str(value.get("run_id", "")),
            timestamp=str(value.get("timestamp", "")),
            scenario=str(value.get("scenario", "")),
            patch_hash=str(value.get("patch_hash", "")),
            score=float(score) if score is not None else None,
            best_score=float(value.get("best_score", 0.0)),
            status=status,
            reasons=tuple(str(item) for item in value.get("reasons", ())),
            patch_path=str(value.get("patch_path", "")),
        )


class RsiHistory:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def append(self, record: RoundRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

    def rounds(self, limit: int | None = None) -> tuple[RoundRecord, ...]:
        if not self.path.is_file():
            return ()
        records: list[RoundRecord] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append(RoundRecord.from_dict(json.loads(line)))
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
        ordered = tuple(records)
        return ordered[-limit:] if limit else ordered

    def best_score(self, default: float = 0.0) -> float:
        scores = [item.best_score for item in self.rounds()]
        return max(scores) if scores else default

    def patch_hashes(self) -> frozenset[str]:
        return frozenset(item.patch_hash for item in self.rounds() if item.patch_hash)
