"""卡片庫/事件環容量 —— 2026-08-16「離開一陣子回來就整包重載」的根治。

實機:Cashcamp thread 019f39d3 的 latest_seq 已 4066,而舊 ring_max=2000。
只要客戶端離開到累積 2000 個事件,游標就落在環外 → 410 SEQ_GONE → 整包冷載
200 張卡。冷載的成本遠高於補幾百個事件,體感就是每次進場都在等。

記憶體:實測平均 0.8 KB/張卡;環裡放的是卡片**參照**不是複本,所以上限由 ring
主導(8000 × 0.8KB ≈ 6.4 MB/session)。
"""
import importlib
import os
import unittest
from unittest import mock

import carddigest as cd


def card(i):
    return cd.make_card(f"c{i}", "t1", "assistant", "markdown",
                        {"text": f"訊息 {i}"}, ts=1786000000.0 + i)


class DefaultsTests(unittest.TestCase):
    def test_defaults_are_raised(self):
        s = cd.SessionCardStore()
        self.assertGreaterEqual(s.ring_max, 8000,
                                "ring 太小 → 離開一陣子回來必然 410 整包重載")
        self.assertGreaterEqual(s.cards_max, 2000,
                                "卡片庫太小 → 往前翻歷史很快就沒得翻")

    def test_explicit_args_still_win(self):
        """既有呼叫端若有傳值,行為不得改變。"""
        s = cd.SessionCardStore(ring_max=10, cards_max=5)
        self.assertEqual((s.ring_max, s.cards_max), (10, 5))

    def test_env_override(self):
        # 還原的 reload 必須在 patch 結束**之後**做 —— 寫在 with 裡面的話,
        # 還原當下 env 仍被 mock 著,模組常數會卡在測試值,污染後面所有測試
        # (2026-08-16 自己踩到:下一個測試看到 ring_max=123)。
        try:
            with mock.patch.dict(os.environ, {"POCKET_CARD_RING_MAX": "123",
                                              "POCKET_CARD_STORE_MAX": "45"}):
                s = importlib.reload(cd).SessionCardStore()
                self.assertEqual((s.ring_max, s.cards_max), (123, 45))
        finally:
            importlib.reload(cd)


class RingBehaviourTests(unittest.TestCase):
    def test_ring_still_trims_at_its_bound(self):
        s = cd.SessionCardStore(ring_max=5, cards_max=100)
        for i in range(12):
            s.upsert_card(card(i))
        self.assertLessEqual(len(s.events), 5)
        # 環是滑動窗:留下的必須是**最新**那段
        self.assertEqual(s.events[-1]["seq"], s.seq)

    def test_seq_keeps_growing_past_ring(self):
        """seq 是全域計數,不因為環裁切而回頭 —— 客戶端游標靠它判斷新舊。"""
        s = cd.SessionCardStore(ring_max=3, cards_max=100)
        for i in range(10):
            s.upsert_card(card(i))
        self.assertEqual(s.seq, 10)

    def test_bigger_ring_survives_a_long_absence(self):
        """回歸:離開期間累積 4000 個事件,新上限下游標仍在環內(不必冷載)。"""
        s = cd.SessionCardStore()          # 用新預設
        for i in range(4000):
            s.upsert_card(card(i))
        oldest_kept = s.events[0]["seq"]
        self.assertLessEqual(oldest_kept, 1,
                             "4000 個事件就已經裁掉開頭 → 使用者仍會撞 410")


class CardsCapTests(unittest.TestCase):
    def test_cards_cap_drops_oldest(self):
        s = cd.SessionCardStore(ring_max=1000, cards_max=4)
        for i in range(9):
            s.upsert_card(card(i))
        self.assertLessEqual(len(s.cards), 4)
        self.assertIn("c8", s.cards, "最新的卡必須留著")
        self.assertNotIn("c0", s.cards, "最舊的卡該被淘汰")

    def test_upsert_same_card_does_not_consume_capacity(self):
        """同一張卡重發(rev 遞增)不該擠掉別人。"""
        s = cd.SessionCardStore(ring_max=1000, cards_max=3)
        for _ in range(10):
            s.upsert_card(card(0))
        self.assertEqual(len(s.cards), 1)


if __name__ == "__main__":
    unittest.main()
