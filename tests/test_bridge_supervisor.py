"""Crash-loop guard contract tests."""
import _isolation  # noqa: F401

import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "deploy" / "bridge-supervisor.sh"


class BridgeSupervisorTest(unittest.TestCase):
    def _env(self, directory):
        env = os.environ.copy()
        env.update({
            "POCKET_BRIDGE_CRASH_STATE_DIR": str(directory),
            "POCKET_BRIDGE_CRASH_LOG": str(directory / "supervisor.log"),
            "POCKET_BRIDGE_CRASH_MAX": "3",
            "POCKET_BRIDGE_CRASH_WINDOW_SEC": "60",
            "POCKET_BRIDGE_CRASH_RESTART_SEC": "0",
        })
        return env

    def test_repeated_failures_open_hold_instead_of_spinning(self):
        with tempfile.TemporaryDirectory(prefix="bridge-supervisor-") as raw:
            state = Path(raw)
            proc = subprocess.Popen(
                ["/bin/bash", str(SUPERVISOR), "/bin/sh", "-c", "exit 7"],
                env=self._env(state),
            )
            hold = state / "crash-loop.hold"
            deadline = time.time() + 5
            while time.time() < deadline and not hold.exists():
                time.sleep(0.05)
            self.assertTrue(hold.exists())
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=3)

            self.assertEqual(len((state / "failures").read_text().splitlines()), 3)
            self.assertIn("crash-loop breaker open", (state / "supervisor.log").read_text())

    def test_clean_child_exit_is_not_restarted(self):
        with tempfile.TemporaryDirectory(prefix="bridge-supervisor-") as raw:
            state = Path(raw)
            result = subprocess.run(
                ["/bin/bash", str(SUPERVISOR), "/bin/sh", "-c", "exit 0"],
                env=self._env(state),
                timeout=3,
            )
            self.assertEqual(result.returncode, 0)
            self.assertFalse((state / "crash-loop.hold").exists())

