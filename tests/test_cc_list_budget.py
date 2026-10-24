"""`/ccsessions` 列表要有時間預算,tmux 卡住時不能掛到 App 逾時。

2026-10-24:macOS 升級後 tmux 伺服器間歇卡住,`_cc_sessions()` 對每個 session 序列地打
好幾次 tmux(每次最長 15s),整支列表 33–92s,App 10s 就放棄,看起來像「CC session 不行」,
其實 session 都活著。更糟的是 `_tmux_alive` 把逾時吞成 False,卡住時的列會被誤標「down」。

釘住的事:
  ‧ 有一條卡住,整體仍在預算內回來,其他條照常
  ‧ 卡住的那條:有上次好資料 → 回它並標 stale;沒有 → status=unknown 佔位,絕不是 down
  ‧ tmux 逾時算出來的假列不進快取(否則「down」會變成長期事實)
  ‧ 慢 task 不被取消、並行呼叫共用同一個 task(不重複打 tmux),跑完後快取補上
  ‧ /health?agents=1 遇 stale 列 → cc=-1(不確定,別重啟)
"""
import _isolation  # noqa: F401

import asyncio
import inspect
import os
import sys
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="cc-list-budget-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class CcListBudget(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(bridge, k) for k in (
            "_tmux_alive", "_tmux_capture_cached", "_cc_conf_rows", "_cc_session_jsonl",
            "_cc_fresh_hook_state", "_cc_pane_title", "_CC_LIST_BUDGET")}
        bridge._CC_ROW_CACHE.clear()
        bridge._CC_ROW_TASKS.clear()
        bridge._cc_conf_rows = lambda: [("fast", "/w/fast", "1"), ("slow", "/w/slow", "1"),
                                        ("off", "/w/off", "0")]
        bridge._cc_fresh_hook_state = lambda name: {"busy": False}

        async def jsonl(name, workdir):
            return None
        bridge._cc_session_jsonl = jsonl

        async def title(name):
            return None
        bridge._cc_pane_title = title

        async def cap(name):
            return ""
        bridge._tmux_capture_cached = cap
        bridge._CC_LIST_BUDGET = 0.5
        self.calls = {"fast": 0, "slow": 0}
        self.slow_delay = 5.0
        self.slow_times_out = False

        async def alive(name):
            self.calls[name] += 1
            if name == "slow":
                await asyncio.sleep(self.slow_delay)
                if self.slow_times_out:
                    bridge._TMUX_TIMED_OUT.set(True)
                    return False        # 吞掉逾時的假值
            return True
        bridge._tmux_alive = alive

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(bridge, k, v)
        bridge._CC_ROW_CACHE.clear()
        bridge._CC_ROW_TASKS.clear()

    def _run(self, coro):
        async def go():
            r = await coro
            await asyncio.sleep(0)
            return r
        return asyncio.run(go())

    def test_stuck_session_does_not_hold_the_list(self):
        t0 = time.monotonic()
        rows = self._run(bridge._cc_sessions())
        self.assertLess(time.monotonic() - t0, 2.0)
        by = {r["name"]: r for r in rows}
        self.assertEqual(set(by), {"fast", "slow"})
        self.assertEqual(by["fast"]["status"], "running")
        self.assertNotIn("stale", by["fast"])
        self.assertEqual(by["slow"]["status"], "unknown")
        self.assertTrue(by["slow"]["stale"])

    def test_stuck_session_returns_last_good_row_marked_stale(self):
        bridge._CC_ROW_CACHE["slow"] = {"name": "slow", "workdir": "/w/slow",
                                        "status": "running", "busy": True}
        rows = self._run(bridge._cc_sessions())
        slow = {r["name"]: r for r in rows}["slow"]
        self.assertEqual(slow["status"], "running")
        self.assertTrue(slow["busy"])
        self.assertTrue(slow["stale"])

    def test_timed_out_row_is_never_reported_down_nor_cached(self):
        self.slow_delay = 0.0
        self.slow_times_out = True
        rows = self._run(bridge._cc_sessions())
        slow = {r["name"]: r for r in rows}["slow"]
        self.assertNotEqual(slow["status"], "down")
        self.assertTrue(slow["stale"])
        self.assertNotIn("slow", bridge._CC_ROW_CACHE)

    def test_slow_task_survives_and_fills_cache_without_duplicate_calls(self):
        self.slow_delay = 1.3

        async def go():
            first = await bridge._cc_sessions()          # 0.5s 預算 → slow 為替身
            second = await bridge._cc_sessions()         # 共用同一個 task
            calls_before_refresh = self.calls["slow"]
            await asyncio.sleep(1.0)                     # 讓 slow 跑完
            third = await bridge._cc_sessions()          # 已完成 → 這次是新一輪重算
            return first, second, third, calls_before_refresh
        first, second, third, calls_before_refresh = asyncio.run(go())
        self.assertTrue({r["name"]: r for r in first}["slow"]["stale"])
        self.assertTrue({r["name"]: r for r in second}["slow"]["stale"])
        self.assertEqual(calls_before_refresh, 1, "slow 不該被重複 spawn")
        s3 = {r["name"]: r for r in third}["slow"]
        self.assertEqual(s3["status"], "running")

    def test_health_gate_treats_stale_as_unknown(self):
        out = self._run(bridge.health(agents=1)) if inspect.iscoroutinefunction(
            getattr(bridge, "health", None)) else None
        if out is None:
            self.skipTest("health 簽章不同,改由整合測試覆蓋")
        self.assertEqual(out["agents_in_flight"]["cc"], -1)


if __name__ == "__main__":
    unittest.main()
