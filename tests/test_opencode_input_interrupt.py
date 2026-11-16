"""S7 的 input / turn / interrupt / history rail(「管線存在≠端到端通」的
bridge 側證據:每條 op 不是只看表上有格,而是真的驅動到 ACP 呼叫與卡片流)。

OPENCODE client 以替身頂替(ensure_session / call / cancel),ACP wire 層的
行為由 test_opencode_client.py 用錄製 frame 蓋;這一檔蓋的是 bridge rail:
ack 形狀、逐字稿落檔、卡片 turn 收尾、忙碌排隊 + drain、interrupt 409 語意、
client_id 去重、歷史的單一母體紀律、**以及 compact 刻意不支援**。
"""
import _isolation  # noqa: F401
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="oce-input-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
import opencode_sdk  # noqa: E402


class _Req:
    headers = {"authorization": "Bearer test-unit-token"}
    query_params = {}


class _FakeOpenCode:
    """OPENCODE 替身:prompt 時照 ACP 形狀把 chunk 打回 on_update。"""

    def __init__(self, reply="收到"):
        self.reply = reply
        self.cancelled = []
        self.prompts = []
        self.replay = None

    def configured(self):
        return True

    def logged_in(self):
        return False      # 刻意:沒有憑證也要全程可用(這家沒有登入牆)

    def supports_image(self):
        return True

    async def ensure_session(self, key):
        opencode_sdk.remember_session(key, f"ses_{key}")
        return f"ses_{key}"

    async def call(self, method, params, timeout=None):
        assert method == "session/prompt"
        self.prompts.append(params)
        sid = params["sessionId"]
        if bridge.OPENCODE.on_update:
            for ch in self.reply:
                bridge.OPENCODE.on_update(sid, {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": ch}})
        return {"stopReason": "end_turn", "usage": {"totalTokens": 1}}

    async def cancel(self, key):
        self.cancelled.append(key)

    def acp_to_key(self, sid):
        return opencode_sdk.OpenCodeClient.acp_to_key(self, sid)

    def replay_messages(self, key):
        return self.replay


class RailTestBase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="oce-rail-")
        self._patches = [
            patch.object(opencode_sdk, "_STATE_DIR", self._dir),
            patch.object(opencode_sdk, "_SESSIONS_FILE",
                         os.path.join(self._dir, "sessions.json")),
        ]
        for p in self._patches:
            p.start()
        self.fake = _FakeOpenCode()
        self._orig = bridge.OPENCODE
        bridge.OPENCODE = self.fake
        self.fake.on_update = bridge._oce_on_update
        bridge.OPENCODE.on_update = bridge._oce_on_update
        bridge._OCE_CARD_DIGESTS.clear()
        bridge._OCE_TURNS.clear()
        bridge._OCE_INPUT_QUEUE.clear()
        self._push = bridge._turn_end_push
        bridge._turn_end_push = lambda *a, **k: None

    def tearDown(self):
        bridge.OPENCODE = self._orig
        bridge._turn_end_push = self._push
        bridge._OCE_TURNS.clear()
        bridge._OCE_INPUT_QUEUE.clear()
        for p in self._patches:
            p.stop()


class InputTest(RailTestBase):
    def test_input_acks_runs_turn_and_lands_transcript(self):
        async def main():
            out = await bridge._oce_input_core("k1", "opencode:k1",
                                               {"content": "你好 opencode"})
            for _ in range(200):
                await asyncio.sleep(0.005)
                if not bridge._OCE_TURNS:
                    break
            return out
        out = asyncio.run(main())
        self.assertTrue(out["accepted"])
        self.assertTrue(out["message_id"])
        msgs = opencode_sdk.transcript_read("k1")
        self.assertEqual([(m["role"], m["content"]) for m in msgs],
                         [("user", "你好 opencode"), ("assistant", "收到")],
                         "user 與 assistant 都要落逐字稿(重啟補卡的來源)")
        self.assertEqual(self.fake.prompts[0]["prompt"],
                         [{"type": "text", "text": "你好 opencode"}])
        # create 的證據:fresh key → 名錄長出 acp_sid
        self.assertEqual(opencode_sdk.get_session("k1")["acp_sid"], "ses_k1")

    def test_input_works_without_any_credentials(self):
        """這家沒有登入牆 —— logged_in() False 也必須能送出
        (加了登入閘門就是把一個能用的 provider 關掉)。"""
        self.assertFalse(self.fake.logged_in())

        async def main():
            out = await bridge._oce_input_core("k1", "opencode:k1",
                                               {"content": "免費模型也要能跑"})
            for _ in range(200):
                await asyncio.sleep(0.005)
                if not bridge._OCE_TURNS:
                    break
            return out
        out = asyncio.run(main())
        self.assertTrue(out["accepted"])
        self.assertEqual(len(self.fake.prompts), 1)

    def test_busy_queues_instead_of_409_and_drains(self):
        async def main():
            bridge._OCE_TURNS["ses_k1"] = {"cid": "c0", "text": "", "key": "k1"}
            out = await bridge._oce_input_core("k1", "opencode:k1",
                                               {"content": "排隊的"})
            self.assertTrue(out["queued"])
            self.assertEqual(out["queue_depth"], 1)
            bridge._OCE_TURNS.pop("ses_k1")
            pending = bridge._OCE_INPUT_QUEUE.get("k1")
            self.assertEqual(pending[0][0], "排隊的")
            await bridge._oce_run_turn("k1", *pending.pop(0))
            return out
        asyncio.run(main())
        msgs = opencode_sdk.transcript_read("k1")
        self.assertIn(("assistant", "收到"),
                      [(m["role"], m["content"]) for m in msgs])

    def test_empty_content_is_400(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(bridge._oce_input_core("k1", "opencode:k1", {}))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_concurrent_distinct_inputs_second_queues_not_double_runs(self):
        """ensure_session 的 await 窗口裡殺進第二則(不同 client_id)輸入:
        要排隊,不准開第二輪(額度雙扣 + _OCE_TURNS 互相覆蓋;佔位鍵
        _oce_turn_hold 的回歸 —— S6 抓到的窗口,這家一開始就補上)。"""
        gate = asyncio.Event()
        orig_ensure = self.fake.ensure_session

        async def slow_ensure(key):
            await gate.wait()
            return await orig_ensure(key)
        self.fake.ensure_session = slow_ensure

        async def main():
            out1 = await bridge._oce_input_core("k1", "opencode:k1",
                                                {"content": "第一則",
                                                 "client_id": "c-1"})
            out2 = await bridge._oce_input_core("k1", "opencode:k1",
                                                {"content": "第二則",
                                                 "client_id": "c-2"})
            gate.set()
            for _ in range(200):
                await asyncio.sleep(0.005)
                if not bridge._OCE_TURNS and not bridge._OCE_INPUT_QUEUE:
                    break
            return out1, out2
        out1, out2 = asyncio.run(main())
        self.assertNotIn("queued", out1)
        self.assertTrue(out2.get("queued"), "第二則要排隊,不是開第二輪")
        self.assertEqual(len(self.fake.prompts), 2, "兩則都要跑到(序列化)")
        self.assertEqual(bridge._OCE_TURNS, {}, "佔位鍵不准殘留")

    def test_client_id_dedup_second_claim_sees_prior(self):
        async def main():
            d1, prior1 = await bridge._oce_input_claim("k1", "cid-123")
            self.assertIsNone(prior1)
            _d2, prior2 = await bridge._oce_input_claim("k1", "cid-123")
            self.assertIsNotNone(prior2, "同 client_id 的重試要對到在途那筆")
            bridge._cc_input_settle(d1, {"ok": True})
        asyncio.run(main())

    def test_turn_failure_surfaces_human_text_and_drains_queue(self):
        """回合失敗也要 drain(否則後送的訊息永遠卡著)+ 錯誤翻成人話。"""
        async def boom(method, params, timeout=None):
            raise opencode_sdk.OpenCodeError("429 rate limit")
        self.fake.call = boom

        async def main():
            await bridge._oce_run_turn("k1", "會失敗的")
        asyncio.run(main())
        self.assertEqual(bridge._OCE_TURNS, {})
        self.assertEqual(bridge._OCE_INPUT_QUEUE, {})


class InterruptTest(RailTestBase):
    def test_idle_is_409(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(bridge.v2_session_interrupt("opencode:k1", _Req()))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.fake.cancelled, [],
                         "閒置不准發 advisory cancel 裝作有中斷")

    def test_busy_cancels_via_acp(self):
        bridge._OCE_TURNS["ses_k1"] = {"cid": "c", "text": "", "key": "k1"}
        out = asyncio.run(bridge.v2_session_interrupt("opencode:k1", _Req()))
        self.assertTrue(out["interrupted"])
        self.assertEqual(self.fake.cancelled, ["k1"])

    def test_not_installed_session_space_is_404(self):
        from fastapi import HTTPException
        with patch.object(self.fake, "configured", lambda: False):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(bridge.v2_session_interrupt("opencode:k1", _Req()))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_v1_interrupt_covers_opencode(self):
        """v1(Open WebUI)線的停止鈕也要是真的 —— v2 有、v1 沒有就是同一
        顆鈕兩條線一真一假(S6 修過的不對稱,新 provider 不准重犯)。"""
        class _V1Req(_Req):
            async def json(self):
                return {"session": "opencode:k1"}
        out = asyncio.run(bridge.app_message_interrupt(_V1Req()))
        self.assertTrue(out["interrupted"])
        self.assertEqual(self.fake.cancelled, ["k1"])


class CompactTest(RailTestBase):
    def test_compact_is_an_honest_unsupported_not_a_dead_button(self):
        """opencode 的 availableCommands 實測**沒有** compact。

        所以這裡要的是「誠實的不支援」:v2 compact 對這家回 400
        UNSUPPORTED_PROVIDER(provider 層沒有這個能力),而不是假裝送出
        一個 `/compact` 給它當普通訊息吃掉 —— 那會變成使用者按了壓縮、
        agent 回一句「我不懂這個指令」。
        """
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(bridge.v2_session_compact("opencode:k1", _Req()))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self.fake.prompts, [],
                         "不准偷偷把 /compact 當訊息送出去")


class HistoryTest(RailTestBase):
    """歷史的單一母體紀律(S6 review 訂的,這家照用)。"""

    def test_transcript_is_the_paging_authority(self):
        for i in range(3):
            opencode_sdk.transcript_append("k1", "user", f"m{i}")
        page = asyncio.run(bridge._oce_op_history("k1", _Req()))
        self.assertEqual(len(page["messages"]), 3)
        self.assertNotIn("source", page, "有逐字稿就不該標 native")

    def test_empty_transcript_falls_back_to_load_replay(self):
        """逐字稿空 → 用 session/load 的 replay 補那一格(零額外 RPC)。"""
        self.fake.replay = [{"id": "oce-h0", "role": "user",
                             "content": "舊的問題", "ts": None},
                            {"id": "oce-h1", "role": "assistant",
                             "content": "舊的回答", "ts": None}]
        page = asyncio.run(bridge._oce_op_history("k1", _Req()))
        self.assertEqual(page["source"], "native")
        self.assertEqual([m["content"] for m in page["messages"]],
                         ["舊的問題", "舊的回答"])
        self.assertFalse(page["more"],
                         "原生路沒有可續的下一頁,不准假裝可以一直翻")

    def test_second_page_never_switches_population(self):
        """offset != 0 一律只看逐字稿 —— 第一頁原生、第二頁逐字稿會讓兩頁
        取樣自不同母體(翻頁重複或缺漏)。"""
        self.fake.replay = [{"id": "x", "role": "user", "content": "原生",
                             "ts": None}]

        class _Req2(_Req):
            query_params = {"offset": "1"}
        page = asyncio.run(bridge._oce_op_history("k1", _Req2()))
        self.assertEqual(page["messages"], [])
        self.assertNotIn("source", page)

    def test_replay_failure_degrades_instead_of_500(self):
        def boom(_key):
            raise RuntimeError("replay exploded")
        self.fake.replay_messages = boom
        page = asyncio.run(bridge._oce_op_history("k1", _Req()))
        self.assertEqual(page, {"messages": [], "more": False, "total": 0})


class ResponseCapabilityTest(unittest.TestCase):
    """審批「答得動嗎」:等待者是記憶體 Future —— 重啟/逾時後要報
    not_resumable,UI 才會灰掉按鈕(漏這格 = 按了吃 409 的死鈕)。"""

    def test_live_when_waiter_present(self):
        async def main():
            fut = asyncio.get_running_loop().create_future()
            bridge._OCE_PERM_WAITERS["oce-cap1"] = fut
            try:
                return bridge._approval_response_capability(
                    "oce-cap1", "opencode", "opencode:k1", "pending")
            finally:
                bridge._OCE_PERM_WAITERS.pop("oce-cap1", None)
        self.assertEqual(asyncio.run(main()), "live")

    def test_not_resumable_when_waiter_gone(self):
        bridge._OCE_PERM_WAITERS.pop("oce-cap2", None)
        self.assertEqual(
            bridge._approval_response_capability(
                "oce-cap2", "opencode", "opencode:k1", "pending"),
            "not_resumable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
