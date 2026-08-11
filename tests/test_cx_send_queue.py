"""CX 送出排隊層 —— 2026-08-11 使用者回報「CX 送出一直顯示失敗」的根因回歸。

三個獨立缺陷,各自對應一組測試:
1. start_turn 在**做任何檢查之前**就清空事件緩衝 + 跳 generation → 忙碌中再送
   一則(即使那則失敗)也會把「正在跑那輪」的串流弄壞、畫面變空白。
2. CX 完全沒有排隊層:忙碌時直送 app-server 撞牆 → 409/502 → app 紅字失敗。
   CC 早有「一定收下 + delivery=queued」語意,這裡對齊。
3. codex 的 echo 卡從來沒帶 client_id(bridge 明明有送 clientUserMessageId),
   任何拿 client_id 對位 echo 的客戶端都永遠對不到。
"""
import asyncio
import unittest

import bridge
import carddigest


def run(coro):
    # 不用 get_event_loop():unittest discover 的情境下沒有 current loop,
    # 單獨跑會過、整套跑會 ERROR(2026-08-11 自己踩到)。
    return asyncio.run(coro)


class FakeCall:
    """假的 JSON-RPC:記錄呼叫,可指定 turn/start 失敗。"""

    def __init__(self, fail_start=False):
        self.fail_start = fail_start
        self.calls = []

    async def __call__(self, method, params, timeout=None):
        self.calls.append(method)
        if method == "turn/start":
            if self.fail_start:
                raise bridge.CodexAppServerError("busy", code=-32600)
            return {"turn": {"id": "turn-1"}}
        return {}


def fresh_client(fail_start=False):
    c = bridge.CodexAppServerClient()
    c.call = FakeCall(fail_start=fail_start)
    c.loaded_threads.add("t1")          # 跳過 thread/resume
    return c


class StartTurnEventBufferTests(unittest.TestCase):
    """缺陷 1:失敗的 start_turn 不得破壞正在跑那輪的事件緩衝。"""

    def test_failed_start_turn_keeps_running_turn_events(self):
        c = fresh_client(fail_start=True)
        c.thread_events["t1"].extend([("text", "上一輪的輸出")])
        gen_before = c.thread_event_generations["t1"]

        with self.assertRaises(bridge.CodexAppServerError):
            run(c.start_turn("t1", [{"type": "text", "text": "hi"}]))

        self.assertEqual(len(c.thread_events["t1"]), 1,
                         "start_turn 失敗卻清空了正在跑那輪的事件 → 畫面變空白")
        self.assertEqual(c.thread_event_generations["t1"], gen_before,
                         "start_turn 失敗卻跳了 generation → 串流被判定過期而斷掉")

    def test_successful_start_turn_does_reset_buffer(self):
        c = fresh_client()
        c.thread_events["t1"].extend([("text", "舊的")])
        gen_before = c.thread_event_generations["t1"]

        run(c.start_turn("t1", [{"type": "text", "text": "hi"}]))

        self.assertEqual(c.thread_events["t1"], [], "成功開新回合本來就該清緩衝")
        self.assertEqual(c.thread_event_generations["t1"], gen_before + 1)


class QueueTests(unittest.TestCase):
    """缺陷 2:忙碌時要收下並排隊,turn 結束自動送出。"""

    def test_enqueue_then_drain_sends_in_order(self):
        c = fresh_client()
        c.active_turns["t1"] = "turn-running"          # 正在跑
        self.assertTrue(c.is_active("t1"))

        self.assertEqual(c.enqueue_input("t1", [{"type": "text", "text": "第一則"}]), 1)
        self.assertEqual(c.enqueue_input("t1", [{"type": "text", "text": "第二則"}]), 2)
        self.assertEqual(c.pending_count("t1"), 2)

        c.active_turns.pop("t1")                       # turn 結束
        run(c.drain_pending("t1"))

        self.assertEqual(c.pending_count("t1"), 1, "drain 應一次只送一則(codex 單 writer)")
        self.assertIn("turn/start", c.call.calls)
        self.assertTrue(c.is_active("t1"), "drain 出去的那則應該真的開跑")

    def test_drain_does_nothing_while_still_active(self):
        c = fresh_client()
        c.active_turns["t1"] = "turn-running"
        c.enqueue_input("t1", [{"type": "text", "text": "等一下"}])

        run(c.drain_pending("t1"))

        self.assertEqual(c.pending_count("t1"), 1, "還在跑就不該搶送")
        self.assertNotIn("turn/start", c.call.calls)

    def test_failing_item_is_dropped_not_retried_forever(self):
        """隊首失敗要丟掉並繼續,否則永遠卡在同一則反覆撞牆(=使用者看到的迴圈)。"""
        c = fresh_client(fail_start=True)
        c.enqueue_input("t1", [{"type": "text", "text": "會失敗"}])

        run(c.drain_pending("t1"))

        self.assertEqual(c.pending_count("t1"), 0)


class EchoClientIdTests(unittest.TestCase):
    """缺陷 3:codex 的 user echo 卡要帶回 client_id。"""

    def test_user_message_card_carries_client_id(self):
        cards = carddigest.codex_item_to_cards({
            "id": "item-1",
            "type": "userMessage",
            "clientUserMessageId": "cid-abc",
            "content": [{"type": "text", "text": "哈囉"}],
        })
        self.assertTrue(cards)
        self.assertEqual((cards[0].get("body") or {}).get("client_id"), "cid-abc",
                         "echo 卡沒帶 client_id → 客戶端永遠對不到自己送的那則")

    def test_missing_client_id_is_simply_absent(self):
        cards = carddigest.codex_item_to_cards({
            "id": "item-2",
            "type": "userMessage",
            "content": [{"type": "text", "text": "沒有 cid"}],
        })
        self.assertNotIn("client_id", cards[0].get("body") or {})


class QueuedStatusTests(unittest.TestCase):
    """狀態卡要能表達 queued(app 據此顯示「已排入佇列」而不是失敗)。"""

    def test_status_reports_queued_when_busy_with_pending(self):
        d = carddigest.CodexThreadDigest()
        d.busy = True
        d.queue_depth = 2
        d._status()
        st = d.store.status
        self.assertEqual(st.get("phase"), "queued")
        self.assertEqual(st.get("queue_depth"), 2)
        self.assertIn("排入佇列", st.get("label", ""))

    def test_busy_without_queue_is_plain_run(self):
        d = carddigest.CodexThreadDigest()
        d.busy = True
        d._status()
        self.assertEqual(d.store.status.get("phase"), "run")


if __name__ == "__main__":
    unittest.main()
