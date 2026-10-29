"""cc2(SDK 路徑)功能追平 tmux CC 的第一批(2026-10-29 機主指示)。

背景:tmux 路徑是 2026-06-27 接上的(當時 SDK 還不存在),SDK 路徑 8-16 才加、
且因為缺 claude_agent_sdk 套件從未真正啟用。套件裝好、逐字流實測通過後,
開始逐項補齊 tmux 既有能力。

本批:
  ① client_id 冪等 —— tmux 2026-10-25 已修,cc2 漏了。cc2 能跑 shell,
     重複送出 = 重複執行指令,而 cc2 還是 team worker 的預設 provider。
  ② busy_since —— app 的 WorkingBar 靠它顯示「已 N 秒」,沒有就 2 秒與 30 分
     長得一樣。
  ③ mode / model 進 status —— app 的模式選擇器與模型顯示原本永遠空白。
  ④ model 真的傳進 ClaudeAgentOptions 並持久化 —— 原本完全沒傳,cc2 無法切模型。
  ⑤ last_event_at —— v2_rows 原本硬寫 None,app 列表排序拿不到時間、永遠墊底。
"""
import _isolation  # noqa: F401
import json
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="cc2-parity-")
os.environ["CC_SDK_STATE"] = os.path.join(_TMP, "sessions.json")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cc_sdk  # noqa: E402


class CC2ParityTest(unittest.TestCase):
    def setUp(self):
        self.r = cc_sdk.CCSdkRegistry()
        self.s = self.r.ensure("t1", workdir="/tmp", permission_mode="acceptEdits")

    def test_busy_since_set_and_cleared(self):
        self.s.digest.turn_begin("turn-1")
        self.assertGreater(self.s.digest.busy_since, 0, "WorkingBar 的計時靠它")
        self.s.digest.turn_end()
        self.assertEqual(self.s.digest.busy_since, 0.0, "回合結束要收掉,不得跨回合累加")

    def test_status_carries_mode_and_model(self):
        self.s.model = "claude-sonnet-5"
        self.s.digest.model = self.s.model
        self.s.digest.turn_begin("turn-1")
        st = self.s.digest.store.status or {}
        self.assertEqual(st.get("mode"), "acceptEdits", "原本硬寫 None → 模式選擇器空白")
        self.assertEqual(st.get("model"), "claude-sonnet-5")
        self.assertTrue(st.get("busy_since"))

    def test_last_event_at_exposed_in_rows(self):
        self.s.digest.turn_begin("turn-1")
        row = self.r.v2_rows()[0]
        self.assertIsNotNone(row.get("last_event_at"), "原本硬寫 None → 列表排序永遠墊底")

    def test_model_persisted_and_restored(self):
        self.s.model = "claude-opus-4-8"
        self.r.save()
        blob = json.load(open(os.environ["CC_SDK_STATE"], encoding="utf-8"))
        self.assertEqual(blob["sessions"]["t1"]["model"], "claude-opus-4-8")
        r2 = cc_sdk.CCSdkRegistry()
        r2.load()
        self.assertEqual(r2.sessions["t1"].model, "claude-opus-4-8", "重啟後模型要還在")

    def test_old_state_without_model_still_loads(self):
        """純加法:舊 state 檔沒有 model 鍵,載入不得炸、視為跟隨預設。"""
        json.dump({"sessions": {"old": {"sdk_session_id": "", "workdir": "/tmp",
                                        "permission_mode": "acceptEdits"}}},
                  open(os.environ["CC_SDK_STATE"], "w", encoding="utf-8"))
        r2 = cc_sdk.CCSdkRegistry()
        r2.load()
        self.assertEqual(r2.sessions["old"].model, "")

    def test_row_subtitle_prefers_conversation(self):
        self.s.digest.user_card("這是我送的指令")
        row = [r for r in self.r.v2_rows() if r["id"] == "cc2:t1"][0]
        self.assertEqual(row.get("subtitle"), "這是我送的指令",
                         "有對話就該顯示它,不是只顯示 workdir")


if __name__ == "__main__":
    unittest.main()
