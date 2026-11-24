"""Typed tool boundary with policy enforcement before execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..git import GitClient
from ..policy import Action, Effect, PermissionPolicy
from ..workspace import RepositoryWorkspace


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    output: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolContext:
    workspace: RepositoryWorkspace
    git: GitClient
    policy: PermissionPolicy


class Tool(Protocol):
    name: str
    description: str
    action: Action

    def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult: ...


class ToolDenied(RuntimeError):
    pass


class ApprovalRequired(RuntimeError):
    def __init__(self, call: ToolCall, reason: str) -> None:
        super().__init__(reason)
        self.call = call


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def execute(self, context: ToolContext, call: ToolCall) -> ToolResult:
        try:
            tool = self._tools[call.name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {call.name}") from exc
        resource = str(call.arguments.get("path") or call.arguments.get("command") or "")
        decision = context.policy.decide(tool.action, resource)
        if decision.effect is Effect.DENY:
            raise ToolDenied(decision.reason)
        if decision.effect is Effect.ASK:
            raise ApprovalRequired(call, decision.reason)
        return tool.execute(context, call.arguments)
