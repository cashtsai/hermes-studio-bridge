"""The maintenance subprocess boundary must remain bounded and observable."""
import _isolation  # noqa: F401
import contextlib
import io
import signal
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

import process_runner


class ProcessRunnerTests(unittest.TestCase):
    def test_timeout_is_mandatory_positive_and_finite(self):
        with self.assertRaises(TypeError):
            process_runner.run(["unused"], operation="test")
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), patch.object(process_runner.subprocess, "Popen") as spawn:
                with self.assertRaises(ValueError):
                    process_runner.run(["unused"], operation="test", timeout=timeout)
                spawn.assert_not_called()

    def test_success_and_failure_return_output_without_logging_content(self):
        for code in (0, 4):
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                result = process_runner.run(
                    [sys.executable, "-c", f"import sys; print('secret-output'); sys.exit({code})", "secret-argument"],
                    operation="test.result", timeout=5)
            self.assertEqual(result.returncode, code)
            self.assertIn("secret-output", result.stdout)
            self.assertNotIn("secret-", output.getvalue())
            self.assertIn('"returncode": ' + str(code), output.getvalue())

    def test_input_is_forwarded_without_logging(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            result = process_runner.run(
                [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
                operation="test.input", timeout=5, input="secret-input")
        self.assertEqual(result.stdout.strip(), "secret-input")
        self.assertNotIn("secret-input", output.getvalue())

    def test_timeout_kills_and_reaps_real_process(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            result = process_runner.run(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                operation="test.timeout", timeout=.1)
        self.assertEqual(result.returncode, 124)
        self.assertIn('"outcome": "timeout"', output.getvalue())

    def test_spawn_failure_is_classified_without_logging_private_path(self):
        output = io.StringIO()
        with patch.object(process_runner.subprocess, "Popen", side_effect=OSError("secret-path")), \
                contextlib.redirect_stderr(output):
            result = process_runner.run(["unused"], operation="test.spawn", timeout=1)
        self.assertEqual(result.returncode, 127)
        self.assertNotIn("secret-path", output.getvalue())
        self.assertIn("spawn_failed", output.getvalue())

    def test_timeout_and_interrupt_kill_the_dedicated_process_group(self):
        for exc in (subprocess.TimeoutExpired("secret-command", 1), KeyboardInterrupt()):
            child = MagicMock(pid=12345)
            child.__enter__.return_value = child
            child.communicate.side_effect = [exc, ("", "")]
            with patch.object(process_runner.subprocess, "Popen", return_value=child) as spawn, \
                    patch.object(process_runner.os, "killpg") as kill, \
                    contextlib.redirect_stderr(io.StringIO()):
                if isinstance(exc, KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        process_runner.run(["unused"], operation="test.cancel", timeout=1)
                else:
                    self.assertEqual(process_runner.run(["unused"], operation="test.timeout", timeout=1).returncode, 124)
                self.assertTrue(spawn.call_args.kwargs["start_new_session"])
                kill.assert_called_once_with(12345, signal.SIGKILL)
                self.assertEqual(child.communicate.call_count, 2)
                child.__exit__.assert_called_once()

    def test_exit_race_does_not_skip_reaping(self):
        child = MagicMock(pid=12345)
        child.__enter__.return_value = child
        child.communicate.side_effect = [subprocess.TimeoutExpired("unused", 1), ("", "")]
        with patch.object(process_runner.subprocess, "Popen", return_value=child), \
                patch.object(process_runner.os, "killpg", side_effect=ProcessLookupError()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(process_runner.run(["unused"], operation="test.race", timeout=1).returncode, 124)
        self.assertEqual(child.communicate.call_count, 2)


if __name__ == "__main__":
    unittest.main()
