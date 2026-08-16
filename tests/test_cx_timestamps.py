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
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
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
    """_same_card 原本 pop 掉 ts → reseed 永遠無法修正已經寫錯的時間。

    但判準必須與 upsert_card 的 ts 政策一致(只接受「更早的權威時間」),
    否則 changed/夾回 兩邊互踩就是 livelock,見 ReseedConvergenceTests。
    """

    def test_earlier_authoritative_ts_counts_as_changed(self):
        """reseed 撈到更早的正確歷史時間 → 必須判定 changed 才寫得進去。
        (這是 ts 納入比較的唯一理由:時間錯了要能修回來。)"""
        d = cd.CodexThreadDigest()
        a = cd.make_card("c1", "t", "assistant", "markdown", {"text": "x"},
                         ts=TURN_START)
        b = cd.make_card("c1", "t", "assistant", "markdown", {"text": "x"},
                         ts=TURN_START - 600)
        self.assertFalse(d._same_card(a, b), "更早的正確時間被判 unchanged → 修不回來")

    def test_later_ts_is_not_changed(self):
        """更晚的時間 upsert 會被 min() 夾回去 → 判 changed 只會無限重播。

        原本這裡寫的是 `abs(delta) > 1 → changed`,把 livelock 當成正確行為
        釘進測試:reseed 判 changed → upsert 夾回原值 → 下輪再判 changed,
        每 8 秒一次、rev 無限成長,而 ts 紋風不動。
        """
        d = cd.CodexThreadDigest()
        a = cd.make_card("c1", "t", "assistant", "markdown", {"text": "x"},
                         ts=TURN_START)
        b = cd.make_card("c1", "t", "assistant", "markdown", {"text": "x"},
                         ts=TURN_START + 600)
        self.assertTrue(d._same_card(a, b),
                        "較晚的時間 upsert 不會採用,判 changed 等於自造 livelock")

    def test_defaulted_now_is_not_changed(self):
        """沒帶 ts 的卡(make_card 補 now)不具權威性 → 不該因此判 changed。"""
        d = cd.CodexThreadDigest()
        a = cd.make_card("c1", "t", "assistant", "markdown", {"text": "x"},
                         ts=TURN_START)
        b = cd.make_card("c1", "t", "assistant", "markdown", {"text": "x"})
        self.assertTrue(d._same_card(a, b), "補的 now 不是權威時間,不該算變更")

    def test_tiny_jitter_still_same(self):
        d = cd.CodexThreadDigest()
        a = cd.make_card("c1", "t", "assistant", "markdown", {"text": "x"},
                         ts=TURN_START)
        b = cd.make_card("c1", "t", "assistant", "markdown", {"text": "x"},
                         ts=TURN_START - 0.2)
        self.assertTrue(d._same_card(a, b), "亞秒抖動不該算變更(會洗版)")


class TsDefaultFlagTests(unittest.TestCase):
    """make_card 要記住 ts 是「呼叫端給的」還是「自己補的 now」。"""

    def test_flag_distinguishes_explicit_from_defaulted(self):
        explicit = cd.make_card("c1", "t", "assistant", "markdown", {"text": "x"},
                                ts=TURN_START)
        defaulted = cd.make_card("c2", "t", "assistant", "markdown", {"text": "x"})
        self.assertNotIn(cd.TS_DEFAULT_KEY, explicit)
        self.assertTrue(defaulted.get(cd.TS_DEFAULT_KEY))

    def test_flag_never_leaks_to_app(self):
        """旗標是 carddigest 內部用的,落地與事件裡都不能出現。"""
        store = cd.SessionCardStore()
        ev = store.upsert_card(
            cd.make_card("c1", "t", "assistant", "markdown", {"text": "x"}))
        self.assertNotIn(cd.TS_DEFAULT_KEY, store.cards["c1"])
        self.assertNotIn(cd.TS_DEFAULT_KEY, ev["data"]["card"])

    def test_defaulted_now_never_overwrites_existing_ts(self):
        store = cd.SessionCardStore()
        store.upsert_card(cd.make_card("c1", "t", "assistant", "markdown",
                                       {"text": "x"}, ts=TURN_START))
        store.upsert_card(cd.make_card("c1", "t", "assistant", "markdown",
                                       {"text": "x"}))          # 沒帶 ts → now
        self.assertAlmostEqual(store.cards["c1"]["ts"], TURN_START, places=3)


