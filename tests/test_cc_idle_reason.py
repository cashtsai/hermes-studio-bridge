"""idle 送不進去時的理由**不可以寫死** —— 寫死會把人帶往錯誤方向。

2026-10-29 實查 `rakutai`:那條 session 的 pane 上明明寫著 `100% context used`,
但 25 次失敗裡有 20 次被標成 `never_rendered_idle`,提示是
「當時是待命中,收到就會立刻開跑」—— 跟事實完全相反。真相是 context 滿了、
要先 /compact。**同一個原因被標成兩種,而多數那種在誤導**。

成因:`not confirmed and not busy` 那條路直接 `reason="never_rendered_idle"`,
從來不問畫面上寫了什麼。而 context 滿的 session 正好符合那個前提
(CLI 不收輸入、也沒有 spinner)。
"""
import _isolation  # noqa: F401
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

# 取自 rakutai 的真實畫面(2026-10-29)。
PANE_CONTEXT_FULL = """\
✢ Fluttering… (3m 59s · ↓ 9.6k tokens)
  ⎿  Next: Doc cleanup: README + CLAUDE_HANDOFF vs code
                                                              100% context used
────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
"""

PANE_PLAIN_IDLE = """\
────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
"""


class IdleReasonMustAskThePane(unittest.TestCase):
    def test_context_full_is_recognised(self):
        """畫面上寫著 100% context used,就不可以說「收到就會立刻開跑」。"""
        self.assertEqual(bridge._cc_not_accepted_reason(PANE_CONTEXT_FULL),
                         "context_full")

    def test_hint_points_at_compact_not_at_retry(self):
        hint = bridge._CC_NOT_ACCEPTED_HINT["context_full"]
        self.assertIn("compact", hint)
        # 誤導的那句不可以出現在 context 滿的提示裡。
        self.assertNotIn("立刻開跑", hint)

    def test_plain_idle_still_falls_back(self):
        """沒有特徵的空畫面仍退回原本判定 —— 修的是誤標,不是把它整個換掉。"""
        self.assertIn(bridge._cc_not_accepted_reason(PANE_PLAIN_IDLE),
                      ("composer_stuck", "awaiting_prompt"))

    def test_every_reason_has_a_hint(self):
        """新的理由會直接當 `_CC_NOT_ACCEPTED_HINT[reason]` 的鍵 ——
        少一個鍵就是 KeyError,而且是在使用者送訊息失敗的那一刻才炸。"""
        for r in ("context_full", "awaiting_prompt", "composer_stuck",
                  "composer_missing", "never_rendered_idle"):
            self.assertIn(r, bridge._CC_NOT_ACCEPTED_HINT)


if __name__ == "__main__":
    unittest.main()
