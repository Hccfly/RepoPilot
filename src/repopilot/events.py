"""Observation contracts kept independent from workflow and persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class RunEvent:
    run_id: str
    name: str
    data: dict[str, Any] = field(default_factory=dict)
    occurred_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class EventSink(Protocol):
    def emit(self, event: RunEvent) -> None: ...


class NullEventSink:
    def emit(self, event: RunEvent) -> None:
        del event
