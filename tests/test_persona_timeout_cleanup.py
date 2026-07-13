"""Persona ACP turn cleanup: normal completion, stall timeout, caller cancel."""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="persona-timeout-canon-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class FakeSession:
    def __init__(self, *, stall: bool):
        self.stall = stall
        self.lock = asyncio.Lock()
        self.cancel_calls = 0
        self.reset_calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def is_busy(self):
        return self.lock.locked()

    async def cancel(self):
        self.cancel_calls += 1

    async def reset(self):
        self.reset_calls += 1

    async def prompt_stream(self, _prompt):
        async with self.lock:
            self.started.set()
            if self.stall:
                await self.release.wait()
            else:
                yield ("text", "done")


class FakePool:
    def __init__(self, session):
        self.session = session

    async def get(self, _key, _home):
        return self.session


class TestPersonaTimeoutCleanup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_pool = bridge.POOL
        self.old_keepalive = bridge.SSE_KEEPALIVE_SECS
        self.old_stall = bridge.PERSONA_STALL_LIMIT_SECS
        bridge.SSE_KEEPALIVE_SECS = 0.01
        bridge.PERSONA_STALL_LIMIT_SECS = 0.03

    async def asyncTearDown(self):
        bridge.POOL = self.old_pool
        bridge.SSE_KEEPALIVE_SECS = self.old_keepalive
        bridge.PERSONA_STALL_LIMIT_SECS = self.old_stall

    async def test_normal_completion_does_not_reset_acp(self):
        session = FakeSession(stall=False)
        bridge.POOL = FakePool(session)
        chunks = [item async for item in bridge._persona_content_stream("xcash", "hi")]
        self.assertIn(("content", "done"), chunks)
        self.assertEqual(session.cancel_calls, 0)
        self.assertEqual(session.reset_calls, 0)
        self.assertFalse(session.is_busy())

    async def test_stall_cancels_owner_and_resets_acp(self):
        session = FakeSession(stall=True)
        bridge.POOL = FakePool(session)
        chunks = [item async for item in bridge._persona_content_stream("xcash", "hi")]
        self.assertTrue(any("回合逾時" in (value or "") for kind, value in chunks
                            if kind == "content"))
        self.assertEqual(session.cancel_calls, 1)
        self.assertEqual(session.reset_calls, 1)
        self.assertFalse(session.is_busy())

    async def test_caller_cancel_releases_lock_without_forced_reset(self):
        session = FakeSession(stall=True)
        bridge.POOL = FakePool(session)

        async def consume():
            async for _ in bridge._persona_content_stream("xcash", "hi"):
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(session.started.wait(), timeout=1.0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(session.cancel_calls, 1)
        self.assertEqual(session.reset_calls, 0)
        self.assertFalse(session.is_busy())


class TestPersonaTurnStatus(unittest.TestCase):
    @patch.object(bridge, "_canon_reply_for_client",
                  return_value="\n\n⚠️ 回合逾時(伺服器端 5 分鐘無回應),已中止。")
    def test_canonical_timeout_is_terminal_failure(self, _reply):
        status = bridge._app_turn_status("xcash", "client-timeout", acp_busy=False)
        self.assertEqual(status["state"], "timeout")
        self.assertEqual(status["label"], "回合逾時")
        self.assertTrue(status["canonical_reply"])
        self.assertEqual(status["error"], "persona turn timed out")

    @patch.object(bridge, "_canon_reply_for_client", return_value="完成了")
    def test_normal_canonical_reply_stays_done(self, _reply):
        key = ("xcash", "client-ok")
        stale = {"state": {"runner_error": "stream detached"},
                 "task": None, "ts": None}
        with patch.dict(bridge._APP_TURN_INFLIGHT, {key: stale}, clear=False):
            status = bridge._app_turn_status("xcash", "client-ok", acp_busy=False)
        self.assertEqual(status["state"], "done")
        self.assertIsNone(status["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
