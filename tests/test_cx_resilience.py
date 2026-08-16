"""CX 韌性(Cindy 對照 #2,2026-08-16)。

兩刀:
  1. thread not found 自動復原 —— daemon 被換過人時 turn/start 撞
     thread not found,正解是重掛(discard loaded → thread/resume)再重試
     一次;resume 也找不到才是真的丟了,照舊拋錯翻 404。絕不自動 restart
     daemon(會把桌面 ChatGPT 踢下線 → 分家重演)。
  2. 重試風暴升級 —— app-server 對 provider 連線失敗無上限重試(`error`
     通知),UI 恆忙。超過 30 次/120s 合成終態錯誤收掉這一輪,誠實說
     「連不上已放棄」,不是 300 秒後謊稱「卡住」。
"""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

_TMP = tempfile.mkdtemp(prefix="cx-resilience-test-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("POCKET_REGISTRY_DB",
                      os.path.join(_TMP, "bridge-registry.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

TID = "019f39d3-e347-7203-ba4f-000000000001"


def _client():
    c = bridge.CodexAppServerClient()
    return c


class TestThreadNotFoundAutoRecover(unittest.TestCase):
    def test_turn_start_recovers_once_via_resume(self):
        c = _client()
        c.loaded_threads.add(TID)   # 舊 daemon 時代的殘影
        calls = []

        async def _call(method, params=None, timeout=30.0):
            calls.append(method)
            if method == "turn/start" and calls.count("turn/start") == 1:
                raise bridge.CodexAppServerError(
                    "thread not found: " + TID, code=-32600)
            if method == "thread/resume":
                return {"threadSettings": {}}
            if method == "turn/start":
                return {"turn": {"id": "turn-2"}}
            return {}

        with patch.object(c, "call", side_effect=_call), \
             patch.object(c, "_start_turn_watchdog", lambda tid: None):
            res = asyncio.run(c.start_turn(TID, [{"type": "text", "text": "hi"}]))
        # 順序:turn/start(炸) → thread/resume(重掛) → turn/start(成)
        self.assertEqual(
            [m for m in calls if m in ("turn/start", "thread/resume")],
            ["turn/start", "thread/resume", "turn/start"])
        self.assertEqual((res.get("turn") or {}).get("id"), "turn-2")
        self.assertIn(TID, c.loaded_threads)
        self.assertEqual(c.active_turns.get(TID), "turn-2")

    def test_resume_also_notfound_propagates(self):
        c = _client()
        c.loaded_threads.add(TID)

        async def _call(method, params=None, timeout=30.0):
            raise bridge.CodexAppServerError(
                "thread not found: " + TID, code=-32600)

        with patch.object(c, "call", side_effect=_call):
            with self.assertRaises(bridge.CodexAppServerError):
                asyncio.run(c.start_turn(TID, [{"type": "text", "text": "x"}]))
        # 真的丟了 → loaded 標記必須是踢掉的狀態(下次進來重走 resume)
        self.assertNotIn(TID, c.loaded_threads)

    def test_lock_conflict_still_wins_over_recovery(self):
        # 鎖衝突與 not-found 都走 -32600;鎖必須先判,不能被誤導去 resume 搶鎖。
        c = _client()
        c.loaded_threads.add(TID)

        async def _call(method, params=None, timeout=30.0):
            raise bridge.CodexAppServerError(
                f"thread-store conflict: thread {TID} already has an active "
                f"writer", code=-32600)

        with patch.object(c, "call", side_effect=_call):
            with self.assertRaises(bridge.CodexAppServerError) as ctx:
                asyncio.run(c.start_turn(TID, [{"type": "text", "text": "x"}]))
        self.assertEqual(ctx.exception.code, bridge._CX_THREAD_LOCKED_CODE)


class TestRetryStormEscalation(unittest.TestCase):
    def _storm(self, c, n, msg="connection failed, will retry", extra=None):
        for _ in range(n):
            params = {"threadId": TID, "message": msg}
            params.update(extra or {})
            c._note_retryable_error(params)

    def test_escalates_after_max_count(self):
        async def _run():
            c = _client()
            c.active_turns[TID] = "turn-1"
            c.turn_started_at[TID] = 0.0
            with patch.object(c, "call", AsyncMock(return_value={})):
                self._storm(c, bridge.CODEX_RETRY_ESCALATE_MAX)
                await asyncio.sleep(0.05)   # 讓升級任務跑完
            self.assertNotIn(TID, c.active_turns)
            self.assertIn("已重試", c.thread_errors.get(TID, ""))
            self.assertIn(TID, c.turn_terminal_at)
            # 對話裡要有真話終態,不是無聲
            texts = [x[1] for x in c.thread_events[TID] if x[0] == "text"]
            self.assertTrue(any("放棄" in t for t in texts))
        asyncio.run(_run())

    def test_will_retry_flag_counts_even_without_message_pattern(self):
        async def _run():
            c = _client()
            c.active_turns[TID] = "turn-1"
            with patch.object(c, "call", AsyncMock(return_value={})):
                self._storm(c, bridge.CODEX_RETRY_ESCALATE_MAX,
                            msg="欄位名不像重試", extra={"willRetry": True})
                await asyncio.sleep(0.05)
            self.assertNotIn(TID, c.active_turns)
        asyncio.run(_run())

    def test_non_retryable_error_not_counted(self):
        c = _client()
        c.active_turns[TID] = "turn-1"
        self._storm(c, 100, msg="invalid request: bad param")
        self.assertNotIn(TID, c.retry_escalations)
        self.assertIn(TID, c.active_turns)   # 沒被收掉

    def test_counter_resets_on_new_turn(self):
        c = _client()
        c.active_turns[TID] = "turn-1"
        self._storm(c, bridge.CODEX_RETRY_ESCALATE_MAX - 1)
        self.assertEqual(c.retry_escalations[TID]["count"],
                         bridge.CODEX_RETRY_ESCALATE_MAX - 1)
        with patch.object(c, "_start_turn_watchdog", lambda tid: None):
            c._handle_notification({"method": "turn/started",
                                    "params": {"threadId": TID,
                                               "turn": {"id": "turn-2"}}})
        self.assertNotIn(TID, c.retry_escalations)

    def test_inactive_thread_ignored(self):
        c = _client()
        self._storm(c, 100)
        self.assertNotIn(TID, c.retry_escalations)


if __name__ == "__main__":
    unittest.main(verbosity=2)
