"""CC AskUserQuestion 多選作答 —— 版面解析 + 送出序列。

所有 fixture 都是 2026-08-11 從 **獨立開的** tmux session(Claude Code 2.1.207,
沒有碰任何人正在用的 session)逐格 `capture-pane` 抓回來的原樣輸出。縮排、
框線、全形字都照抄,請勿手動美化 —— 這份檔案的價值就在「跟真的一模一樣」。

背景:PR #83 修好了多選版面的**解析**(縮排門檻、剝 checkbox、multiselect 旗標),
但「怎麼送出」完全沒修。舊路徑 `_cc_key_core` 對 semantic=="question" 一律
「送數字 + 0.08s 後送 Enter」,那是**單選**版面的語意;在多選版面:

- 數字 = 切換第 N 列的勾選(不移動游標、不送出)
- Enter = 切換游標所在那一列
- 真正的送出鈕是清單下面那一列不帶編號的 `     Submit`

所以數字+Enter 的結果是「勾了 A 又順手切掉 B」,而且永遠不會真的送出。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import asyncio
import contextlib
import os
import unittest
from unittest import mock

os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")   # bridge 在 import 時讀

import bridge  # noqa: E402


# ── 實機 fixture ①:單題多選,尚未勾選任何選項(游標在第 1 列)───────────────
MS_FRESH = """────────────────────────────────────────────────────────────────────────────────
←  ☐ Snack pick  ✔ Submit  →

Which snack(s) do you want?

❯ 1. [ ] Chips
  Salty, crunchy — classic choice
  2. [ ] Chocolate
  Sweet treat, candy bar or chocolate bites
  3. [ ] Fruit
  Fresh and light option
  4. [ ] Popcorn
  Light, crunchy, great for snacking
  5. [ ] Type something
     Submit
────────────────────────────────────────────────────────────────────────────────
  6. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
"""

# ── 實機 fixture ②:同一題,送過 "1" 與 "3" 之後 ───────────────────────────
# 重點:兩個 checkbox 變成 [✔]、游標**仍然停在第 1 列**(數字不移動游標),
# 而且頁籤列的 ☐ 變成 ☒(該題已有作答)。
MS_TOGGLED = MS_FRESH.replace("☐ Snack pick", "☒ Snack pick") \
                     .replace("1. [ ] Chips", "1. [✔] Chips") \
                     .replace("3. [ ] Fruit", "3. [✔] Fruit")

# ── 實機 fixture ③:游標走到送出鈕上(Down 走完 5 列後再一次 Down)──────────
MS_SUBMIT_FOCUSED = MS_TOGGLED.replace("❯ 1. [✔] Chips", "  1. [✔] Chips") \
                              .replace("     Submit", "❯    Submit")

# ── 實機 fixture ④:游標停在自由輸入列 —— 這時候送數字會被打進輸入框 ────────
# 實測:在這個狀態送 "1",畫面變成 `❯ 4. [✔] 1`(那個 1 是文字,不是勾選)。
MS_INPUT_FOCUSED = MS_FRESH.replace("❯ 1. [ ] Chips", "  1. [ ] Chips") \
                           .replace("  5. [ ] Type something",
                                    "❯ 5. [ ] Type something")

# ── 實機 fixture ⑤:按下送出鈕後的確認頁。注意**沒有** "Enter to select"
# footer —— `_cc_prompt` 看不到這一頁,只能用文字認。
MS_REVIEW = """────────────────────────────────────────────────────────────────────────────────
←  ☒ Snack pick  ✔ Submit  →

Review your answers

 ● Which snack(s) do you want?
   → Chips, Fruit

Ready to submit your answers?

❯ 1. Submit answers
  2. Cancel
"""

# ── 實機 fixture ⑥:三題 ask 的第一題。送出鈕寫 "Next"(不是最後一題)。─────
MS_Q1_OF_3 = """────────────────────────────────────────────────────────────────────────────────
←  ☐ Fruits  ☐ Drinks  ☐ Desserts  ✔ Submit  →

Which fruits do you want?

❯ 1. [ ] Apple
  Crisp and classic
  2. [ ] Banana
  Sweet and portable
  3. [ ] Berries
  Strawberries, blueberries, etc.
  4. [ ] Type something
     Next
────────────────────────────────────────────────────────────────────────────────
  5. Chat about this

