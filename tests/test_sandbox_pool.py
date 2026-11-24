import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from repopilot.config import SandboxSettings
from repopilot.sandbox import (
    DockerContainerPool,
    DockerPooledSandboxRunner,
    SandboxError,
    build_verification_runner,
    ensure_image,
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


def _action(code: str) -> VerificationAction:
    return VerificationAction("probe", "probe", "test", ("python", "-c", code))


def _root() -> Path:
    with tempfile.TemporaryDirectory() as temp:
        return Path(temp).resolve()


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout.encode("utf-8")
        self.stderr = stderr.encode("utf-8")


class EnsureImageTests(unittest.TestCase):
    def test_never_policy_does_not_touch_docker(self) -> None:
        with patch("repopilot.sandbox.subprocess.run") as run:
            ensure_image("docker", _IMAGE, policy="never")
        run.assert_not_called()

    def test_missing_policy_pulls_only_when_absent(self) -> None:
        with patch("repopilot.sandbox.subprocess.run") as run:
            run.return_value = _FakeCompleted(0)
            ensure_image("docker", _IMAGE, policy="missing")
        self.assertEqual(run.call_count, 1)
        self.assertIn("inspect", run.call_args.args[0])

        with patch("repopilot.sandbox.subprocess.run") as run:
            run.side_effect = [_FakeCompleted(1), _FakeCompleted(0)]
            ensure_image("docker", _IMAGE, policy="missing")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0][1], "pull")

    def test_failed_pull_is_reported(self) -> None:
        with patch("repopilot.sandbox.subprocess.run") as run:
            run.return_value = _FakeCompleted(1, stderr="no such image")
            with self.assertRaises(SandboxError):
                ensure_image("docker", _IMAGE, policy="always")

    def test_unknown_policy_is_rejected(self) -> None:
        with self.assertRaises(SandboxError):
            ensure_image("docker", _IMAGE, policy="sometimes")


class PoolConfigTests(unittest.TestCase):
    def _pool(self, **kwargs) -> DockerContainerPool:
        return DockerContainerPool(_root(), **{"image": _IMAGE, **kwargs})

    def test_pool_size_must_be_positive(self) -> None:
        with self.assertRaises(SandboxError):
            self._pool(size=0)

    def test_image_is_required(self) -> None:
        with self.assertRaises(SandboxError):
            DockerContainerPool(_root(), image="")

    def test_option_values_cannot_inject_flags(self) -> None:
        for field in ("image", "network", "memory", "cpus", "user"):
            with self.subTest(field=field), self.assertRaises(SandboxError):
                DockerContainerPool(
                    _root(), **{"image": _IMAGE, field: "-v/etc:/etc"}
                )

    def test_created_container_argv_mounts_and_pins_network(self) -> None:
        pool = self._pool(network="none", memory="256m")
        argv = pool.create_argv(Path("cid"))

        self.assertEqual(argv[1], "create")
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertEqual(argv[argv.index("--memory") + 1], "256m")
        self.assertIn(f"{pool.workspace_root}:/work", argv)
        self.assertEqual(argv[argv.index("-w") + 1], "/work")
        self.assertEqual(argv[argv.index("--cidfile") + 1], "cid")
        self.assertEqual(argv[-2:], ["sleep", "infinity"])

    def test_exec_argv_targets_the_container(self) -> None:
        pool = self._pool()
        argv = pool.exec_argv("abc123", _action("print(1)"))
        self.assertEqual(argv[1:3], ["exec", "abc123"])
        self.assertEqual(argv[-3:], ["python", "-c", "print(1)"])


