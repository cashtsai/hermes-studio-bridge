"""Console 待審核(P0)的工程守門。

兩部分,理由同 `test_console_cjk.py`:

  1. **行為**:把 `console_approval_p0.test.cjs`(複選完整集合 / style→class /
     鍵盤 a·d / approval.resolved / 能力 fail-open)接進 Python 套件,CI 一條
     線跑完。
  2. **結構**:不依賴 node 的靜態斷言 —— 這幾條是「一行就能把病裝回去」的
     改動,而且裝回去之後**不會有任何東西壞掉的跡象**,只會安靜地回到盲簽/
     按錯鍵/答案送不出去。

病史(docs/APPROVAL_HUB_SPEC.md + 2026-11-18 稽核):
  · `firstApprovalButton` 退回 `btns[btns.length - 1]` → 鍵盤 `d` 按到選單
    最後一項(可能是「本次全允許」)。
  · 審批區只有 icon + title 兩個節點 → `detail`/`risk` 到不了畫面 = 盲簽。
  · `opts.slice(0, 8)` → 第 9 個以後靜默消失。
  · 全域 SSE 連 `data.type` 都沒讀 → `approval.resolved` 被當刷新鈴。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONSOLE = ROOT / "static" / "console"


class ConsoleApprovalBehaviorTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for Console behavior checks")
    def test_shipped_console_approval_behavior(self):
        result = subprocess.run(
            [shutil.which("node"), "--test",
             str(Path(__file__).with_name("console_approval_p0.test.cjs"))],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ConsoleApprovalStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (CONSOLE / "app.js").read_text(encoding="utf-8")
        cls.html = (CONSOLE / "index.html").read_text(encoding="utf-8")
        cls.css = (CONSOLE / "style.css").read_text(encoding="utf-8")

    # ── P0-2:鍵盤 a/d ────────────────────────────────────────────────
    def test_keyboard_shortcut_never_guesses_a_button(self):
        body = self.app[self.app.index("function firstApprovalButton("):][:700]
        self.assertNotIn("btns.length - 1", body,
                         "退回最後一顆 = 鍵盤 d 按到「全部允許」;查不到語意就要回 null")
        self.assertIn("ap-no", body)
        self.assertIn(":not(:disabled)", body, "停用中的按鈕不該被快捷鍵按下去")

    def test_option_style_is_mapped_not_guessed_from_label(self):
        self.assertIn("function approvalStyleClass(", self.app)
        fn = self.app[self.app.index("function approvalStyleClass("):][:420]
        for word in ("primary", "danger", "deny"):
            self.assertIn(f'"{word}"', fn, f"style 字彙缺了 {word}")
        # bridge 負責語意標註(APPROVAL_HUB_SPEC §1),app 端永不再用 label 猜。
        for heuristic in ("拒絕", "deny_re", "toLowerCase().includes"):
            self.assertNotIn(heuristic, fn, "不准用 label 猜語意")

    # ── P0-3:detail / risk ──────────────────────────────────────────
    def test_approval_bar_has_detail_and_risk_nodes(self):
        block = re.search(r'<div id="approval".*?\n      </div>', self.html, re.S)
        self.assertIsNotNone(block, "index.html 找不到 #approval 區塊")
        for node in ("ap-detail", "ap-risk", "ap-actions", "ap-title"):
            self.assertIn(f'id="{node}"', block.group(0), f"審批區缺了 {node}")
        for node in ("ap-detail", "ap-risk"):
            self.assertIn(node, self.app, f"{node} 有容器卻沒人寫進去")
        self.assertIn(".ap-detail{", self.css.replace("\n", ""), "detail 沒有樣式會撐爆版面")

    def test_danger_key_has_its_own_visual(self):
        self.assertIn(".ap-danger{", self.css.replace("\n", ""),
                      "否決鍵是不可逆的那一邊,不能只跟允許差一點色溫")

    # ── 順手修 ───────────────────────────────────────────────────────
    def test_options_are_never_silently_truncated(self):
        self.assertNotIn("slice(0, 8)", self.app, "第 9 個以後靜默消失")
        self.assertIn("APPROVAL_MAX_OPTS", self.app)
        self.assertIn("ap-more", self.app, "真的要收就得明示「還有 N 個」")

    def test_internal_option_keys_do_not_leak_into_labels(self):
        fn = self.app[self.app.index("function approvalOptionLabel("):][:300]
        self.assertIn("[0-9]", fn, "只有 CC 的 TUI 數字鍵該顯示前綴")
        self.assertNotIn('o.key ? o.key + ". "', self.app,
                         "舊寫法會印成「approve_for_session. 本次全允許」")

    def test_global_sse_reads_the_event_type(self):
        self.assertIn("handleListEvent", self.app)
        fn = self.app[self.app.index("function handleListEvent("):][:600]
        self.assertIn('"approval.resolved"', fn,
                      "approval.resolved 是為了『收到即收卡』才加的 persisted 事件")
        self.assertIn("scheduleListRefresh", fn, "其餘型別維持 debounce,不擴大戰線")

    # ── 前置:能力驅動 ───────────────────────────────────────────────
    def test_capability_gating_is_live_on_the_local_console(self):
        fn = self.app[self.app.index("function canUse(cap)"):][:400]
        self.assertNotIn("return !borrowed() ||", fn,
                         "本機無條件放行 = bridge 宣告的 capabilities 完全沒作用")
        self.assertIn("CAP_VOCAB", fn)

    def test_capability_vocabulary_excludes_the_unreliable_negatives(self):
        vocab = re.search(r"const CAP_VOCAB = new Set\(\[(.*?)\]\)", self.app, re.S)
        self.assertIsNotNone(vocab)
        words = set(re.findall(r'"([a-z_]+)"', vocab.group(1)))
        # 這兩個的「缺席」不代表不支援 —— 放進來就是把本機現在能用的東西藏掉。
        self.assertNotIn("changes", words,
                         "changes 只有 console-remote 會宣告,本機列永遠不帶")
        self.assertNotIn("interrupt", words,
                         "hermes 列沒宣告 interrupt,但 /interrupt 的 hp 分支是通的")
        self.assertTrue({"input", "approve"} <= words, "該守的還是要守")

    def test_service_worker_version_bumped_for_changed_assets(self):
        sw = (CONSOLE / "sw.js").read_text(encoding="utf-8")
        m = re.search(r"pocket-console-shell-v(\d+)", sw)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 14,
                                "app.js/index.html/style.css 改了就要翻 SW cache 版本")


class ConsoleApprovalHarnessTests(unittest.TestCase):
    """離線預覽 harness 必須吃**出貨檔本身**,不准自己抄一份 payload 渲染器。"""

    def test_preview_harness_renders_the_shipped_code(self):
        page = CONSOLE / "__fixtures__" / "approval-preview.html"
        self.assertTrue(page.exists(), "docs/console-p0-preview 的圖是這頁算出來的")
        src = page.read_text(encoding="utf-8")
        self.assertIn('fetch("../app.js")', src, "抽段跑出貨碼,不要抄第二份")
        self.assertIn('fetch("../index.html")', src, "標記也要取自出貨檔")
        self.assertIn("renderApproval", src)
        self.assertIn("firstApprovalButton", src, "鍵盤 d 按到哪顆要當場問出來,不是手寫說明")

    def test_preview_sections_still_exist_in_app_js(self):
        """harness 用字串切段 —— 切點被改名就該在這裡紅,而不是算圖時才發現空白。"""
        app = (CONSOLE / "app.js").read_text(encoding="utf-8")
        for start, end in (("function approvalIdOf(meta) {", "// 跨 session 彙總"),
                           ("function firstApprovalButton(", "// ── IME 三重判準"),
                           ("function borrowed() {", "function syncControls("),
                           ("let listTimer = null;", "main();")):
            a = app.find(start)
            self.assertGreaterEqual(a, 0, f"切點不見了:{start}")
            self.assertGreater(app.find(end, a), a, f"結束切點不見了:{end}")


if __name__ == "__main__":
    unittest.main()
