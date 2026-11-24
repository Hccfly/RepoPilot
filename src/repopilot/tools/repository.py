"""Minimal read-only tools used by P0."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from ..policy import Action
from ..workspace import WorkspaceError
from .base import ToolContext, ToolResult

_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        "coverage",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
    }
)


class ReadFileTool:
    name = "read_file"
    description = "Read a UTF-8 text file inside the repository boundary."
    action = Action.READ

    def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        path = context.workspace.validate_read(str(arguments["path"]))
        content = path.read_text(encoding="utf-8", errors="replace")
        requested_chars = int(arguments.get("max_chars", len(content)))
        max_chars = max(1, min(requested_chars, context.workspace.max_file_bytes))
        truncated = len(content) > max_chars
        content = content[:max_chars]
        relative = path.relative_to(context.workspace.root).as_posix()
        return ToolResult(
            True,
            content,
            {"path": relative, "characters": len(content), "truncated": truncated},
        )


class GitStatusTool:
    name = "git_status"
    description = "Read the current branch and porcelain working-tree status."
    action = Action.READ

    def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        del arguments
        lines = context.git.status_porcelain()
        output = "\n".join(lines) if lines else "working tree clean"
        return ToolResult(
            True,
            output,
            {"branch": context.git.current_branch(), "changed_entries": len(lines)},
        )


class RepositoryInventoryTool:
    name = "repository_inventory"
    description = "List relevant tracked and untracked files while honoring Git ignore rules."
    action = Action.READ

    def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        requested_limit = int(arguments.get("limit", 500))
        limit = max(1, min(requested_limit, 2000))
        entries: list[str] = []
        for relative in context.git.list_files():
            normalized = relative.replace("\\", "/")
            if set(normalized.lower().split("/")) & _EXCLUDED_PARTS:
                continue
            try:
                candidate = context.workspace.validate_read(normalized)
            except WorkspaceError:
                continue
            entries.append(candidate.relative_to(context.workspace.root).as_posix())
            if len(entries) >= limit:
                break
        entries.sort()
        return ToolResult(
            True,
            json.dumps(entries, ensure_ascii=False),
            {"files": entries, "count": len(entries), "truncated": len(entries) >= limit},
        )


class SearchCodeTool:
    name = "search_code"
    description = "Search repository text with ripgrep and return file, line, column, and excerpt."
    action = Action.READ

    def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ValueError("search query must not be empty")
        if len(query) > 200:
            raise ValueError("search query exceeds 200 characters")
        requested_limit = int(arguments.get("limit", 100))
        limit = max(1, min(requested_limit, 500))
        rg = shutil.which("rg")
        if not rg:
            raise WorkspaceError("ripgrep (rg) is required for search_code")

        command = [
            rg,
            "--vimgrep",
            "--no-heading",
            "--sort",
            "path",
            "--fixed-strings",
            "--color",
            "never",
            "--hidden",
            "--max-count",
            str(limit),
        ]
        for excluded in sorted(_EXCLUDED_PARTS):
            command.extend(["--glob", f"!{excluded}/**"])
        for secret_pattern in ("!.env", "!.env.*", "!*.pem", "!*.key", "!*.p12", "!*.pfx"):
            command.extend(["--glob", secret_pattern])
        command.extend(["--", query, "."])
        completed = subprocess.run(
            command,
            cwd=context.workspace.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if completed.returncode not in (0, 1):
            raise WorkspaceError(completed.stderr.strip() or "rg search failed")

        hits: list[dict[str, Any]] = []
        for raw_line in completed.stdout.splitlines():
            parts = raw_line.split(":", 3)
            if len(parts) != 4:
                continue
            raw_path, raw_line_number, raw_column, excerpt = parts
            normalized = raw_path.removeprefix(".\\").removeprefix("./").replace("\\", "/")
            try:
                safe_path = context.workspace.validate_read(normalized)
                line_number = int(raw_line_number)
                column = int(raw_column)
            except (WorkspaceError, ValueError):
                continue
            hits.append(
                {
                    "path": safe_path.relative_to(context.workspace.root).as_posix(),
                    "line": line_number,
                    "column": column,
                    "excerpt": excerpt[:500],
                }
            )
            if len(hits) >= limit:
                break
        return ToolResult(
            True,
            json.dumps(hits, ensure_ascii=False),
            {"query": query, "hits": hits, "count": len(hits), "truncated": len(hits) >= limit},
        )
