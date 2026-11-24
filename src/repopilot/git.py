"""Small Git adapter for read operations and P1's validated patch lifecycle."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GitResult:
    args: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str


class GitClient:
    def __init__(self, root: Path, timeout_seconds: float = 15.0) -> None:
        self.root = root.resolve()
        self.timeout_seconds = timeout_seconds

    def current_branch(self) -> str:
        result = self._run("branch", "--show-current")
        return result.stdout.strip() or "(detached HEAD)"

    def status_porcelain(self) -> tuple[str, ...]:
        result = self._run("status", "--porcelain=v1", "--untracked-files=all")
        return tuple(line for line in result.stdout.splitlines() if line)

    def dirty_paths(self) -> frozenset[str]:
        result = self._run("status", "--porcelain=v1", "-z", "--untracked-files=all")
        records = result.stdout.split("\0")
        paths: set[str] = set()
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            if len(record) < 4:
                raise GitError("unexpected porcelain status record")
            status = record[:2]
            paths.add(record[3:].replace("\\", "/"))
            if "R" in status or "C" in status:
                if index >= len(records) or not records[index]:
                    raise GitError("incomplete rename/copy status record")
                paths.add(records[index].replace("\\", "/"))
                index += 1
        return frozenset(paths)

    def list_files(self) -> tuple[str, ...]:
        """List tracked and untracked, non-ignored files using Git's own rules."""
        result = self._run("ls-files", "-z", "--cached", "--others", "--exclude-standard")
        return tuple(item for item in result.stdout.split("\0") if item)

    def diff(self, *, staged: bool = False) -> str:
        args = ["diff", "--no-ext-diff", "--no-color"]
        if staged:
            args.append("--cached")
        return self._run(*args).stdout

    def check_patch(self, unified_diff: str) -> None:
        self._apply("--check", unified_diff)

    def apply_patch(self, unified_diff: str) -> None:
        self._apply(None, unified_diff)

    def _apply(self, mode: str | None, unified_diff: str) -> None:
        """Apply (or check) a patch, retrying with --recount when counts are wrong."""
        args = ["apply", "--whitespace=nowarn"]
        if mode:
            args.append(mode)
        try:
            self._run(*args, "-", input_text=unified_diff)
        except GitError:
            self._run(*args, "--recount", "-", input_text=unified_diff)

    def head_sha(self) -> str:
        return self._run("rev-parse", "HEAD").stdout.strip()

    def stage_paths(self, paths: Sequence[str]) -> None:
        if not paths:
            raise GitError("没有可提交的路径")
        self._run("add", "--", *paths)

    def commit_paths(self, paths: Sequence[str], message: str) -> str:
        """Commit exactly ``paths``, leaving anything else in the index untouched."""
        if not paths:
            raise GitError("没有可提交的路径")
        if not message.strip():
            raise GitError("提交说明不能为空")
        self._run("commit", "-m", message, "--", *paths)
        return self.head_sha()

    def committed_paths(self, sha: str) -> tuple[str, ...]:
        result = self._run("show", "--pretty=format:", "--name-only", sha)
        return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())

    def staged_paths(self) -> tuple[str, ...]:
        result = self._run("diff", "--cached", "--name-only", "-z")
        return tuple(item for item in result.stdout.split("\0") if item)

    def _run(self, *args: str, input_text: str | None = None) -> GitResult:
        completed = subprocess.run(
            ["git", "-C", str(self.root), *args],
            # Pass patches as bytes. On Windows, text-mode stdin rewrites LF to
            # CRLF and the extra carriage returns make otherwise valid hunks fail.
            input=input_text.encode("utf-8") if input_text is not None else None,
            capture_output=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        result = GitResult(
            tuple(args),
            completed.returncode,
            completed.stdout.decode("utf-8", errors="replace"),
            completed.stderr.decode("utf-8", errors="replace"),
        )
        if result.exit_code != 0:
            raise GitError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result
