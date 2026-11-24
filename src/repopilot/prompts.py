"""Prompt placeholders. Keep orchestration rules in code, not hidden in prompts."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

EXPLORER_SYSTEM_PROMPT = """
You are RepoPilot Explorer. Inspect the repository using read-only tools.
Every conclusion must cite a file path and, when available, a line number.
Do not propose edits until the relevant implementation and tests have been inspected.
""".strip()

PLANNER_SYSTEM_PROMPT = """
You are RepoPilot Planner. Produce a minimal, structured change plan from repository evidence.
List affected files, intended behavior, validation commands, and unresolved risks.
Do not claim that any edit or verification has already happened.
""".strip()

REVIEWER_SYSTEM_PROMPT = """
You are RepoPilot Reviewer. Check the proposed diff against the task and collected evidence.
Report correctness risks, missing tests, and unrelated changes. Do not modify files.
""".strip()

EDITOR_SYSTEM_PROMPT = """
You are RepoPilot Editor. Produce one minimal unified diff for the approved plan.
Use repository-relative paths with --- a/path and +++ b/path headers.
Do not include binary patches, renames, shell commands, or Markdown fences.
Existing files may only be edited when their contents were supplied.
""".strip()

REPAIR_SYSTEM_PROMPT = """
You are RepoPilot Repair. A verification command failed after an approved patch.
Produce one minimal unified diff that fixes only the reported failure.
Only touch files that appear in the supplied file contents.
Never weaken or delete tests to make them pass.
""".strip()


def explorer_prompt(goal: str, inventory: Sequence[str]) -> str:
    inventory_json = json.dumps(list(inventory), ensure_ascii=False)
    return f"""
请围绕任务选择代码检索词和最值得读取的候选文件。只能从给出的仓库清单中选择文件。
必须输出 JSON 对象，不要输出 Markdown。格式：
{{"queries":["检索词"],"files":["relative/path.py"],"reason":"选择依据"}}

任务：{goal}
仓库文件清单：{inventory_json}
""".strip()


def planner_prompt(
    goal: str,
    evidence: Sequence[dict[str, Any]],
    file_context: Sequence[dict[str, str]],
) -> str:
    evidence_json = json.dumps(list(evidence), ensure_ascii=False)
    context_json = json.dumps(list(file_context), ensure_ascii=False)
    return f"""
请依据实际仓库证据生成最小修改计划。每个步骤的 files 只能引用已读取文件。
verification 只描述建议执行的验证，不得声称已经执行。必须输出 JSON 对象，不要输出 Markdown。
格式：
{{
  "summary":"计划摘要",
  "steps":[
    {{"description":"修改内容","files":["relative/path.py"],"verification":["pytest ..."]}}
  ],
  "risks":["仍需确认的风险"]
}}

任务：{goal}
检索证据：{evidence_json}
已读取文件内容：{context_json}
""".strip()


def editor_prompt(
    goal: str,
    plan: dict[str, Any],
    file_context: Sequence[dict[str, str]],
) -> str:
    plan_json = json.dumps(plan, ensure_ascii=False)
    context_json = json.dumps(list(file_context), ensure_ascii=False)
    return f"""
请依据任务、已批准计划和实际文件内容生成最小 unified diff。必须输出 JSON 对象，不要输出
Markdown。unified_diff 必须包含标准的 ---、+++ 和 @@ 行。格式：
{{"unified_diff":"--- a/path\\n+++ b/path\\n@@ ...","rationale":"修改理由"}}

任务：{goal}
计划：{plan_json}
已读取文件内容：{context_json}
""".strip()


def reviewer_prompt(
    goal: str,
    plan: dict[str, Any],
    unified_diff: str,
    evidence: Sequence[dict[str, Any]],
    verification: Sequence[dict[str, Any]],
    outline: str,
) -> str:
    plan_json = json.dumps(plan, ensure_ascii=False)
    evidence_json = json.dumps(list(evidence), ensure_ascii=False)
    verification_json = json.dumps(list(verification), ensure_ascii=False)
    return f"""
请只依据下面给出的 diff、证据和验证结果做只读审查，不要假设未列出的内容。
每条 finding 必须给出 file（只能是 diff 涉及的文件）和尽力给出的 line。
必须输出 JSON 对象，不要输出 Markdown。格式：
{{"decision":"approve|request_changes|reject","summary":"结论",
  "findings":[{{"severity":"high|medium|low","file":"a.py","line":1,"message":"问题"}}]}}

任务：{goal}
计划：{plan_json}
diff：
{unified_diff}
证据：{evidence_json}
验证结果：{verification_json}
相关符号：{outline}
""".strip()


def repair_prompt(
    goal: str,
    original_goal: str,
    failures: Sequence[dict[str, Any]],
    file_context: Sequence[dict[str, str]],
) -> str:
    failures_json = json.dumps(list(failures), ensure_ascii=False)
    context_json = json.dumps(list(file_context), ensure_ascii=False)
    return f"""
上一次补丁已经通过审批并应用，但验证失败。请生成只修复该失败的最小 unified diff，
只能修改下面提供的文件。必须输出 JSON 对象，不要输出 Markdown。格式：
{{"unified_diff":"--- a/path\\n+++ b/path\\n@@ ...","rationale":"修复理由"}}

任务：{goal}
原始目标：{original_goal}
失败的验证：{failures_json}
当前文件内容：{context_json}
""".strip()