class ReseedConvergenceTests(unittest.TestCase):
    """B-3 回歸:reseed 必須收斂。

    `_same_card`(ts 差 > 1s 就算 changed)和 `upsert_card`(ts 一律 min())
    兩個改動互相打架:reseed 判 changed → upsert 把 ts 夾回舊值 → 下一輪
    reseed 又判 changed……常駐 reseed 每 8 秒跑一次 → rev 無限成長、每輪都
    對所有訂閱者重推整批卡片,而使用者看到的時間根本沒變。
    """

    TURN = {
        "id": "019fcc82-8a66-7200-8000-000000000000",
        "startedAtMs": TURN_START * 1000,
        "completedAtMs": TURN_END * 1000,
        "items": [
            {"id": "i1", "type": "userMessage",
             "content": [{"type": "text", "text": "問題"}]},
            {"id": "i2", "type": "agentMessage", "text": "回答一"},
            {"id": "i3", "type": "agentMessage", "text": "回答二"},
        ],
    }

    def test_repeated_reseed_stops_bumping_rev(self):
        d = cd.CodexThreadDigest()
        d.seed_turns([self.TURN], emit_unchanged=False)
        first = {cid: c["rev"] for cid, c in d.store.cards.items()}
        first_ts = {cid: c["ts"] for cid, c in d.store.cards.items()}
        for _ in range(5):                       # 模擬常駐 reseed 連跑 5 輪
            d.seed_turns([self.TURN], emit_unchanged=False)
        after = {cid: c["rev"] for cid, c in d.store.cards.items()}
        self.assertEqual(first, after,
                         "同一份 turn 反覆 reseed 仍在遞增 rev → livelock,"
                         "每 8 秒對所有 client 重推整批卡片")
        self.assertEqual(first_ts, {cid: c["ts"] for cid, c in d.store.cards.items()},
                         "rev 一直漲但 ts 沒動,正是 livelock 的指紋")

    def test_live_card_then_reseed_converges(self):
        """真實觸發路徑:卡先在 live 建立(ts≈turn 開頭),reseed 用內插算出
        較晚的時間 → 舊版就是在這裡開始無限打架。"""
        d = cd.CodexThreadDigest()
        d.store.turn_id = self.TURN["id"]
        for item in self.TURN["items"][1:]:
            for card in cd.codex_item_to_cards(item, self.TURN["id"],
                                               phase="completed",
                                               ts=TURN_START + 1):
                d.store.upsert_card(card)
        for _ in range(4):
            d.seed_turns([self.TURN], emit_unchanged=False)
        revs = {cid: c["rev"] for cid, c in d.store.cards.items()}
        for _ in range(4):
            d.seed_turns([self.TURN], emit_unchanged=False)
        self.assertEqual(revs, {cid: c["rev"] for cid, c in d.store.cards.items()},
                         "live 卡 + reseed 內插時間仍在無限重播")

    def test_wrong_now_ts_is_still_repairable(self):
        """收斂不能靠「乾脆不比較 ts」換來:寫錯成 now 的卡仍要被 reseed 修回。"""
        d = cd.CodexThreadDigest()
        d.store.turn_id = self.TURN["id"]
        wrong_now = TURN_END + 86400          # 卡被蓋成「現在」(比歷史晚很多)
        for item in self.TURN["items"][1:]:
            for card in cd.codex_item_to_cards(item, self.TURN["id"],
                                               phase="completed", ts=wrong_now):
                d.store.upsert_card(card)
        d.seed_turns([self.TURN], emit_unchanged=False)
        for c in d.store.cards.values():
            self.assertLessEqual(c["ts"], TURN_END + 1,
                                 "reseed 沒能把錯成 now 的時間修回歷史時間")


class CrossProviderUpsertTests(unittest.TestCase):
    """upsert_card 是 CC/persona/OpenClaw/CX 共用的 SessionCardStore,ts 政策
    改動的影響面不只 CX;這裡壓一條非 CX 的路徑當護欄。"""

    def test_cc_stream_delta_keeps_first_ts_and_bumps_rev(self):
        """CC 串流:同一張卡 delta 反覆重發、都沒帶 ts。時間要釘在第一次,
        rev 要照常遞增(app 靠 rev 原位替換)。"""
        store = cd.SessionCardStore()
        store.upsert_card(cd.make_card("card-cc-1", "t1", "assistant", "markdown",
                                       {"text": "部分"}, ts=TURN_START))
        for i in range(3):
            store.upsert_card(cd.make_card("card-cc-1", "t1", "assistant",
                                           "markdown", {"text": f"部分{i}"}))
        card = store.cards["card-cc-1"]
        self.assertAlmostEqual(card["ts"], TURN_START, places=3,
                               msg="CC 串流重發把時間拉到 now → 列表排序錯亂")
        self.assertEqual(card["rev"], 4)

    def test_persona_style_history_card_ts_preserved(self):
        """persona/OpenClaw 回補歷史卡:帶正確歷史時間 → 不可被之後的重發蓋掉。"""
        store = cd.SessionCardStore()
        hist = cd.make_card("card-hp-1", "", "assistant", "text",
                            {"text": "歷史"}, ts=TURN_START)
        store.upsert_card(hist)
        store.upsert_card(cd.make_card("card-hp-1", "", "assistant", "text",
                                       {"text": "歷史"}, ts=TURN_START + 3600))
        self.assertAlmostEqual(store.cards["card-hp-1"]["ts"], TURN_START, places=3)


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
