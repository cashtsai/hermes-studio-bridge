"""stalled 錯誤永久黏著 —— 2026-08-16 實機:Cashcamp session 卡過一次之後,
即使後續回合成功跑完並正確回話,app 上的紅色「錯誤」永遠不消。

實測序列:
  1. watchdog 判定 "Codex turn stalled (no provider event)" → thread_errors 寫入
  2. 使用者送新指令 → turn 真的開跑(status=running, activeTurn=True)
     …但 error 仍在
  3. turn 正常完成、codex 也正確回了「studio Codex 待命中」
     …status 卻是 failed,error 仍在

根因:`turn/completed` 刻意不清 stalled(那是對的 —— watchdog 中止時 codex 會補送
一個「正常完成」,不擋就來不及看到卡過),但**沒有任何地方**會在新回合開跑時清掉,
於是永久黏著。清除的權威時機是 start_turn 成功回來。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import asyncio
import unittest

import bridge


def run(coro):
    return asyncio.run(coro)


class FakeCall:
    def __init__(self):
        self.calls = []

    async def __call__(self, method, params, timeout=None):
        self.calls.append(method)
        if method == "turn/start":
            return {"turn": {"id": "turn-new"}}
        return {}


def client():
    c = bridge.CodexAppServerClient()
    c.call = FakeCall()
    c.loaded_threads.add("t1")
    return c


class StallErrorClearingTests(unittest.TestCase):
    STALL = "Codex turn stalled (no provider event)"

    def test_new_turn_clears_a_stalled_error(self):
        """回歸:這是使用者實際卡住的那條路。"""
        c = client()
        c.thread_errors["t1"] = self.STALL

        run(c.start_turn("t1", [{"type": "text", "text": "新指令"}]))

        self.assertNotIn("t1", c.thread_errors,
                         "新回合開跑了,舊的 stalled 錯誤必須清掉 —— "
                         "否則那條 session 永遠顯示紅色錯誤")

    def test_new_turn_clears_any_error_not_just_stalled(self):
        c = client()
        c.thread_errors["t1"] = "Codex turn failed: 什麼別的錯"
        run(c.start_turn("t1", [{"type": "text", "text": "x"}]))
        self.assertNotIn("t1", c.thread_errors)

    def test_failed_start_does_not_clear(self):
        """turn/start 沒成功就不能清 —— 錯誤仍然成立。"""
        c = client()

        async def boom(method, params, timeout=None):
            raise bridge.CodexAppServerError("nope", code=-32603)

        c.call = boom
        c.thread_errors["t1"] = self.STALL

        with self.assertRaises(bridge.CodexAppServerError):
            run(c.start_turn("t1", [{"type": "text", "text": "x"}]))

        self.assertEqual(c.thread_errors.get("t1"), self.STALL,
                         "start 失敗卻把錯誤清掉 = 假裝好了")

    def test_runtime_status_recovers_after_new_turn(self):
        """對外狀態也要跟著回來,不能還是 failed。"""
        c = client()
        c.thread_errors["t1"] = self.STALL
        self.assertEqual(c.runtime_status("t1"), "failed")

        run(c.start_turn("t1", [{"type": "text", "text": "x"}]))
        c.active_turns.pop("t1", None)          # 回合結束

        self.assertNotEqual(c.runtime_status("t1"), "failed",
                            "新回合跑完了還報 failed → app 一直紅著")


if __name__ == "__main__":
    unittest.main()
