"""ACP 韌性三刀(A1 看門狗 / A2 佇列可見 / A3 健康巡檢,2026-08-16)。

對照文件:~/docs/HERMES_OPENCLAW_CONN_HARDENING_20260816.md
全部假 subprocess / 假 reader —— 不 spawn 真 hermes。旗標 ACP_RESILIENCE
每測各自設定(per-call 讀 env,不用 reload 模組)。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import asyncio
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import acp_client  # noqa: E402

_RES_ENV = ("ACP_RESILIENCE", "ACP_TURN_STALL_SECS", "ACP_STALL_DEGRADE_N",
            "ACP_HEALTH_SWEEP_SECS")


def _fake_proc(returncode=None):
    proc = Mock()
    proc.returncode = returncode
    proc.terminate = Mock()
    proc.kill = Mock()
    proc.wait = AsyncMock(return_value=0)
    return proc


def _quiet_session(home="/tmp/no-acp-state"):
    """一條不碰真 subprocess 的 session:啟動/對帳/送線全部假掉。"""
    s = acp_client.ACPSession(home)
    s.session_id = "sid-test"
    s.ensure_started = AsyncMock()
    s._sync_canonical_session = AsyncMock()
    s._send = AsyncMock()
    return s


class _EnvBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _RES_ENV}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        acp_client.LOG = None   # 測試注入的 log 掛鉤不可外漏到下一測


class TestA1TurnWatchdog(_EnvBase):
    async def test_watchdog_fires_on_silent_turn(self):
        """無輸出回合:看門狗 cancel+reset,誠實終態,鎖釋放,計數 +1。"""
        os.environ["ACP_RESILIENCE"] = "1"
        os.environ["ACP_TURN_STALL_SECS"] = "1"
        events = []
        acp_client.configure(log=lambda ev, **f: events.append((ev, f)))
        s = _quiet_session()
        s.proc = _fake_proc(returncode=None)

        items = []
        async for item in s.prompt_stream("hi"):
            items.append(item)

        self.assertEqual(items[-1], ("error", "Hermes 回合卡住,已重置"))
        self.assertFalse(s._lock.locked(), "看門狗開刀後 per-persona 鎖必須釋放")
        self.assertIsNone(s.proc, "reset() 必須把卡住的程序收掉")
        self.assertEqual(len(s._stall_resets), 1)
        self.assertFalse(s.degraded)
        # cancel()(advisory)有先送 session/cancel
        sent = [c.args[0] for c in s._send.await_args_list if c.args]
        self.assertTrue(any(o.get("method") == "session/cancel" for o in sent))
        self.assertIn("acp_turn_stalled", [e for e, _ in events])

    async def test_healthy_turn_unaffected_and_clears_degrade(self):
        """有輸出的回合照常流完,且成功回合把降級狀態整組洗白。"""
        os.environ["ACP_RESILIENCE"] = "1"
        os.environ["ACP_TURN_STALL_SECS"] = "1"
        s = _quiet_session()
        now = time.time()
        s._stall_resets = [now, now]
        s._sweep_resets = [now]
        s.degraded = True
        s._sweep_cooldown = True

        async def fake_attempt(text):
            yield ("text", "哈")
            yield ("text", "囉")
        s._attempt = fake_attempt

        items = [i async for i in s.prompt_stream("hi")]
        self.assertIn(("text", "哈"), items)
        self.assertIn(("text", "囉"), items)
        self.assertNotIn("error", [k for k, _ in items])
        self.assertFalse(s._lock.locked())
        self.assertTrue(s._proved_alive)
        self.assertEqual(s._stall_resets, [])
        self.assertEqual(s._sweep_resets, [])
        self.assertFalse(s.degraded)
        self.assertFalse(s._sweep_cooldown)

    async def test_degrade_counter_threshold(self):
        """30 分鐘窗內連續 stall >= N → degraded(N 可由 env 調)。"""
        os.environ["ACP_STALL_DEGRADE_N"] = "2"
        s = acp_client.ACPSession("/tmp/no-acp-state")
        s._note_stall_reset()
        self.assertFalse(s.degraded)
        s._note_stall_reset()
        self.assertTrue(s.degraded)
        # 窗外的舊 stall 不算數
        s2 = acp_client.ACPSession("/tmp/no-acp-state")
        s2._stall_resets = [time.time() - 3600]     # 一小時前,窗外
        s2._note_stall_reset()
        self.assertEqual(len(s2._stall_resets), 1)
        self.assertFalse(s2.degraded)

    async def test_flag_off_no_watchdog(self):
        """旗標關:看門狗完全不啟動(零行為差異)。"""
        os.environ.pop("ACP_RESILIENCE", None)
        s = _quiet_session()
        s._watch_turn = AsyncMock()

        async def fake_attempt(text):
            yield ("text", "ok")
        s._attempt = fake_attempt

        _ = [i async for i in s.prompt_stream("hi")]
        s._watch_turn.assert_not_called()

        os.environ["ACP_RESILIENCE"] = "1"
        _ = [i async for i in s.prompt_stream("hi")]
        s._watch_turn.assert_called_once()


class TestA2QueueVisibility(_EnvBase):
    async def test_queue_depth_and_busy_observable(self):
        """第二個 turn 排隊時 queue_depth=1、is_busy=True;結束歸零,兩輪
        訊息都有跑到(排隊不丟訊息)。"""
        s = _quiet_session()
        gate = asyncio.Event()
        ran = []

        def make_attempt():
            async def fake_attempt(text):
                ran.append(text)
                yield ("text", f"re:{text}")
                await gate.wait()
            return fake_attempt
        s._attempt = make_attempt()

        async def consume(text):
            return [i async for i in s.prompt_stream(text)]

        t1 = asyncio.create_task(consume("first"))
        await asyncio.sleep(0.05)
        self.assertTrue(s.is_busy())
        self.assertEqual(s.queue_depth(), 0)     # 正在跑的不算排隊
        t2 = asyncio.create_task(consume("second"))
        await asyncio.sleep(0.05)
        self.assertEqual(s.queue_depth(), 1)
        gate.set()
        r1, r2 = await asyncio.gather(t1, t2)
        self.assertEqual(s.queue_depth(), 0)
        self.assertFalse(s.is_busy())
        self.assertEqual(ran, ["first", "second"])
        self.assertIn(("text", "re:first"), r1)
        self.assertIn(("text", "re:second"), r2)


class TestA3HealthSweep(_EnvBase):
    async def test_sweep_resets_dead_proc(self):
        """程序死了 → 巡檢先行 reset(不等下一回合踩雷)+ 留痕。"""
        os.environ["ACP_RESILIENCE"] = "1"
        os.environ["ACP_HEALTH_SWEEP_SECS"] = "0.05"
        events = []
        acp_client.configure(log=lambda ev, **f: events.append(ev))
        pool = acp_client.ACPPool()
        s = await pool.get("k", "/tmp/no-acp-state")
        self.assertIsNotNone(pool._sweeper, "首次 get 必須惰啟巡檢")
        s.proc = _fake_proc(returncode=1)
        try:
            await asyncio.sleep(0.2)
            self.assertIsNone(s.proc, "巡檢必須把死程序 reset 掉")
            self.assertEqual(len(s._sweep_resets), 1)
            self.assertIn("acp_proc_died_swept", events)
            self.assertFalse(s.degraded)
        finally:
            pool._sweeper.cancel()

    async def test_sweep_skips_busy_session(self):
        """回合進行中死掉由回合自己收尾(reader fail-fast),巡檢不搶刀。"""
        pool = acp_client.ACPPool()
        s = acp_client.ACPSession("/tmp/no-acp-state")
        s.proc = _fake_proc(returncode=1)
        await s._lock.acquire()
        try:
            await pool._sweep_one("k", s)
            self.assertIsNotNone(s.proc)
            self.assertEqual(s._sweep_resets, [])
        finally:
            s._lock.release()

    async def test_crash_loop_cooldown(self):
        """10 分鐘內第 3 次巡檢收屍 → degraded + 停止巡檢重啟;下個使用者
        回合解除冷卻。"""
        os.environ["ACP_RESILIENCE"] = "1"
        events = []
        acp_client.configure(log=lambda ev, **f: events.append(ev))
        pool = acp_client.ACPPool()
        s = acp_client.ACPSession("/tmp/no-acp-state")
        now = time.time()
        s._sweep_resets = [now - 5, now - 3]
        s.proc = _fake_proc(returncode=9)
        await pool._sweep_one("k", s)
        self.assertIsNone(s.proc)
        self.assertTrue(s.degraded)
        self.assertTrue(s._sweep_cooldown)
        self.assertIn("acp_crash_loop", events)
        # 冷卻中:再死一次也不動刀
        s.proc = _fake_proc(returncode=9)
        await pool._sweep_one("k", s)
        self.assertIsNotNone(s.proc, "冷卻期間巡檢必須停手")
        # 下個使用者回合(prompt_stream 開頭)解除冷卻
        s.proc = None
        s.ensure_started = AsyncMock()
        s._sync_canonical_session = AsyncMock()

        async def fake_attempt(text):
            yield ("text", "回來了")
        s._attempt = fake_attempt
        _ = [i async for i in s.prompt_stream("hi")]
        self.assertFalse(s._sweep_cooldown)
        self.assertFalse(s.degraded, "成功回合要把降級洗白")

    async def test_flag_off_no_sweeper(self):
        os.environ.pop("ACP_RESILIENCE", None)
        pool = acp_client.ACPPool()
        await pool.get("k", "/tmp/no-acp-state")
        self.assertIsNone(pool._sweeper)


if __name__ == "__main__":
    unittest.main(verbosity=2)
