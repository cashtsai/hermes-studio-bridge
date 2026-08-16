"""CX 送出排隊層 —— 2026-08-11 使用者回報「CX 送出一直顯示失敗」的根因回歸。

三個獨立缺陷,各自對應一組測試:
1. start_turn 在**做任何檢查之前**就清空事件緩衝 + 跳 generation → 忙碌中再送
   一則(即使那則失敗)也會把「正在跑那輪」的串流弄壞、畫面變空白。
2. CX 完全沒有排隊層:忙碌時直送 app-server 撞牆 → 409/502 → app 紅字失敗。
   CC 早有「一定收下 + delivery=queued」語意,這裡對齊。
3. codex 的 echo 卡從來沒帶 client_id(bridge 明明有送 clientUserMessageId),
   任何拿 client_id 對位 echo 的客戶端都永遠對不到。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
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
    """狀態卡要能表達積壓,但 **不可以** 借用 phase="queued" 來表達。

    app 契約(`TerminalCardStore.statusPhase`)裡 queued = 「訊息已收下、session
    還沒接手」,而 `CodexCardSessionView` / `AgentCardSessionView` 都在
    `statusPhase == "queued"` 時**隱藏 WorkingBar**(回合中唯一的停止鍵),
    `noteServerBusy(busy && phase != "queued")` 也會忽略 busy。把「正在跑且有
    積壓」標成 queued → 使用者一排後續訊息,當前回合就再也停不下來。
    """

    def test_running_with_backlog_stays_run(self):
        d = carddigest.CodexThreadDigest()
        d.busy = True
        d.queue_depth = 2
        d._status()
        st = d.store.status
        self.assertEqual(st.get("phase"), "run",
                         "正在跑卻標 queued → app 隱藏停止鍵,回合停不下來")
        self.assertEqual(st.get("queue_depth"), 2, "積壓要走自己的欄位")
        self.assertIn("2", st.get("label", ""), "積壓要在 label 講人話")
        self.assertIn("排隊", st.get("label", ""))

    def test_accepted_but_not_started_is_queued(self):
        """queued 的原義(收下了、還沒開跑)保留。"""
        d = carddigest.CodexThreadDigest()
        d.busy = False
        d.queue_depth = 1
        d._status()
        st = d.store.status
        self.assertEqual(st.get("phase"), "queued")
        self.assertIn("排入佇列", st.get("label", ""))

    def test_busy_without_queue_is_plain_run(self):
        d = carddigest.CodexThreadDigest()
        d.busy = True
        d._status()
        self.assertEqual(d.store.status.get("phase"), "run")
        self.assertEqual(d.store.status.get("queue_depth"), 0)

    def test_idle_is_idle(self):
        d = carddigest.CodexThreadDigest()
        d._status()
        self.assertEqual(d.store.status.get("phase"), "idle")


class QueueDepthSyncTests(unittest.TestCase):
    """佇列整批失敗清空時,depth 也必須歸零(否則狀態列永遠掛著「另有 N 則排隊」)。"""

    def setUp(self):
        self.saved_app = bridge.CODEX_APP
        self.saved_digests = dict(bridge._CX_CARD_DIGESTS)

    def tearDown(self):
        bridge.CODEX_APP = self.saved_app
        bridge._CX_CARD_DIGESTS.clear()
        bridge._CX_CARD_DIGESTS.update(self.saved_digests)

    def test_depth_returns_to_zero_after_whole_queue_fails(self):
        c = fresh_client(fail_start=True)
        bridge.CODEX_APP = c
        d = bridge._CX_CARD_DIGESTS["t1"] = carddigest.CodexThreadDigest()
        d.busy = True
        c.enqueue_input("t1", [{"type": "text", "text": "一"}], text="一")
        c.enqueue_input("t1", [{"type": "text", "text": "二"}], text="二")
        bridge._cx_sync_queue_depth("t1")
        self.assertEqual(d.queue_depth, 2)

        run(c.drain_pending("t1"))       # 兩則都失敗 → 佇列清空

        self.assertEqual(c.pending_count("t1"), 0)
        self.assertEqual(d.queue_depth, 0,
                         "整批失敗後 depth 卡著不歸零 → 狀態列永遠掛著排隊字樣")

    def test_dropped_message_surfaces_an_error_card(self):
        """被丟掉的訊息不能靜默消失 —— 使用者的泡泡還停在「已排入下一輪」。"""
        c = fresh_client(fail_start=True)
        bridge.CODEX_APP = c
        d = bridge._CX_CARD_DIGESTS["t1"] = carddigest.CodexThreadDigest()
        c.enqueue_input("t1", [{"type": "text", "text": "會被丟掉"}], text="會被丟掉")

        run(c.drain_pending("t1"))

        texts = [(card.get("body") or {}).get("text", "")
                 for card in d.store.cards.values()]
        self.assertTrue(any("丟棄" in t for t in texts),
                        f"排隊訊息被丟掉卻沒有任何卡片告知使用者:{texts}")
        self.assertTrue(any("會被丟掉" in t for t in texts), "錯誤卡要帶原文摘要")


class InterruptErrorCodeTests(unittest.TestCase):
    """M-8:interrupt 的「沒有回合可中斷」不能被翻成「上一輪正在跑」。"""

    def test_no_active_turn_maps_to_its_own_code(self):
        c = fresh_client()
        with self.assertRaises(bridge.CodexAppServerError) as ctx:
            run(c.interrupt_turn("t1"))
        self.assertEqual(ctx.exception.code, bridge._CX_NO_ACTIVE_TURN_CODE)
        with self.assertRaises(bridge.BridgeError) as http_ctx:
            bridge._codex_http_error(ctx.exception)
        self.assertEqual(http_ctx.exception.status_code, 409)
        self.assertEqual(http_ctx.exception.code, "CX_NO_ACTIVE_TURN",
                         "沒有回合可中斷卻回報 CX_TURN_IN_FLIGHT → 與事實完全相反")

    def test_busy_error_still_maps_to_in_flight(self):
        err = bridge.CodexAppServerError("busy", code=-32600)
        with self.assertRaises(bridge.BridgeError) as http_ctx:
            bridge._codex_http_error(err)
        self.assertEqual(http_ctx.exception.code, "CX_TURN_IN_FLIGHT")


class InputDedupTests(unittest.TestCase):
    """H-4:排隊層拿掉 409 那面「意外的防護牆」之後,重試 = 保證重複執行。

    重試路徑不是假設性的:app 端 90s client timeout 重送、OfflineOutbox 自動
    補送、retryPending 可連點。同一個 client_id 在 TTL 內只能真的執行一次。
    """

    def setUp(self):
        bridge._CX_INPUT_INFLIGHT.clear()

    tearDown = setUp

    def test_second_request_with_same_client_id_does_not_execute_again(self):
        async def scenario():
            entry, prior = await bridge._cx_input_claim("t1", "cid-1")
            self.assertIsNotNone(entry)
            self.assertIsNone(prior)
            bridge._cx_input_settle(entry, {"delivery": "accepted", "queued": False})

            entry2, prior2 = await bridge._cx_input_claim("t1", "cid-1")
            self.assertIsNone(entry2, "同一個 client_id 又拿到 claim → 會送第二次")
            self.assertIsNotNone(prior2)
            return await bridge._cx_input_replay(prior2)

        replayed = run(scenario())
        self.assertEqual(replayed.get("delivery"), "accepted",
                         "重複請求要回原本那一次的結果")

    def test_queued_result_is_replayed_too(self):
        """入佇列那條路也要去重,否則同一則會被排進佇列兩次 = 真的送兩次。"""
        async def scenario():
            entry, _ = await bridge._cx_input_claim("t1", "cid-q")
            bridge._cx_input_settle(entry, {"delivery": "queued", "queued": True,
                                            "queue_depth": 1})
            _, prior = await bridge._cx_input_claim("t1", "cid-q")
            return await bridge._cx_input_replay(prior)

        replayed = run(scenario())
        self.assertTrue(replayed.get("queued"))
        self.assertEqual(replayed.get("queue_depth"), 1)

    def test_failed_first_attempt_releases_the_claim(self):
        """第一次失敗要放掉 claim,否則重試被自己擋整整一個 TTL(issue #9 同款坑)。"""
        async def scenario():
            entry, _ = await bridge._cx_input_claim("t1", "cid-err")
            bridge._cx_input_release(entry)
            return await bridge._cx_input_claim("t1", "cid-err")

        entry2, prior2 = run(scenario())
        self.assertIsNotNone(entry2, "失敗後重試被自己的 claim 擋住 → 永遠送不出去")
        self.assertIsNone(prior2)

    def test_different_client_ids_are_independent(self):
        async def scenario():
            e1, _ = await bridge._cx_input_claim("t1", "cid-a")
            e2, p2 = await bridge._cx_input_claim("t1", "cid-b")
            return e1, e2, p2

        e1, e2, p2 = run(scenario())
        self.assertIsNotNone(e1)
        self.assertIsNotNone(e2, "不同 client_id 不該互相擋")
        self.assertIsNone(p2)

    def test_no_client_id_is_not_deduped(self):
        """沒帶 client_id 就無從去重(維持原行為,不能悄悄把訊息吃掉)。"""
        async def scenario():
            return await bridge._cx_input_claim("t1", None)

        entry, prior = run(scenario())
        self.assertIsNone(entry)
        self.assertIsNone(prior)


class InputAckShapeTests(unittest.TestCase):
    """B-2:app 解的是 **boolean `queued`**(`StudioBridgeV2.InputAck`),
    不是字串 `delivery`。persona 的 v2 input 早就回 `"queued": queued`,
    CX 少了它 → app 永遠當成沒排隊,泡泡不會標「⏳ 已排入下一輪」。"""

    def setUp(self):
        self.saved_app = bridge.CODEX_APP
        self.saved_digests = dict(bridge._CX_CARD_DIGESTS)
        bridge._CX_INPUT_INFLIGHT.clear()

    def tearDown(self):
        bridge.CODEX_APP = self.saved_app
        bridge._CX_CARD_DIGESTS.clear()
        bridge._CX_CARD_DIGESTS.update(self.saved_digests)
        bridge._CX_INPUT_INFLIGHT.clear()

    def _post(self, body, active):
        c = fresh_client()
        if active:
            c.active_turns["t1"] = "turn-running"
        bridge.CODEX_APP = c

        class Req:
            headers: dict = {}

            async def json(self):
                return body

        async def scenario():
            return await bridge.codex_session_input("t1", Req())

        real_auth, real_body = bridge._check_auth, bridge._json_body

        async def fake_body(_req):
            return body

        bridge._check_auth = lambda *_a, **_k: None
        bridge._json_body = fake_body
        try:
            return run(scenario())
        finally:
            bridge._check_auth, bridge._json_body = real_auth, real_body

    def test_queued_path_returns_boolean_queued_true(self):
        res = self._post({"text": "排隊那則", "client_id": "cid-1"}, active=True)
        self.assertEqual(res.get("delivery"), "queued")
        self.assertIs(res.get("queued"), True,
                      "少了 boolean queued → app 的 InputAck 永遠當成沒排隊")
        self.assertEqual(res.get("queue_depth"), 1)

    def test_direct_path_returns_boolean_queued_false(self):
        res = self._post({"text": "直送那則", "client_id": "cid-2"}, active=False)
        self.assertEqual(res.get("delivery"), "accepted")
        self.assertIs(res.get("queued"), False)

    def test_retry_with_same_client_id_does_not_send_twice(self):
        """端到端:同一個 client_id 送兩次,app-server 只能被打一次。"""
        c = fresh_client()
        bridge.CODEX_APP = c
        body = {"text": "重試", "client_id": "cid-dup"}

        class Req:
            headers: dict = {}

        real_auth, real_body = bridge._check_auth, bridge._json_body

        async def fake_body(_req):
            return body

        bridge._check_auth = lambda *_a, **_k: None
        bridge._json_body = fake_body
        try:
            first = run(bridge.codex_session_input("t1", Req()))
            second = run(bridge.codex_session_input("t1", Req()))
        finally:
            bridge._check_auth, bridge._json_body = real_auth, real_body

        self.assertEqual(c.call.calls.count("turn/start"), 1,
                         "同一個 client_id 打了兩次 turn/start = 真的執行兩次")
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(second.get("delivery"), first.get("delivery"))


if __name__ == "__main__":
    unittest.main()
