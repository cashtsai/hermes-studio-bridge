"""CX 用量條爆表 + 預覽停在開場白 + updatedAt 落後 —— 2026-08-15 實機回報。

實機數字(Cashcamp thread 019f39d3):
  used = 517,323,425 / size = 258,400 → **200,203%**,其中 494,226,944(96%)
  是同一段 context 被反覆 cache 重讀灌出來的累計值。
  preview 停在 07-07 的開場白(codex 的 state DB 裡 preview 與
  first_user_message 一字不差)。
  updatedAt bridge 報 07-07,codex 自己的 DB 是 08-15 —— 差 39 天。
"""
import unittest
from unittest import mock

import bridge


WINDOW = 258_400


def tu(input_tokens, cached=0, output=0, total=None, window=WINDOW):
    """codex 的 tokenUsage 形狀(**累計**,不是單輪)。"""
    return {
        "inputTokens": input_tokens,
        "cachedInputTokens": cached,
        "outputTokens": output,
        "totalTokens": total if total is not None else input_tokens + output,
        "modelContextWindow": window,
    }


class ContextUsedTests(unittest.TestCase):
    """`used` 要是「當前上下文佔用」,與 size 同一把尺。"""

    def test_delta_of_input_tokens_is_the_context(self):
        prev = tu(1_000_000, cached=900_000)
        cur = tu(1_120_000, cached=1_000_000)
        self.assertEqual(bridge._codex_context_used(cur, prev), 120_000)

    def test_no_previous_snapshot_gives_none(self):
        """算不出就別猜 —— 寧可不顯示,也不要顯示差兩三個數量級的數字。"""
        self.assertIsNone(bridge._codex_context_used(tu(500_000), None))

    def test_delta_is_clamped_to_the_window(self):
        """一個 turn 可能有多次模型請求,增量會超過視窗;夾到上限而不是畫 >100%。"""
        self.assertEqual(
            bridge._codex_context_clamp(686_279, WINDOW), WINDOW)
        self.assertEqual(bridge._codex_context_clamp(120_000, WINDOW), 120_000)
        self.assertIsNone(bridge._codex_context_clamp(None, WINDOW))

    def test_zero_or_negative_delta_gives_none(self):
        """thread 被 compact / app-server 重啟後累計會歸零或倒退。"""
        prev = tu(900_000)
        self.assertIsNone(bridge._codex_context_used(tu(900_000), prev))
        self.assertIsNone(bridge._codex_context_used(tu(10_000), prev))

    def test_regression_lifetime_total_is_never_used_as_context(self):
        """回歸:實機那組數字不得再產生 >100% 的用量條。"""
        prev = tu(515_000_000, cached=494_000_000, output=1_600_000,
                  total=516_600_000)
        cur = tu(515_686_279, cached=494_226_944, output=1_637_146,
                 total=517_323_425)
        out = bridge._codex_usage_map(cur, prev=prev)
        self.assertIsNotNone(out)
        self.assertLessEqual(out["used"], out["size"],
                             "用量條又爆表了(used 取到生命週期累計)")
        # 兩次通知之間夾了多輪請求 → 增量會超過視窗,夾到上限代表「已經滿了」
        # 累計值仍要保留 —— 花費是靠它算的
        self.assertEqual(out["total_tokens"], 517_323_425)


class UsageShapeTests(unittest.TestCase):
    def test_used_is_omitted_when_not_computable(self):
        """app 的 ContextUsage.used 是 optional,缺欄位 → 整條用量條不畫。"""
        out = bridge._codex_usage_map(tu(500_000), prev=None)
        self.assertIsNotNone(out)
        self.assertNotIn("used", out)
        self.assertEqual(out["size"], WINDOW, "size 仍要給(其他用途)")

    def test_cost_fields_survive_without_used(self):
        out = bridge._codex_usage_map(tu(500_000, cached=400_000, output=1_000),
                                      prev=None)
        self.assertEqual(out["input_tokens"], 500_000)
        self.assertEqual(out["cache_read_tokens"], 400_000)
        self.assertEqual(out["output_tokens"], 1_000)

    def test_used_present_and_sane_when_computable(self):
        out = bridge._codex_usage_map(tu(220_000, cached=200_000),
                                      prev=tu(100_000, cached=90_000))
        self.assertEqual(out["used"], 120_000)
        self.assertLess(out["used"], out["size"])


class _FakeStore:
    def __init__(self, cards):
        self.cards = {c["id"]: c for c in cards}
        self.order = [c["id"] for c in cards]


class _FakeDigest:
    def __init__(self, store):
        self.store = store


class LatestPreviewTests(unittest.TestCase):
    """preview 不該永遠是開場白。"""

    def test_newest_card_text_wins(self):
        d = _FakeDigest(_FakeStore([
            {"id": "c1", "body": {"fallback_text": "開場白"}},
            {"id": "c2", "body": {"fallback_text": "最新的一句話"}},
        ]))
        with mock.patch.dict(bridge._CX_CARD_DIGESTS, {"t1": d}, clear=False):
            self.assertEqual(bridge._cx_latest_card_text("t1"), "最新的一句話")

    def test_skips_empty_cards(self):
        d = _FakeDigest(_FakeStore([
            {"id": "c1", "body": {"fallback_text": "有內容"}},
            {"id": "c2", "body": {"fallback_text": "   "}},
        ]))
        with mock.patch.dict(bridge._CX_CARD_DIGESTS, {"t1": d}, clear=False):
            self.assertEqual(bridge._cx_latest_card_text("t1"), "有內容")

    def test_no_digest_is_empty_not_crash(self):
        """沒開過卡片流的 thread 維持原樣,不為了預覽多打一次 API。"""
        self.assertEqual(bridge._cx_latest_card_text("不存在的-thread"), "")

    def test_whitespace_is_collapsed(self):
        d = _FakeDigest(_FakeStore([
            {"id": "c1", "body": {"fallback_text": "多行\n\n  內容"}},
        ]))
        with mock.patch.dict(bridge._CX_CARD_DIGESTS, {"t1": d}, clear=False):
            self.assertEqual(bridge._cx_latest_card_text("t1"), "多行 內容")


if __name__ == "__main__":
    unittest.main()
