"""CC 選單解析 —— 用實機擷取的 pane 當基準。

2026-07-29 事故:App 的核准卡片上「問題整句消失、混進假選項、Type something.
出現兩次」。根因是舊解析器固定往上掃 `lines[-28:]`,掃進了對話正文,把訊息裡
的編號段落(「4. 草稿存附件 — …」)當成選單選項。
"""
import unittest

import bridge


# 實機擷取(tmux capture-pane,cc-65bc73e9,2026-07-29)。縮排與框線原樣保留 ——
# 這份 fixture 的價值就在「跟真的一模一樣」,請勿手動美化。
REAL_PANE = """  Running 1 shell command…
  ⎿  $ S=/private/tmp/claude-502/-Users-xcash/ae903608-2459-48fc-ae20-3ca28da2b7
     38/scratchpad; nohup zsh -c 'sleep 6; for i in 1 2 3 4 5 6 7 8; do tmux
     capture-pane -p -t cc-65bc73e9 > '"$S"'/pane-live-$i.txt 2>/dev/null; sleep
     4; done' >/dev/null 2>&1 & echo "背景擷取已掛（每 4 秒一張，共 8 張）"
────────────────────────────────────────────────────────────────────────────────
 ☐ 優先序

Pocket 核准卡片這個 bug，要不要插隊到四個 UX 項前面？

❯ 1. 先修這個 bug（推薦）
     它擋住你用手機指揮我本身。修完 bridge 解析 + app
     顯示問題與選項說明，再回頭做頁籤滑動、雙擊、上傳進度、草稿附件。
  2. 四個 UX 先做完
     這個 bug 排到後面。但在那之前我每次問你問題，你在 Pocket
     都只看得到標籤、看不到問題與說明。
  3. 兩邊一起，但只修解析
     bridge 端修掛問題不見、假選項、重複 key（不動 app，不用出新
     build）；選項說明要 app 配合，跟 UX 那四項一起出。
  4. Type something.
────────────────────────────────────────────────────────────────────────────────
  5. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
"""


class CCPromptMenuParseTests(unittest.TestCase):
    def test_real_pane_keeps_the_question(self):
        p = bridge._cc_prompt(REAL_PANE)
        self.assertIsNotNone(p)
        self.assertEqual(p["semantic"], "question")
        self.assertEqual(p["title"],
                         "Pocket 核准卡片這個 bug，要不要插隊到四個 UX 項前面？")

    def test_real_pane_options_are_exactly_the_menu(self):
        opts = bridge._cc_prompt(REAL_PANE)["options"]
        self.assertEqual([o["key"] for o in opts], ["1", "2", "3", "4", "5"])
        self.assertEqual(opts[0]["label"], "先修這個 bug（推薦）")
        self.assertEqual(opts[3]["label"], "Type something.")
        self.assertEqual(opts[4]["label"], "Chat about this")

    def test_option_descriptions_survive(self):
        """說明是使用者做決定的依據 —— 舊版整段丟掉,只剩標籤等於沒得選。"""
        opts = bridge._cc_prompt(REAL_PANE)["options"]
        self.assertIn("它擋住你用手機指揮我本身", opts[0]["description"])
        self.assertIn("顯示問題與選項說明", opts[0]["description"])   # 跨行要接起來
        self.assertIn("不動 app，不用出新", opts[2]["description"])
        self.assertEqual(opts[3]["description"], "")                  # 內建項無說明

    def test_border_between_options_does_not_end_the_block(self):
        """TUI 會在選項之間畫框線(第 4、5 項中間),不能拿框線當停止點。"""
        opts = bridge._cc_prompt(REAL_PANE)["options"]
        self.assertEqual(len(opts), 5, "框線截斷了選單 → 最後一項會消失")

    def test_numbered_prose_above_the_menu_is_not_an_option(self):
        """回歸:2026-07-29 的實際炸法 —— 訊息正文的編號段落被當成選項。"""
        prose = (
            "3. 上傳進度 — 最麻煩，而且有個你該知道的真相：現在六個輸入列\n"
            "4. 草稿存附件 — 可做。附件本體是純 Data 且已 Codable，repo 裡 OfflineOutbox\n"
            "\n"
        )
        p = bridge._cc_prompt(prose + REAL_PANE)
        self.assertEqual(p["title"],
                         "Pocket 核准卡片這個 bug，要不要插隊到四個 UX 項前面？")
        labels = [o["label"] for o in p["options"]]
        for bad in labels:
            self.assertNotIn("草稿存附件", bad)
            self.assertNotIn("上傳進度", bad)
        self.assertEqual([o["key"] for o in p["options"]], ["1", "2", "3", "4", "5"])

    def test_duplicate_keys_collapse(self):
        """同號重複只留第一個 —— 假選項撞號是「Type something. 出現兩次」的成因。"""
        pane = (
            "問題？\n"
            "  1. 甲\n"
            "  2. 乙\n"
            "  2. 乙的分身\n"
            "Enter to select · Esc to cancel\n"
        )
        opts = bridge._cc_prompt(pane)["options"]
        self.assertEqual([o["key"] for o in opts], ["1", "2"])
        self.assertEqual(opts[1]["label"], "乙")

    def test_busy_pane_is_never_a_prompt(self):
        self.assertIsNone(bridge._cc_prompt(
            REAL_PANE + "\n· Fermenting… (1m 51s · ↓ 6.5k tokens)\n"))


if __name__ == "__main__":
    unittest.main()
