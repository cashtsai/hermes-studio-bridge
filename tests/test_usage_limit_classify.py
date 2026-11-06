"""usage_limit 錯誤分類(2026-11-06,t3code 坑清單:「quota 錯誤要分類為
usage_limit + reset 時間」)。

三家 provider 的限額文案 → 分類正確 + reset 解析;普通錯誤不誤判;
卡片流 error 卡附 meta {"error_kind","reset_at"};session.status 蓋
phase="usage_limited"(新回合開始即解除)。
"""
import _isolation  # noqa: F401
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carddigest  # noqa: E402
import cc_sdk      # noqa: E402

_FUTURE_EPOCH = 1893456000  # 2030-01-01(digest 測試要用「未來」的 reset)


class TestClassifier(unittest.TestCase):
    # ── CC(Claude Code)樣本 ──
    def test_cc_sdk_pipe_epoch(self):
        meta = carddigest.classify_error(
            "Claude AI usage limit reached|1762387200")
        self.assertEqual(meta["error_kind"], "usage_limit")
        self.assertEqual(meta["reset_at"], 1762387200.0)

    def test_cc_tui_resets_clock(self):
        now = time.time()
        meta = carddigest.classify_error(
            "5-hour limit reached ∙ resets 3pm", now)
        self.assertEqual(meta["error_kind"], "usage_limit")
        self.assertIsNotNone(meta["reset_at"])
        self.assertGreater(meta["reset_at"], now)
        self.assertLessEqual(meta["reset_at"], now + 86400 + 60)

    def test_cc_tz_wording(self):
        now = time.time()
        meta = carddigest.classify_error(
            "Claude usage limit reached. Your limit will reset at 7pm "
            "(Asia/Taipei).", now)
        self.assertEqual(meta["error_kind"], "usage_limit")
        self.assertIsNotNone(meta["reset_at"])

    # ── CX(codex app-server)樣本 ──
    def test_cx_hit_usage_limit_relative(self):
        now = time.time()
        meta = carddigest.classify_error(
            "You've hit your usage limit. Try again in 4 days 2 hours.", now)
        self.assertEqual(meta["error_kind"], "usage_limit")
        self.assertAlmostEqual(meta["reset_at"] - now,
                               4 * 86400 + 2 * 3600, delta=2)

    def test_cx_rate_limit_no_reset(self):
        meta = carddigest.classify_error(
            "Rate limit reached. Please try again later.")
        self.assertEqual(meta["error_kind"], "usage_limit")
        self.assertIsNone(meta["reset_at"], "解析不到時間要誠實 null")

    # ── agy / Gemini 樣本 ──
    def test_gemini_resource_exhausted(self):
        meta = carddigest.classify_error(
            "RESOURCE_EXHAUSTED: Quota exceeded for quota metric "
            "'Gemini 2.5 Pro Requests'")
        self.assertEqual(meta["error_kind"], "usage_limit")
        self.assertIsNone(meta["reset_at"])

    def test_gemini_retry_in_seconds(self):
        now = time.time()
        meta = carddigest.classify_error(
            "429 RESOURCE_EXHAUSTED. Please retry in 32.5s.", now)
        self.assertEqual(meta["error_kind"], "usage_limit")
        self.assertAlmostEqual(meta["reset_at"] - now, 32.5, delta=1)

    # ── 普通錯誤不誤判 ──
    def test_ordinary_errors_not_classified(self):
        for text in (
            "Prompt is too long: context window exceeded",
            "command failed with exit code 1",
            "authentication_error: invalid api key",
            "no conversation found",
            "File size limit exceeded for attachment",
            "",
        ):
            self.assertIsNone(carddigest.classify_error(text), text)


