"""seed 讀不到最新進度時,不准拿舊卡假裝是現況(2026-10-10 實害)。

機主送一個任務給 CX 的 Pocket session,跑到一半遇上 bridge 重啟。重啟清掉
記憶體裡的 card digest,重新播種時 `thread/turns/list` 讀不到最新進度
(那條 thread 的 rollout 檔 160 MB,`thread/read` 已經逾時過兩次),於是
視窗被**較早的對話**蓋過去 —— 他看到的是「指令跑一跑從對話窗消失了」,
而所有 API 都回 200,畫面上沒有任何線索。

這跟同一天修掉的「送不出去卻回 200」是同一種病:**失敗偽裝成成功**。
修法:seed 失敗而卡片庫裡已經有舊卡時,推一張系統卡明說「這不是現況」。
"""
import _isolation  # noqa: F401
import asyncio
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="cx-seed-degraded-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
import carddigest  # noqa: E402

THREAD = "019f6e0e-4680-7d62-8ea1-786acd93e71a"


def _fresh_digest(with_old_cards: bool):
    d = carddigest.CodexThreadDigest()
    if with_old_cards:
        d.store.upsert_card(carddigest.make_card(
            "card-cx-old-1", d.store.turn_id, "assistant", "text",
            {"text": "這是上週的回覆", "fallback_text": "這是上週的回覆"}))
    bridge._CX_CARD_DIGESTS[THREAD] = d
    return d


class CXSeedDegradedTest(unittest.TestCase):
    def setUp(self):
        self.orig_call = bridge.CODEX_APP.call
        bridge._CX_CARD_DIGESTS.pop(THREAD, None)

    def tearDown(self):
        bridge.CODEX_APP.call = self.orig_call
        bridge._CX_CARD_DIGESTS.pop(THREAD, None)

    def _seed_with_timeout(self, d):
        async def boom(*a, **k):
            raise bridge.CodexAppServerError("thread/turns/list timed out")
        bridge.CODEX_APP.call = boom
        asyncio.run(bridge._cx_seed_card_digest(THREAD, d, required=True))

    def test_degraded_card_pushed_when_old_cards_exist(self):
        """庫裡有舊卡 → 一定要明說「這不是現況」,否則使用者會當成最新。"""
        d = _fresh_digest(with_old_cards=True)
        self._seed_with_timeout(d)
        texts = [str(c.get("body", {}).get("text", "")) for c in d.store.cards.values()]
        codes = [c.get("body", {}).get("error_code") for c in d.store.cards.values()]
        self.assertIn("CX_SEED_DEGRADED", codes, "沒有推降級卡 —— 舊內容會被當成現況")
        self.assertTrue(any("不是現況" in t for t in texts))
        # 舊卡不可以被刪掉(還是有參考價值,只是要標明)
        self.assertTrue(any("上週的回覆" in t for t in texts))

    def test_only_one_degraded_card_per_session(self):
        """TTL 重新 seed 會一直失敗 —— 不能每次都疊一張,聊天室會變公告欄。"""
        d = _fresh_digest(with_old_cards=True)
        for _ in range(3):
            self._seed_with_timeout(d)
        codes = [c.get("body", {}).get("error_code") for c in d.store.cards.values()]
        self.assertEqual(codes.count("CX_SEED_DEGRADED"), 1)

    def test_retry_is_scheduled_and_recovers_in_place(self):
        """會自己好的東西不該要使用者下拉 —— 背景重試,成功後把那張卡
        **原地換成**「已讀到最新」,不是再疊一張。"""
        import bridge as b
        d = _fresh_digest(with_old_cards=True)
        calls = {"n": 0}

        async def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise b.CodexAppServerError("thread/turns/list timed out")
            return {"data": []}

        b.CODEX_APP.call = flaky
        orig_delays = b._CX_SEED_RETRY_DELAYS
        b._CX_SEED_RETRY_DELAYS = (0.01,)       # 測試不要真的等 5 秒

        async def run():
            await b._cx_seed_card_digest(THREAD, d, required=True)
            t = getattr(d, "seed_retry_task", None)
            assert t is not None, "沒有排重試"
            await t
        try:
            asyncio.run(run())
        finally:
            b._CX_SEED_RETRY_DELAYS = orig_delays

        cards = list(d.store.cards.values())
        degraded = [c for c in cards if c["id"] == b._CX_DEGRADED_CARD_ID]
        self.assertEqual(len(degraded), 1, "降級卡應該只有一張(原地覆蓋)")
        self.assertIn("已讀到最新", str(degraded[0]["body"]["text"]))

    def test_recovery_allows_warning_again_next_time(self):
        """讀得到之後旗標要歸位,下次再壞還要能再講一次。"""
        d = _fresh_digest(with_old_cards=True)
        self._seed_with_timeout(d)
        self.assertTrue(getattr(d, "seed_degraded_carded", False))

        async def ok(*a, **k):
            return {"data": []}
        bridge.CODEX_APP.call = ok
        d.seeded = False
        asyncio.run(bridge._cx_seed_card_digest(THREAD, d, required=True))
        self.assertFalse(getattr(d, "seed_degraded_carded", True),
                         "seed 成功後沒有把旗標清掉,下次壞掉就不會再提醒")


if __name__ == "__main__":
    unittest.main()
