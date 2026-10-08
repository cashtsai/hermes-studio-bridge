"""CX seed 完全空 → 要有說明卡,不能靜默空白(2026-10-08 根因排查)。

實錄症狀:一條 session(pixel-cx)打得開但完全空白,所有 API 都回 200,
使用者無從分辨「這條真的沒講過話」與「壞了」。

根因在資料面(bridge 修不了):`codex_delegation` 建立的委派型 session,
整段歷史被壓成單筆 `compacted` 紀錄(rollout 第 2 行),codex 的
`thread/turns/list` 結構上就列不出 turn,所以 bridge 永遠 seed 到 0 筆
—— rollout 檔本身完好(459KB 真實內容都在)。

bridge 的責任是**說明現況**,這組測試釘住三件事:
1. seed 回 0 筆且卡片庫空 → 推一張說明卡。
2. 一條 session 只推一張(TTL 重 seed 不疊卡)。
3. seed 真的有東西時,不得推這張卡(反向保護)。

跑法:PYTHONPATH=. python tests/test_cx_empty_history.py
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

_TMP = tempfile.mkdtemp(prefix="cx-empty-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
import carddigest  # noqa: E402

TID = "019fb395-be4b-7563-8874-28d545503589"


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _texts(d):
    return [(d.store.cards.get(c) or {}).get("body", {}).get("text") or ""
            for c in d.store.order]


class EmptySeedVisibilityTests(unittest.TestCase):

    def setUp(self):
        self.saved = dict(bridge._CX_CARD_DIGESTS)
        bridge._CX_CARD_DIGESTS.clear()
        self.saved_app = bridge.CODEX_APP

    def tearDown(self):
        bridge._CX_CARD_DIGESTS.clear()
        bridge._CX_CARD_DIGESTS.update(self.saved)
        bridge.CODEX_APP = self.saved_app

    def _seed_with(self, turns):
        """跑一次 seed,turns 由參數決定;回傳 digest。"""
        d = bridge._CX_CARD_DIGESTS[TID] = carddigest.CodexThreadDigest()
        fake = AsyncMock(return_value={"data": list(turns)})
        with patch.object(bridge.CODEX_APP, "call", fake), \
                patch.object(bridge.CODEX_APP, "pending_approval_for_thread",
                             return_value=None), \
                patch.object(bridge, "_media_capture_sync", lambda *a, **k: None), \
                patch.object(bridge, "_cx_preview_cache_note", lambda *a, **k: None):
            run(bridge._cx_seed_card_digest(TID, d, required=True))
        return d

    def test_empty_seed_pushes_explanation_card(self):
        d = self._seed_with([])
        texts = _texts(d)
        self.assertTrue(texts, "seed 空白時必須推一張說明卡,不能什麼都不給")
        self.assertTrue(any("讀不到歷史訊息" in t for t in texts), texts)

    def test_card_is_pushed_only_once_across_reseeds(self):
        d = self._seed_with([])
        first = len(d.store.order)
        # TTL 到期重 seed(仍然空)→ 不得再疊一張。
        fake = AsyncMock(return_value={"data": []})
        with patch.object(bridge.CODEX_APP, "call", fake), \
                patch.object(bridge.CODEX_APP, "pending_approval_for_thread",
                             return_value=None), \
                patch.object(bridge, "_media_capture_sync", lambda *a, **k: None), \
                patch.object(bridge, "_cx_preview_cache_note", lambda *a, **k: None):
            run(bridge._cx_seed_card_digest(TID, d, required=True))
        self.assertEqual(len(d.store.order), first,
                         "重 seed 不得疊出第二張說明卡")

    def test_non_empty_seed_does_not_push_the_card(self):
        turn = {"id": "t-1", "items": [
            {"id": "i-1", "type": "agentMessage", "text": "正常內容"}]}
        d = self._seed_with([turn])
        texts = _texts(d)
        self.assertTrue(texts, "有 turn 就該有卡")
        self.assertFalse(any("讀不到歷史訊息" in t for t in texts),
                         f"seed 有東西時不該推空白說明卡:{texts}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
