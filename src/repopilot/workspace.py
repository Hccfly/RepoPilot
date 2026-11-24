"""Repository discovery and path-boundary enforcement."""

from __future__ import annotations

import locale
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class WorkspaceError(RuntimeError):
    pass


_DUBIOUS_OWNERSHIP = "detected dubious ownership in repository at"
_DUBIOUS_REPOSITORY = re.compile(r"detected dubious ownership in repository at '([^']+)'")


_DENIED_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_ed25519",
    "credentials",
    "credentials.json",
}
_DENIED_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


@dataclass(frozen=True, slots=True)
class RepositoryWorkspace:
    root: Path
    max_file_bytes: int = 1_000_000

    @classmethod
    def discover(cls, start: str | Path, max_file_bytes: int = 1_000_000) -> RepositoryWorkspace:
        start_path = Path(start).expanduser().resolve()
        completed = subprocess.run(
            ["git", "-C", str(start_path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if completed.returncode != 0:
            detail = _decode(completed.stderr).strip()
            if _DUBIOUS_OWNERSHIP in detail:
                raise WorkspaceError(_dubious_ownership_message(detail, start_path))
            raise WorkspaceError(detail or "not a Git repository")
        return cls(
            Path(_decode(completed.stdout).strip()).resolve(),
            max_file_bytes=max_file_bytes,
        )

    def resolve(self, path: str | Path) -> Path:
        candidate = Path(path)
        resolved = (
            (self.root / candidate).resolve()
            if not candidate.is_absolute()
            else candidate.resolve()
        )
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError(f"path escapes repository root: {path}") from exc
        return resolved

    def validate_read(self, path: str | Path) -> Path:
        resolved = self.resolve(path)
        self._validate_not_protected(resolved, path)
        if not resolved.is_file():
            raise WorkspaceError(f"file does not exist: {path}")
        if resolved.stat().st_size > self.max_file_bytes:
            raise WorkspaceError(f"file exceeds {self.max_file_bytes} bytes: {path}")
        return resolved

    def validate_write(self, path: str | Path) -> Path:
        resolved = self.resolve(path)
        self._validate_not_protected(resolved, path)
        if resolved.exists() and not resolved.is_file():
            raise WorkspaceError(f"write target is not a regular file: {path}")
        return resolved

    @staticmethod
    def _validate_not_protected(resolved: Path, original: str | Path) -> None:
        lowered_parts = {part.lower() for part in resolved.parts}
        if lowered_parts & {".git", ".ssh", ".aws"}:
            raise WorkspaceError(f"protected path cannot be accessed: {original}")
        if resolved.name.lower() in _DENIED_NAMES or resolved.suffix.lower() in _DENIED_SUFFIXES:
            raise WorkspaceError(f"secret-like file cannot be accessed: {original}")


def _decode(data: bytes) -> str:
    # Git for Windows mixes UTF-8 repository paths with ANSI-encoded account names.
    lines: list[str] = []
    for raw in data.splitlines():
        try:
            lines.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            lines.append(raw.decode(locale.getencoding(), errors="replace"))
    return "\n".join(lines)


def _dubious_ownership_message(detail: str, start_path: Path) -> str:
    match = _DUBIOUS_REPOSITORY.search(detail)
    repository = (match.group(1) if match else str(start_path)).replace("\\", "/")
    return (
        f"Git 拒绝访问仓库（所有权异常）: {repository}\n"
        "该目录的所有者与当前用户不同，常见于沙箱或其他账户创建的仓库。\n"
        f"如确认该目录可信，请执行: git config --global --add safe.directory '{repository}'\n"
        f"Git 原始输出:\n{detail}"
    )
