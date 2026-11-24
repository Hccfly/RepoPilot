"""Central permission policy for every side-effecting capability."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .config import PermissionSettings


class Action(StrEnum):
    READ = "read"
    EDIT = "edit"
    VERIFY = "verify"
    GIT_WRITE = "git_write"
    NETWORK = "network"


class Effect(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    action: Action
    effect: Effect
    reason: str

    @property
    def allowed_without_prompt(self) -> bool:
        return self.effect is Effect.ALLOW


class PermissionPolicy:
    def __init__(self, settings: PermissionSettings | None = None) -> None:
        self._settings = settings or PermissionSettings()

    def decide(self, action: Action, resource: str = "") -> PolicyDecision:
        raw = getattr(self._settings, action.value)
        try:
            effect = Effect(raw)
        except ValueError as exc:
            raise ValueError(f"invalid permission effect for {action.value}: {raw}") from exc
        reason = f"configured {action.value}={effect.value}"
        if action is Action.GIT_WRITE and _is_destructive_git(resource):
            return PolicyDecision(action, Effect.DENY, "destructive Git operations are blocked")
        return PolicyDecision(action, effect, reason)


def _is_destructive_git(resource: str) -> bool:
    normalized = " ".join(resource.lower().split())
    blocked = ("git reset --hard", "git clean", "git push --force", "git checkout --")
    return any(normalized.startswith(command) for command in blocked)
