import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repopilot.workspace import RepositoryWorkspace, WorkspaceError


def _completed(
    stderr: bytes = b"", returncode: int = 128, stdout: bytes = b""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=("git",), returncode=returncode, stdout=stdout, stderr=stderr
    )


class WorkspaceTests(unittest.TestCase):
    def test_read_is_confined_to_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = root / "main.py"
            source.write_text("print('ok')", encoding="utf-8")
            workspace = RepositoryWorkspace(root)

            self.assertEqual(workspace.validate_read("main.py"), source)

            with self.assertRaises(WorkspaceError):
                workspace.resolve(root.parent / "outside.txt")

    def test_secret_like_files_are_denied(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            secret = root / ".env"
            secret.write_text("TOKEN=secret", encoding="utf-8")
            workspace = RepositoryWorkspace(root)

            with self.assertRaises(WorkspaceError):
                workspace.validate_read(secret)

    def test_git_internal_and_large_files_are_denied(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            git_file = root / ".git" / "config"
            git_file.parent.mkdir()
            git_file.write_text("internal", encoding="utf-8")
            large = root / "large.txt"
            large.write_text("0123456789", encoding="utf-8")
            workspace = RepositoryWorkspace(root, max_file_bytes=5)

            with self.assertRaises(WorkspaceError):
                workspace.validate_read(git_file)
            with self.assertRaises(WorkspaceError):
                workspace.validate_read(large)


class WorkspaceDiscoveryTests(unittest.TestCase):
    def test_dubious_ownership_reports_safe_directory_fix(self) -> None:
        stderr = (
            b"fatal: detected dubious ownership in repository at 'D:/demo/repo'\n"
            b"'D:/demo/repo' is owned by:\n"
            b"\tPC/Sandbox (S-1-5-21-1-1004)\n"
            b"but the current user is:\n"
            b"\tPC/current (S-1-5-21-1-1001)\n"
        )
        with (
            patch("repopilot.workspace.subprocess.run", return_value=_completed(stderr)),
            self.assertRaises(WorkspaceError) as raised,
        ):
            RepositoryWorkspace.discover("D:/demo/repo")

        message = str(raised.exception)
        self.assertIn("git config --global --add safe.directory 'D:/demo/repo'", message)

    def test_non_utf8_account_name_uses_locale_encoding_fallback(self) -> None:
        stderr = b"fatal: cannot use " + "胡楚晨".encode("gbk") + b" as owner\n"
        with (
            patch("repopilot.workspace.locale.getencoding", return_value="gbk"),
            patch("repopilot.workspace.subprocess.run", return_value=_completed(stderr)),
            self.assertRaises(WorkspaceError) as raised,
        ):
            RepositoryWorkspace.discover("demo")

        self.assertIn("胡楚晨", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