class TestCxDigest(unittest.TestCase):
    def test_usage_limit_error_card_and_phase(self):
        d = carddigest.CodexThreadDigest()
        d.handle("turn/started", {"turn": {"id": "t1"}})
        d.handle("turn/completed", {"turn": {
            "id": "t1",
            "error": {"message":
                      "You've hit your usage limit. Try again in 2 hours."}}})
        metas = [c["body"].get("meta") for c in d.store.cards.values()
                 if (c.get("body") or {}).get("meta")]
        self.assertEqual(len(metas), 1)
        self.assertEqual(metas[0]["error_kind"], "usage_limit")
        self.assertAlmostEqual(metas[0]["reset_at"] - time.time(),
                               7200, delta=10)
        self.assertEqual(d.store.status["phase"], "usage_limited")
        self.assertIsNotNone(d.store.status["usage_limit_reset_at"])
        # 新回合開得起來 = 限額已解,phase 回 run
        d.handle("turn/started", {"turn": {"id": "t2"}})
        self.assertEqual(d.store.status["phase"], "run")
        self.assertIsNone(d.usage_limit)

    def test_ordinary_error_keeps_idle(self):
        d = carddigest.CodexThreadDigest()
        d.handle("turn/started", {"turn": {"id": "t1"}})
        d.handle("turn/completed", {"turn": {
            "id": "t1", "error": {"message": "sandbox denied: exit 1"}}})
        self.assertEqual(d.store.status["phase"], "idle")
        metas = [c for c in d.store.cards.values()
                 if (c.get("body") or {}).get("meta")]
        self.assertEqual(metas, [])


class TestPersonaDigest(unittest.TestCase):
    def test_gemini_quota_error(self):
        d = carddigest.GeminiDigest()
        d.turn_begin("c1")
        d.turn_end("c1", "", error=(
            "RESOURCE_EXHAUSTED: Quota exceeded for quota metric "
            "'Gemini 2.5 Pro Requests'"))
        err = [c for c in d.store.cards.values() if c["id"].endswith("-err")]
        self.assertEqual(err[0]["body"]["meta"]["error_kind"], "usage_limit")
        self.assertIsNone(err[0]["body"]["meta"]["reset_at"])
        self.assertEqual(d.store.status["phase"], "usage_limited")
        self.assertIsNone(d.store.status["usage_limit_reset_at"])
        d.turn_begin("c2")
        self.assertEqual(d.store.status["phase"], "run")

    def test_ordinary_error_keeps_idle(self):
        d = carddigest.PersonaDigest()
        d.turn_begin("c1")
        d.turn_end("c1", "", error="工具失敗:exit 1")
        self.assertEqual(d.store.status["phase"], "idle")


class TestCc2Digest(unittest.TestCase):
    def test_cc_usage_limit_pipe_format(self):
        dg = cc_sdk.CC2Digest("t")
        dg.turn_begin("turn-1")
        dg.turn_end(error=f"Claude AI usage limit reached|{_FUTURE_EPOCH}")
        self.assertEqual(dg.store.status["phase"], "usage_limited")
        self.assertEqual(dg.store.status["usage_limit_reset_at"],
                         float(_FUTURE_EPOCH))
        metas = [c["body"]["meta"] for c in dg.store.cards.values()
                 if (c.get("body") or {}).get("meta")]
        self.assertEqual(metas[0]["error_kind"], "usage_limit")
        dg.turn_begin("turn-2")
        self.assertEqual(dg.store.status["phase"], "run")

    def test_hint_path_uses_raw_classification(self):
        # _run_turn_safe 的人話 hint 不含英文樣式 —— 分類先吃原始例外,
        # 預掛在 digest.usage_limit,turn_end 的卡仍要帶 meta。
        dg = cc_sdk.CC2Digest("t2")
        dg.turn_begin("turn-1")
        dg.usage_limit = carddigest.classify_error(
            f"usage limit reached|{_FUTURE_EPOCH}")
        dg.turn_end(error="Claude 額度或速率上限擋住了這一輪。")
        self.assertEqual(dg.store.status["phase"], "usage_limited")
        metas = [c["body"].get("meta") for c in dg.store.cards.values()
                 if (c.get("body") or {}).get("meta")]
        self.assertEqual(metas[0]["error_kind"], "usage_limit")

    def test_ordinary_error_keeps_idle(self):
        dg = cc_sdk.CC2Digest("t3")
        dg.turn_begin("turn-1")
        dg.turn_end(error="RuntimeError: boom")
        self.assertEqual(dg.store.status["phase"], "idle")


if __name__ == "__main__":
    unittest.main()
