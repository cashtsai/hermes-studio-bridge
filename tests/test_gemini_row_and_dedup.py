"""Gemini 對齊 CC/CX 的三件(2026-10-31 端到端盤點的 A3 / A6 / C7)。

這三件的共同點:**其他四家都有,gemini 沒有**,而且落差在 app 端看得見。

  A3 `last_event_at` 回 float epoch,但契約與 app 都只解 ISO 字串
      → 「最後活動時間」永遠空白。codex 同一個坑已經用 `_v2_iso_utc` 修過。
  C7 列表不回報待審 → 「它在等你」看不出來。Gemini 的核准只有 180 秒窗口,
      看不到就等於逾時被拒。
  A6 input 沒有 client_id 去重 → 每一條重試路徑都是保證跑第二輪。
      對 Gemini 特別嚴重:額度是 Google 帳號層級的,重跑就是真的多扣一次。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="gm-row-dedup-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class RowAlignTest(unittest.TestCase):

    def test_epoch_becomes_iso_string(self):
        """app 的 `AgentSession.lastEventAt` 只解 ISO —— 送 float 就是永遠空白。"""
        r = bridge._gm_row_align({"id": "gemini:a", "last_event_at": 1762000000.0})
        self.assertIsInstance(r["last_event_at"], str)
        self.assertTrue(r["last_event_at"].endswith("Z"), r["last_event_at"])

    def test_already_iso_passes_through(self):
        iso = "2026-10-31T04:00:00Z"
        r = bridge._gm_row_align({"id": "gemini:a", "last_event_at": iso})
        self.assertEqual(r["last_event_at"], iso)

    def test_missing_timestamp_is_none_not_epoch_zero(self):
        """沒有時間就是沒有 —— 不要變成 1970。"""
        for v in (None, 0, 0.0, ""):
            with self.subTest(v=v):
                r = bridge._gm_row_align({"id": "gemini:a", "last_event_at": v})
                self.assertIsNone(r["last_event_at"])

    def test_pending_approval_surfaces_as_waiting(self):
        """待審要在列表上看得出來(其他四家都有這層)。"""
        fake = {"id": "gm-1", "title": "Gemini 要使用「Bash」"}
        with patch.object(bridge, "_gemini_pending_by_session",
                          return_value={"gemini:k1": fake}):
            r = bridge._gm_row_align({"id": "gemini:k1", "status": "idle"})
        self.assertEqual(r["status"], "waiting_approval")
        self.assertEqual(r["meta"]["approval"], fake)

    def test_no_pending_keeps_original_status(self):
        with patch.object(bridge, "_gemini_pending_by_session", return_value={}):
            r = bridge._gm_row_align({"id": "gemini:k1", "status": "running"})
        self.assertEqual(r["status"], "running")
        self.assertNotIn("approval", r.get("meta") or {})

    def test_scan_failure_does_not_break_the_list(self):
        """查待審爆掉時寧可少一個提示,不要讓整個 session 列表掛掉。"""
        with patch.object(bridge, "_gemini_pending_by_session",
                          side_effect=RuntimeError("db gone")):
            r = bridge._gm_row_align({"id": "gemini:k1", "status": "idle"})
        self.assertEqual(r["status"], "idle")

    def test_original_row_is_not_mutated(self):
        """回新 dict —— 呼叫端的快取不該被就地改掉。"""
        src = {"id": "gemini:a", "last_event_at": 1762000000.0, "meta": {}}
        bridge._gm_row_align(src)
        self.assertEqual(src["last_event_at"], 1762000000.0)


class InputDedupTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        bridge._CC_INPUT_INFLIGHT.clear()

    async def test_same_client_id_claims_once(self):
        """第二次同 client_id 要拿到 prior,不是新的佔位。"""
        entry, prior = await bridge._gm_input_claim("k1", "cid-1")
        self.assertIsNotNone(entry)
        self.assertIsNone(prior)

        entry2, prior2 = await bridge._gm_input_claim("k1", "cid-1")
        self.assertIsNone(entry2, "重複請求不該拿到新的佔位")
        self.assertIsNotNone(prior2, "要拿到前一次的 entry")

    async def test_different_sessions_do_not_collide(self):
        """去重鍵要含 session —— 不同對話用同一個 client_id 是合法的。"""
        e1, p1 = await bridge._gm_input_claim("k1", "cid-x")
        e2, p2 = await bridge._gm_input_claim("k2", "cid-x")
        self.assertIsNotNone(e1)
        self.assertIsNotNone(e2, "不同 session 不該被當成重複")
        self.assertIsNone(p2)

    async def test_gemini_key_is_namespaced(self):
        """gemini 的 key 要帶前綴,免得跟同名的 CC session 撞。"""
        await bridge._gm_input_claim("same-name", "cid-n")
        e, prior = await bridge._cc_input_claim("same-name", "cid-n")
        self.assertIsNotNone(e, "CC 的同名 session 不該被 gemini 的佔位擋住")
        self.assertIsNone(prior)

    async def test_duplicate_returns_the_first_result(self):
        """重複請求拿**同一份**結果,不是再跑一輪。"""
        entry, _ = await bridge._gm_input_claim("k1", "cid-2")
        first = {"ok": True, "message_id": "m-1", "accepted": True}
        bridge._cc_input_settle(entry, first)

        _, prior = await bridge._gm_input_claim("k1", "cid-2")
        out = await bridge._gm_input_wait_prior(prior)
        self.assertEqual(out["message_id"], "m-1")
        self.assertTrue(out.get("deduplicated") or out["message_id"] == "m-1")

    async def test_pending_duplicate_acks_instead_of_rerunning(self):
        """前一次還在跑 → 回 ack,**不要**再送一輪(寧可讓人看到「已收下」)。"""
        entry, _ = await bridge._gm_input_claim("k1", "cid-3")
        _, prior = await bridge._gm_input_claim("k1", "cid-3")
        with patch.object(bridge, "_CX_INPUT_INFLIGHT_WAIT", 0.01):
            out = await bridge._gm_input_wait_prior(prior)
        self.assertTrue(out["accepted"])
        self.assertTrue(out["deduplicated"])

    async def test_no_client_id_means_no_dedup(self):
        """沒帶 client_id 就不去重(不能把所有無 id 的請求併成一次)。"""
        e1, p1 = await bridge._gm_input_claim("k1", None)
        e2, p2 = await bridge._gm_input_claim("k1", None)
        self.assertIsNone(p1)
        self.assertIsNone(p2, "沒有 client_id 的兩次請求是兩件事")


if __name__ == "__main__":
    unittest.main()
