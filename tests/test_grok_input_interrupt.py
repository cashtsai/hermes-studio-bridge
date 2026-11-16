"""S6 的 input / turn / interrupt rail(「管線存在≠端到端通」的 bridge 側
證據:每條 op 不是只看表上有格,而是真的驅動到 ACP 呼叫與卡片流)。

GROK client 以替身頂替(ensure_session / call / cancel),ACP wire 層的
行為由 test_grok_client.py 用錄製 frame 蓋;這一檔蓋的是 bridge rail:
ack 形狀、逐字稿落檔、卡片 turn 收尾、忙碌排隊 + drain、interrupt 409 語意、
client_id 去重。
"""
import _isolation  # noqa: F401
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="gk-input-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
import grok_sdk  # noqa: E402


class _Req:
    headers = {"authorization": "Bearer test-unit-token"}
    query_params = {}


class _FakeGrok:
    """GROK 替身:prompt 時照 ACP 形狀把 chunk 打回 on_update。"""

    def __init__(self, reply="收到"):
        self.reply = reply
        self.cancelled = []
        self.prompts = []

    def configured(self):
        return True

    def logged_in(self):
        return True

    async def ensure_session(self, key):
        grok_sdk.remember_session(key, f"acp-{key}")
        return f"acp-{key}"

    async def call(self, method, params, timeout=None):
        assert method == "session/prompt"
        self.prompts.append(params)
        sid = params["sessionId"]
        if bridge.GROK.on_update:
            for ch in self.reply:
                bridge.GROK.on_update(sid, {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": ch}})
        return {"stopReason": "end_turn"}

    async def cancel(self, key):
        self.cancelled.append(key)

    def acp_to_key(self, sid):
        return grok_sdk.GrokClient.acp_to_key(self, sid)  # 名錄查詢,無狀態


class RailTestBase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="gk-rail-")
        self._patches = [
            patch.object(grok_sdk, "_STATE_DIR", self._dir),
            patch.object(grok_sdk, "_SESSIONS_FILE",
                         os.path.join(self._dir, "sessions.json")),
        ]
        for p in self._patches:
            p.start()
        self.fake = _FakeGrok()
        self._orig = bridge.GROK
        bridge.GROK = self.fake
        # 替身也要掛 bridge 的 on_update(S6 開機時掛在真 client 上)。
        self.fake.on_update = bridge._gk_on_update
        bridge.GROK.on_update = bridge._gk_on_update
        bridge._GK_CARD_DIGESTS.clear()
        bridge._GK_TURNS.clear()
        bridge._GK_INPUT_QUEUE.clear()
        self._push = bridge._turn_end_push
        bridge._turn_end_push = lambda *a, **k: None

    def tearDown(self):
        bridge.GROK = self._orig
        bridge._turn_end_push = self._push
        bridge._GK_TURNS.clear()
        bridge._GK_INPUT_QUEUE.clear()
        for p in self._patches:
            p.stop()


class InputTest(RailTestBase):
    def test_input_acks_runs_turn_and_lands_transcript(self):
        async def main():
            out = await bridge._gk_input_core("k1", "grok:k1",
                                              {"content": "你好 grok"})
            # 背景 turn 跑完(_BG_TASKS 裡那顆)
            for _ in range(200):
                await asyncio.sleep(0.005)
                if not bridge._GK_TURNS:
                    break
            return out
        out = asyncio.run(main())
        self.assertTrue(out["accepted"])
        self.assertTrue(out["message_id"])
        msgs = grok_sdk.transcript_read("k1")
        self.assertEqual([(m["role"], m["content"]) for m in msgs],
                         [("user", "你好 grok"), ("assistant", "收到")],
                         "user 與 assistant 都要落逐字稿(重啟補卡的來源)")
        self.assertEqual(self.fake.prompts[0]["prompt"],
                         [{"type": "text", "text": "你好 grok"}])
        # create 的證據:fresh key → 名錄長出 acp_sid
        self.assertEqual(grok_sdk.get_session("k1")["acp_sid"], "acp-k1")

    def test_busy_queues_instead_of_409_and_drains(self):
        async def main():
            bridge._GK_TURNS["acp-k1"] = {"cid": "c0", "text": "", "key": "k1"}
            out = await bridge._gk_input_core("k1", "grok:k1",
                                              {"content": "排隊的"})
            self.assertTrue(out["queued"])
            self.assertEqual(out["queue_depth"], 1)
            # 模擬上一輪收尾:直接跑 drain 段(run_turn 的 finally)
            bridge._GK_TURNS.pop("acp-k1")
            pending = bridge._GK_INPUT_QUEUE.get("k1")
            self.assertEqual(pending[0][0], "排隊的")
            await bridge._gk_run_turn("k1", *pending.pop(0))
            return out
        asyncio.run(main())
        msgs = grok_sdk.transcript_read("k1")
        self.assertIn(("assistant", "收到"),
                      [(m["role"], m["content"]) for m in msgs])

    def test_empty_content_is_400(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(bridge._gk_input_core("k1", "grok:k1", {}))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_concurrent_distinct_inputs_second_queues_not_double_runs(self):
        """ensure_session 的 await 窗口裡殺進第二則(不同 client_id)輸入:
        要排隊,不准開第二輪(額度雙扣 + _GK_TURNS 互相覆蓋;佔位鍵
        _gk_turn_hold 的回歸)。"""
        gate = asyncio.Event()
        orig_ensure = self.fake.ensure_session

        async def slow_ensure(key):
            await gate.wait()            # 把第一輪卡在登記之前
            return await orig_ensure(key)
        self.fake.ensure_session = slow_ensure

        async def main():
            out1 = await bridge._gk_input_core("k1", "grok:k1",
                                               {"content": "第一則",
                                                "client_id": "c-1"})
            out2 = await bridge._gk_input_core("k1", "grok:k1",
                                               {"content": "第二則",
                                                "client_id": "c-2"})
            gate.set()
            for _ in range(200):
                await asyncio.sleep(0.005)
                if not bridge._GK_TURNS and not bridge._GK_INPUT_QUEUE:
                    break
            return out1, out2
        out1, out2 = asyncio.run(main())
        self.assertNotIn("queued", out1)
        self.assertTrue(out2.get("queued"), "第二則要排隊,不是開第二輪")
        self.assertEqual(len(self.fake.prompts), 2, "兩則都要跑到(序列化)")
        self.assertEqual(bridge._GK_TURNS, {}, "佔位鍵不准殘留")

    def test_client_id_dedup_second_claim_sees_prior(self):
        async def main():
            d1, prior1 = await bridge._gk_input_claim("k1", "cid-123")
            self.assertIsNone(prior1)
            _d2, prior2 = await bridge._gk_input_claim("k1", "cid-123")
            self.assertIsNotNone(prior2, "同 client_id 的重試要對到在途那筆")
            bridge._cc_input_settle(d1, {"ok": True})
        asyncio.run(main())


class InterruptTest(RailTestBase):
    def test_idle_is_409(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(bridge.v2_session_interrupt("grok:k1", _Req()))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.fake.cancelled, [],
                         "閒置不准發 advisory cancel 裝作有中斷")

    def test_busy_cancels_via_acp(self):
        bridge._GK_TURNS["acp-k1"] = {"cid": "c", "text": "", "key": "k1"}
        out = asyncio.run(bridge.v2_session_interrupt("grok:k1", _Req()))
        self.assertTrue(out["interrupted"])
        self.assertEqual(self.fake.cancelled, ["k1"])

    def test_not_installed_session_space_is_404(self):
        from fastapi import HTTPException
        with patch.object(self.fake, "configured", lambda: False):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(bridge.v2_session_interrupt("grok:k1", _Req()))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_v1_interrupt_covers_grok(self):
        """v1(Open WebUI)線的停止鈕也要是真的 —— v2 有、v1 沒有就是同一
        顆鈕兩條線一真一假(review 抓的不對稱,修掉後釘住)。"""
        class _V1Req(_Req):
            async def json(self):
                return {"session": "grok:k1"}
        out = asyncio.run(bridge.app_message_interrupt(_V1Req()))
        self.assertTrue(out["interrupted"])
        self.assertEqual(self.fake.cancelled, ["k1"])


class CompactTest(RailTestBase):
    def test_compact_sends_slash_compact_through_input_rail(self):
        out = asyncio.run(bridge.v2_session_compact("grok:k1", _Req()))
        self.assertTrue(out["started"])
        # 走與一般送話同一條管線:prompt 的正文就是 /compact
        self.assertEqual(self.fake.prompts[0]["prompt"][0]["text"], "/compact")

    def test_compact_while_busy_is_409(self):
        from fastapi import HTTPException
        bridge._GK_TURNS["acp-k1"] = {"cid": "c", "text": "", "key": "k1"}
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(bridge.v2_session_compact("grok:k1", _Req()))
        self.assertEqual(ctx.exception.status_code, 409)


class ResponseCapabilityTest(unittest.TestCase):
    """審批「答得動嗎」:grok 等待者是記憶體 Future —— 重啟/逾時後要報
    not_resumable,UI 才會灰掉按鈕(漏這格 = 按了吃 409 的死鈕)。"""

    def test_live_when_waiter_present(self):
        async def main():
            fut = asyncio.get_running_loop().create_future()
            bridge._GK_PERM_WAITERS["gk-cap1"] = fut
            try:
                return bridge._approval_response_capability(
                    "gk-cap1", "grok", "grok:k1", "pending")
            finally:
                bridge._GK_PERM_WAITERS.pop("gk-cap1", None)
        self.assertEqual(asyncio.run(main()), "live")

    def test_not_resumable_when_waiter_gone(self):
        bridge._GK_PERM_WAITERS.pop("gk-cap2", None)
        self.assertEqual(
            bridge._approval_response_capability(
                "gk-cap2", "grok", "grok:k1", "pending"),
            "not_resumable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
