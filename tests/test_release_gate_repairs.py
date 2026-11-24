"""Behavioral coverage for the failed release gates, without live providers."""
import _isolation  # noqa: F401
import asyncio
import contextlib
import io
import os
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
import bridge


class MediaDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        bridge._EXC_LOG_STATE.clear()

    def test_unknown_persona_never_uses_another_personas_outbox(self):
        with patch.object(bridge, "home_for") as home:
            self.assertEqual(bridge._media_persona_outbox("missing-persona"), "")
            self.assertEqual(bridge._media_persona_outbox(""), "")
            home.assert_not_called()

    def test_known_persona_uses_only_outbox(self):
        with patch.dict(bridge.PERSONAS, {"example": {}}), \
                patch.object(bridge, "home_for", return_value="/tmp/example-home"):
            self.assertEqual(bridge._media_session_cwd("hermes:example"),
                             "/tmp/example-home/outbox")

    def test_outbox_failure_logs_type_without_private_path(self):
        output = io.StringIO()
        with patch.dict(bridge.PERSONAS, {"example": {}}), \
                patch.object(bridge, "home_for", side_effect=OSError("private-secret-path")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(bridge._media_persona_outbox("example"), "")
        self.assertIn("media_persona_outbox", output.getvalue())
        self.assertIn("OSError", output.getvalue())
        self.assertNotIn("private-secret-path", output.getvalue())

    def test_rejection_diagnostic_failure_is_observable_and_nonfatal(self):
        output = io.StringIO()
        with patch.object(bridge.media_artifacts, "vet_attachable_path", return_value=None), \
                patch.object(bridge.media_artifacts, "local_path_of",
                             side_effect=ValueError("private-secret-path")), \
                contextlib.redirect_stdout(output):
            self.assertIsNone(bridge._media_vet_logged("private-secret-path", [], "example"))
        self.assertIn("media_ref_diagnostic", output.getvalue())
        self.assertNotIn("private-secret-path", output.getvalue())


class CaptureCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_kills_and_reaps_real_child(self):
        real_spawn = asyncio.create_subprocess_exec
        children = []
        async def spawn(*args, **kwargs):
            child = await real_spawn(*args, **kwargs)
            children.append(child)
            return child
        with patch.object(bridge.asyncio, "create_subprocess_exec", side_effect=spawn):
            result = await bridge._capture_stdout(
                sys.executable, "-c", "import time; time.sleep(60)", timeout=.1)
        self.assertEqual(result, (124, ""))
        self.assertIsNotNone(children[0].returncode)

    async def test_cancellation_kills_and_reaps_real_child(self):
        real_spawn = asyncio.create_subprocess_exec
        ready = asyncio.Event()
        children = []
        async def spawn(*args, **kwargs):
            child = await real_spawn(*args, **kwargs)
            children.append(child)
            ready.set()
            return child
        with patch.object(bridge.asyncio, "create_subprocess_exec", side_effect=spawn):
            task = asyncio.create_task(bridge._capture_stdout(
                sys.executable, "-c", "import time; time.sleep(60)"))
            await asyncio.wait_for(ready.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(children[0].returncode)

    async def test_exit_race_is_logged_and_reaped(self):
        child = Mock()
        child.communicate = AsyncMock(side_effect=[asyncio.TimeoutError(), (b"", b"")])
        child.kill.side_effect = ProcessLookupError()
        with patch.object(bridge.asyncio, "create_subprocess_exec", AsyncMock(return_value=child)), \
                patch.object(bridge, "_log_event") as log:
            self.assertEqual(await bridge._capture_stdout("example"), (124, ""))
        self.assertEqual(child.communicate.await_count, 2)
        log.assert_any_call("capture_process_already_exited")

    async def test_git_uses_the_same_cleanup_path(self):
        with patch.object(bridge, "_capture_stdout", AsyncMock(return_value=(0, "ok"))) as run:
            self.assertEqual(await bridge._git_out("status", timeout=3), (0, "ok"))
        run.assert_awaited_once_with("git", "status", timeout=3)


if __name__ == "__main__":
    unittest.main()
