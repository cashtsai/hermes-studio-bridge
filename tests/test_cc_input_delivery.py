"""CC 送出交付語意(2026-07-28 靜默掉訊息事故)。

現場:CLI 忙碌 + `100% context used`,Pocket 送出的字停在 `❯` 輸入框裡沒送出,
bridge 卻回 200 → app 顯示「已送達」,訊息永遠不會被處理。

這組測試釘住三件事:
  1. 輸入行沒清空 → 一定不能回 200(丟 409 CC_INPUT_NOT_ACCEPTED + 原因)
  2. 補 Enter 後真的送出 → 回 200,且帶 delivery 語意
  3. 忙碌 / 看不到輸入框 / 從沒 render 出來 → 一律不謊稱 accepted
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

_TMP = tempfile.mkdtemp(prefix="cc-input-delivery-")
os.environ["HOME"] = _TMP
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

TEXT = "現在hermes and openclaw在人格那邊原則上先讓他擇一而已"
BORDER = "─" * 80


def pane_busy_holding(text=TEXT, context_full=True):
    """現場那張圖:spinner + context 滿 + 字卡在輸入框(提示符後是 U+00A0)。"""
    head = "✽ Fiddle-faddling… (6m 27s · ↓ 3.7k tokens)"
    if context_full:
        head += "    100% context used"
    return "\n".join([
        "  上一輪的回覆內容",
        head,
        BORDER,
        f"❯ {text}",
        BORDER,
        "   Claude | 5h 91% | 7d 27%                    /rc",
        "  ⏵⏵ auto mode on · 5 shells, 1 monitor",
    ])


def pane_holding_wrapped(text=TEXT):
    """同樣卡住,但輸入框在 80 欄折行 —— 原字串比對會對不上,squash 後要抓得到。"""
    mid = len(text) // 2
    return "\n".join([
        "  上一輪的回覆內容",
        BORDER,
        f"❯ {text[:mid]}",
        f"  {text[mid:]}",
        BORDER,
        "  ⏵⏵ auto mode on",
    ])


def pane_idle_empty():
    return "\n".join(["  上一輪的回覆內容", BORDER, "❯ ", BORDER, "  ⏵⏵ auto mode on"])


def pane_echoed(text=TEXT, busy=True):
    """送出成功:字進了 transcript,輸入框空了。"""
    lines = ["  上一輪的回覆內容", f"> {text}"]
    if busy:
        lines.append("✽ Fiddle-faddling… (0m 3s · ↓ 1.2k tokens)")
    lines += [BORDER, "❯ ", BORDER, "  ⏵⏵ auto mode on"]
    return "\n".join(lines)


def pane_queued_echo_with_lagging_composer(text=TEXT):
    """真 CLI 忙碌中送出的實測畫面(2026-07-28):訊息已經排進 Claude Code 自己
    的佇列並回顯在輸入框上方,但輸入框本身還沒清空(幾百毫秒的渲染延遲)。
    這時候補 Enter 會把同一則訊息排進佇列**第二次**。"""
    return "\n".join([
        "  上一輪的回覆內容",
        "✽ Fiddle-faddling… (0m 8s · ↓ 1.2k tokens)",
        f"  ❯ {text}",                 # 已排入 CLI 佇列的回顯
        BORDER,
        f"❯ {text}",                   # 輸入框還沒清乾淨
        BORDER,
        "  ⏵⏵ auto mode on",
    ])


def pane_no_composer():
    """畫面重繪 / overlay 蓋住:看不到提示符。舊碼在這裡直接回報成功。"""
    return "\n".join(["  full-screen overlay", "  no prompt marker here", "  ..."])


def pane_permission_prompt():
    return "\n".join([
        "  Bash(rm -rf /tmp/x) wants to run",
        "  Do you want to proceed?",
        "  1. Yes",
        "  2. No, and tell Claude what to do differently",
        BORDER,
        "❯ ",
    ])


class PaneScript:
    """依序吐畫面,用完就一直回最後一張。"""

    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = 0

    async def __call__(self, name):
        self.calls += 1
        i = min(self.calls - 1, len(self.frames) - 1)
        return self.frames[i]


class CCInputDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.budget = bridge._CC_VERIFY_BUDGET_SECS
        self.retries = bridge._CC_VERIFY_MAX_ENTER_RETRIES
        self.settle = bridge._CC_VERIFY_SETTLE_SECS
        self.poll = bridge._CC_VERIFY_POLL_SECS
        bridge._CC_VERIFY_BUDGET_SECS = 1.2        # 測試跑快一點,語意不變
        bridge._CC_VERIFY_SETTLE_SECS = 0.05
        bridge._CC_VERIFY_POLL_SECS = 0.05
        bridge._CC_VERIFY_MAX_ENTER_RETRIES = 3
        self.regrasp = bridge._CC_IDLE_REGRASP_SECS
        bridge._CC_IDLE_REGRASP_SECS = 0.05
        self.shell_poll = bridge._CC_SHELL_RESET_POLL_SECS
        bridge._CC_SHELL_RESET_POLL_SECS = 0.01
        bridge._CC_PASTE_LOCKS.clear()
        bridge._CC_TURN_GEN.clear()
        self.tmux = AsyncMock(return_value=(0, "", ""))
        self.stdin = AsyncMock(return_value=(0, "", ""))
        self.alive = AsyncMock(return_value=True)
        self.p = [
            patch.object(bridge, "_tmux_run", self.tmux),
            patch.object(bridge, "_tmux_run_stdin", self.stdin),
            patch.object(bridge, "_tmux_alive", self.alive),
        ]
        for p in self.p:
            p.start()

    async def asyncTearDown(self):
        for p in self.p:
            p.stop()
        bridge._CC_VERIFY_BUDGET_SECS = self.budget
        bridge._CC_VERIFY_SETTLE_SECS = self.settle
        bridge._CC_VERIFY_POLL_SECS = self.poll
        bridge._CC_VERIFY_MAX_ENTER_RETRIES = self.retries
        bridge._CC_IDLE_REGRASP_SECS = self.regrasp
        bridge._CC_SHELL_RESET_POLL_SECS = self.shell_poll

    def enters(self):
        return [c for c in self.tmux.call_args_list
                if len(c.args) >= 4 and c.args[3] == "Enter"]

    def clears(self):
        return [c for c in self.tmux.call_args_list
                if len(c.args) >= 4 and c.args[3] == "C-u"]

    # ── 1. 輸入行沒清空 → 不准回 200 ────────────────────────────────────
    async def test_stranded_text_never_returns_ok(self):
        # 第一張是**貼上之前**的畫面(框是空的)。貼上前框裡就有殘字是另一個
        # 案子,見 test_uncleanable_residue_is_rejected_not_concatenated。
        script = PaneScript([pane_idle_empty(), pane_busy_holding()])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            with self.assertRaises(bridge.HTTPException) as cm:
                await bridge._cc_paste_text("s1", TEXT)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(cm.exception.code, "CC_INPUT_NOT_ACCEPTED")
        # context 100% 這種 CLI 拒收狀態要分辨得出來
        self.assertIn("context_full", cm.exception.message)
        # 補過 Enter(第一次送出 + 重試),而且失敗後把殘字清掉不留殭屍草稿
        self.assertGreaterEqual(len(self.enters()), 2)
        self.assertGreaterEqual(len(self.clears()), 2)

    async def test_stranded_wrapped_cjk_still_detected(self):
        """折行 + NBSP 的輸入框:squash 比對要抓得到,不能判成已清空。"""
        script = PaneScript([pane_idle_empty(), pane_holding_wrapped()])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            with self.assertRaises(bridge.HTTPException) as cm:
                await bridge._cc_paste_text("s1", TEXT)
        self.assertEqual(cm.exception.status_code, 409)

    async def test_stranded_reason_composer_stuck_without_context_full(self):
        script = PaneScript([pane_idle_empty(),
                             pane_busy_holding(context_full=False)])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            with self.assertRaises(bridge.HTTPException) as cm:
                await bridge._cc_paste_text("s1", TEXT)
        self.assertIn("composer_stuck", cm.exception.message)

    # ── 1b. 貼上前的殘字清不掉 → 不准貼在殘字後面 ──────────────────────
    async def test_uncleanable_residue_is_rejected_not_concatenated(self):
        """2026-10-10 機主實害:context 滿 → 上一則擱淺在框裡 → app 重送 →
        舊碼清框失敗只記一筆 log **然後照樣貼**,殘字和新字黏成一團送出去
        (log 實證:`!cd …bridge-safe-restart.sh!cd …bridge-safe`)。
        清不乾淨就必須回 409,而且**一個字都不准貼**。"""
        script = PaneScript([pane_busy_holding(text="上一則擱淺的字")])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            with self.assertRaises(bridge.HTTPException) as cm:
                await bridge._cc_paste_text("s1", TEXT)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(cm.exception.code, "CC_INPUT_NOT_ACCEPTED")
        self.assertIn("context_full", cm.exception.message)       # 滿載要認得出
        self.assertIn("/compact", cm.exception.detail)             # 給得出下一步
        # 真正的紅線:沒有把文字送進 tmux buffer、也沒有貼、也沒有 Enter。
        self.assertEqual(self.stdin.call_count, 0, "殘字清不掉卻還是貼了")
        pastes = [c for c in self.tmux.call_args_list
                  if c.args and c.args[0] == "paste-buffer"]
        self.assertEqual(pastes, [])
        self.assertEqual(self.enters(), [])

    async def test_residue_cleared_after_redraw_lag_still_sends(self):
        """滿載的 TUI 消化兩百顆 BSpace 要時間:前幾次回讀都還是舊畫面。
        舊碼只等 3 × 0.2s 就判「清不乾淨」—— 當天 17 筆 clear_failed 多半是
        這個假陽性。多等幾輪就該清掉並正常送出。"""
        script = PaneScript([pane_busy_holding(text="殘字"),   # pre-check
                             pane_busy_holding(text="殘字"),   # 還沒重畫
                             pane_busy_holding(text="殘字"),   # 還沒重畫
                             pane_idle_empty(),                # 終於清掉了
                             pane_echoed()])                   # 送出成功
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            r = await bridge._cc_paste_text("s1", TEXT)
        self.assertTrue(r["confirmed"])            # 進了 transcript = 真送出
        self.assertEqual(r["reason"], "echoed_in_pane")
        bspaces = [c for c in self.tmux.call_args_list
                   if len(c.args) >= 5 and c.args[-1] == "BSpace"]
        self.assertGreaterEqual(len(bspaces), 3, "逐字刪不夠有耐心")

    async def test_clear_never_blind_presses_keys_without_composer(self):
        """看不到輸入框(信任對話框 / 全螢幕 overlay)時,絕不往畫面裡盲打
        幾十顆 BSpace —— 那等於替機主亂按。直接回 409 讓人處理完再送。"""
        script = PaneScript([pane_no_composer()])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            with self.assertRaises(bridge.HTTPException) as cm:
                await bridge._cc_paste_text("s1", TEXT)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertIn("composer_missing", cm.exception.message)
        keys = [c.args[3:] for c in self.tmux.call_args_list
                if c.args and c.args[0] == "send-keys"]
        self.assertEqual(keys, [], f"對著 overlay 盲打了按鍵:{keys}")

    # ── 2. 重試後成功 → 回 200 ──────────────────────────────────────────
    async def test_enter_retry_escapes_completion_popup_first(self):
        """含路徑的指令會跳出自動完成選單,**Enter 被選單吃掉** —— 補 Enter
        之前要先 Escape 關掉它。2026-10-09 實機:`!date` 正常、含路徑的全失敗,
        reason=composer_stuck/attempts=4;Escape 之後 Enter 立刻送出。"""
        # 不忙(沒有 spinner)但字卡在框裡 = 選單吃掉 Enter 的現場。
        script = PaneScript([pane_idle_empty(), pane_holding_wrapped(),
                             pane_holding_wrapped(), pane_echoed(busy=False)])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            r = await bridge._cc_paste_text("s1", TEXT)
        keys = [c.args[3] for c in self.tmux.call_args_list
                if len(c.args) >= 4 and c.args[0] == "send-keys"]
        # Escape 必須排在補送的那個 Enter 前面
        self.assertIn("Escape", keys)
        self.assertLess(keys.index("Escape"), len(keys) - 1)
        self.assertEqual(keys[keys.index("Escape") + 1], "Enter")
        self.assertTrue(r["confirmed"])

    async def test_no_escape_while_pane_is_busy(self):
        """pane 在跑回合時 Escape = 中斷那個回合 —— 絕對不准送。"""
        script = PaneScript([pane_idle_empty(), pane_busy_holding(),
                             pane_busy_holding()])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            with self.assertRaises(bridge.HTTPException):
                await bridge._cc_paste_text("s1", TEXT)
        keys = [c.args[3] for c in self.tmux.call_args_list
                if len(c.args) >= 4 and c.args[0] == "send-keys"]
        self.assertNotIn("Escape", keys)

    async def test_retry_then_accepted(self):
        script = PaneScript([
            pane_idle_empty(),          # pre-check
            pane_busy_holding(),        # 卡著(第 1 次)
            pane_busy_holding(),        # 連續兩次 → 補 Enter
            pane_busy_holding(),        # 還卡著(第 1 次)
            pane_busy_holding(),        # 連續兩次 → 再補
            pane_echoed(),              # 送出了
        ])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            r = await bridge._cc_paste_text("s1", TEXT)
        self.assertTrue(r["confirmed"])
        self.assertEqual(r["attempts"], 2)
        self.assertEqual(r["delivery"], "queued")   # pane 仍 busy → 排隊語意

    async def test_queued_echo_never_triggers_duplicate_enter(self):
        """已排進 CLI 佇列(上方有回顯)就不准再補 Enter —— 補了會排隊兩次。"""
        script = PaneScript([pane_idle_empty(),
                             pane_queued_echo_with_lagging_composer()])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            r = await bridge._cc_paste_text("s1", TEXT)
        self.assertTrue(r["confirmed"])
        self.assertEqual(r["reason"], "echoed_in_pane")
        self.assertEqual(r["attempts"], 0)
        self.assertEqual(len(self.enters()), 1)      # 只有原本那一次 Enter

    async def test_single_lagging_snapshot_does_not_retry(self):
        """輸入框只慢一拍就補 Enter 同樣會重複送出:要連續兩次還在才補。"""
        script = PaneScript([pane_idle_empty(),
                             pane_busy_holding(),      # 慢一拍
                             pane_echoed()])           # 下一拍就清了
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            r = await bridge._cc_paste_text("s1", TEXT)
        self.assertTrue(r["confirmed"])
        self.assertEqual(r["attempts"], 0)
        self.assertEqual(len(self.enters()), 1)

    async def test_idle_accept_is_accepted_not_queued(self):
        script = PaneScript([pane_idle_empty(), pane_echoed(busy=False)])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            r = await bridge._cc_paste_text("s1", TEXT)
        self.assertEqual(r["delivery"], "accepted")
        self.assertTrue(r["confirmed"])
        self.assertEqual(r["attempts"], 0)

    async def test_hook_turn_generation_counts_as_proof(self):
        """UserPromptSubmit hook 跳號是權威證據,畫面來不及顯示也算送出。"""
        script = PaneScript([pane_idle_empty(), pane_no_composer()])

        async def bump(name):
            bridge._CC_TURN_GEN["s1"] = 7
            return pane_no_composer()

        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            task = asyncio.create_task(bridge._cc_paste_text("s1", TEXT))
            await asyncio.sleep(0.1)
            bridge._CC_TURN_GEN["s1"] = 1
            r = await task
        self.assertTrue(r["confirmed"])

    # ── 3. 不准 fail-open ───────────────────────────────────────────────
    async def test_missing_composer_is_not_success(self):
        """舊碼 `rfind("❯") < 0` 直接當送出成功 —— 這是靜默掉訊息的來源之一。
        真 CLI 實測(啟動中的信任對話框)確認:字會被打進看不見的輸入框擱淺,
        所以「整段預算都看不到輸入框」要當失敗,不是佇列。"""
        script = PaneScript([pane_idle_empty(), pane_no_composer()])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            with self.assertRaises(bridge.HTTPException) as cm:
                await bridge._cc_paste_text("s1", TEXT)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(cm.exception.code, "CC_INPUT_NOT_ACCEPTED")
        self.assertIn("composer_missing", cm.exception.message)

    async def test_scrolled_composer_is_detected_by_tail_probe(self):
        """長訊息會把輸入框**捲動**,畫面上只剩尾巴 —— 只比對前 24 字就會
        判成「字沒卡在框裡」,pane 又在忙 → 回 queued + 200 = 靜默吞訊息。

        2026-10-10 機主實害:07:28 送出一則帶附件註記(長路徑)的訊息,app
        顯示「等待背景工作結果…」等了近 20 分鐘,訊息其實一直躺在輸入框裡,
        而 bridge 回的是 200。這條釘住「尾巴看得到就算還卡著」。
        """
        long_text = "剛剛我測試 gemini 成功回覆了,也回覆很快,現在幫我看一下列表樣式," \
                    "請幫我做的跟其他一樣,然後也幫我檢查 gemini 對話欄內的功能" \
                    "是否都能夠對比 cc cx 的功能把他補全"
        # 捲動後的畫面:**開頭那段不在畫面上**,只有尾巴。
        scrolled = "\n".join([
            "  上一輪的回覆內容",
            "✽ Fiddle-faddling… (0m 57s · ↓ 1.2k tokens)",
            BORDER,
            "❯ 請幫我做的跟其他一樣,然後也幫我檢查 gemini 對話欄內的功能",
            "  是否都能夠對比 cc cx 的功能把他補全",
            BORDER,
            "  ⏵⏵ auto mode on",
        ])
        script = PaneScript([pane_idle_empty(), scrolled])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            with self.assertRaises(bridge.HTTPException) as cm:
                await bridge._cc_paste_text("s1", long_text)
        self.assertEqual(cm.exception.status_code, 409,
                         "字還卡在框裡卻回了 200 —— 這就是靜默吞訊息")

    async def test_never_rendered_while_busy_is_queued_not_success(self):
        """字沒出現在畫面、但 pane 確實在忙 → CLI 可能已收進自己的佇列,
        誠實回 queued/unconfirmed,但絕不宣稱 accepted。"""
        # 貼上前框是空的(busy 畫面裡的「別的字」是上一輪的回顯,不是殘字)。
        script = PaneScript([pane_echoed(text="別的字"),
                             pane_busy_holding(text="別的字")])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            r = await bridge._cc_paste_text("s1", TEXT)
        self.assertFalse(r["confirmed"])
        self.assertEqual(r["delivery"], "queued")
        self.assertEqual(r["reason"], "never_rendered")

    async def test_never_rendered_while_idle_is_rejected(self):
        """2026-07-29 訊息被吃掉事故:善彰 07:51 送「壓縮」,session 已閒置 4
        分鐘,bridge 回 200 + delivery=queued,app 顯示「已排入佇列,等待接手…」
        —— 但那則訊息在 CC transcript 裡從頭到尾不存在。

        待命中的 CLI 收到輸入會**立刻**開跑,所以「驗證不到 + pane 不忙」只能
        是沒送到。idle 不可能排隊,必須 fail-closed 回 409 讓 app 重送。"""
        script = PaneScript([pane_idle_empty()])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            with self.assertRaises(bridge.HTTPException) as cm:
                await bridge._cc_paste_text("s1", "壓縮")
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(cm.exception.code, "CC_INPUT_NOT_ACCEPTED")
        # 殘字要清掉,否則會變成跨重開機的殭屍草稿
        self.assertGreaterEqual(
            len([c for c in self.tmux.call_args_list
                 if len(c.args) >= 4 and c.args[3] == "C-u"]), 2)

    async def test_idle_regrasp_saves_a_slow_spinner(self):
        """判死前的最後一次回讀:Enter 落地到 spinner 畫出來有幾百毫秒空隙,
        剛好卡在那裡的送出是真的收下了,不能誤殺成 409。"""
        script = PaneScript([pane_idle_empty(), pane_idle_empty(),
                             pane_busy_holding(text="別的字")])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            r = await bridge._cc_paste_text("s1", TEXT)
        self.assertEqual(r["delivery"], "queued")

    async def test_resend_ignores_stale_echo_of_same_text(self):
        """重送同一句:畫面上舊那則的回顯不算這次送出的證據。"""
        script = PaneScript([pane_echoed(busy=True)])    # 貼上前就有同樣的字
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            r = await bridge._cc_paste_text("s1", TEXT)
        self.assertFalse(r["confirmed"])

    # ── 4. 特殊態:等待審核 ─────────────────────────────────────────────
    async def test_permission_prompt_rejected_before_pasting(self):
        script = PaneScript([pane_permission_prompt()])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            with self.assertRaises(bridge.HTTPException) as cm:
                await bridge._cc_paste_text("s1", TEXT)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(cm.exception.code, "CC_INPUT_NOT_ACCEPTED")
        self.assertEqual(self.stdin.await_count, 0)      # 根本沒貼進去
        self.assertEqual(len(self.enters()), 0)

    # ── 5. 併發送出序列化 ───────────────────────────────────────────────
    async def test_concurrent_sends_are_serialized(self):
        order = []

        async def slow_pane(name):
            order.append("capture")
            await asyncio.sleep(0)
            return pane_echoed(busy=True)

        with patch.object(bridge, "_cc_capture_pane_fresh", slow_pane):
            lock = bridge._cc_paste_lock("s1")
            self.assertFalse(lock.locked())
            await asyncio.gather(bridge._cc_paste_text("s1", "第一則"),
                                 bridge._cc_paste_text("s1", "第二則"))
        # 同一把鎖 → 兩次送出不會交錯把 C-u 插進對方的 paste/Enter 之間
        self.assertIs(bridge._cc_paste_lock("s1"), lock)
        self.assertFalse(lock.locked())

    # ── 6. HTTP 層語意 ──────────────────────────────────────────────────
    async def test_input_core_surfaces_delivery(self):
        script = PaneScript([pane_idle_empty(), pane_echoed(busy=False)])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            r = await bridge._cc_input_core("s1", {"text": TEXT, "client_id": "c1"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["delivery"], "accepted")
        self.assertTrue(r["confirmed"])

    async def test_input_core_propagates_not_accepted(self):
        script = PaneScript([pane_busy_holding()])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            with self.assertRaises(bridge.HTTPException) as cm:
                await bridge._cc_input_core("s1", {"text": TEXT, "client_id": "c2"})
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(cm.exception.code, "CC_INPUT_NOT_ACCEPTED")

    async def test_shell_mode_paste_is_accepted_not_409(self):
        """端到端:貼 `!` 開頭的指令 → 輸入框切進 shell 模式 → Enter → 指令跑掉、
        框清空回 normal → accepted。修之前這條會是 409 composer_missing。"""
        cmd = "!" + TEXT
        # 三格:① 貼上前(normal 空框)② 貼上後切進 shell 模式、字在框裡
        # ③ Enter 之後框清空回 normal,指令回顯進 transcript(普通空格)。
        shell_held = "\n".join(["  上一輪的回覆內容", BORDER,
                                f"!\u00a0{TEXT}", BORDER, "  ! for shell mode"])
        after = "\n".join(["  上一輪的回覆內容", f"! {TEXT}",
                           BORDER, "\u276f\u00a0", BORDER, "  \u23f5\u23f5 auto mode on"])
        script = PaneScript([pane_idle_empty(), shell_held, after])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            r = await bridge._cc_paste_text("s1", cmd)
        self.assertTrue(r["confirmed"])
        self.assertEqual(r["delivery"], "accepted")
        # 只有原本那一次 Enter,不准補送(shell 模式補 Enter = 指令跑第二次)
        self.assertEqual(len(self.enters()), 1)

    async def test_shell_reset_clears_residue_before_exiting(self):
        """框裡有殘字時要**清到空**才退 —— 2026-10-09 實害第二輪:
        Pocket-branch 卡著一段折成兩行的舊 `!` 指令,連 4 次
        `recovered: false`,那條 session 一則訊息都送不進去。
        病根:`C-u` 只清一行,框沒空 `BSpace` 就只是刪一個字。"""
        residue = "\n".join(["  上一輪的回覆內容", BORDER,
                             "!\u00a0sed -i '' 's|aaa|bbb|g' /private/tmp/很長的",
                             "  路徑/折到第二行",
                             BORDER, "  ! for shell mode"])
        shell_empty = "\n".join(["  上一輪的回覆內容", BORDER,
                                 "!\u00a0", BORDER, "  ! for shell mode"])
        # ① pre_pane 有殘字 ② 清一次還沒空 ③ 清乾淨 ④ 退出 shell 模式 ⑤ 驗證
        script = PaneScript([residue, residue, shell_empty,
                             pane_idle_empty(), pane_echoed(busy=True)])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            r = await bridge._cc_paste_text("s1", TEXT)
        self.assertTrue(r["confirmed"])
        calls = [c.args for c in self.tmux.call_args_list if c.args and c.args[0] == "send-keys"]
        # 逐字刪(`-N <n> BSpace`)—— 2026-10-10 實測:C-u 清不掉多行殘字,
        # C-u+BSpace 交替會誤觸自動建議把字補回來,只有逐字刪可靠。
        bulk = [a for a in calls if "-N" in a and a[-1] == "BSpace"]
        self.assertTrue(bulk, "殘字沒有被逐字刪掉 —— 又改回 C-u 了?")
        # 刪的次數要照輸入框字數給,不能只刪一兩下交差
        self.assertGreater(int(bulk[0][bulk[0].index("-N") + 1]), 20)
        # 清空之後才准送退出 shell 模式的那一下 BSpace
        plain_bspace = [i for i, a in enumerate(calls) if a[-1] == "BSpace" and "-N" not in a]
        self.assertTrue(plain_bspace, "沒有送退出 shell 模式的 BSpace")
        self.assertGreater(plain_bspace[-1], calls.index(bulk[0]),
                           "要先清空再退出,順序不能顛倒")

    def test_composer_is_empty_detects_residue(self):
        empty = "\n".join([BORDER, "!\u00a0", BORDER])
        dirty = "\n".join([BORDER, "!\u00a0還沒清掉的字", BORDER])
        self.assertTrue(bridge._cc_composer_is_empty(empty))
        self.assertFalse(bridge._cc_composer_is_empty(dirty))
        # 看不到輸入框 → 保守當成「不確定」,不可當成已清空
        self.assertFalse(bridge._cc_composer_is_empty(pane_no_composer()))

    async def test_shell_reset_waits_for_repaint(self):
        """TUI 重畫慢一拍不可以判成「退不出來」。2026-10-09 實機:立刻回讀
        讀到舊畫面 → 假的 409,使用者看到紅字、app 重送一次才成功。"""
        shell_empty = "\n".join(["  上一輪的回覆內容", BORDER,
                                 "!\u00a0", BORDER, "  ! for shell mode"])
        # ① pre_pane ② 退出後第一次回讀「還沒重畫」③ 第二次才看到 normal
        script = PaneScript([shell_empty, shell_empty, pane_idle_empty(),
                             pane_echoed(busy=True)])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            r = await bridge._cc_paste_text("s1", TEXT)
        self.assertTrue(r["confirmed"])

    async def test_shell_command_is_not_reset(self):
        """這一則本來就要走 shell —— 不准把模式退掉。"""
        shell_empty = "\n".join(["  上一輪的回覆內容", BORDER,
                                 "!\u00a0", BORDER, "  ! for shell mode"])
        held = "\n".join(["  上一輪的回覆內容", BORDER,
                          "!\u00a0!echo hi", BORDER, "  ! for shell mode"])
        after = "\n".join(["  上一輪的回覆內容", "! echo hi",
                           BORDER, "\u276f\u00a0", BORDER, "  \u23f5\u23f5 auto mode on"])
        script = PaneScript([shell_empty, held, after])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            await bridge._cc_paste_text("s1", "!echo hi")
        keys = [c.args[3] for c in self.tmux.call_args_list
                if len(c.args) >= 4 and c.args[0] == "send-keys"]
        self.assertNotIn("BSpace", keys)

    async def test_shell_mode_that_will_not_reset_is_rejected(self):
        """退不出來就 409 —— 貼下去等於拿使用者的訊息當 shell 指令跑。"""
        shell_empty = "\n".join(["  上一輪的回覆內容", BORDER,
                                 "!\u00a0", BORDER, "  ! for shell mode"])
        script = PaneScript([shell_empty])          # 怎麼讀都還在 shell 模式
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            with self.assertRaises(bridge.HTTPException) as cm:
                await bridge._cc_paste_text("s1", TEXT)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertIn("shell mode", cm.exception.message)
        # 不准把字貼進去
        self.assertEqual(len(self.stdin.call_args_list), 0)

class ComposerParsingTest(unittest.TestCase):
    def test_split_stops_at_box_border(self):
        body, region = bridge._cc_composer_split(pane_busy_holding())
        self.assertIsNotNone(region)
        self.assertIn(TEXT, region)
        self.assertNotIn("auto mode on", region)     # 框下面的 statusline 不算
        self.assertIn("上一輪的回覆內容", body)

    def test_split_returns_none_when_no_marker(self):
        self.assertIsNone(bridge._cc_composer_split(pane_no_composer())[1])

    def test_squash_kills_nbsp_and_wrap(self):
        self.assertIn(bridge._cc_squash(TEXT),
                      bridge._cc_squash(pane_holding_wrapped()))

    # ── shell 模式(輸入以 `!` 開頭)────────────────────────────────────
    #
    # 2026-10-09 機主回報:在 Pocket 的 CC 分頁貼 `!` 開頭的指令一直送不出去,
    # 官方 app 正常。實機 capture-pane 證據(Pocket-branch):
    #   normal: '\u276f\u00a0'
    #   shell : '!\u00a0 launchctl kickstart -k gui/$(id -u)/ai.studio.hermes-bridge'
    # 只認 \u276f/\u203a 的話整段驗證預算都找不到輸入框 → composer_missing →
    # 409,而且訊息被 C-u 清掉。
    def test_shell_mode_prompt_is_a_composer(self):
        pane = "\n".join(["  上一輪的回覆內容", BORDER,
                          "!\u00a0echo hello", BORDER, "  ! for shell mode"])
        body, region = bridge._cc_composer_split(pane)
        self.assertIsNotNone(region)
        self.assertIn("echo hello", region)
        self.assertNotIn("echo hello", body)

    def test_shell_mode_transcript_echo_is_not_a_composer(self):
        """transcript 的回顯用**普通空格**('! echo hello'),輸入框是 NBSP。
        拿裸 `!` 當標記就會把回顯誤判成輸入框 —— 這條釘住那個分界。"""
        pane = "\n".join(["  上一輪的回覆內容",
                          "! echo hello",          # transcript 回顯(普通空格)
                          "  \u23bf\u00a0hello",
                          BORDER, "\u276f\u00a0", BORDER, "  \u23f5\u23f5 auto mode on"])
        body, region = bridge._cc_composer_split(pane)
        self.assertIsNotNone(region)
        self.assertNotIn("echo hello", region)     # 輸入框是空的
        self.assertIn("echo hello", body)          # 回顯留在 transcript 側

    def test_context_full_regex(self):
        self.assertTrue(bridge._CC_CONTEXT_FULL_RE.search("100% context used"))
        self.assertTrue(bridge._CC_CONTEXT_FULL_RE.search("97 % Context Used"))
        self.assertFalse(bridge._CC_CONTEXT_FULL_RE.search("42% context used"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
