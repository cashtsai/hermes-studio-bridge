"""GAP-R2:直開 CC/CX session 的回合完成推播(feat/turnend-push)。

產品鐵律:使用者永遠知道進度與狀態,不用回頭開 app 確認。
sub-agent/delegation/persona 完成本來就有推播;這組測試釘住「使用者直開的
CC/CX session 在**沒人看畫面**(subscribers==0)時收尾 → 推播」的契約,以及
所有「不推」判定:有人在看不推、委派雙響不推、桌面版鎖定不推、60s 節流、
TURNEND_PUSH=0 全停用、preview=False 裝置只看占位文案(由 push_notify 的
no_preview_body 承擔,這裡驗參數有帶)。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import asyncio
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

# 防 production 汙染:canonical DB 導去 tmp,絕不碰真家目錄的庫。
_TMP = tempfile.mkdtemp(prefix="turnend-push-canon-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge      # noqa: E402
import carddigest  # noqa: E402


class PushRecorder:
    """替身 push_notify:錄下 (title, body, data, kwargs),不打 APNs。"""

    def __init__(self):
        self.calls = []

    async def __call__(self, title, body, data=None, **kwargs):
        self.calls.append({"title": title, "body": body,
                           "data": data, "kwargs": kwargs})
        return {"sent": 1, "total": 1, "skipped": 0, "failures": []}


def _assistant_card(text, cid="card-x-1"):
    return carddigest.make_card(cid, "turn-1", "assistant", "markdown",
                                {"text": text, "fallback_text": text})


class TurnEndPushCoreTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bridge._TURNEND_PUSH_LAST.clear()

    async def test_payload_contract_and_privacy(self):
        """巢形沿用 pocket.kind=message + sessionId(app 免改即可 deep-link);
        no_preview_body 必帶 —— preview=False 裝置只看占位,內容不進鎖屏。"""
        rec = PushRecorder()
        with patch.object(bridge, "push_notify", rec):
            bridge._turn_end_push("claude_code:dev", "dev", "改好了,測試全綠")
            await asyncio.sleep(0)
        self.assertEqual(len(rec.calls), 1)
        c = rec.calls[0]
        self.assertEqual(c["title"], "✅ dev")
        self.assertEqual(c["body"], "改好了,測試全綠")
        self.assertEqual(c["data"]["pocket"],
                         {"kind": "message", "sessionId": "claude_code:dev"})
        self.assertEqual(c["kwargs"]["thread_id"], "claude_code:dev")
        self.assertTrue(c["kwargs"]["content_available"])
        self.assertEqual(c["kwargs"]["no_preview_body"], "回合已完成")

    async def test_throttle_per_session(self):
        """同 session 60s 內最多一則;不同 session 各自計數。"""
        rec = PushRecorder()
        with patch.object(bridge, "push_notify", rec):
            bridge._turn_end_push("codex:t1", "t1", "a")
            bridge._turn_end_push("codex:t1", "t1", "b")   # 節流掉
            bridge._turn_end_push("codex:t2", "t2", "c")   # 不同 session,照推
            await asyncio.sleep(0)
        self.assertEqual(len(rec.calls), 2)
        # 節流窗過了要再推得出來
        bridge._TURNEND_PUSH_LAST["codex:t1"] = (
            time.time() - bridge._TURNEND_PUSH_MIN_SECS - 1)
        with patch.object(bridge, "push_notify", rec):
            bridge._turn_end_push("codex:t1", "t1", "d")
            await asyncio.sleep(0)
        self.assertEqual(len(rec.calls), 3)

    async def test_env_kill_switch(self):
        rec = PushRecorder()
        with patch.object(bridge, "push_notify", rec), \
                patch.dict(os.environ, {"TURNEND_PUSH": "0"}):
            bridge._turn_end_push("codex:t3", "t3", "x")
            await asyncio.sleep(0)
        self.assertEqual(rec.calls, [])

    async def test_failed_turn_and_truncation(self):
        rec = PushRecorder()
        with patch.object(bridge, "push_notify", rec):
            bridge._turn_end_push("codex:t4", "t4", "e" * 500, ok=False)
            bridge._turn_end_push("codex:t5", "t5", "")   # 沒摘要 → 占位文案
            await asyncio.sleep(0)
        self.assertTrue(rec.calls[0]["title"].startswith("⚠️ "))
        self.assertEqual(len(rec.calls[0]["body"]), 80)
        self.assertEqual(rec.calls[1]["body"], "回合已完成,回來看看結果")


class LastReplyTest(unittest.TestCase):
    def test_picks_last_assistant_text_cleaned(self):
        store = carddigest.SessionCardStore()
        store.upsert_card(_assistant_card("第一則", "card-a"))
        store.upsert_card(_assistant_card(
            "<details>思考</details>\n收工。\n\n  一切正常", "card-b"))
        store.upsert_card(carddigest.make_card(
            "card-tool", "turn-1", "system", "tool_call",
            {"tool": "Bash", "fallback_text": "Bash"}))
        self.assertEqual(bridge._turnend_last_reply(store), "收工。 一切正常")

    def test_empty_store(self):
        self.assertEqual(
            bridge._turnend_last_reply(carddigest.SessionCardStore()), "")


class CxTurnEndPushTest(unittest.IsolatedAsyncioTestCase):
    TID = "0199aaaa-bbbb-cccc-dddd-eeeeffff0001"

    def setUp(self):
        bridge._TURNEND_PUSH_LAST.clear()
        self._saved_cache = bridge._CODEX_V2_VISIBLE_CACHE
        self.digest = carddigest.CodexThreadDigest()
        self.digest.store.upsert_card(_assistant_card("PR 已開好", "card-cx-1"))
        bridge._CX_CARD_DIGESTS[self.TID] = self.digest

    def tearDown(self):
        bridge._CX_CARD_DIGESTS.pop(self.TID, None)
        bridge._CODEX_V2_VISIBLE_CACHE = self._saved_cache

    async def _run(self, err_msg=""):
        rec = PushRecorder()
        with patch.object(bridge, "push_notify", rec), \
                patch.object(bridge, "_turnend_delegation_covers",
                             return_value=False):
            bridge._cx_turn_end_push(self.TID, err_msg)
            await asyncio.sleep(0)
        return rec.calls

    async def test_pushes_when_unwatched(self):
        bridge._CODEX_V2_VISIBLE_CACHE = [
            {"id": self.TID, "name": "修 CI", "preview": "…"}]
        calls = await self._run()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["title"], "✅ 修 CI")
        self.assertEqual(calls[0]["body"], "PR 已開好")
        self.assertEqual(calls[0]["data"]["pocket"]["sessionId"],
                         f"codex:{self.TID}")

    async def test_display_falls_back_to_tid(self):
        bridge._CODEX_V2_VISIBLE_CACHE = []
        calls = await self._run()
        self.assertEqual(calls[0]["title"], f"✅ Codex {self.TID[:8]}")

    async def test_skips_with_active_subscriber(self):
        """有人在看畫面(SSE/agent_call 收割)→ 不推。"""
        self.digest.store.subscribers = 1
        self.assertEqual(await self._run(), [])

    async def test_skips_when_desktop_locked(self):
        self.digest.locked = True
        self.assertEqual(await self._run(), [])

    async def test_skips_unknown_thread(self):
        """app 沒開過的 thread(無 digest)→ 桌面/內部 thread,不推。"""
        rec = PushRecorder()
        with patch.object(bridge, "push_notify", rec):
            bridge._cx_turn_end_push("no-such-thread")
            await asyncio.sleep(0)
        self.assertEqual(rec.calls, [])

    async def test_skips_delegation_thread(self):
        rec = PushRecorder()
        with patch.object(bridge, "push_notify", rec), \
                patch.object(bridge, "_turnend_delegation_covers",
                             return_value=True):
            bridge._cx_turn_end_push(self.TID)
            await asyncio.sleep(0)
        self.assertEqual(rec.calls, [])

    async def test_error_turn(self):
        calls = await self._run(err_msg="context limit exceeded")
        self.assertTrue(calls[0]["title"].startswith("⚠️ "))
        self.assertEqual(calls[0]["body"], "context limit exceeded")


class CcFollowerBackgroundEndTest(unittest.IsolatedAsyncioTestCase):
    """CC follower 整圈:subscribers==0、回合在跑 → 低頻巡 busy,busy→idle
    收尾時發 turn end 事件 + 推播;有人在看則只發事件不推。"""

    def _setup_store(self, name):
        bridge._TURNEND_PUSH_LAST.clear()
        store = bridge._cc_card_store(name)
        store.seeded = True
        store.queued_until = time.time() + 60
        store.upsert_card(_assistant_card("跑完了,結果在 /tmp/out", f"card-{name}"))
        return store

    async def _drive(self, name, subscribers):
        store = self._setup_store(name)
        store.subscribers = subscribers
        rec = PushRecorder()
        busy_seq = [True]          # 第一巡 busy,之後 idle → 收尾

        async def fake_status(_name):
            return {"busy": busy_seq.pop(0) if busy_seq else False,
                    "mode": None, "prompt": None}

        async def fake_jsonl(_name, _workdir):
            return ""

        old_every = bridge._TURNEND_BG_POLL_EVERY
        bridge._TURNEND_BG_POLL_EVERY = 1
        try:
            with patch.object(bridge, "push_notify", rec), \
                    patch.object(bridge, "_cc_status_core", fake_status), \
                    patch.object(bridge, "_cc_session_jsonl", fake_jsonl), \
                    patch.object(bridge, "_turnend_delegation_covers",
                                 return_value=False):
                task = asyncio.create_task(
                    bridge._cc_card_follower(name, "/tmp"))
                deadline = time.time() + 8
                try:
                    while time.time() < deadline:
                        ended = any(
                            e["type"] == "turn" and e["data"].get("state") == "end"
                            for e in store.events)
                        if ended and (rec.calls or subscribers > 0):
                            break
                        await asyncio.sleep(0.1)
                finally:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
        finally:
            bridge._TURNEND_BG_POLL_EVERY = old_every
            bridge._CC_CARD_STORES.pop(name, None)
        return store, rec

    async def test_unwatched_completion_pushes(self):
        store, rec = await self._drive("turnend-bg-test", subscribers=0)
        self.assertTrue(any(
            e["type"] == "turn" and e["data"].get("state") == "end"
            for e in store.events), "背景巡邏要能發出 turn end 事件")
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(rec.calls[0]["title"], "✅ turnend-bg-test")
        self.assertEqual(rec.calls[0]["body"], "跑完了,結果在 /tmp/out")

    async def test_watched_completion_does_not_push(self):
        store, rec = await self._drive("turnend-fg-test", subscribers=1)
        self.assertTrue(any(
            e["type"] == "turn" and e["data"].get("state") == "end"
            for e in store.events))
        self.assertEqual(rec.calls, [], "有人在看畫面就不推")


if __name__ == "__main__":
    unittest.main()
