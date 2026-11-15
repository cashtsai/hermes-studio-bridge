"""CC 評分問卷蓋台的偵測(2026-11-15)。

犯案紀錄:11-13 與 11-15 兩天內兩次,「How is Claude doing this session?」
問卷佔走輸入框 → bridge 掃不到 composer → 手機端只說「不在可輸入狀態」,
使用者看不到有個框在等按鍵,session 看起來就是死了。

最重要的測試是**誤觸防護**:11-13 的對話裡原封不動聊過這句英文(在討論
這個 bug 本身),transcript 會把它渲染在畫面上 —— 單看文字必誤觸,
誤觸的代價是把 '0' 打進使用者的草稿。所以判定是雙條件:
問卷文字在**尾段** AND 輸入框真的不見了。
"""
import _isolation  # noqa: F401
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="cc-survey-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

# 真實形狀(11-15 shan-ops 實況):問卷在底部,沒有 ❯ 輸入框。
SURVEY_PANE = """  ✔ 景君宮白牌支線:BuaBue 皮 + 景君宮品牌/神尊/風格

● How is Claude doing this session? (optional)
  1: Bad    2: Fine   3: Good   0: Dismiss

────────────────────────────────────────────────────────────────────────"""

# 正常待命:有輸入框。
IDLE_PANE = """  some transcript text above

────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────
   Claude | 7d 79%"""

# 誤觸陷阱:對話內容**聊到**問卷(帶著同一句英文與 0: Dismiss),
# 但輸入框好好的在下面 —— 絕對不能判成蓋台。
QUOTED_IN_CHAT = """● 剛剛的卡住是這個框造成的:
  「How is Claude doing this session?」
  「1: Bad    2: Fine    3: Good    0: Dismiss」
  bridge 掃不到 composer 所以手機端說不在可輸入狀態

────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────
   Claude | 7d 12%"""


class TestSurveyOverlay(unittest.TestCase):
    def test_real_survey_pane_detected(self):
        self.assertTrue(bridge._cc_survey_overlay(SURVEY_PANE))

    def test_idle_pane_not_detected(self):
        self.assertFalse(bridge._cc_survey_overlay(IDLE_PANE))

    def test_survey_text_quoted_in_conversation_is_not_overlay(self):
        """**誤觸防護的核心**:聊到這個 bug 的對話會帶著一模一樣的字串,
        但輸入框在 → 不是蓋台。誤觸會把 '0' 打進使用者的草稿。"""
        self.assertFalse(bridge._cc_survey_overlay(QUOTED_IN_CHAT))

    def test_survey_scrolled_into_history_not_detected(self):
        """問卷字樣已捲出尾段(上面)、底部是正常輸入框 → 不算。"""
        pane = SURVEY_PANE + "\n" + ("\n".join("line %d" % i for i in range(20))) + \
            "\n────────────\n❯ \n────────────"
        self.assertFalse(bridge._cc_survey_overlay(pane))

    def test_reason_reports_survey_when_blocked(self):
        """自動關失敗時,reason 要說真話(survey_blocking),
        不是籠統的 composer_stuck。"""
        self.assertEqual(bridge._cc_not_accepted_reason(SURVEY_PANE),
                         "survey_blocking")
        self.assertIn("survey_blocking", bridge._CC_NOT_ACCEPTED_HINT)


if __name__ == "__main__":
    unittest.main()
