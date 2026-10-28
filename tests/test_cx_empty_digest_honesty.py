"""卡片庫空掉時 bridge 一定要出聲 —— 而且要分清楚是「誰的錯」。

2026-10-26 實害:Cashcamp(thread 019f39d3)送出後 Codex 答完、逐字稿有完整回覆,
但 v2 卡片庫 cards=0 / latest_seq=1。`/cards` 回 **HTTP 200 + 空清單 + 0.005 秒**,
開事件流 60 秒收到 0 張卡,**沒有任何錯誤、沒有任何日誌**。
使用者看到的是一個打得開、送得出、永遠沒反應的空殼 —— 查了一整天。

真因是卡片建構器把沒有 per-item id 的 item 整批丟掉(已由
`test_cx_idless_item_cards` 那一刀修掉)。但**守門本身還有縫**:

    if not turns and not d.store.cards:     # ← 要兩個都空才出聲
        _cx_feed_empty_history(thread_id)

「turns 有回合、卡片 0 張」正好從縫裡掉出去。那是**最該出聲的形狀** ——
因為它代表 bridge 自己有缺陷,而不是 codex 的索引列不出東西。

兩種形狀畫面一模一樣(空白 + 全部 200),但該做的事相反:
  · turns 也空   → codex 索引列不出回合,資料面問題,bridge 只能說明
  · turns 有東西 → **bridge 缺陷,要有人去修**
所以事件名與卡片文案刻意分開,日誌還帶上回合數,下次一眼看得出「讀到幾個卻掉光」。
"""
import _isolation  # noqa: F401
import os
import sys
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="cx-empty-honesty-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
import carddigest  # noqa: E402


def _mk_digest():
    """用真的 digest,不用替身 —— 替身一旦跟真型別脫節,測試會變成空轉。

    (本檔第一版用了不存在的 `carddigest.CardStore`,`SkipTest` 把 9 條全吞成
    skip:輸出看起來是綠的,實際上一條都沒跑。寧可讓它炸。)
    """
    return carddigest.CodexThreadDigest()


class EmptyDigestReasonTests(unittest.TestCase):
    """**這一組才是釘住那道縫的。**

    底下 CardsDroppedCardTests 直接呼叫推卡函式,所以就算把分派條件退回舊寫法
    (`not turns and not cards`)它們照樣全綠 —— 我第一版就是這樣,9 條測試對
    真正的 bug 零鑑別力。分派判斷抽成純函式之後才測得到。
    """

    def test_turns_without_cards_is_a_bridge_defect(self):
        """2026-10-26 Cashcamp 的形狀:讀到 12 個回合、卡片 0 張。
        舊守門在這裡完全沉默 —— 這條就是那個回歸測試。"""
        self.assertEqual(bridge._cx_empty_digest_reason([1] * 12, {}),
                         "cards_dropped")

    def test_no_turns_no_cards_is_a_data_problem(self):
        self.assertEqual(bridge._cx_empty_digest_reason([], {}), "empty_history")

    def test_having_cards_means_nothing_to_say(self):
        self.assertIsNone(bridge._cx_empty_digest_reason([1, 2], {"c1": {}}))
        self.assertIsNone(bridge._cx_empty_digest_reason([], {"c1": {}}))

    def test_never_silent_when_cards_are_empty(self):
        """不變式:卡片空掉**一定**要有話講,不管 turns 是什麼。
        這條是整個修復的核心 —— 空白畫面不准再配上零日誌。"""
        for turns in ([], [1], [1] * 99, (), ("a",)):
            self.assertIsNotNone(bridge._cx_empty_digest_reason(turns, {}),
                                 f"turns={turns!r} 時卡片空掉卻沒有任何說法")


