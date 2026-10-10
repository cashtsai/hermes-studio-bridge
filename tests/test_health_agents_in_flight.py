"""`/health?agents=1`:安全重啟要知道 CC / CX 在不在忙(2026-10-10 實害)。

機主 08:28 還在跑一個 CX 任務(rollout 顯示它正在執行 xcodebuild),08:31
照「安全重啟」動手 —— 因為 `bridge-safe-restart.sh` 只看 `turns_in_flight`
(**只數人格回合**),對 CC/CX 完全沒有概念。重啟清掉記憶體裡的 card digest,
重新播種只拿得到已落地的舊 turn,那段對話就從他視窗上消失,他看到的是
「指令跑一跑不見了」。

這裡釘住三件事:
 1. 預設 health **不付** CC 掃描的成本(看門狗每分鐘打一次)
 2. `?agents=1` 會回 CC/CX 的忙碌數
 3. CC 問不到時回 **-1**(= 不確定),呼叫端必須當成「別重啟」,不是 0
"""
import _isolation  # noqa: F401
import asyncio
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="health-agents-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class HealthAgentsInFlightTest(unittest.TestCase):
    def setUp(self):
        self.orig_cc = bridge._cc_sessions
        self.orig_turns = dict(getattr(bridge.CODEX_APP, "active_turns", {}) or {})

    def tearDown(self):
        bridge._cc_sessions = self.orig_cc
        bridge.CODEX_APP.active_turns = self.orig_turns

    def test_default_health_has_no_agent_scan(self):
        """看門狗那條路不該付掃描成本 —— 預設不帶這個欄位。"""
        async def boom():
            raise AssertionError("預設 health 不該去掃 CC")
        bridge._cc_sessions = boom
        out = asyncio.run(bridge.health())
        self.assertNotIn("agents_in_flight", out)
        self.assertIn("turns_in_flight", out)

    def test_agents_param_counts_busy_cc_and_cx(self):
        async def two_busy_one_idle():
            return [{"name": "A", "busy": True}, {"name": "B", "busy": False},
                    {"name": "C", "busy": True}]
        bridge._cc_sessions = two_busy_one_idle
        bridge.CODEX_APP.active_turns = {"t1": object(), "t2": object()}
        out = asyncio.run(bridge.health(agents=1))
        self.assertEqual(out["agents_in_flight"], {"cc": 2, "cx": 2})

    def test_cc_failure_reports_unknown_not_zero(self):
        """問不到要回 -1。回 0 等於「沒人在忙」—— 那正是會害人重啟的謊。"""
        async def boom():
            raise RuntimeError("tmux 問不到")
        bridge._cc_sessions = boom
        bridge.CODEX_APP.active_turns = {}
        out = asyncio.run(bridge.health(agents=1))
        self.assertEqual(out["agents_in_flight"]["cc"], -1)

    def test_restart_script_waits_on_agents(self):
        """腳本真的有用 ?agents=1,而且把 -1 當成不確定。"""
        sh = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "scripts/bridge-safe-restart.sh"),
            encoding="utf-8").read()
        self.assertIn("agents=1", sh, "腳本還在用不含 CC/CX 的舊 health")
        self.assertIn("agents_in_flight", sh)
        self.assertIn("cc == -1", sh, "CC 問不到時必須視為不確定,不可當 0")


if __name__ == "__main__":
    unittest.main()
