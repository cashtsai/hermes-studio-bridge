"""Console 的中文(CJK)工程守門。

兩部分:
  1. 行為:把 `console_cjk_input.test.cjs`(IME 三重判準 + 搜尋摺疊)接進
     Python 套件,CI 一條線跑完。
  2. 結構:不依賴 node 的靜態斷言 —— 這幾條是「一行就能毀掉整類中文體驗」
     的改動,被順手改掉時不會有任何功能壞掉的跡象,只會安靜地變醜/誤送。

為什麼等寬字體要盯:等寬 stack 尾端若留著泛型 `monospace`,Windows 會把
CJK code point 經泛型字族解析到 SimSun(新細明體/宋體)—— 程式碼框裡一混
中文就變襯線體。這是只有 CJK 團隊會知道、而且沒人會回報成 bug 的事。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONSOLE = ROOT / "static" / "console"


class ConsoleCjkBehaviorTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for Console behavior checks")
    def test_shipped_console_ime_guard_and_search_folding(self):
        result = subprocess.run(
            [shutil.which("node"), "--test",
             str(Path(__file__).with_name("console_cjk_input.test.cjs"))],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ConsoleCjkTypographyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = (CONSOLE / "style.css").read_text(encoding="utf-8")
        cls.app = (CONSOLE / "app.js").read_text(encoding="utf-8")
        cls.term = (CONSOLE / "terminal.js").read_text(encoding="utf-8")
        cls.mem = (CONSOLE / "memory.js").read_text(encoding="utf-8")

    def _font_stacks(self, var_name):
        """抓 `--mono:` / `--sans:` 的值(可跨行)。"""
        m = re.search(r"--" + var_name + r":\s*([^;]+);", self.css, re.S)
        self.assertIsNotNone(m, f"--{var_name} 不見了")
        return " ".join(m.group(1).split())

    def test_mono_stack_has_no_bare_monospace_tail(self):
        mono = self._font_stacks("mono")
        # `ui-monospace` 是具名的 UI 等寬字族,不是泛型 —— 允許。泛型
        # `monospace` 出現在名單任何位置都不行:Windows 會從那裡掉進 SimSun。
        names = [n.strip().strip('"\'') for n in mono.split(",")]
        self.assertNotIn("monospace", names,
                         "等寬 stack 不准含泛型 monospace(Windows CJK → SimSun):" + mono)

    def test_mono_stack_names_cjk_faces_traditional_first(self):
        mono = self._font_stacks("mono")
        self.assertIn("PingFang TC", mono, "等寬 stack 必須明確點名 CJK 字面")
        self.assertIn("Microsoft JhengHei", mono, "Windows 的繁中字面(微軟正黑體)")

    def test_sans_stack_puts_traditional_chinese_before_simplified(self):
        sans = self._font_stacks("sans")
        for tc, sc in (("PingFang TC", "PingFang SC"),
                       ("Microsoft JhengHei", "Microsoft YaHei")):
            if sc in sans:
                self.assertLess(sans.index(tc), sans.index(sc),
                                f"主場是台灣:{tc} 必須排在 {sc} 之前")
            self.assertIn(tc, sans)

    def test_no_component_reintroduces_a_generic_monospace_stack(self):
        # 元件層若自己寫一份 stack,上面的守門就被繞過 —— 一律引用 var(--mono)。
        offenders = [line.strip() for line in self.css.splitlines()
                     if "font-family" in line and re.search(r"\bmonospace\b", line)]
        self.assertEqual(offenders, [],
                         "元件請用 var(--mono),不要自己拼等寬 stack:" + str(offenders))

    def test_body_line_height_is_cjk_safe(self):
        # 中文正文行高下限 1.7(漢字無降部、字面滿框,1.5 會黏成一片)。
        m = re.search(r"\nbody\{[^}]*line-height:([\d.]+)", self.css, re.S)
        self.assertIsNotNone(m, "body 的 line-height 不見了")
        self.assertGreaterEqual(float(m.group(1)), 1.7, "中文正文行高不得低於 1.7")


class ConsoleImeWiringTests(unittest.TestCase):
    """每個會「按 Enter 就動作」的輸入框都必須過 IME 守衛。"""

    @classmethod
    def setUpClass(cls):
        cls.app = (CONSOLE / "app.js").read_text(encoding="utf-8")
        cls.term = (CONSOLE / "terminal.js").read_text(encoding="utf-8")
        cls.mem = (CONSOLE / "memory.js").read_text(encoding="utf-8")

    def test_ime_guard_implements_all_three_criteria(self):
        for needle in ("isComposing", "keyCode === 229", "recentlyComposing"):
            self.assertIn(needle, self.app, f"IME 三重判準缺了 {needle}")
        self.assertIn("composingUntil", self.app,
                      "Safari 的確認 keydown 在 compositionend 之後才到,必須有時間窗")

    def test_guard_is_shared_not_reimplemented(self):
        self.assertIn("window.PocketIME", self.app, "守衛必須導出給其他面共用")
        for name, src in (("terminal.js", self.term), ("memory.js", self.mem)):
            self.assertIn("PocketIME", src, f"{name} 的 Enter 處理沒接上 IME 守衛")

    def test_every_enter_handler_is_guarded(self):
        """掃全部 console JS:任何 `e.key === "Enter"` 的動作分支附近都要有守衛。

        漏掉一個就是一個「中文選字誤送」的洞(dsh 自己也漏了一個 ShareDialog)。
        """
        for path in sorted(CONSOLE.glob("*.js")):
            src = path.read_text(encoding="utf-8")
            for m in re.finditer(r'\.key === "Enter"', src):
                # 取該 handler 前 600 字檢查守衛(同一個 addEventListener 區塊內)
                window = src[max(0, m.start() - 600):m.start()]
                self.assertTrue(
                    "imeKey" in window or "PocketIME" in window,
                    f"{path.name} 第 {src[:m.start()].count(chr(10)) + 1} 行的 Enter "
                    f"處理沒有 IME 守衛 —— 中文選字會誤觸")

    def test_service_worker_version_bumped_for_changed_assets(self):
        sw = (CONSOLE / "sw.js").read_text(encoding="utf-8")
        m = re.search(r"pocket-console-shell-v(\d+)", sw)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 13,
                                "app.js/md.js/style.css 改了就要翻 SW cache 版本")


if __name__ == "__main__":
    unittest.main()