class CardsDroppedCardTests(unittest.TestCase):
    """turns 有回合、卡片 0 張 → 推卡 + 大聲記 log。"""

    def setUp(self):
        self.tid = "019f39d3-e347-7203"
        self.d = _mk_digest()
        p = mock.patch.dict(bridge._CX_CARD_DIGESTS, {self.tid: self.d}, clear=False)
        p.start()
        self.addCleanup(p.stop)

    def test_pushes_a_card_so_the_screen_is_not_silently_blank(self):
        with mock.patch.object(bridge, "_log_event") as log:
            bridge._cx_feed_cards_dropped(self.tid, turn_count=12)
        self.assertIn(bridge._CX_CARDS_DROPPED_CARD_ID, self.d.store.cards)
        events = [c.args[0] for c in log.call_args_list if c.args]
        self.assertIn("cx_cards_dropped", events)

    def test_log_carries_the_turn_count(self):
        """下次再發生時要一眼看出「讀到幾個回合卻掉光」,不用從 189MB 檔反推。"""
        with mock.patch.object(bridge, "_log_event") as log:
            bridge._cx_feed_cards_dropped(self.tid, turn_count=37)
        kw = [c.kwargs for c in log.call_args_list
              if c.args and c.args[0] == "cx_cards_dropped"]
        self.assertTrue(kw)
        self.assertEqual(kw[0].get("turns"), 37)

    def test_only_one_card_per_session(self):
        """每次 TTL 重新 seed 都推一張的話,空白聊天室會變成一疊重複系統卡。"""
        bridge._cx_feed_cards_dropped(self.tid, 3)
        bridge._cx_feed_cards_dropped(self.tid, 3)
        bridge._cx_feed_cards_dropped(self.tid, 3)
        ids = [c for c in self.d.store.cards if "cards-dropped" in c]
        self.assertEqual(len(ids), 1)

    def test_text_blames_the_bridge_not_the_user(self):
        """文案要讓機主知道「不是你的對話壞了」,而且原始紀錄還在。"""
        t = bridge.CX_CARDS_DROPPED_CARD_TEXT
        self.assertIn("bridge", t)
        self.assertIn("完好", t)

    def test_distinct_from_empty_history(self):
        """兩種形狀畫面一樣但病因相反 —— 卡片 id、文案、事件名都不能混用。"""
        self.assertNotEqual(bridge.CX_CARDS_DROPPED_CARD_TEXT,
                            bridge.CX_EMPTY_HISTORY_CARD_TEXT)
        self.assertNotIn("card-cx-empty", bridge._CX_CARDS_DROPPED_CARD_ID)

    def test_missing_digest_does_not_raise(self):
        bridge._cx_feed_cards_dropped("沒有這條", 1)   # 不可以炸


class CardsRecoveredTests(unittest.TestCase):
    """後來產得出卡 → 警告要原地換掉,不能永遠掛著。"""

    def setUp(self):
        self.tid = "recover-me"
        self.d = _mk_digest()
        p = mock.patch.dict(bridge._CX_CARD_DIGESTS, {self.tid: self.d}, clear=False)
        p.start()
        self.addCleanup(p.stop)

    def test_warning_is_replaced_in_place(self):
        bridge._cx_feed_cards_dropped(self.tid, 5)
        before = self.d.store.cards[bridge._CX_CARDS_DROPPED_CARD_ID]
        self.assertIn("產不出", (before.get("body") or {}).get("text", ""))

        bridge._cx_feed_cards_recovered(self.tid)
        after = self.d.store.cards[bridge._CX_CARDS_DROPPED_CARD_ID]
        self.assertIn("讀得出來", (after.get("body") or {}).get("text", ""))
        # 原地換:不可以變成兩張卡(畫面上掛著互相矛盾的兩句話最糟)。
        self.assertEqual(len([c for c in self.d.store.cards if "cards-dropped" in c]), 1)

    def test_flag_resets_so_it_can_speak_again(self):
        """恢復後要能再壞一次、再講一次 —— 同 seed_degraded 的語意。"""
        bridge._cx_feed_cards_dropped(self.tid, 5)
        bridge._cx_feed_cards_recovered(self.tid)
        self.assertFalse(getattr(self.d, "cards_dropped_carded", True))
        bridge._cx_feed_cards_dropped(self.tid, 5)
        txt = (self.d.store.cards[bridge._CX_CARDS_DROPPED_CARD_ID]
               .get("body") or {}).get("text", "")
        self.assertIn("產不出", txt)

    def test_recover_without_prior_warning_is_a_noop(self):
        """沒推過警告就不要憑空冒出一張「已恢復」。"""
        bridge._cx_feed_cards_recovered(self.tid)
        self.assertNotIn(bridge._CX_CARDS_DROPPED_CARD_ID, self.d.store.cards)


if __name__ == "__main__":
    unittest.main()
