"""CX 訊息時間戳 —— 2026-08-11 使用者回報「CX 訊息時間都錯誤」的回歸。

2026-08-05 的修復(045b6b7)只涵蓋「冷載回放」一條路徑,而且欄位名清單剛好把
codex 實際使用的 `*Ms` 後綴全漏掉,所以症狀依舊。實際上有四層獨立缺陷:

1. 欄位名不對:codex 的數字時間欄位一律 startedAtMs/completedAtMs。
2. ThreadItem schema **完全沒有時間欄位**(實證),per-item 時間拿不到 →
   只能 turn 級 + 內插,否則整串訊息時間一模一樣。
3. turn.startedAt 是 optional+nullable,缺席時要有保底(turn.id 是 UUIDv7,
   前 48 bit 就是毫秒 epoch,而且 id 是 required)。
4. ts 一旦寫錯就**永遠修不回來**:upsert 採新卡 ts(每次重發蓋成 now)、
   _same_card 把 ts pop 掉(reseed 判定 unchanged 而跳過)。
"""
import unittest

import carddigest as cd


TURN_START = 1786000000.0          # 2026-08-04 前後
TURN_END = TURN_START + 300        # 跑了 5 分鐘


class ItemTsFieldTests(unittest.TestCase):
    def test_ms_suffixed_fields_are_read(self):
        """codex 用 startedAtMs(毫秒);原清單只有 startedAt → 永遠撈不到。"""
        got = cd._cx_item_ts({"id": "t", "startedAtMs": TURN_START * 1000}, {})
        self.assertAlmostEqual(got, TURN_START, places=3)

    def test_iso_string_still_works(self):
        got = cd._cx_item_ts({"id": "t", "startedAt": "2026-08-04T11:22:15+00:00"}, {})
        self.assertIsNotNone(got)

    def test_no_time_anywhere_returns_none(self):
        """撈不到就回 None(呼叫端沿用 now)—— 不得亂猜。"""
        self.assertIsNone(cd._cx_item_ts({"id": "not-a-uuid"}, {"type": "userMessage"}))


class Uuid7FallbackTests(unittest.TestCase):
    """turn.startedAt 可為 null,但 turn.id 是 required 的 UUIDv7。"""

    # 實測值:此 id 解出 2026-08-04T11:22:15.014Z
    UID = "019fcc82-8a66-7200-8000-000000000000"

    def test_uuid7_decodes_to_epoch(self):
        got = cd._cx_uuid7_ts(self.UID)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got, 1785842535.014, delta=1.0)

    def test_used_when_started_at_missing(self):
        got = cd._cx_item_ts({"id": self.UID, "startedAt": None}, {})
        self.assertAlmostEqual(got, 1785842535.014, delta=1.0)

    def test_non_uuid7_rejected(self):
        self.assertIsNone(cd._cx_uuid7_ts("019fcc82-8a66-4200-8000-000000000000"))  # v4
        self.assertIsNone(cd._cx_uuid7_ts("hello"))
        self.assertIsNone(cd._cx_uuid7_ts(None))


class InterpolationTests(unittest.TestCase):
    """item 本身沒有時間 → 在 turn 區間內攤開,別讓整串同一時間。"""

    def test_items_spread_across_turn_window(self):
        turn = {"id": "t", "startedAtMs": TURN_START * 1000,
                "completedAtMs": TURN_END * 1000}
        first = cd._cx_item_ts(turn, {}, index=0, total=3)
        mid = cd._cx_item_ts(turn, {}, index=1, total=3)
        last = cd._cx_item_ts(turn, {}, index=2, total=3)
        self.assertAlmostEqual(first, TURN_START, places=3)
        self.assertAlmostEqual(last, TURN_END, places=3)
        self.assertLess(first, mid)
        self.assertLess(mid, last)

    def test_single_item_uses_start(self):
        turn = {"id": "t", "startedAtMs": TURN_START * 1000,
                "completedAtMs": TURN_END * 1000}
        self.assertAlmostEqual(cd._cx_item_ts(turn, {}, index=0, total=1),
                               TURN_START, places=3)


class TsMustNotRegressTests(unittest.TestCase):
    """時間一旦正確就不能被後續重發拉到 now(卡片是照 ts 排序的)。"""

    def _card(self, ts):
        return cd.make_card("card-cx-1", "t1", "assistant", "markdown",
                            {"text": "x"}, ts=ts)

    def test_upsert_keeps_earlier_ts(self):
        store = cd.SessionCardStore()
        store.upsert_card(self._card(TURN_START))
        store.upsert_card(self._card(TURN_START + 9999))   # 重發,帶「現在」
        self.assertAlmostEqual(store.cards["card-cx-1"]["ts"], TURN_START, places=3,
                               msg="重發把歷史時間拉到現在 → 排序也會跟著錯")

    def test_rev_still_increments(self):
        store = cd.SessionCardStore()
        store.upsert_card(self._card(TURN_START))
        store.upsert_card(self._card(TURN_START + 10))
        self.assertGreater(store.cards["card-cx-1"]["rev"], 1)


class SameCardTsTests(unittest.TestCase):
    """_same_card 原本 pop 掉 ts → reseed 永遠無法修正已經寫錯的時間。"""

    def test_ts_difference_counts_as_changed(self):
        d = cd.CodexThreadDigest()
        a = dict(cd.make_card("c1", "t", "assistant", "markdown", {"text": "x"}),
                 ts=TURN_START)
        b = dict(a, ts=TURN_START + 600)
        self.assertFalse(d._same_card(a, b), "時間差 10 分鐘卻判定 unchanged → 修不回來")

    def test_tiny_jitter_still_same(self):
        d = cd.CodexThreadDigest()
        a = dict(cd.make_card("c1", "t", "assistant", "markdown", {"text": "x"}),
                 ts=TURN_START)
        b = dict(a, ts=TURN_START + 0.2)
        self.assertTrue(d._same_card(a, b), "亞秒抖動不該算變更(會洗版)")


class SeedTurnsTests(unittest.TestCase):
    def test_seeded_cards_carry_history_time_not_now(self):
        d = cd.CodexThreadDigest()
        d.seed_turns([{
            "id": "019fcc82-8a66-7200-8000-000000000000",
            "startedAtMs": TURN_START * 1000,
            "completedAtMs": TURN_END * 1000,
            "items": [
                {"id": "i1", "type": "userMessage",
                 "content": [{"type": "text", "text": "問題"}]},
                {"id": "i2", "type": "agentMessage", "text": "回答"},
            ],
        }])
        tss = [c["ts"] for c in d.store.cards.values()]
        self.assertTrue(tss)
        for ts in tss:
            self.assertLess(ts, TURN_END + 1,
                            "歷史卡的時間被蓋成 now(這就是使用者看到的症狀)")
            self.assertGreaterEqual(ts, TURN_START - 1)


if __name__ == "__main__":
    unittest.main()
