"""CC 模型切換:/model 一送出就跳確認框、吃掉輸入框的字,通用貼字驗證因此誤判
「沒送進去」,回假的 CC_INPUT_NOT_ACCEPTED 且把確認框晾著沒人按(2026-10-25
機主實測:模型選了沒反應)。釘住 expect_dialog 旗標的契約:

  1. expect_dialog=True + 驗證判失敗 + 畫面上有確認框 → 當成已收下(accepted),
     **不** raise、**不** 送 C-u(C-u 會打進開著的確認框)。
  2. expect_dialog=False(預設,所有送訊息路徑)→ 同情境照樣 raise 409 ——
     證明這個修正對既有行為零影響,也證明測試真的走到了會失敗的那條路。
  3. 畫面上**沒有**確認框時,expect_dialog=True 不吞失敗,照樣 raise ——
     旗標只赦免「確認框真的出現」,不是無條件放行。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須第一個 import
import os
import sys
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge  # noqa: E402

DIALOG_PANE = "<<switch-model-dialog>>"
SWITCH_DIALOG = {"title": "Switch model?",
                 "options": [{"key": "1", "label": "Yes, switch to Sonnet 5"},
                             {"key": "2", "label": "No, go back"}]}


class CCModelDialogPatch:
    """把 _cc_paste_text_locked 在 verify 之前的所有 tmux I/O 都換成替身,
    verify 回傳失敗,最後一次抓畫面回傳『確認框』(或不回),由各測試決定。"""
    def __init__(self, final_pane):
        self.final_pane = final_pane
        self.cu_sent = False
        self._panes = []

    def _run(self, *args, **kw):
        # 只在意「verify 之後」的 C-u(stranded 失敗分支那顆,會打進開著的確認框)。
        # verify 之前還有一顆貼字前的清場 C-u —— 那打在空框上,完全正常,不算。
        if getattr(self, "_verify_done", False) and len(args) >= 4 and args[3] == "C-u":
            self.cu_sent = True
        return (0, "", "")

    def install(self, stack):
        p = lambda tgt, **k: stack.enter_context(patch.object(bridge, tgt, **k))
        # verify 之前:畫面乾淨(沒 pre-prompt、不在 shell 模式、框是空的)
        async def cap(name):
            # 最後一次(verify 之後的 dialog 探測)回 final_pane,其餘回空
            return self.final_pane if self._verify_done else ""
        self._verify_done = False
        async def verify(*a, **k):
            self._verify_done = True
            return {"state": "stranded", "reason": "composer_missing",
                    "attempts": 2, "pane": ""}
        async def clear(name, pane):
            return (True, "")
        async def alive(name):
            return True
        async def run(*a, **k):
            return self._run(*a, **k)
        async def run_stdin(*a, **k):
            return (0, "", "")
        p("_cc_capture_pane_fresh", side_effect=cap)
        p("_cc_verify_submitted", side_effect=verify)
        p("_cc_force_clear_composer", side_effect=clear)
        p("_tmux_alive", side_effect=alive)
        p("_tmux_run", side_effect=run)
        p("_tmux_run_stdin", side_effect=run_stdin)
        p("_cc_shell_mode", return_value=False)
        p("_cc_pane_busy", return_value=False)
        # 把 expect_dialog 的輪詢窗縮小,讓「沒有確認框」那條測試不用真的等 3.5s。
        p("_CC_EXPECT_DIALOG_POLL_SECS", new=0.4)
        p("_CC_EXPECT_DIALOG_POLL_INTERVAL", new=0.1)
        p("_cc_prompt", side_effect=lambda pane: SWITCH_DIALOG if pane == DIALOG_PANE else None)
        # 真鎖會卡;換成無操作的 async context manager
        @asynccontextmanager
        async def nolock(name):
            yield
        p("_cc_paste_lock", side_effect=nolock)


class CCModelDialogTest(unittest.IsolatedAsyncioTestCase):
    async def test_dialog_present_counts_as_accepted_no_cu(self):
        import contextlib
        h = CCModelDialogPatch(final_pane=DIALOG_PANE)
        with contextlib.ExitStack() as stack:
            h.install(stack)
            res = await bridge._cc_paste_text("probe", "/model sonnet", expect_dialog=True)
        self.assertEqual(res.get("delivery"), "accepted")
        self.assertTrue(res.get("dialog"))
        self.assertFalse(h.cu_sent, "確認框開著時絕不能送 C-u(會打進框裡)")

    async def test_default_still_raises_on_same_verdict(self):
        import contextlib
        h = CCModelDialogPatch(final_pane=DIALOG_PANE)
        with contextlib.ExitStack() as stack:
            h.install(stack)
            with self.assertRaises(bridge.BridgeError) as cm:
                await bridge._cc_paste_text("probe", "/model sonnet")  # expect_dialog 預設 False
        self.assertEqual(cm.exception.code, "CC_INPUT_NOT_ACCEPTED")

    async def test_dialog_that_appears_a_beat_late_is_still_caught(self):
        # 真正的回歸測試:確認框在 Enter 之後慢一拍才畫出來。單截一張畫面會漏掉
        # (2026-10-25 production 實測:第一次剛好抓到、第二次沒抓到就誤判失敗)。
        # 輪詢版必須撐到它出現。這裡讓 verify 之後的前兩次抓畫面是空的,第三次才
        # 冒出確認框。
        import contextlib
        h = CCModelDialogPatch(final_pane=DIALOG_PANE)
        calls = {"n": 0}
        orig_final = h.final_pane

        def _late_cap_factory():
            async def cap(name):
                if not h._verify_done:
                    return ""
                calls["n"] += 1
                return DIALOG_PANE if calls["n"] >= 3 else ""   # 第 3 次才出現
            return cap

        with contextlib.ExitStack() as stack:
            h.install(stack)
            # 覆蓋掉 install 裝的 cap,換成「慢一拍」版
            stack.enter_context(patch.object(bridge, "_cc_capture_pane_fresh",
                                             side_effect=_late_cap_factory()))
            res = await bridge._cc_paste_text("probe", "/model sonnet", expect_dialog=True)
        self.assertTrue(res.get("dialog"))
        self.assertGreaterEqual(calls["n"], 3, "應該是輪詢到第 3 次才抓到,不是一次就中")
        self.assertFalse(h.cu_sent)

    async def test_expect_dialog_without_a_dialog_still_raises(self):
        import contextlib
        h = CCModelDialogPatch(final_pane="")   # verify 後畫面上沒有確認框
        with contextlib.ExitStack() as stack:
            h.install(stack)
            with self.assertRaises(bridge.BridgeError) as cm:
                await bridge._cc_paste_text("probe", "/model sonnet", expect_dialog=True)
        self.assertEqual(cm.exception.code, "CC_INPUT_NOT_ACCEPTED")


REAL_DIALOG_PANE = """\
  驗證：assembleDebug 綠、單測 45/45 綠。
▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔
   Switch model?
   Your next response will be slower and use more tokens

   This conversation is cached for the current model. Switching to Sonnet 5
   means the full history gets re-read on your next message.

   ❯ 1. Yes, switch to Sonnet 5
     2. No, go back
"""


class ModelSwitchKeyTest(unittest.TestCase):
    """直接針對 2026-10-25 production 擷取的真實「Switch model?」畫面 ——
    `_cc_prompt` 對它回 None(偵測不到),這支專用偵測器必須認得。"""

    def test_detects_real_switch_model_dialog(self):
        self.assertIsNone(bridge._cc_prompt(REAL_DIALOG_PANE),
                          "前提:_cc_prompt 本來就看不到這個框(這正是 bug 根因)")
        self.assertEqual(bridge._cc_model_switch_key(REAL_DIALOG_PANE), "1")

    def test_quiet_idle_pane_is_none(self):
        self.assertIsNone(bridge._cc_model_switch_key(
            "❯ \n────\n   Claude | 5h 66%\n  auto mode on (shift+tab to cycle)"))

    def test_generic_permission_prompt_is_not_a_model_switch(self):
        self.assertIsNone(bridge._cc_model_switch_key(
            "Do you want to proceed?\n ❯ 1. Yes\n   2. No"))


if __name__ == "__main__":
    unittest.main()
