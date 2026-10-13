"""CC status 要讓手機看得出「在跑多久」與「在等人」。

2026-10-12 上架前的信任度稽核,兩個洞:

**① 分不出「在跑」和「死了」。** app 的 WorkingBar 只有一顆脈動點,2 秒與
20 分鐘長得一模一樣。遠端遙控最怕的就是這個 —— 使用者的唯一選項是走回電腦
前看,而那正好推翻 Pocket 的定位。bridge 本來就在 UserPromptSubmit hook 裡
知道回合何時開始,只是沒回給 app。

**② 隱形卡住(2026-08-18 事故)。** pane 停在一個 `_cc_prompt` 接不住的確認
框(當時是 `Switch model?`)→ hook 說不忙、prompt 回 None → 回給 app 的狀態
與「真的閒置」**逐欄相同**。使用者在 App 送的每個指令都進不去 modal,他以為
在改模型,其實一次都沒生效,只覺得「session 叫不起來」。
`_cc_pane_blocked` 這支判讀函式 2026-08-16 就寫好了,但在這刀之前**沒有任何
生產呼叫點**,所以洞一直開著。

釘住的事:
  ‧ busy_since 來自 UserPromptSubmit,不是「我第一次看到它在忙」
  ‧ 不知道就不報(省略欄位),不給一個會對長回合說「已 2 秒」的數字
  ‧ Stop → 清掉;看到 busy=False 也清掉(防 Stop hook 漏掉)
  ‧ blocked 只在 prompt 解不出、又不忙、又被 cc_detect 判為 blocked 時為真
    —— 不猜選項(猜了就是替使用者按鈕)
"""
import _isolation  # noqa: F401

import asyncio
import os
import sys
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="cc-status-busy-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
import cc_detect  # noqa: E402

NAME = "unit-cc-status"

# 2026-08-18 事故的那個框(model_switch_confirm 規則)。
BLOCKED_PANE = """\
────────────────────────────────────────
 Switch model?
 ❯ 1. Yes, switch to sonnet
   2. No, go back
   Enter to confirm · Esc to cancel
"""

IDLE_PANE = """\
────────────────────────────────────────
 ❯
   ? for shortcuts
"""


class _Harness(unittest.TestCase):
    """把 _cc_status_core 的外部依賴全換掉,只留要測的那段邏輯。"""

    def setUp(self):
        self.pane = IDLE_PANE
        self.hook = None
        self._saved = {}
        for attr, fn in (
            ("_tmux_alive", lambda name: self._alive),
            ("_tmux_capture_cached", lambda name: self.pane),
            ("_cc_fresh_hook_state", lambda name: self.hook),
            ("_cc_prompt", lambda pane: None),
            ("_cc_conf_rows", lambda: []),
            ("_cc_refine_q_index", lambda name, prompt: None),
        ):
            self._saved[attr] = getattr(bridge, attr)
        self._alive = True

        async def _alive_stub(name):
            return self._alive

        async def _capture_stub(name):
            return self.pane

        async def _refine_stub(name, prompt):
            return None

        bridge._tmux_alive = _alive_stub
        bridge._tmux_capture_cached = _capture_stub
        bridge._cc_fresh_hook_state = lambda name: self.hook
        bridge._cc_prompt = lambda pane: None
        bridge._cc_conf_rows = lambda: []
        bridge._cc_refine_q_index = _refine_stub
        bridge._CC_TURN_STARTED_AT.pop(NAME, None)

    def tearDown(self):
        for attr, fn in self._saved.items():
            setattr(bridge, attr, fn)
        bridge._CC_TURN_STARTED_AT.pop(NAME, None)

    def status(self):
        return asyncio.run(bridge._cc_status_core(NAME))


class BusySinceTest(_Harness):

    def test_busy_since_comes_from_hook_turn_start(self):
        started = time.time() - 300          # 跑了 5 分鐘
        bridge._CC_TURN_STARTED_AT[NAME] = started
        self.hook = {"busy": True, "updated_at": time.time()}
        st = self.status()
        self.assertTrue(st["busy"])
        self.assertAlmostEqual(st["busy_since"], started, places=3,
                               msg="必須是回合真正的起算點,不是「現在」")

    def test_unknown_start_is_omitted_not_faked(self):
        """bridge 重啟後、或回合從 Mac 直接起的,我們不知道起算點。

        這時**不給數字**:一個對跑了半小時的回合說「已 2 秒」的計時器,比沒有
        計時器更傷 —— 它會讓使用者以為剛送出去,然後等更久。
        """
        self.hook = {"busy": True, "updated_at": time.time()}
        st = self.status()
        self.assertTrue(st["busy"])
        self.assertNotIn("busy_since", st)

    def test_hook_ttl_does_not_erase_the_timer(self):
        """_CC_HOOK_STATE 有 600 秒 TTL;長回合過了 10 分鐘正是最需要
        「它還活著」訊號的時候,所以起算點刻意存在另一份沒有 TTL 的 dict。"""
        started = time.time() - 3600
        bridge._CC_TURN_STARTED_AT[NAME] = started
        self.hook = None                      # TTL 過期 → 回退 pane 判讀
        self.pane = "esc to interrupt"        # pane 說在忙
        st = self.status()
        self.assertTrue(st["busy"], "pane 說在忙就是在忙")
        self.assertAlmostEqual(st["busy_since"], started, places=3)

    def test_idle_poll_clears_a_leaked_start(self):
        """Stop hook 漏掉的防線:看到 busy=False 就清掉。

        沒有這條,下一個回合會繼承一個過早的起算點,畫面謊報「已 3 小時」。
        """
        bridge._CC_TURN_STARTED_AT[NAME] = time.time() - 9999
        self.hook = {"busy": False, "updated_at": time.time()}
        st = self.status()
        self.assertFalse(st["busy"])
        self.assertNotIn("busy_since", st)
        self.assertNotIn(NAME, bridge._CC_TURN_STARTED_AT)

    def test_dead_tmux_clears_start(self):
        bridge._CC_TURN_STARTED_AT[NAME] = time.time()
        self._alive = False
        st = self.status()
        self.assertFalse(st["running"])
        self.assertNotIn(NAME, bridge._CC_TURN_STARTED_AT)


