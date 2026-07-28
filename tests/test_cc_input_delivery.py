"""CC 送出交付語意(2026-07-28 靜默掉訊息事故)。

現場:CLI 忙碌 + `100% context used`,Pocket 送出的字停在 `❯` 輸入框裡沒送出,
bridge 卻回 200 → app 顯示「已送達」,訊息永遠不會被處理。

這組測試釘住三件事:
  1. 輸入行沒清空 → 一定不能回 200(丟 409 CC_INPUT_NOT_ACCEPTED + 原因)
  2. 補 Enter 後真的送出 → 回 200,且帶 delivery 語意
  3. 忙碌 / 看不到輸入框 / 從沒 render 出來 → 一律不謊稱 accepted
"""

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

    def enters(self):
        return [c for c in self.tmux.call_args_list
                if len(c.args) >= 4 and c.args[3] == "Enter"]

    def clears(self):
        return [c for c in self.tmux.call_args_list
                if len(c.args) >= 4 and c.args[3] == "C-u"]

    # ── 1. 輸入行沒清空 → 不准回 200 ────────────────────────────────────
    async def test_stranded_text_never_returns_ok(self):
        script = PaneScript([pane_busy_holding()])
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
        script = PaneScript([pane_holding_wrapped()])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            with self.assertRaises(bridge.HTTPException) as cm:
                await bridge._cc_paste_text("s1", TEXT)
        self.assertEqual(cm.exception.status_code, 409)

    async def test_stranded_reason_composer_stuck_without_context_full(self):
        script = PaneScript([pane_busy_holding(context_full=False)])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            with self.assertRaises(bridge.HTTPException) as cm:
                await bridge._cc_paste_text("s1", TEXT)
        self.assertIn("composer_stuck", cm.exception.message)

    # ── 2. 重試後成功 → 回 200 ──────────────────────────────────────────
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

    async def test_never_rendered_is_not_success(self):
        """貼上的字從沒出現在畫面(CLI 還沒把 PTY 讀走)→ 不能宣稱已送達。"""
        script = PaneScript([pane_idle_empty()])
        with patch.object(bridge, "_cc_capture_pane_fresh", script):
            r = await bridge._cc_paste_text("s1", TEXT)
        self.assertFalse(r["confirmed"])
        self.assertEqual(r["delivery"], "queued")
        self.assertEqual(r["reason"], "never_rendered")

    async def test_resend_ignores_stale_echo_of_same_text(self):
        """重送同一句:畫面上舊那則的回顯不算這次送出的證據。"""
        script = PaneScript([pane_echoed(busy=False)])   # 貼上前就有同樣的字
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
            return pane_echoed(busy=False)

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

    def test_context_full_regex(self):
        self.assertTrue(bridge._CC_CONTEXT_FULL_RE.search("100% context used"))
        self.assertTrue(bridge._CC_CONTEXT_FULL_RE.search("97 % Context Used"))
        self.assertFalse(bridge._CC_CONTEXT_FULL_RE.search("42% context used"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
