"""Explicit workflow skeleton; replace nodes with LangGraph adapters in P0/P1."""

from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import RunPhase, RunState
from .events import EventSink, NullEventSink, RunEvent


class InvalidTransition(RuntimeError):
    pass


_TRANSITIONS: dict[RunPhase, frozenset[RunPhase]] = {
    RunPhase.CREATED: frozenset({RunPhase.DISCOVER, RunPhase.CANCELLED}),
    RunPhase.DISCOVER: frozenset({RunPhase.PLAN, RunPhase.FAILED, RunPhase.CANCELLED}),
    RunPhase.PLAN: frozenset({RunPhase.WAIT_APPROVAL, RunPhase.FAILED, RunPhase.CANCELLED}),
    RunPhase.WAIT_APPROVAL: frozenset({RunPhase.PATCH, RunPhase.CANCELLED}),
    RunPhase.PATCH: frozenset({RunPhase.VERIFY, RunPhase.FAILED}),
    RunPhase.VERIFY: frozenset({RunPhase.REPAIR, RunPhase.REVIEW, RunPhase.FAILED}),
    RunPhase.REPAIR: frozenset({RunPhase.VERIFY, RunPhase.FAILED}),
    RunPhase.REVIEW: frozenset({RunPhase.DONE, RunPhase.REPAIR, RunPhase.FAILED}),
    RunPhase.DONE: frozenset(),
    RunPhase.FAILED: frozenset(),
    RunPhase.CANCELLED: frozenset(),
}


@dataclass(slots=True)
class WorkflowEngine:
    event_sink: EventSink = field(default_factory=NullEventSink)

    def transition(self, state: RunState, target: RunPhase) -> RunState:
        allowed = _TRANSITIONS[state.phase]
        if target not in allowed:
            raise InvalidTransition(f"cannot transition from {state.phase.value} to {target.value}")
        previous = state.phase
        state.phase = target
        self.event_sink.emit(
            RunEvent(
                run_id=state.run_id,
                name="phase_changed",
                data={"from": previous.value, "to": target.value},
            )
        )
        return state
