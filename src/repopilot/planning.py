"""P0 read-only Explorer -> Planner workflow."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .concurrency import run_ordered
from .config import WorkspaceSettings
from .contracts import ChangePlan, Evidence, PlanStep, RunPhase, RunState
from .llm import ModelClient, ModelMessage
from .prompts import EXPLORER_SYSTEM_PROMPT, PLANNER_SYSTEM_PROMPT, explorer_prompt, planner_prompt
from .tools.base import ToolCall, ToolContext, ToolRegistry
from .workflow import WorkflowEngine


class PlanningError(RuntimeError):
    pass


@dataclass(slots=True)
class ReadOnlyPlanningWorkflow:
    model: ModelClient
    tools: ToolRegistry
    tool_context: ToolContext
    workflow: WorkflowEngine
    settings: WorkspaceSettings

    def run(self, state: RunState) -> RunState:
        if state.phase is not RunPhase.DISCOVER:
            raise PlanningError(f"planning must start at discover, got {state.phase.value}")
        try:
            file_context = self._discover(state)
            self.workflow.transition(state, RunPhase.PLAN)
            state.plan = self._plan(state, file_context)
            self.workflow.transition(state, RunPhase.WAIT_APPROVAL)
        # P0 promises a persisted-visible FAILED state for provider, parsing,
        # and tool failures; the CLI must not lose the run to an uncaught SDK exception.
        except Exception as exc:  # noqa: BLE001
            state.error = f"{type(exc).__name__}: {exc}"
            if state.phase in (RunPhase.DISCOVER, RunPhase.PLAN):
                self.workflow.transition(state, RunPhase.FAILED)
        return state

    def _discover(self, state: RunState) -> list[dict[str, str]]:
        inventory_result = self.tools.execute(
            self.tool_context,
            ToolCall(
                "repository_inventory",
                {"limit": self.settings.max_inventory_files},
            ),
        )
        inventory = list(inventory_result.metadata["files"])
        if not inventory:
            raise PlanningError("仓库中没有可读取文件")
        state.inventory = inventory

        response = self.model.complete(
            [
                ModelMessage("system", EXPLORER_SYSTEM_PROMPT),
                ModelMessage("user", explorer_prompt(state.task.goal, inventory)),
            ],
            json_mode=True,
        )
        explorer = _parse_explorer(response.text)
        state.search_queries = explorer["queries"]
        inventory_set = set(inventory)
        invalid_files = [path for path in explorer["files"] if path not in inventory_set]
        if invalid_files:
            raise PlanningError(f"Explorer 引用了仓库清单外文件: {invalid_files}")

        limits = _split_budget(self.settings.max_search_results, len(state.search_queries))
        search_results = run_ordered(
            lambda job: self.tools.execute(
                self.tool_context,
                ToolCall("search_code", {"query": job[0], "limit": job[1]}),
            ),
            list(zip(state.search_queries, limits)),
            max_workers=self.settings.max_parallel_reads,
        )
        hits: list[dict[str, Any]] = []
        for result in search_results:
            for hit in result.metadata["hits"]:
                if len(hits) >= self.settings.max_search_results:
                    break
                hits.append(hit)
                state.evidence.append(
                    Evidence(
                        kind="search_hit",
                        source=hit["path"],
                        line=hit["line"],
                        summary=hit["excerpt"].strip(),
                    )
                )

        candidates = _unique([*explorer["files"], *(hit["path"] for hit in hits)])[
            : self.settings.max_context_files
        ]
        if not candidates:
            raise PlanningError("Explorer 没有选出可读取文件，检索也没有命中")

        reads = run_ordered(
            lambda path: self.tools.execute(
                self.tool_context,
                ToolCall(
                    "read_file",
                    {"path": path, "max_chars": self.settings.max_context_chars},
                ),
            ),
            candidates,
            max_workers=self.settings.max_parallel_reads,
        )
        contexts: list[dict[str, str]] = []
        for result in reads:
            relative = str(result.metadata["path"])
            state.inspected_files.append(relative)
            state.evidence.append(
                Evidence(
                    kind="file_read",
                    source=relative,
                    summary=(
                        f"已读取 {result.metadata['characters']} 个字符"
                        + ("（内容已截断）" if result.metadata["truncated"] else "")
                    ),
                )
            )
            contexts.append({"path": relative, "content": result.output})
        return contexts

    def _plan(self, state: RunState, file_context: list[dict[str, str]]) -> ChangePlan:
        evidence_payload = [
            {
                "kind": item.kind,
                "source": item.source,
                "line": item.line,
                "summary": item.summary,
            }
            for item in state.evidence
        ]
        response = self.model.complete(
            [
                ModelMessage("system", PLANNER_SYSTEM_PROMPT),
                ModelMessage(
                    "user",
                    planner_prompt(state.task.goal, evidence_payload, file_context),
                ),
            ],
            json_mode=True,
        )
        return _parse_change_plan(response.text, set(state.inventory))


def _parse_explorer(text: str) -> dict[str, Any]:
    payload = _parse_json_object(text, "Explorer")
    queries = _string_list(payload.get("queries"), "Explorer.queries", allow_empty=False)
    files = _string_list(payload.get("files"), "Explorer.files", allow_empty=True)
    return {"queries": _unique(queries)[:5], "files": _unique(files)[:12]}


def _parse_change_plan(text: str, known_files: set[str]) -> ChangePlan:
    payload = _parse_json_object(text, "Planner")
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise PlanningError("Planner.summary 必须是非空字符串")
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanningError("Planner.steps 必须是非空数组")

    steps: list[PlanStep] = []
    for index, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, dict):
            raise PlanningError(f"Planner.steps[{index}] 必须是对象")
        description = raw_step.get("description")
        if not isinstance(description, str) or not description.strip():
            raise PlanningError(f"Planner.steps[{index}].description 必须是非空字符串")
        files = _string_list(
            raw_step.get("files", []), f"Planner.steps[{index}].files", True
        )
        unknown = [path for path in files if path not in known_files]
        if unknown:
            raise PlanningError(f"Planner 引用了仓库清单之外的文件: {unknown}")
        verification = _string_list(
            raw_step.get("verification", []),
            f"Planner.steps[{index}].verification",
            True,
        )
        steps.append(
            PlanStep(
                description=description.strip(),
                files=tuple(_unique(files)),
                verification=tuple(_unique(verification)),
            )
        )
    risks = _string_list(payload.get("risks", []), "Planner.risks", True)
    if not any(step.files for step in steps):
        raise PlanningError("Planner 的计划没有引用任何已读取文件")
    return ChangePlan(summary.strip(), tuple(steps), tuple(_unique(risks)))


def _parse_json_object(text: str, source: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise PlanningError(f"{source} 返回空内容")
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise PlanningError(f"{source} 返回的不是合法 JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise PlanningError(f"{source} 必须返回 JSON 对象")
    return payload


def _string_list(value: Any, field: str, allow_empty: bool) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        requirement = "数组" if allow_empty else "非空数组"
        raise PlanningError(f"{field} 必须是{requirement}")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise PlanningError(f"{field} 只能包含非空字符串")
    return [item.strip().replace("\\", "/") for item in value]


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _split_budget(total: int, parts: int) -> list[int]:
    """Split a result budget so parallel searches stay within the same ceiling."""
    if parts <= 0:
        return []
    total = max(0, total)
    base, extra = divmod(total, parts)
    return [max(1, base + (1 if index < extra else 0)) for index in range(parts)]
