"""CC 選單解析 —— 用實機擷取的 pane 當基準。

2026-07-29 事故:App 的核准卡片上「問題整句消失、混進假選項、Type something.
出現兩次」。根因是舊解析器固定往上掃 `lines[-28:]`,掃進了對話正文,把訊息裡
的編號段落(「4. 草稿存附件 — …」)當成選單選項。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
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

    def test_single_select_pane_is_not_multiselect(self):
        self.assertFalse(bridge._cc_prompt(REAL_PANE)["multiselect"])


# 實機擷取(tmux capture-pane,Pocket-Droid,2026-08-11)。多選版面(multiSelect):
# 選項帶 [ ] checkbox、說明行只縮 2 格(單選版縮 5 格)、多題 header 是「← ☐ … →」
# 頁籤列。舊解析器在這個版面上把 2 格縮排的說明誤判成問題本文 → 標題變成選項 4
# 的說明、選項 1-4 全丟,App 只剩「[ ] Type something」+「Chat about this」兩顆爛按鈕。
MULTISELECT_PANE = """────────────────────────────────────────────────────────────────────────────────
←  ☐ 追 122 範圍  ✔ Submit  →

Build 122 這輪要實作哪些?(可複選;Fleet view 我建議 defer 到 bridge 部署
registry)

❯ 1. [ ] Markdown 保真 a+c+d (清單/h5h6/diff行內)
  清單(平+巢狀)+任務清單 + h5/h6 + diff 逐字著色。使用者天天看得到、低風險、無
  bridge 依賴。建議必做。
  2. [ ] 送出耐久性 e
  outbox 落盤 + 啟動補送 + 持久化 failed flag。修「app 被殺未送訊息靜默消失」的
  correctness bug。中、低風險,有現成 CardStore 落盤範式可抄。
  3. [ ] 程式碼語法上色 b
  淨新的 tokenizer 子系統(iOS ~459
  行)。使用者可見但份量大、自成一塊。可獨立後續做。
  4. [ ] 串流增量渲染 g + CC/CX v2 f
  g=串流長卡 perf(O(n²)→O(n));f=CC/CX 控制面切 v2(input/interrupt/mode/model)。f
  有回歸風險且需先驗 bridge 是否收 CC/CX v2 session id。
  5. [ ] Type something
     Submit
────────────────────────────────────────────────────────────────────────────────
  6. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
"""


class CCMultiSelectMenuParseTests(unittest.TestCase):
    """回歸:2026-08-11 的實際炸法 —— 多選版面 2 格縮排說明被當成問題本文。"""

    def test_title_is_the_question_not_an_option_description(self):
        p = bridge._cc_prompt(MULTISELECT_PANE)
        self.assertIsNotNone(p)
        self.assertIn("Build 122 這輪要實作哪些?", p["title"])
        # 炸開當下的實際症狀:標題被換成選項 4 的說明
        self.assertNotIn("g=串流長卡", p["title"])

    def test_all_six_options_survive(self):
        opts = bridge._cc_prompt(MULTISELECT_PANE)["options"]
        self.assertEqual([o["key"] for o in opts],
                         ["1", "2", "3", "4", "5", "6"])

    def test_checkbox_prefix_is_stripped_and_flagged(self):
        p = bridge._cc_prompt(MULTISELECT_PANE)
        self.assertTrue(p["multiselect"])
        labels = [o["label"] for o in p["options"]]
        for lbl in labels:
            self.assertFalse(lbl.startswith("["), lbl)
        self.assertEqual(labels[0], "Markdown 保真 a+c+d (清單/h5h6/diff行內)")
        self.assertEqual(labels[4], "Type something")
        self.assertEqual(labels[5], "Chat about this")

    def test_two_space_descriptions_attach_to_their_option(self):
        opts = bridge._cc_prompt(MULTISELECT_PANE)["options"]
        self.assertIn("低風險、無 bridge 依賴", opts[0]["description"])
        self.assertIn("CardStore 落盤範式可抄", opts[1]["description"])
        # 說明折行要接起來(選項 3 的說明在「459 / 行)」處折行)
        self.assertIn("iOS ~459 行", opts[2]["description"])
        self.assertIn("f 有回歸風險", opts[3]["description"])

    def test_tab_chip_row_never_leaks_into_the_title(self):
        p = bridge._cc_prompt(MULTISELECT_PANE)
        self.assertNotIn("追 122 範圍", p["title"])
        self.assertNotIn("Submit", p["title"])

    def test_wrapped_question_lines_join(self):
        p = bridge._cc_prompt(MULTISELECT_PANE)
        self.assertIn("bridge 部署 registry)", p["title"])


if __name__ == "__main__":
    unittest.main()