class PooledFactoryTests(unittest.TestCase):
    def _runner(self, **overrides):
        settings = replace(
            SandboxSettings(mode="docker", image=_IMAGE, pool_size=2), **overrides
        )
        return build_verification_runner(
            settings, _root(), timeout_seconds=5, max_output_chars=100
        )

    def test_pool_size_selects_the_pooled_runner(self) -> None:
        self.assertIsInstance(self._runner(), DockerPooledSandboxRunner)

    def test_zero_pool_size_keeps_the_per_action_runner(self) -> None:
        runner = self._runner(pool_size=0)
        self.assertNotIsInstance(runner, DockerPooledSandboxRunner)

    def test_pool_settings_are_forwarded(self) -> None:
        runner = self._runner(pull="always", pool_size=3)
        self.assertEqual(runner.pool_size, 3)
        self.assertEqual(runner.pull, "always")


@unittest.skipUnless(IMAGE_READY, f"需要本地镜像 {_IMAGE} 与可用的 Docker daemon")
class PooledRunTests(unittest.TestCase):
    def _runner(self, *, size: int = 2, timeout: float = 120.0):
        pool = DockerContainerPool(
            _root(),
            image=_IMAGE,
            size=size,
            pull="never",
            timeout_seconds=timeout,
        )
        return DockerPooledSandboxRunner(pool)

    def test_action_runs_inside_a_pooled_container(self) -> None:
        runner = self._runner()
        try:
            result = runner.run(_action("import os;print(os.getcwd())"))
        finally:
            runner.close()
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "/work")

    def test_sequential_actions_reuse_the_pool(self) -> None:
        runner = self._runner(size=1)
        try:
            first = runner.run(_action("print('one')"))
            second = runner.run(_action("print('two')"))
        finally:
            runner.close()
        self.assertIn("one", first.stdout)
        self.assertIn("two", second.stdout)

    def test_each_action_gets_a_fresh_container(self) -> None:
        runner = self._runner(size=1)
        try:
            first = runner.run(
                _action("import pathlib;pathlib.Path('/tmp/rp-marker.txt').write_text('x')")
            )
            second = runner.run(
                _action("import pathlib;print(pathlib.Path('/tmp/rp-marker.txt').exists())")
            )
        finally:
            runner.close()

        self.assertEqual(first.exit_code, 0, first.stderr)
        self.assertEqual(second.stdout.strip(), "False")

    def test_parallel_actions_both_complete(self) -> None:
        runner = self._runner(size=2)
        results: dict[int, object] = {}

        def work(index: int) -> None:
            results[index] = runner.run(_action(f"print('n{index}')"))

        try:
            threads = [threading.Thread(target=work, args=(i,)) for i in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=180)
        finally:
            runner.close()

        self.assertEqual(sorted(item.stdout.strip() for item in results.values()), ["n0", "n1"])

    def test_timeout_is_reported_and_the_container_is_not_reused(self) -> None:
        runner = self._runner(size=1, timeout=2)
        try:
            slow = runner.run(_action("import time;time.sleep(60)"))
            fast = runner.run(_action("print('after-timeout')"))
        finally:
            runner.close()

        self.assertTrue(slow.timed_out)
        self.assertLess(slow.duration_seconds, 30)
        self.assertEqual(fast.exit_code, 0, fast.stderr)
        self.assertIn("after-timeout", fast.stdout)

    def test_close_leaves_no_containers_behind(self) -> None:
        runner = self._runner(size=2)
        runner.run(_action("print('x')"))
        runner.close()
        time.sleep(0.5)

        leftover = subprocess.run(
            [_DOCKER, "ps", "-aq", "--filter", f"label={runner.pool.label}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(leftover.stdout.strip(), "")

    def test_workspace_is_mounted_into_pooled_containers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "marker.txt").write_text("pooled", encoding="utf-8")
            pool = DockerContainerPool(root, image=_IMAGE, size=1, pull="never")
            runner = DockerPooledSandboxRunner(pool)
            try:
                result = runner.run(
                    _action("import pathlib;print(pathlib.Path('marker.txt').read_text())")
                )
            finally:
                runner.close()

        self.assertEqual(result.stdout.strip(), "pooled")


if __name__ == "__main__":
    unittest.main()