Enter to select · Tab/Arrow keys to navigate · Esc to cancel
"""

# ── 實機 fixture ⑦/⑧:帶 ANSI 的頁籤列(`capture-pane -pe`)。純文字兩者
# 完全相同,只有背景色不同 —— 這就是 q_index 解不出來的原因。
TAB_ANSI_ON_Q1 = ("← \x1b[38;5;16m\x1b[48;5;153m ☒ Fruits \x1b[39m\x1b[49m"
                  " ☐ Drinks  ☐ Desserts  ✔ Submit  →")
TAB_ANSI_ON_Q2 = ("\x1b[39m←  ☒ Fruits \x1b[38;5;16m\x1b[48;5;153m ☐ Drinks "
                  "\x1b[39m\x1b[49m ☐ Desserts  ✔ Submit  →")


def _reset_answer_idem():
    """清掉 `/answer` 的行程級冪等快取與 per-session 序列鎖。"""
    bridge._CC_ANSWER_DONE.clear()
    bridge._CC_ANSWER_LAST_SIG.clear()
    bridge._CC_ANSWER_CLIENT.clear()
    bridge._CC_SEQ_LOCKS.clear()


class SubmitRowTests(unittest.TestCase):
    """送出鈕那一列 —— bridge 對外唯一的送出途徑,舊版根本不認得它。"""

    def test_unfocused_submit_row_is_found(self):
        lines = MS_FRESH.splitlines()
        footer = max(i for i, ln in enumerate(lines) if "enter to select" in ln.lower())
        sub = bridge._cc_submit_row(lines, footer)
        self.assertIsNotNone(sub)
        self.assertEqual(sub["label"], "Submit")
        self.assertFalse(sub["focused"])

    def test_focused_submit_row_is_detected(self):
        lines = MS_SUBMIT_FOCUSED.splitlines()
        footer = max(i for i, ln in enumerate(lines) if "enter to select" in ln.lower())
        self.assertTrue(bridge._cc_submit_row(lines, footer)["focused"])

    def test_next_label_when_more_questions_remain(self):
        lines = MS_Q1_OF_3.splitlines()
        footer = max(i for i, ln in enumerate(lines) if "enter to select" in ln.lower())
        self.assertEqual(bridge._cc_submit_row(lines, footer)["label"], "Next")

    def test_single_select_layout_has_no_submit_row(self):
        from tests.test_cc_prompt_menu_parse import REAL_PANE
        lines = REAL_PANE.splitlines()
        footer = max(i for i, ln in enumerate(lines) if "enter to select" in ln.lower())
        self.assertIsNone(bridge._cc_submit_row(lines, footer))

    def test_tab_bar_submit_chip_is_not_the_submit_row(self):
        """多題頁籤列上的 `✔ Submit` 是頁籤,不是那顆內嵌送出鈕。"""
        lines = MS_Q1_OF_3.splitlines()
        footer = max(i for i, ln in enumerate(lines) if "enter to select" in ln.lower())
        self.assertEqual(lines[bridge._cc_submit_row(lines, footer)["line"]].strip(),
                         "Next")


class SubmitRowNotADescriptionTests(unittest.TestCase):
    """回歸:`     Submit` 5 格縮排,舊解析器會把它黏成上一個選項的說明。"""

    def test_submit_row_does_not_become_a_description(self):
        opts = bridge._cc_prompt(MS_FRESH)["options"]
        by_key = {o["key"]: o for o in opts}
        self.assertEqual(by_key["5"]["label"], "Type something")
        self.assertEqual(by_key["5"]["description"], "")

    def test_next_row_does_not_become_a_description(self):
        opts = bridge._cc_prompt(MS_Q1_OF_3)["options"]
        self.assertEqual({o["key"]: o["description"] for o in opts}["4"], "")


class CheckedStateTests(unittest.TestCase):
    """勾選狀態要解出來 —— /answer 靠它算「現況 ⊕ 目標」的差集。"""

    def test_fresh_pane_has_nothing_checked(self):
        p = bridge._cc_prompt(MS_FRESH)
        self.assertEqual(bridge._cc_ms_checked(p), set())

    def test_toggled_pane_reports_exactly_the_checked_keys(self):
        p = bridge._cc_prompt(MS_TOGGLED)
        self.assertEqual(bridge._cc_ms_checked(p), {"1", "3"})

    def test_chat_about_this_has_no_checkbox_state(self):
        opts = {o["key"]: o for o in bridge._cc_prompt(MS_FRESH)["options"]}
        self.assertNotIn("checked", opts["6"])
        self.assertIn("checked", opts["5"])   # 自由輸入列有勾選框

    def test_single_select_options_have_no_checked_field(self):
        from tests.test_cc_prompt_menu_parse import REAL_PANE
        for o in bridge._cc_prompt(REAL_PANE)["options"]:
            self.assertNotIn("checked", o)


class FocusRowTests(unittest.TestCase):
    """游標在哪 —— 停在自由輸入列時送數字會變成打字(實測驗證過的坑)。"""

    def test_cursor_on_a_real_option(self):
        self.assertEqual(bridge._cc_ms_focus_row(MS_FRESH.splitlines()),
                         ("option", "1"))

    def test_cursor_on_the_free_text_row_is_flagged(self):
        self.assertEqual(bridge._cc_ms_focus_row(MS_INPUT_FOCUSED.splitlines()),
                         ("input", "5"))

    def test_cursor_on_the_submit_button(self):
        self.assertEqual(bridge._cc_ms_focus_row(MS_SUBMIT_FOCUSED.splitlines())[0],
                         "submit")

    def test_cursor_on_chat_about_this_is_not_an_option(self):
        pane = MS_FRESH.replace("❯ 1. [ ] Chips", "  1. [ ] Chips") \
                       .replace("  6. Chat about this", "❯ 6. Chat about this")
        self.assertEqual(bridge._cc_ms_focus_row(pane.splitlines())[0], "other")


class TabBarTests(unittest.TestCase):
    """頁籤列 → 第幾題/共幾題。"""

    def test_single_question_bar(self):
        p = bridge._cc_prompt(MS_FRESH)
        self.assertEqual(p["q_total"], 1)
        self.assertEqual(p["q_index"], 0)
        self.assertEqual(p["q_headers"], ["Snack pick"])

    def test_three_question_bar_counts_questions_not_the_submit_chip(self):
        p = bridge._cc_prompt(MS_Q1_OF_3)
        self.assertEqual(p["q_total"], 3)
        self.assertEqual(p["q_headers"], ["Fruits", "Drinks", "Desserts"])

    def test_plain_text_cannot_tell_which_question_so_we_do_not_guess(self):
        """誠實條款:純文字擷取分不出題號時回 None,不編一個出來。"""
        pane = MS_Q1_OF_3.replace("☐ Fruits", "☒ Fruits")
        self.assertIsNone(bridge._cc_prompt(pane)["q_index"])

    def test_ansi_capture_pins_the_current_question(self):
        """`capture-pane -pe` 帶背景色 → 題號就精確了(兩份 fixture 的純文字
        完全相同,只有反白位置不同 —— 這正是不能用純文字猜的證據)。"""
        self.assertEqual(bridge._cc_strip_sgr(TAB_ANSI_ON_Q1),
                         bridge._cc_strip_sgr(TAB_ANSI_ON_Q2))
        self.assertEqual(bridge._cc_tab_bar(TAB_ANSI_ON_Q1)["q_index"], 0)
        self.assertEqual(bridge._cc_tab_bar(TAB_ANSI_ON_Q2)["q_index"], 1)

    def test_answered_flags(self):
        self.assertEqual(bridge._cc_tab_bar(TAB_ANSI_ON_Q2)["q_answered"],
                         [True, False, False])

    def test_conversation_text_is_not_a_tab_bar(self):
        self.assertIsNone(bridge._cc_tab_bar("我要問你一件事:☐ 這個符號會出現在正文"))
        self.assertIsNone(bridge._cc_tab_bar(""))


class AskSigTests(unittest.TestCase):
    """同一個 ask 的多題推進不該每題都當成全新提示重推。"""

    def test_same_ask_keeps_the_same_signature_across_questions(self):
        q1 = bridge._cc_prompt(MS_Q1_OF_3)
        q2_pane = (MS_Q1_OF_3.replace("☐ Fruits", "☒ Fruits")
                   .replace("Which fruits do you want?", "Which drinks do you want?")
                   .replace("Apple", "Water").replace("Banana", "Juice")
                   .replace("Berries", "Soda"))
        q2 = bridge._cc_prompt(q2_pane)
        # 題目與選項整組換掉 → 舊的 prompt sig 一定不同(那正是重推的成因)
        self.assertNotEqual(bridge._cc_prompt_sig(q1), bridge._cc_prompt_sig(q2))
        # …但 ask sig 相同 → watcher 會就地更新同一張卡
        self.assertTrue(bridge._cc_ask_sig(q1))
        self.assertEqual(bridge._cc_ask_sig(q1), bridge._cc_ask_sig(q2))

    def test_a_different_ask_gets_a_different_signature(self):
        other = MS_Q1_OF_3.replace("☐ Desserts", "☐ Cheeses")
        self.assertNotEqual(bridge._cc_ask_sig(bridge._cc_prompt(MS_Q1_OF_3)),
                            bridge._cc_ask_sig(bridge._cc_prompt(other)))

    def test_single_question_ask_has_no_ask_sig(self):
        """單題沒有「推進到下一題」的情境 → 走原本的建卡路徑。"""
        self.assertEqual(bridge._cc_ask_sig(bridge._cc_prompt(MS_FRESH)), "")

    def test_checkbox_marks_do_not_change_the_ask_sig(self):
        answered = MS_Q1_OF_3.replace("☐ Fruits", "☒ Fruits")
        self.assertEqual(bridge._cc_ask_sig(bridge._cc_prompt(MS_Q1_OF_3)),
                         bridge._cc_ask_sig(bridge._cc_prompt(answered)))


class ApprovalPayloadTests(unittest.TestCase):
    """approval 卡是 CC 卡片流預設看到的那一張,以前最貧乏。"""

    def test_descriptions_reach_the_approval_record(self):
        p = bridge._cc_approval_payload("cc-x", bridge._cc_prompt(MS_FRESH))
        by_key = {o["key"]: o for o in p["options"]}
        self.assertEqual(by_key["1"]["description"], "Salty, crunchy — classic choice")
        self.assertNotIn("description", by_key["6"])     # 無說明就不寫空字串

    def test_meta_carries_multiselect_and_question_position(self):
        p = bridge._cc_approval_payload("cc-x", bridge._cc_prompt(MS_Q1_OF_3))
        self.assertTrue(p["meta"]["multiselect"])
        self.assertEqual(p["meta"]["q_total"], 3)
        self.assertEqual(p["meta"]["q_headers"], ["Fruits", "Drinks", "Desserts"])

    def test_checked_state_reaches_the_card(self):
        p = bridge._cc_approval_payload("cc-x", bridge._cc_prompt(MS_TOGGLED))
        self.assertEqual({o["key"] for o in p["options"] if o.get("checked")},
                         {"1", "3"})

    def test_single_select_question_is_marked_not_multiselect(self):
        from tests.test_cc_prompt_menu_parse import REAL_PANE
        p = bridge._cc_approval_payload("cc-x", bridge._cc_prompt(REAL_PANE))
        self.assertFalse(p["meta"]["multiselect"])

    def test_permission_prompts_keep_their_styles(self):
        pane = ("Claude wants to run a command\n"
                "  1. Yes, allow\n  2. Don't allow\n")
        p = bridge._cc_approval_payload("cc-x", bridge._cc_prompt(pane))
        self.assertEqual(p["kind"], "permission")
        self.assertEqual([o["style"] for o in p["options"]], ["primary", "danger"])
        self.assertEqual(p["meta"], {})      # permission 不談 multiselect

    def test_all_eight_options_survive(self):
        """舊版只寫 6 個 —— 多選版面 6 個選項就已經頂到上限。"""
        p = bridge._cc_approval_payload("cc-x", bridge._cc_prompt(MS_FRESH))
        self.assertEqual(len(p["options"]), 6)


class FakeTmux:
    """把 tmux 換成一台照著實機行為走的假機器。

    行為全部照 2026-08-11 的實測結果:數字 toggle、Down 逐列往下、走過最後一列
    才聚焦送出鈕、送出鈕 Enter 進確認頁、確認頁 "1" 才成交。
    """

    def __init__(self, options=5, label="Submit"):
        self.checked: set = set()
        self.focus = 0                 # 0..options-1 = 清單列;options = 送出鈕
        self.options = options
        self.label = label
        self.stage = "menu"            # menu → review → done
        self.sent: list = []
        self.typed = ""

    def pane(self) -> str:
        if self.stage == "review":
            return MS_REVIEW
        if self.stage == "done":
            return "⏺ User answered Claude's questions:\n"
        rows = []
        for i in range(self.options):
            mark = "✔" if str(i + 1) in self.checked else " "
            ptr = "❯" if self.focus == i else " "
            label = "Type something" if i == self.options - 1 else f"Opt {i + 1}"
            if i == self.options - 1 and self.typed:
                label = self.typed
            rows.append(f"{ptr} {i + 1}. [{mark}] {label}")
            rows.append(f"  說明 {i + 1}")
        sub = ("❯    " if self.focus >= self.options else "     ") + self.label
        # 實機行為:走過送出鈕再往下,送出鈕的 ❯ 不會消失,「Chat about this」
        # 也跟著亮起來 —— 那時候按 Enter 會把整個提問取消掉。
        chat = "❯" if self.focus > self.options else " "
        return ("────\n←  ☐ Q  ✔ Submit  →\n\n這題要選什麼?\n\n"
                + "\n".join(rows) + f"\n{sub}\n────\n"
                f"{chat} {self.options + 1}. Chat about this\n\n"
                "Enter to select · ↑/↓ to navigate · Esc to cancel\n")

    async def run(self, *args, **kw):
        if args[0] == "capture-pane":
            return 0, self.pane(), ""
        if args[0] == "has-session":
            return 0, "", ""
        if args[0] != "send-keys":
            return 0, "", ""
        keys = list(args[3:])
        if keys[:1] == ["-l"]:
            ch = keys[1]
            self.sent.append(ch)
            if self.stage == "review":
                if ch == "1":
                    self.stage = "done"
                return 0, "", ""
            if self.focus == self.options - 1:
                self.typed += ch          # 停在自由輸入列 → 變成打字(實測行為)
                return 0, "", ""
            i = int(ch) - 1
            if 0 <= i < self.options:
                key = str(i + 1)
                self.checked ^= {key}
            return 0, "", ""
        key = keys[0]
        self.sent.append(key)
        if key == "Down":
            self.focus = min(self.focus + 1, self.options + 1)
        elif key == "Up":
            self.focus = max(self.focus - 1, 0)
        elif key == "Enter" and self.focus > self.options:
            self.stage = "cancelled"       # 「Chat about this」= 取消整個提問
        elif key == "Enter" and self.focus == self.options:
            self.stage = "review" if self.label == "Submit" else "menu"
            if self.label == "Next":
                self.focus = 0
        return 0, "", ""


class NoisyLaggyTmux(FakeTmux):
    """底部狀態列每次擷取都在變(實機真的如此:`Claude | 5h 80% | 7d 23%`、
    spinner),而游標重繪慢一拍。

    這是 2026-08-11 實跑失敗兩次的真實條件:因為「整張 pane 變了」永遠成立,
    等重繪的迴圈立刻放行 → 讀到游標還沒動的畫面 → 多按一次 Down → 游標落到
    「Chat about this」→ Enter 把整個提問取消掉(CC 端變成「你想釐清什麼?」)。
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self._tick = 0
        self._stale = ""
        self._lag = 0

    def _noise(self, pane: str) -> str:
        self._tick += 1
        return pane + f"\n   Claude | 5h {80 - self._tick % 5}% | 7d 23%\n"

    async def run(self, *args, **kw):
        if args[0] == "capture-pane" and self._lag > 0:
            self._lag -= 1
            return 0, self._noise(self._stale), ""
        if args[0] == "capture-pane":
            return 0, self._noise(self.pane()), ""
        if args[0] == "send-keys":
            self._stale = self.pane()
            self._lag = 1
        return await super().run(*args, **kw)


