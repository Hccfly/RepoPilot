"""P3 symbol-aware repository map and change-impact context.

Python files are parsed with ``ast``; JavaScript/TypeScript files fall back to
declaration patterns. Nothing here executes or imports repository code.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .git import GitClient

_MAX_SYMBOLS_PER_FILE = 200
_MAX_NESTING = 3
_SKIP_PARTS = frozenset(
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
_PYTHON_SUFFIXES = frozenset({".py"})
_SCRIPT_SUFFIXES = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})
_CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")


class RepoMapError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Symbol:
    name: str
    kind: str
    path: str
    line: int
    parent: str | None = None

    def render(self) -> str:
        prefix = f"{self.parent}." if self.parent else ""
        return f"{self.kind} {prefix}{self.name} (line {self.line})"


@dataclass(frozen=True, slots=True)
class FileOutline:
    path: str
    symbols: tuple[Symbol, ...]
    imports: tuple[str, ...]

    def defined_names(self) -> frozenset[str]:
        return frozenset(symbol.name for symbol in self.symbols)


@dataclass(slots=True)
class RepoMap:
    root: Path
    git: GitClient
    max_files: int = 400
    max_file_bytes: int = 1_000_000

    def build(self) -> tuple[FileOutline, ...]:
        outlines: list[FileOutline] = []
        for relative in self._candidates():
            text = self._read(relative)
            if text is None:
                continue
            suffix = Path(relative).suffix.lower()
            if suffix in _PYTHON_SUFFIXES:
                outlines.append(_python_outline(relative, text))
            elif suffix in _SCRIPT_SUFFIXES:
                outlines.append(_script_outline(relative, text))
            if len(outlines) >= self.max_files:
                break
        return tuple(outlines)

    def outline(self, paths: Sequence[str] | None = None, *, max_chars: int = 6000) -> str:
        wanted = {str(path).replace("\\", "/") for path in paths} if paths else None
        chunks: list[str] = []
        for entry in self.build():
            if wanted is not None and entry.path not in wanted:
                continue
            header = f"{entry.path}:"
            body = [f"  {symbol.render()}" for symbol in entry.symbols]
            if entry.imports:
                body.append(f"  imports: {', '.join(entry.imports[:8])}")
            chunks.append("\n".join([header, *body]))
        return "\n".join(chunks)[:max_chars]

    def impact(self, changed_files: Iterable[str], *, limit: int = 20) -> tuple[str, ...]:
        """Files that likely depend on the changed files: importers and callers."""
        changed = {str(path).replace("\\", "/") for path in changed_files}
        outlines = self.build()
        modules = set()
        for path in changed:
            without_suffix = path.rsplit(".", 1)[0]
            modules.add(without_suffix.replace("/", "."))
            modules.add(Path(path).stem)

        call_names: set[str] = set()
        for path in changed:
            text = self._read(path)
            if text:
                call_names.update(name for name in _CALL.findall(text) if len(name) > 2)

        impacted: list[str] = []
        for entry in outlines:
            if entry.path in changed:
                continue
            imported = any(
                reference in module or module.endswith(reference)
                for reference in entry.imports
                for module in modules
            )
            if imported or (entry.defined_names() & call_names):
                impacted.append(entry.path)
            if len(impacted) >= limit:
                break
        return tuple(impacted)

    def _candidates(self) -> tuple[str, ...]:
        entries: list[str] = []
        for raw in self.git.list_files():
            normalized = raw.replace("\\", "/").removeprefix("./")
            if set(normalized.lower().split("/")) & _SKIP_PARTS:
                continue
            if Path(normalized).suffix.lower() not in (_PYTHON_SUFFIXES | _SCRIPT_SUFFIXES):
                continue
            entries.append(normalized)
        entries.sort()
        return tuple(entries)

    def _read(self, relative: str) -> str | None:
        target = self.root / relative
        try:
            if not target.is_file() or target.stat().st_size > self.max_file_bytes:
                return None
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None


class _PythonCollector(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.symbols: list[Symbol] = []
        self.imports: list[str] = []
        self._stack: list[str] = []

    def visit_Import(self, node: ast.Import) -> None:
        self.imports.extend(alias.name for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self.imports.append(node.module)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._container(node, "class")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._container(node, "method" if self._stack else "function")

    visit_AsyncFunctionDef = visit_FunctionDef

    def _container(self, node: ast.ClassDef | ast.FunctionDef, kind: str) -> None:
        if len(self._stack) >= _MAX_NESTING or len(self.symbols) >= _MAX_SYMBOLS_PER_FILE:
            return
        parent = self._stack[-1] if self._stack else None
        self.symbols.append(Symbol(node.name, kind, self.path, node.lineno, parent))
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()


def _python_outline(path: str, text: str) -> FileOutline:
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError:
        return FileOutline(path, (), ())
    collector = _PythonCollector(path)
    collector.visit(tree)
    return FileOutline(
        path,
        tuple(collector.symbols[:_MAX_SYMBOLS_PER_FILE]),
        tuple(dict.fromkeys(collector.imports)),
    )


_SCRIPT_PATTERNS = (
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)", re.MULTILINE), "class"),
    (
        re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*(\w+)", re.MULTILINE),
        "function",
    ),
    (
        re.compile(
            r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(",
            re.MULTILINE,
        ),
        "function",
    ),
)
_SCRIPT_IMPORT = re.compile(r"from\s+['\"]([^'\"]+)['\"]")


def _script_outline(path: str, text: str) -> FileOutline:
    symbols: list[Symbol] = []
    seen: set[str] = set()
    for pattern, kind in _SCRIPT_PATTERNS:
        for match in pattern.finditer(text):
            name = match.group(1)
            if name in seen or len(symbols) >= _MAX_SYMBOLS_PER_FILE:
                continue
            seen.add(name)
            symbols.append(
                Symbol(name, kind, path, text.count("\n", 0, match.start()) + 1)
            )
    symbols.sort(key=lambda item: item.line)
    return FileOutline(path, tuple(symbols), tuple(dict.fromkeys(_SCRIPT_IMPORT.findall(text))))
