import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from repopilot.config import SandboxSettings
from repopilot.sandbox import (
    DockerSandboxRunner,
    SandboxError,
    build_verification_runner,
)
from repopilot.verification import VerificationAction

_IMAGE = "python:3.12-slim"
_DOCKER = shutil.which("docker")


def _run_docker(*args: str) -> bool:
    if not _DOCKER:
        return False
    try:
        completed = subprocess.run(
            [_DOCKER, *args], capture_output=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


DOCKER_READY = _run_docker("info", "--format", "{{.ServerVersion}}")
IMAGE_READY = DOCKER_READY and _run_docker("image", "inspect", _IMAGE)


def _action(name: str, *argv: str) -> VerificationAction:
    return VerificationAction(name=name, description=name, project="test", argv=tuple(argv))


def _root() -> Path:
    with tempfile.TemporaryDirectory() as temp:
        return Path(temp).resolve()


class DockerArgvTests(unittest.TestCase):
    def _runner(self, root: Path, **kwargs) -> DockerSandboxRunner:
        return DockerSandboxRunner(root, image=_IMAGE, **kwargs)

    def test_argv_mounts_the_workspace_and_appends_the_action(self) -> None:
        root = _root()
        runner = self._runner(root, network="none", memory="512m", cpus="1.0")
        action = _action("probe", "python", "-c", "print(1)")

        argv = runner.build_argv(action, Path("cid"))

        self.assertTrue(Path(argv[0]).name.lower().startswith("docker"))
        self.assertEqual(argv[1], "run")
        self.assertIn("--rm", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertEqual(argv[argv.index("--memory") + 1], "512m")
        self.assertEqual(argv[argv.index("--cpus") + 1], "1.0")
        self.assertIn(f"{root}:/work", argv)
        self.assertEqual(argv[argv.index("-w") + 1], "/work")
        self.assertEqual(argv[-3:], ["python", "-c", "print(1)"])
        self.assertIn("--cidfile", argv)
        self.assertIn("io.repopilot=1", argv)

    def test_missing_image_is_rejected(self) -> None:
        with self.assertRaises(SandboxError):
            DockerSandboxRunner(_root(), image="")

    def test_option_values_cannot_inject_extra_flags(self) -> None:
        root = _root()
        for field in ("image", "network", "memory", "cpus", "user"):
            with (
                self.subTest(field=field),
                self.assertRaises(SandboxError),
            ):
                DockerSandboxRunner(root, **{"image": _IMAGE, field: "--privileged"})

    def test_relative_workspace_is_rejected(self) -> None:
        with self.assertRaises(SandboxError):
            DockerSandboxRunner(Path("."), image=_IMAGE)

    def test_factory_builds_a_docker_runner(self) -> None:
        runner = build_verification_runner(
            SandboxSettings(mode="docker", image=_IMAGE),
            _root(),
            timeout_seconds=5,
            max_output_chars=100,
        )
        self.assertIsInstance(runner, DockerSandboxRunner)

    def test_factory_requires_an_image(self) -> None:
        with self.assertRaises(SandboxError):
            build_verification_runner(
                SandboxSettings(mode="docker"),
                _root(),
                timeout_seconds=5,
                max_output_chars=100,
            )


@unittest.skipUnless(IMAGE_READY, f"需要本地镜像 {_IMAGE} 与可用的 Docker daemon")
class DockerRunTests(unittest.TestCase):
    def test_runs_the_action_inside_the_mounted_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "hello.txt").write_text("hello", encoding="utf-8")
            runner = DockerSandboxRunner(root, image=_IMAGE, timeout_seconds=120)
            result = runner.run(
                _action(
                    "probe",
                    "python",
                    "-c",
                    "import pathlib;print(pathlib.Path('hello.txt').read_text())",
                )
            )

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertIn("hello", result.stdout)

    def test_exit_code_is_propagated(self) -> None:
        runner = DockerSandboxRunner(_root(), image=_IMAGE, timeout_seconds=120)
        result = runner.run(_action("probe", "python", "-c", "import sys;sys.exit(3)"))
        self.assertEqual(result.exit_code, 3)
        self.assertFalse(result.passed)

    def test_timeout_kills_the_container(self) -> None:
        runner = DockerSandboxRunner(_root(), image=_IMAGE, timeout_seconds=2)
        result = runner.run(_action("probe", "python", "-c", "import time;time.sleep(60)"))

        self.assertTrue(result.timed_out)
        self.assertLess(result.duration_seconds, 30)
        leftover = subprocess.run(
            [_DOCKER, "ps", "-aq", "--filter", "label=io.repopilot=1"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(leftover.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
