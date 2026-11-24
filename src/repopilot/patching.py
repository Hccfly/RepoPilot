"""P1 unified-diff validation, conflict detection, application, and exact undo."""

from __future__ import annotations

import base64
import difflib
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .git import GitClient, GitError
from .workspace import RepositoryWorkspace


class PatchError(RuntimeError):
    pass


class PatchConflict(PatchError):
    pass


class PatchPolicyError(PatchError):
    pass


class CommitError(PatchError):
    pass


class PatchStatus:
    PROPOSED = "proposed"
    APPLIED = "applied"
    UNDONE = "undone"
    COMMITTED = "committed"


@dataclass(frozen=True, slots=True)
class PatchProposal:
    unified_diff: str
    touched_files: tuple[str, ...]
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "unified_diff": self.unified_diff,
            "touched_files": list(self.touched_files),
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PatchProposal:
        return cls(
            unified_diff=str(value["unified_diff"]),
            touched_files=tuple(str(item) for item in value["touched_files"]),
            rationale=str(value["rationale"]),
        )


@dataclass(frozen=True, slots=True)
class FileBaseline:
    path: str
    existed: bool
    sha256: str | None
    original_base64: str | None

    @property
    def original_bytes(self) -> bytes | None:
        if self.original_base64 is None:
            return None
        return base64.b64decode(self.original_base64.encode("ascii"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "existed": self.existed,
            "sha256": self.sha256,
            "original_base64": self.original_base64,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> FileBaseline:
        return cls(
            path=str(value["path"]),
            existed=bool(value["existed"]),
            sha256=str(value["sha256"]) if value.get("sha256") is not None else None,
            original_base64=(
                str(value["original_base64"]) if value.get("original_base64") is not None else None
            ),
        )


@dataclass(frozen=True, slots=True)
class PatchReceipt:
    patch_hash: str
    touched_files: tuple[str, ...]
    reverse_diff: str
    after_hashes: dict[str, str | None]
    applied_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_hash": self.patch_hash,
            "touched_files": list(self.touched_files),
            "reverse_diff": self.reverse_diff,
            "after_hashes": self.after_hashes,
            "applied_at": self.applied_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PatchReceipt:
        return cls(
            patch_hash=str(value["patch_hash"]),
            touched_files=tuple(str(item) for item in value["touched_files"]),
            reverse_diff=str(value["reverse_diff"]),
            after_hashes={
                str(path): str(digest) if digest is not None else None
                for path, digest in value["after_hashes"].items()
            },
            applied_at=str(value["applied_at"]),
        )


@dataclass(slots=True)
class PatchRunRecord:
    run_id: str
    repository: str
    goal: str
    state: dict[str, Any]
    proposal: PatchProposal
    baselines: tuple[FileBaseline, ...]
    initial_dirty_paths: tuple[str, ...] = ()
    status: str = PatchStatus.PROPOSED
    receipt: PatchReceipt | None = None
    commit: dict[str, Any] | None = None
    repairs: tuple[PatchProposal, ...] = ()
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "repository": self.repository,
            "goal": self.goal,
            "state": self.state,
            "proposal": self.proposal.to_dict(),
            "baselines": [item.to_dict() for item in self.baselines],
            "initial_dirty_paths": list(self.initial_dirty_paths),
            "status": self.status,
            "receipt": self.receipt.to_dict() if self.receipt else None,
            "commit": self.commit,
            "repairs": [item.to_dict() for item in self.repairs],
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PatchRunRecord:
        receipt = value.get("receipt")
        return cls(
            run_id=str(value["run_id"]),
            repository=str(value["repository"]),
            goal=str(value["goal"]),
            state=dict(value["state"]),
            proposal=PatchProposal.from_dict(value["proposal"]),
            baselines=tuple(FileBaseline.from_dict(item) for item in value["baselines"]),
            initial_dirty_paths=tuple(str(item) for item in value.get("initial_dirty_paths", [])),
            status=str(value["status"]),
            receipt=PatchReceipt.from_dict(receipt) if receipt else None,
            commit=dict(value["commit"]) if value.get("commit") else None,
            repairs=tuple(PatchProposal.from_dict(item) for item in value.get("repairs", [])),
            created_at=str(value["created_at"]),
        )

    @property
    def all_diffs(self) -> tuple[PatchProposal, ...]:
        return (self.proposal, *self.repairs)


class UnifiedDiffPatchEngine:
    def __init__(self, workspace: RepositoryWorkspace, git: GitClient) -> None:
        self.workspace = workspace
        self.git = git

    def prepare(
        self,
        unified_diff: str,
        rationale: str,
        *,
        protected_dirty_paths: Iterable[str] = (),
    ) -> tuple[PatchProposal, tuple[FileBaseline, ...]]:
        touched = parse_touched_files(unified_diff)
        dirty = set(self.git.dirty_paths())
        dirty.update(path.replace("\\", "/") for path in protected_dirty_paths)
        baselines: list[FileBaseline] = []
        for relative in touched:
            target = self.workspace.validate_write(relative)
            if relative in dirty:
                raise PatchPolicyError(f"补丁触及任务开始前已有改动的文件: {relative}")
            baselines.append(_capture_baseline(relative, target))
        proposal = PatchProposal(
            unified_diff.rstrip("\r\n") + "\n",
            touched_files=touched,
            rationale=rationale.strip(),
        )
        try:
            self.git.check_patch(proposal.unified_diff)
        except GitError as exc:
            raise PatchConflict(str(exc)) from exc
        return proposal, tuple(baselines)

    def apply(self, record: PatchRunRecord) -> PatchReceipt:
        if record.status != PatchStatus.PROPOSED:
            raise PatchError(f"run {record.run_id} is not awaiting apply")
        self._assert_baselines_unchanged(record.baselines)
        try:
            self.git.check_patch(record.proposal.unified_diff)
            self.git.apply_patch(record.proposal.unified_diff)
        except GitError as exc:
            raise PatchConflict(str(exc)) from exc

        after_hashes = {
            baseline.path: _current_hash(self.workspace.root / baseline.path)
            for baseline in record.baselines
        }
        receipt = PatchReceipt(
            patch_hash=hashlib.sha256(record.proposal.unified_diff.encode("utf-8")).hexdigest(),
            touched_files=record.proposal.touched_files,
            reverse_diff=_build_reverse_diff(self.workspace.root, record.baselines),
            after_hashes=after_hashes,
        )
        record.receipt = receipt
        record.status = PatchStatus.APPLIED
        record.state["approval"] = "approved"
        record.state["phase"] = "verify"
        record.state["changed_files"] = list(record.proposal.touched_files)
        return receipt

    def prepare_repair(
        self,
        unified_diff: str,
        rationale: str,
        *,
        allowed_files: Iterable[str],
    ) -> tuple[PatchProposal, tuple[FileBaseline, ...]]:
        """P3 repairs may only touch files the approved proposal already covers."""
        touched = parse_touched_files(unified_diff)
        allowed = {path.replace("\\", "/") for path in allowed_files}
        outside = [path for path in touched if path not in allowed]
        if outside:
            raise PatchPolicyError(f"修复补丁越出已批准范围: {outside}")
        baselines = tuple(
            _capture_baseline(relative, self.workspace.validate_write(relative))
            for relative in touched
        )
        proposal = PatchProposal(unified_diff.rstrip("\r\n") + "\n", touched, rationale.strip())
        try:
            self.git.check_patch(proposal.unified_diff)
        except GitError as exc:
            raise PatchConflict(str(exc)) from exc
        return proposal, baselines

    def apply_repair(
        self,
        record: PatchRunRecord,
        proposal: PatchProposal,
        baselines: tuple[FileBaseline, ...],
    ) -> PatchReceipt:
        if record.status != PatchStatus.APPLIED or record.receipt is None:
            raise PatchError(f"run {record.run_id} has no applied patch to repair")
        self._assert_baselines_unchanged(baselines)
        try:
            self.git.check_patch(proposal.unified_diff)
            self.git.apply_patch(proposal.unified_diff)
        except GitError as exc:
            raise PatchConflict(str(exc)) from exc

        receipt = PatchReceipt(
            patch_hash=record.receipt.patch_hash,
            touched_files=record.proposal.touched_files,
            # Undo still restores the pre-run bytes, so the reverse diff covers all repairs.
            reverse_diff=_build_reverse_diff(self.workspace.root, record.baselines),
            after_hashes={
                baseline.path: _current_hash(self.workspace.root / baseline.path)
                for baseline in record.baselines
            },
        )
        record.receipt = receipt
        record.repairs = (*record.repairs, proposal)
        return receipt

    def undo(self, record: PatchRunRecord) -> None:
        if record.status == PatchStatus.COMMITTED:
            raise CommitError("该运行已经提交，撤销会留下未提交改动；请自行使用 git 处理")
        if record.status != PatchStatus.APPLIED or record.receipt is None:
            raise PatchError(f"run {record.run_id} has no applied patch to undo")
        for baseline in record.baselines:
            current = _current_hash(self.workspace.root / baseline.path)
            expected = record.receipt.after_hashes[baseline.path]
            if current != expected:
                raise PatchConflict(f"应用补丁后文件又发生变化，拒绝覆盖: {baseline.path}")
        self._restore(record.baselines)
        record.status = PatchStatus.UNDONE
        record.state["phase"] = "cancelled"
        record.state["changed_files"] = []

    def verify_applied_state(self, record: PatchRunRecord) -> None:
        """Refuse to continue when the workspace no longer matches the applied receipt."""
        if record.receipt is None:
            raise CommitError("缺少补丁回执")
        for path, expected in record.receipt.after_hashes.items():
            current = _current_hash(self.workspace.root / path)
            if current != expected:
                raise PatchConflict(f"应用补丁后文件又发生变化，拒绝继续: {path}")

    def _assert_baselines_unchanged(self, baselines: tuple[FileBaseline, ...]) -> None:
        for baseline in baselines:
            current = _current_hash(self.workspace.root / baseline.path)
            if current != baseline.sha256:
                raise PatchConflict(f"提案生成后文件发生变化，拒绝应用: {baseline.path}")

    def _restore(self, baselines: tuple[FileBaseline, ...]) -> None:
        current_backups = {
            item.path: (
                (self.workspace.root / item.path).read_bytes()
                if (self.workspace.root / item.path).is_file()
                else None
            )
            for item in baselines
        }
        restored: list[str] = []
        try:
            for baseline in baselines:
                target = self.workspace.validate_write(baseline.path)
                original = baseline.original_bytes
                if original is None:
                    if target.exists():
                        target.unlink()
                else:
                    _atomic_write(target, original)
                restored.append(baseline.path)
        except OSError as exc:
            for relative in reversed(restored):
                target = self.workspace.root / relative
                after = current_backups[relative]
                if after is None:
                    if target.exists():
                        target.unlink()
                else:
                    _atomic_write(target, after)
            raise PatchError(f"撤销失败并已回滚撤销操作: {exc}") from exc


def parse_touched_files(unified_diff: str) -> tuple[str, ...]:
    if not unified_diff.strip():
        raise PatchError("补丁不能为空")
    forbidden = ("GIT binary patch", "Binary files ", "rename from ", "rename to ")
    if any(marker in unified_diff for marker in forbidden):
        raise PatchPolicyError("P1 不支持 binary patch 或 rename")

    lines = unified_diff.splitlines()
    touched: list[str] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- "):
            index += 1
            continue
        if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
            raise PatchError("unified diff 缺少 +++ 文件头")
        old_path = _parse_header_path(lines[index][4:])
        new_path = _parse_header_path(lines[index + 1][4:])
        if old_path == "/dev/null" and new_path == "/dev/null":
            raise PatchError("补丁文件头不能同时为 /dev/null")
        old_relative = None if old_path == "/dev/null" else _strip_git_prefix(old_path)
        new_relative = None if new_path == "/dev/null" else _strip_git_prefix(new_path)
        if old_relative is not None and new_relative is not None and old_relative != new_relative:
            raise PatchPolicyError(
                f"P1 不支持跨路径补丁或 rename: {old_relative} -> {new_relative}"
            )
        touched.append(new_relative or old_relative or "")
        index += 2
    if not touched:
        raise PatchError("没有找到 unified diff 文件头")
    if len(touched) != len(set(touched)):
        raise PatchError("同一文件不能在一个补丁中出现多次")
    return tuple(touched)


def _parse_header_path(value: str) -> str:
    path = value.split("\t", 1)[0].strip()
    if not path or path.startswith('"'):
        raise PatchPolicyError("P1 不支持空路径或带引号的 Git 路径")
    return path.replace("\\", "/")


def _strip_git_prefix(path: str) -> str:
    if path.startswith(("a/", "b/")):
        path = path[2:]
    if not path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise PatchPolicyError(f"补丁路径必须是仓库相对路径: {path}")
    parts = Path(path).parts
    if ".." in parts:
        raise PatchPolicyError(f"补丁路径不能包含 ..: {path}")
    return Path(*parts).as_posix()


def _capture_baseline(relative: str, target: Path) -> FileBaseline:
    if not target.exists():
        return FileBaseline(relative, False, None, None)
    content = target.read_bytes()
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchPolicyError(f"P1 只支持 UTF-8 文本文件: {relative}") from exc
    return FileBaseline(
        path=relative,
        existed=True,
        sha256=hashlib.sha256(content).hexdigest(),
        original_base64=base64.b64encode(content).decode("ascii"),
    )


def _current_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise PatchConflict(f"目标不再是普通文件: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_reverse_diff(root: Path, baselines: tuple[FileBaseline, ...]) -> str:
    chunks: list[str] = []
    for baseline in baselines:
        target = root / baseline.path
        after = target.read_bytes() if target.is_file() else None
        before = baseline.original_bytes
        try:
            after_text = after.decode("utf-8") if after is not None else ""
            before_text = before.decode("utf-8") if before is not None else ""
        except UnicodeDecodeError as exc:
            raise PatchPolicyError(f"补丁产生了非 UTF-8 内容: {baseline.path}") from exc
        from_name = f"a/{baseline.path}" if after is not None else "/dev/null"
        to_name = f"b/{baseline.path}" if before is not None else "/dev/null"
        chunks.extend(
            difflib.unified_diff(
                after_text.splitlines(keepends=True),
                before_text.splitlines(keepends=True),
                fromfile=from_name,
                tofile=to_name,
            )
        )
    return "".join(chunks)


def _atomic_write(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.repopilot-tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()
