"""Gemini 回合失敗要講人話(對齊 CC/CX 的第一步)。

2026-10-11 把 Gemini 的 UI 入口藏起來時,`StudioChatUI.swift` 的註解點名兩個
理由:**quota 失敗**、**權限請求只能自動拒**。後者在 bridge 側已經補成真的核准
流程(出卡、180 秒窗口、使用者按了才放行);前者沒動過 —— 失敗一律呈現成
「Gemini 回合失敗:<原始例外前 160 字>」。

額度用完時那串字特別糟:使用者分不出「我沒配額」和「你們壞了」,也不知道要
等多久、能不能改用別的 provider。

⚠️ 這裡**刻意不報剩餘百分比**。CC/CX 的 `/app/v1/usage` 給得出數字,是因為
Codex 寫 `payload.rate_limits`、Claude 有 statusLine hook 的 json;
**Gemini 沒有任何本機用量來源**(2026-10-31 查過 `~/.gemini/`)。沒有來源就
不猜 —— 同 usage 段的信任邊界:不能推算一個看起來像官方的數字。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="gm-failure-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class GeminiFailureTextTest(unittest.TestCase):

    # ── 額度:當初把入口藏起來的那個理由 ──────────────────────────

    def test_quota_says_whose_limit_and_what_to_do(self):
        """額度用完要講三件事:是誰的限制、不是連線問題、現在能做什麼。"""
        for exc in (RuntimeError("RESOURCE_EXHAUSTED: quota exceeded"),
                    RuntimeError("429 Too Many Requests"),
                    RuntimeError("rate limit reached for model")):
            with self.subTest(exc=str(exc)):
                self.assertEqual(bridge._gemini_failure_kind(exc), "quota")
                txt = bridge._gemini_failure_text(exc)
                self.assertIn("額度", txt)
                self.assertIn("不是連線問題", txt, "要排除使用者的錯誤歸因")
                # 給得出替代路徑 —— 這件事 CC/CX 做得到,不該讓人卡死。
                self.assertTrue("CC" in txt or "CX" in txt, "要給替代選項")

    def test_quota_does_not_invent_a_percentage(self):
        """**不准猜數字。** Gemini 沒有本機用量來源,報百分比就是編的。"""
        txt = bridge._gemini_failure_text(RuntimeError("quota exceeded"))
        self.assertNotIn("%", txt)
        for n in ("0 ", "100", "剩餘"):
            self.assertNotIn(n, txt, f"不該出現看似配額數字的 {n!r}")

    # ── 其他分類 ──────────────────────────────────────────────

    def test_auth_points_at_the_terminal_login(self):
        exc = RuntimeError("UNAUTHENTICATED: invalid credentials")
        self.assertEqual(bridge._gemini_failure_kind(exc), "auth")
        self.assertIn("登入", bridge._gemini_failure_text(exc))

    def test_timeout_suggests_splitting_the_work(self):
        exc = TimeoutError("deadline exceeded")
        self.assertEqual(bridge._gemini_failure_kind(exc), "timeout")
        self.assertIn("拆小", bridge._gemini_failure_text(exc))

    def test_unavailable_blames_upstream_not_the_user(self):
        exc = RuntimeError("503 Service Unavailable")
        self.assertEqual(bridge._gemini_failure_kind(exc), "unavailable")
        self.assertIn("上游", bridge._gemini_failure_text(exc))

    # ── 認不出來的:**不能把線索吃掉** ────────────────────────

    def test_unknown_failure_keeps_the_raw_message(self):
        """認不出的分類要照舊附原始訊息。

        換成一句含糊的「發生錯誤」比技術字串更糟 —— 那是唯一的線索,
        吃掉之後查的人什麼都沒有。
        """
        exc = ValueError("some weird internal thing 7f3a")
        self.assertEqual(bridge._gemini_failure_kind(exc), "other")
        txt = bridge._gemini_failure_text(exc)
        self.assertIn("some weird internal thing 7f3a", txt)

    def test_exception_type_name_also_matches(self):
        """分類看的是「型別名 + 訊息」—— 有些實作把資訊放在型別上。"""
        class QuotaExceededError(RuntimeError):
            pass
        self.assertEqual(
            bridge._gemini_failure_kind(QuotaExceededError("nope")), "quota")

    def test_long_raw_message_is_truncated(self):
        exc = RuntimeError("x" * 500)
        txt = bridge._gemini_failure_text(exc)
        self.assertLess(len(txt), 220, "不得把整串原始訊息塞進卡片")


if __name__ == "__main__":
    unittest.main()
