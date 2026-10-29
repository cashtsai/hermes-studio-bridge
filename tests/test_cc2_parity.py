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

    def test_user_card_carries_attachments_and_client_id(self):
        """app 要顯示縮圖、要把待送泡泡對上這張卡 —— 兩個欄位缺一都不行。"""
        atts = [{"path": "/tmp/a.png", "available": True,
                 "kind": "image", "filename": "a.png"}]
        self.s.digest.user_card("看這張圖", attachments=atts, client_id="cid-1")
        body = list(self.s.digest.store.cards.values())[-1]["body"]
        self.assertEqual(body["attachments"][0]["path"], "/tmp/a.png")
        self.assertEqual(body["client_id"], "cid-1")

    def test_queue_preserves_attachments(self):
        """忙碌時排隊的那則,附件不能在排空時掉。"""
        import asyncio
        atts = [{"path": "/tmp/b.png", "available": True}]
        self.s.busy = True
        res = asyncio.run(self.s.submit("排隊的", attachments=atts, client_id="cid-2"))
        self.assertTrue(res["queued"])
        self.assertEqual(self.s.queue[0][0], "排隊的")
        self.assertEqual(self.s.queue[0][1], atts)
        self.assertEqual(self.s.queue[0][2], "cid-2")

    def test_row_subtitle_prefers_conversation(self):
        self.s.digest.user_card("這是我送的指令")
        row = [r for r in self.r.v2_rows() if r["id"] == "cc2:t1"][0]
        self.assertEqual(row.get("subtitle"), "這是我送的指令",
                         "有對話就該顯示它,不是只顯示 workdir")


if __name__ == "__main__":
    unittest.main()


class CC2StructuralParityTest(unittest.TestCase):
    """結構性三項:終端交接、重啟復原、按目錄續接(2026-10-29)。

    這三項原本被判定「SDK 路徑做不到」,因為它們依賴 tmux 的常駐 pane。
    實際上 SDK/CLI 提供了足夠的原語:session id 可續接、`~/.claude/sessions/`
    有活著的持有者登記、`list_sessions` 可按目錄找最近一條。
    """

    def setUp(self):
        self.r = cc_sdk.CCSdkRegistry()

    def test_project_key_matches_sdk(self):
        """歷史目錄名算錯 → _seed_history 靜默找不到檔案 → 歷史整段不見。"""
        from claude_agent_sdk import project_key_for_directory as official
        for d in ("/tmp/foo", "/Users/xcash/apps/studio-os"):
            self.assertEqual(cc_sdk._project_key(d), official(d),
                             "必須與 SDK 官方演算法一致(realpath + 非英數轉 -)")

    def test_dotted_path_no_longer_naive(self):
        """舊寫法 replace('/','-') 會把點號留著 → 對不上真實目錄。"""
        key = cc_sdk._project_key("/Users/xcash/apps/foo.bar")
        self.assertNotIn(".", key, "點號必須被正規化掉")

    def test_live_holder_detection_ignores_dead_pid(self):
        """殘留的登記檔不該擋住續接(pid 已死 = 沒人在用)。"""
        live = cc_sdk._live_session_ids()
        self.assertIsInstance(live, set)

    def test_adopt_by_cwd_can_be_disabled(self):
        """接到不相干對話時的逃生口。"""
        os.environ["CC_SDK_ADOPT_BY_CWD"] = "0"
        try:
            s = self.r.ensure("adopt-off", workdir="/tmp")
            self.assertEqual(s.sdk_session_id, "", "關掉就不該自動接")
        finally:
            os.environ.pop("CC_SDK_ADOPT_BY_CWD", None)

    def test_interrupted_turn_is_persisted_and_announced(self):
        """重啟後要誠實說上一則沒跑完,而不是安靜地什麼都沒發生。"""
        s = self.r.ensure("restart-case", workdir="/tmp")
        s.busy = True
        s.in_flight_prompt = "幫我部署到正式環境"
        self.r.save()
        blob = json.load(open(os.environ["CC_SDK_STATE"], encoding="utf-8"))
        self.assertEqual(blob["sessions"]["restart-case"]["in_flight"],
                         "幫我部署到正式環境")
        r2 = cc_sdk.CCSdkRegistry()
        r2.load()
        texts = [str((c.get("body") or {}).get("text") or "")
                 for c in r2.sessions["restart-case"].digest.store.cards.values()]
        self.assertTrue(any("被中斷" in t for t in texts), "要出一張說明卡")
        self.assertFalse(any("自動重送" in t for t in texts),
                         "絕不自動重送 —— 工具副作用可能已執行一半")

    def test_queue_survives_restart(self):
        s = self.r.ensure("queue-case", workdir="/tmp")
        s.queue = [("排隊的一則", [], "")]
        self.r.save()
        r2 = cc_sdk.CCSdkRegistry()
        r2.load()
        self.assertEqual(r2.sessions["queue-case"].queue, ["排隊的一則"])