class LaggyTmux(FakeTmux):
    """重繪比按鍵慢一拍的 TUI。

    2026-08-11 本端點實跑時真的踩到:固定 sleep 之後回讀,讀到的還是按鍵前的
    畫面 → 導覽迴圈以為「還沒到送出鈕」→ 多按一次 Down → 游標落到「Chat
    about this」→ Enter 把整個提問取消掉(CC 端顯示成使用者要求改聊)。
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self._stale = ""
        self._lag = 0

    async def run(self, *args, **kw):
        if args[0] == "capture-pane" and self._lag > 0:
            self._lag -= 1
            return 0, self._stale, ""
        if args[0] == "send-keys":
            self._stale = self.pane()      # 按鍵「之前」的畫面
            self._lag = 1
        return await super().run(*args, **kw)


class AnswerEndpointTests(unittest.IsolatedAsyncioTestCase):
    """端點行為 —— 用假 tmux 跑完整序列(真機驗證見 PR 說明)。"""

    def setUp(self):
        # 每個 test = 一次獨立的真實情境。`/answer` 的冪等快取是行程級的
        # (同一個 prompt + 同一組 keys 在 TTL 內回原結果),不清就會讓
        # 後面的測試拿到前一個測試的結果。
        _reset_answer_idem()

    async def _answer(self, fake, body):
        req = mock.Mock()
        req.json = mock.AsyncMock(return_value=body)
        with mock.patch.object(bridge, "_tmux_run", fake.run), \
             mock.patch.object(bridge, "_cc_conf_rows",
                               lambda: [("cc-x", "/tmp", "1")]), \
             mock.patch.object(bridge, "_tmux_alive",
                               mock.AsyncMock(return_value=True)), \
             mock.patch.object(bridge, "_check_auth", lambda *_a, **_k: None), \
             mock.patch.object(bridge, "_CC_ANSWER_KEY_GAP", 0), \
             mock.patch.object(bridge, "_CC_ANSWER_SETTLE", 0):
            bridge._PANE_CACHE.pop("cc-x", None)
            return await bridge.cc_session_answer("cc-x", req)

    async def test_toggles_only_and_never_sends_enter_when_submit_false(self):
        """核心修正:多選版面的 Enter 是「切換游標列」,不是送出。"""
        fake = FakeTmux()
        res = await self._answer(fake, {"keys": ["1", "3"], "submit": False})
        self.assertTrue(res["ok"])
        self.assertEqual(res["selected"], ["1", "3"])
        self.assertFalse(res["submitted"])
        self.assertNotIn("Enter", fake.sent)
        self.assertEqual(fake.checked, {"1", "3"})

    async def test_full_submit_walks_to_the_button_and_confirms(self):
        fake = FakeTmux()
        res = await self._answer(fake, {"keys": ["2"], "submit": True})
        self.assertTrue(res["submitted"])
        self.assertTrue(res["confirmed_review"])
        self.assertEqual(fake.stage, "done")
        # 走到送出鈕 → Enter → 確認頁按 "1"
        self.assertEqual(fake.sent.count("Down"), 5)
        self.assertEqual(fake.sent[-2:], ["Enter", "1"])

    async def test_keys_are_the_target_set_not_blind_keystrokes(self):
        """使用者已在終端機勾了 2、4;要求 {1,3} 時只送差集,不會盲送 1、3
        把已勾的 2、4 留在裡面(那才是舊路徑會做的事)。"""
        fake = FakeTmux()
        fake.checked = {"2", "4"}
        res = await self._answer(fake, {"keys": ["1", "3"], "submit": False})
        self.assertEqual(fake.checked, {"1", "3"})
        self.assertEqual(sorted(res["sent"]), ["1", "2", "3", "4"])

    async def test_already_correct_selection_sends_nothing(self):
        fake = FakeTmux()
        fake.checked = {"1", "3"}
        res = await self._answer(fake, {"keys": ["3", "1"], "submit": False})
        self.assertEqual(res["sent"], [])
        self.assertEqual(res["selected"], ["1", "3"])

    async def test_cursor_on_free_text_row_is_moved_before_typing_digits(self):
        """實測坑:游標停在自由輸入列時數字會變成打字。先 Up 移開再送。"""
        fake = FakeTmux()
        fake.focus = fake.options - 1
        res = await self._answer(fake, {"keys": ["1"], "submit": False})
        self.assertEqual(fake.typed, "")          # 一個字都沒被打進輸入框
        self.assertEqual(fake.checked, {"1"})
        self.assertEqual(res["selected"], ["1"])

    async def test_next_button_advances_instead_of_claiming_submitted(self):
        """多題 ask 的非最後一題:鈕上寫 Next,按下去只是推進,不是成交。"""
        fake = FakeTmux(label="Next")
        res = await self._answer(fake, {"keys": ["1"], "submit": True})
        self.assertFalse(res["submitted"])
        self.assertTrue(res["advanced"])
        self.assertEqual(res["submit_label"], "Next")

    async def test_submitted_flag_waits_for_the_review_screen_to_close(self):
        """回歸(實跑踩過):CC 要約 1 秒才結案,期間 spinner 已經在動但確認頁
        還在 —— 只看「畫面變了」會把一次**成功**的送出回報成 submitted:false。"""

        class SlowClose(FakeTmux):
            def __init__(self, **kw):
                super().__init__(**kw)
                self._ticks = 0

            def pane(self):
                if self.stage == "done" and self._ticks < 3:
                    self._ticks += 1        # 結案中:確認頁還在,只有 spinner 變
                    return MS_REVIEW + f"\n✻ 收尾中… ({self._ticks}s)\n"
                return super().pane()

        fake = SlowClose()
        res = await self._answer(fake, {"keys": ["1"], "submit": True})
        self.assertEqual(fake.stage, "done")
        self.assertTrue(res["submitted"], "確認頁只是還沒收掉,不是沒送出")

    async def test_changing_status_bar_plus_lag_still_lands_on_submit(self):
        """回歸(實跑失敗兩次的真實條件):狀態列一直在變 + 游標重繪慢一拍。

        「等 pane 變了」在這裡永遠立刻成立,所以必須等的是**游標真的移動了**。
        """
        fake = NoisyLaggyTmux()
        res = await self._answer(fake, {"keys": ["1", "3"], "submit": True})
        self.assertNotEqual(fake.stage, "cancelled", "游標走過頭把提問取消掉了")
        self.assertTrue(res["submitted"])
        self.assertEqual(fake.checked, {"1", "3"})
        self.assertEqual(fake.sent.count("Down"), 5)

    async def test_slow_redraw_does_not_overshoot_onto_chat_about_this(self):
        """回歸(實跑踩過):重繪慢一拍時不能多按 Down,否則 Enter 會取消提問。"""
        fake = LaggyTmux()
        res = await self._answer(fake, {"keys": ["1"], "submit": True})
        self.assertNotEqual(fake.stage, "cancelled")
        self.assertTrue(res["submitted"])
        self.assertEqual(fake.sent.count("Down"), 5)

    async def test_cursor_already_past_the_submit_button_is_walked_back(self):
        """使用者先在終端機把游標按到「Chat about this」上了。"""
        fake = FakeTmux()
        fake.focus = fake.options + 1
        res = await self._answer(fake, {"keys": ["1"], "submit": True})
        self.assertNotEqual(fake.stage, "cancelled")
        self.assertTrue(res["submitted"])

    async def test_stale_prompt_is_refused_instead_of_typing_garbage(self):
        fake = FakeTmux()
        fake.stage = "done"                       # 提示已經自己解掉了
        with self.assertRaises(Exception) as ctx:
            await self._answer(fake, {"keys": ["1"], "submit": True})
        self.assertEqual(getattr(ctx.exception, "status_code", None), 409)
        self.assertEqual(fake.sent, [])

    async def test_key_that_is_not_on_screen_is_refused(self):
        fake = FakeTmux()
        with self.assertRaises(Exception) as ctx:
            await self._answer(fake, {"keys": ["9"], "submit": False})
        self.assertEqual(getattr(ctx.exception, "status_code", None), 409)
        self.assertEqual(fake.sent, [])

    async def test_non_digit_keys_are_rejected(self):
        fake = FakeTmux()
        with self.assertRaises(Exception) as ctx:
            await self._answer(fake, {"keys": ["yes"], "submit": False})
        self.assertEqual(getattr(ctx.exception, "status_code", None), 400)

    async def test_empty_keys_are_rejected(self):
        fake = FakeTmux()
        with self.assertRaises(Exception):
            await self._answer(fake, {"keys": [], "submit": False})

    async def test_single_select_prompt_falls_back_to_the_old_key_path(self):
        """既有 /key 行為一字不改地沿用(單選/權限題向後相容)。"""
        from tests.test_cc_prompt_menu_parse import REAL_PANE

        class SingleSelect(FakeTmux):
            def pane(self):
                return REAL_PANE

        fake = SingleSelect()
        # patch `_cc_key_core_locked`:`/answer` 外層已經拿著同一把
        # per-session 序列鎖，再走公開的 `_cc_key_core` 會自己撞自己。
        with mock.patch.object(bridge, "_cc_key_core_locked",
                               mock.AsyncMock(return_value={"ok": True})) as core:
            res = await self._answer(fake, {"keys": ["2"], "submit": True})
        core.assert_awaited_once_with("cc-x", "2")
        self.assertFalse(res["multiselect"])
        self.assertTrue(res["submitted"])

    async def test_single_select_refuses_multiple_keys(self):
        from tests.test_cc_prompt_menu_parse import REAL_PANE

        class SingleSelect(FakeTmux):
            def pane(self):
                return REAL_PANE

        with self.assertRaises(Exception) as ctx:
            await self._answer(SingleSelect(), {"keys": ["1", "2"]})
        self.assertEqual(getattr(ctx.exception, "status_code", None), 400)


class KeyEndpointUnchangedTests(unittest.IsolatedAsyncioTestCase):
    """/key 維持原行為 —— 這條路徑上還掛著權限題與舊 app build。"""

    async def test_question_menu_still_gets_digit_plus_enter(self):
        from tests.test_cc_prompt_menu_parse import REAL_PANE
        sent = []

        async def run(*args, **kw):
            if args[0] == "capture-pane":
                return 0, REAL_PANE, ""
            if args[0] == "send-keys":
                sent.append(list(args[3:]))
            return 0, "", ""

        with mock.patch.object(bridge, "_tmux_run", run), \
             mock.patch.object(bridge, "_tmux_alive",
                               mock.AsyncMock(return_value=True)), \
             mock.patch.object(bridge, "_cc_conf_rows",
                               lambda: [("cc-x", "/tmp", "1")]), \
             mock.patch.object(bridge, "_cc_session_jsonl",
                               mock.AsyncMock(return_value=None)):
            bridge._PANE_CACHE.pop("cc-x", None)
            res = await bridge._cc_key_core("cc-x", "2")
        self.assertTrue(res["ok"])
        self.assertEqual(sent, [["-l", "2"], ["Enter"]])


# ═════════════ 修 review 抓到的缺陷（併發鎖 / 冪等 / 假成功 / 預算）═════════

class _SlowTmux(FakeTmux):
    """每個按鍵都慢一點 —— 好讓兩個請求真的有機會交錯。"""

    async def run(self, *args, **kw):
        if args[0] == "send-keys":
            await asyncio.sleep(0.02)
        return await super().run(*args, **kw)


def _answer_patches(fake, rows=(("cc-x", "/tmp", "1"),)):
    return (mock.patch.object(bridge, "_tmux_run", fake.run),
            mock.patch.object(bridge, "_cc_conf_rows", lambda: list(rows)),
            mock.patch.object(bridge, "_tmux_alive",
                              mock.AsyncMock(return_value=True)),
            mock.patch.object(bridge, "_check_auth", lambda *_a, **_k: None),
            mock.patch.object(bridge, "_CC_ANSWER_KEY_GAP", 0),
            mock.patch.object(bridge, "_CC_ANSWER_SETTLE", 0))


async def _post_answer(session: str, body: dict):
    req = mock.Mock()
    req.json = mock.AsyncMock(return_value=body)
    bridge._PANE_CACHE.pop(session, None)
    return await bridge.cc_session_answer(session, req)


class ConcurrencyLockTests(unittest.IsolatedAsyncioTestCase):
    """M-H:`/answer` 的送出序列最壞要按十幾個鍵、二十幾秒，中間完全沒有互斥。
    兩台裝置、卡片 vs 輸入列、`/answer` 撞 `/key` —— 按鍵會交錯打進同一個
    pane，數字是 toggle，交錯的結果是勾錯選項甚至在確認頁按到 Cancel。"""

    def setUp(self):
        _reset_answer_idem()

    async def test_second_concurrent_answer_gets_409_not_interleaved_keys(self):
        fake = _SlowTmux()
        with contextlib.ExitStack() as st:
            for p in _answer_patches(fake):
                st.enter_context(p)
            a = asyncio.create_task(_post_answer("cc-x", {"keys": ["1", "3"],
                                                          "submit": False}))
            await asyncio.sleep(0.01)          # 讓第一個真的開始跑
            with self.assertRaises(bridge.BridgeError) as cm:
                await _post_answer("cc-x", {"keys": ["2", "4"], "submit": False})
            first = await a
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(cm.exception.code, "SESSION_BUSY_TYPING")
        # 第一個請求完整跑完，勾選正是它要的那一組（沒有被第二個插花）
        self.assertEqual(first["selected"], ["1", "3"])
        self.assertEqual(fake.checked, {"1", "3"})

    async def test_key_endpoint_cannot_interleave_into_an_answer(self):
        """`/answer` 進行中，`/key` 也要被擋（同一把鎖）。"""
        fake = _SlowTmux()
        with contextlib.ExitStack() as st:
            for p in _answer_patches(fake):
                st.enter_context(p)
            a = asyncio.create_task(_post_answer("cc-x", {"keys": ["1", "2", "3"],
                                                          "submit": False}))
            await asyncio.sleep(0.01)
            with self.assertRaises(bridge.BridgeError) as cm:
                await bridge._cc_key_core("cc-x", "5")
            await a
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(cm.exception.code, "SESSION_BUSY_TYPING")
        self.assertEqual(fake.checked, {"1", "2", "3"})

    async def test_lock_is_released_even_when_the_sequence_fails(self):
        fake = FakeTmux()
        with contextlib.ExitStack() as st:
            for p in _answer_patches(fake):
                st.enter_context(p)
            with self.assertRaises(bridge.BridgeError):
                await _post_answer("cc-x", {"keys": ["9"], "submit": False})
            res = await _post_answer("cc-x", {"keys": ["1"], "submit": False})
        self.assertEqual(res["selected"], ["1"])
        self.assertFalse(bridge._cc_seq_lock("cc-x").locked())

    async def test_lock_is_per_session(self):
        """不同 session 各自一把鎖，不能互相擋。"""
        f1, f2 = _SlowTmux(), _SlowTmux()
        rows = [("cc-a", "/tmp", "1"), ("cc-b", "/tmp", "1")]
        with mock.patch.object(bridge, "_cc_conf_rows", lambda: rows), \
             mock.patch.object(bridge, "_tmux_alive",
                               mock.AsyncMock(return_value=True)), \
             mock.patch.object(bridge, "_check_auth", lambda *_a, **_k: None), \
             mock.patch.object(bridge, "_CC_ANSWER_KEY_GAP", 0), \
             mock.patch.object(bridge, "_CC_ANSWER_SETTLE", 0):
            async def call(sess, fake, keys):
                with mock.patch.object(bridge, "_tmux_run", fake.run):
                    return await _post_answer(sess, {"keys": keys, "submit": False})
            r1, r2 = await asyncio.gather(call("cc-a", f1, ["1"]),
                                          call("cc-b", f2, ["2"]))
        self.assertEqual(r1["selected"], ["1"])
        self.assertEqual(r2["selected"], ["2"])


class IdempotencyTests(unittest.IsolatedAsyncioTestCase):
    """M-H:序列最壞 ≈22s、app 端 timeout 15s → app 先放棄、使用者重試 →
    同一組答案送兩次（第二次會答到下一題去）。端點必須冪等。"""

    def setUp(self):
        _reset_answer_idem()

    async def _answer(self, fake, body):
        with contextlib.ExitStack() as st:
            for p in _answer_patches(fake):
                st.enter_context(p)
            return await _post_answer("cc-x", body)

    async def test_same_keys_on_the_same_prompt_replays_instead_of_retoggling(self):
        """雙擊:第二次不能再跑一輪 toggle —— 那會把剛勾好的整組切掉。"""
        fake = FakeTmux()
        first = await self._answer(fake, {"keys": ["1", "3"], "submit": False})
        sent_after_first = list(fake.sent)
        second = await self._answer(fake, {"keys": ["1", "3"], "submit": False})
        self.assertTrue(second.get("replayed"))
        self.assertEqual(second["selected"], first["selected"])
        self.assertEqual(fake.sent, sent_after_first, "重播不該再送任何鍵")
        self.assertEqual(fake.checked, {"1", "3"})

    async def test_client_id_replays_after_the_prompt_is_gone(self):
        """app timeout → 重送。那時 prompt 早就收掉了，只有 client_id 認得。"""
        fake = FakeTmux()
        first = await self._answer(fake, {"keys": ["2"], "submit": True,
                                          "client_id": "dev-1"})
        self.assertTrue(first["submitted"])
        self.assertEqual(fake.stage, "done")     # 提問已結案，畫面沒 prompt 了
        sent_after_first = list(fake.sent)
        again = await self._answer(fake, {"keys": ["2"], "submit": True,
                                          "client_id": "dev-1"})
        self.assertTrue(again.get("replayed"))
        self.assertTrue(again["submitted"])
        self.assertEqual(fake.sent, sent_after_first, "重送不該再按任何鍵")

    async def test_a_different_prompt_is_not_replayed(self):
        """下一題剛好長得一樣也不能被誤判成重播（sig 換了就清快取）。"""
        fake = FakeTmux()
        await self._answer(fake, {"keys": ["1"], "submit": False})
        fake2 = FakeTmux(options=4)          # 不同題（選項數不同 → sig 不同）
        res = await self._answer(fake2, {"keys": ["1"], "submit": False})
        self.assertFalse(res.get("replayed"))
        self.assertEqual(fake2.checked, {"1"})

    async def test_a_different_key_set_is_not_replayed(self):
        fake = FakeTmux()
        await self._answer(fake, {"keys": ["1"], "submit": False})
        res = await self._answer(fake, {"keys": ["1", "2"], "submit": False})
        self.assertFalse(res.get("replayed"))
        self.assertEqual(fake.checked, {"1", "2"})

    async def test_result_reports_its_time_budget(self):
        """app 端要知道該把 timeout 設多大（`budget_secs`）。"""
        fake = FakeTmux()
        res = await self._answer(fake, {"keys": ["1"], "submit": False})
        self.assertEqual(res["budget_secs"], bridge._CC_ANSWER_BUDGET_SECS)
        # app 端 StudioBridge.swift 的 timeout 必須大於這個數(見 PR 說明)
        self.assertGreaterEqual(bridge._CC_ANSWER_BUDGET_SECS, 25)


class NotSubmittedIsNotA200Test(unittest.IsolatedAsyncioTestCase):
    """M:確認頁沒收掉 = 這次沒成交。舊碼回 HTTP 200 + submitted:false，
    呼叫端一律把 200 當成功、對使用者說「已送出」—— 使用者以為答完了，
    CC 其實還停在確認頁等人按。"""

    def setUp(self):
        _reset_answer_idem()

    async def test_unclosed_review_screen_is_a_409(self):
        class StuckReview(FakeTmux):
            def pane(self):
                if self.stage == "done":
                    return MS_REVIEW          # 確認頁永遠不收
                return super().pane()

        fake = StuckReview()
        with contextlib.ExitStack() as st:
            for p in _answer_patches(fake):
                st.enter_context(p)
            with self.assertRaises(bridge.BridgeError) as cm:
                await _post_answer("cc-x", {"keys": ["1"], "submit": True})
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(cm.exception.code, "ANSWER_NOT_SUBMITTED")

    async def test_next_button_is_still_a_200(self):
        """「推進到下一題」不是失敗 —— 那條路必須維持 2xx。"""
        fake = FakeTmux(label="Next")
        with contextlib.ExitStack() as st:
            for p in _answer_patches(fake):
                st.enter_context(p)
            res = await _post_answer("cc-x", {"keys": ["1"], "submit": True})
        self.assertFalse(res["submitted"])
        self.assertTrue(res["advanced"])


class ApprovalUpdateTidyTests(unittest.TestCase):
    """L:`_cc_approval_update` 舊碼 try 裡 close 完、finally 再 close 一次，
    而且在**已關閉**的 cursor 上讀 `rowcount`。"""

    def test_rowcount_is_read_before_close_and_reports_truthfully(self):
        import sqlite3
        import tempfile as _tf
        db = os.path.join(_tf.mkdtemp(prefix="cc-appr-"), "canonical.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE approvals(id TEXT PRIMARY KEY, title TEXT, "
                    "detail TEXT, kind TEXT, options TEXT, meta TEXT, status TEXT)")
        con.execute("INSERT INTO approvals VALUES('a1','t','d','question',NULL,NULL,'pending')")
        con.execute("INSERT INTO approvals VALUES('a2','t','d','question',NULL,NULL,'expired')")
        con.commit()
        con.close()
        prompt = bridge._cc_prompt(MS_FRESH)
        with mock.patch.object(bridge, "CANON_DB", db):
            self.assertTrue(bridge._cc_approval_update("a1", "cc-x", prompt))
            self.assertFalse(bridge._cc_approval_update("a2", "cc-x", prompt))
            self.assertFalse(bridge._cc_approval_update("nope", "cc-x", prompt))
        con = sqlite3.connect(db)
        try:
            row = con.execute("SELECT title, status FROM approvals "
                              "WHERE id='a1'").fetchone()
        finally:
            con.close()
        self.assertEqual(row[1], "pending")
        self.assertNotEqual(row[0], "t")     # 真的有寫進去


if __name__ == "__main__":
    unittest.main()