class BlockedTest(_Harness):

    def test_the_2026_08_18_pane_is_reported_as_blocked(self):
        self.pane = BLOCKED_PANE
        self.hook = {"busy": False, "updated_at": time.time()}
        st = self.status()
        self.assertFalse(st["busy"])
        self.assertIsNone(st["prompt"], "前提:_cc_prompt 接不住這個框")
        self.assertTrue(st["blocked"],
                        "它在等人 —— 這正是「session 叫不起來」的那個狀態")

    def test_cc_detect_really_classifies_that_pane_as_blocked(self):
        """前提自檢:上面那條靠的是 manifest 規則,規則漂了這裡會先紅。"""
        self.assertEqual(cc_detect.classify(BLOCKED_PANE)["state"], "blocked")


    def test_plain_idle_is_not_blocked(self):
        """不得誤報 —— 誤報的代價是使用者去找一個不存在的確認框。"""
        self.pane = IDLE_PANE
        self.hook = {"busy": False, "updated_at": time.time()}
        self.assertFalse(self.status()["blocked"])

    def test_busy_is_never_blocked(self):
        self.pane = BLOCKED_PANE
        self.hook = {"busy": True, "updated_at": time.time()}
        self.assertFalse(self.status()["blocked"], "在跑就不是在等人")

    def test_parsed_prompt_wins_over_blocked(self):
        """解得出選項 → app 已經有答題卡可按,不要再多一條「它在等人」。"""
        self.pane = BLOCKED_PANE
        self.hook = {"busy": False, "updated_at": time.time()}
        bridge._cc_prompt = lambda pane: {"semantic": "permission",
                                          "options": ["Yes", "No"]}
        self.assertFalse(self.status()["blocked"])

    def test_blocked_field_always_present(self):
        """app 可以無條件讀這欄,不必區分「沒這欄」與「False」。"""
        self.hook = {"busy": False, "updated_at": time.time()}
        self.assertIn("blocked", self.status())


class WiringTest(unittest.TestCase):
    """這個 class **不** stub 任何東西 —— 問的是真貨。"""

    def test_real_cc_prompt_cannot_parse_the_blocked_pane(self):
        """**前提的真正自檢。**

        BlockedTest 把 `_cc_prompt` 換成回 None 的假貨。如果真貨其實解得出這個
        框,那整條修就是在補一個不存在的洞 —— 而測試會一直綠。這條直接問真貨。
        """
        self.assertIsNone(bridge._cc_prompt(BLOCKED_PANE),
                          "真的 _cc_prompt 若解得出來,blocked 這條就沒必要")
        self.assertFalse(bridge._cc_pane_busy(BLOCKED_PANE))
        self.assertTrue(bridge._cc_pane_blocked(BLOCKED_PANE))


    def test_hook_commit_records_and_clears_turn_start(self):
        body = {"cwd": "/tmp", "last_assistant_message": "done"}
        bridge._CC_TURN_STARTED_AT.pop(NAME, None)
        bridge._cc_hook_commit(NAME, "UserPromptSubmit", body, "", "test")
        self.assertIn(NAME, bridge._CC_TURN_STARTED_AT)
        bridge._cc_hook_commit(NAME, "Stop", body, "", "test")
        self.assertNotIn(NAME, bridge._CC_TURN_STARTED_AT)

    def test_new_turn_resets_the_clock(self):
        bridge._CC_TURN_STARTED_AT[NAME] = time.time() - 9999
        bridge._cc_hook_commit(NAME, "UserPromptSubmit", {"cwd": "/tmp"}, "", "t")
        self.assertLess(time.time() - bridge._CC_TURN_STARTED_AT[NAME], 5,
                        "新輸入 = 新回合,舊起算點必須被覆寫")
        bridge._CC_TURN_STARTED_AT.pop(NAME, None)

    def test_v2_card_follower_forwards_both_fields(self):
        """**出貨版的 CC 畫面走卡片流**,不是 v1 /ccsessions status。

        follower 的 status 是逐欄手組的 —— 漏掉這兩欄,整個修就只在使用者關掉
        卡片流旗標時才有效(= 沒人看得到的修)。這正是 `_cc_pane_blocked` 當年
        的下場:函式寫好了,沒接線,洞開兩個月。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "bridge.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('status["busy_since"] = st["busy_since"]', src)
        self.assertIn('status["blocked"] = True', src)

    def test_pane_blocked_has_a_production_call_site(self):
        """這支 2026-08-16 寫好但沒人呼叫,洞因此開到 10-13。別再變孤兒。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "bridge.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertGreaterEqual(src.count("_cc_pane_blocked("), 2,
                                "定義 + 至少一個呼叫點")


if __name__ == "__main__":
    unittest.main(verbosity=2)
