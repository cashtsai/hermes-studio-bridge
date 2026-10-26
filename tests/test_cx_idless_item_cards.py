"""無 id 的 codex item 也要產出卡片 + 卡片帶 seq(2026-10-26 Cashcamp 顯示不出回覆)。

實害:thread 019f39d3 的 thread/turns/list 有完整逐字稿,但 item 沒有 per-item id
(compacted/replayed rollout 常見),卡片建構器原本 `if not iid: return []` 直接丟掉
→ 舊版 history 有內容、v2 卡片庫卻 0 張,app 卡片流顯示不出回覆、冷載也補不回來。

修:seed/回放時給無 id item 一個確定性後備 id(turn_id + 位置),同份 turns/list 每次
reseed 都一樣,不重複;即時串流不帶 index 維持原行為。snapshot 另外把真 seq 寫進卡片。
"""
import _isolation  # noqa: F401
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="cx-idless-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carddigest  # noqa: E402


class CXIdlessItemCardsTest(unittest.TestCase):
    def test_idless_item_dropped_without_index_live(self):
        item = {"type": "agentMessage", "text": "答覆"}
        self.assertEqual(carddigest.codex_item_to_cards(item, turn_id="t1"), [])

    def test_idless_item_gets_deterministic_card_with_index(self):
        item = {"type": "agentMessage", "text": "答覆"}
        cards = carddigest.codex_item_to_cards(item, turn_id="t1", index=3)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["id"], "card-cx-t1-t3")
        self.assertEqual(cards[0]["role"], "assistant")

    def test_fallback_id_deterministic_across_reseeds(self):
        item = {"type": "agentMessage", "text": "答覆"}
        a = carddigest.codex_item_to_cards(item, turn_id="t1", index=3)[0]["id"]
        b = carddigest.codex_item_to_cards(item, turn_id="t1", index=3)[0]["id"]
        self.assertEqual(a, b)

    def test_real_id_not_overridden_by_fallback(self):
        item = {"type": "agentMessage", "text": "答覆", "id": "rs_abc"}
        cards = carddigest.codex_item_to_cards(item, turn_id="t1", index=3)
        self.assertEqual(cards[0]["id"], "card-cx-rs_abc")

    def test_seed_turns_populates_idless_thread_and_stamps_seq(self):
        digest = carddigest.CodexThreadDigest()
        turns = [{"id": "turn1", "items": [
            {"type": "agentMessage", "text": "完成了 Plex 建置"},
            {"type": "agentMessage", "text": "請到 cashbox.local 完成 claim"},
        ]}]
        digest.seed_turns(turns)
        snap = digest.store.snapshot()
        self.assertGreaterEqual(len(snap["cards"]), 1, "無 id item 修後應產出卡片,不再是 0")
        self.assertTrue(all(c.get("seq") is not None for c in snap["cards"]),
                        "snapshot 每張卡都要帶真 seq")


if __name__ == "__main__":
    unittest.main()
