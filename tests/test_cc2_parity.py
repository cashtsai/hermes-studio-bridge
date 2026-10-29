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
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="cc2-parity-")
os.environ["CC_SDK_STATE"] = os.path.join(_TMP, "sessions.json")
# 測試隔離:不准碰機主真實的 ~/.claude。
# 原本只隔離了 state 檔,但 `ensure(workdir="/tmp")` 仍會走「按目錄接回上一段
# 對話」去 list_sessions 真實環境,把真的 /tmp session 接進來、再把它的
# transcript 當歷史種子讀進卡片庫 —— 測試結果於是取決於機主機器上剛好有什麼
# 對話。2026-10-29 真的踩到:一條新跑的 /tmp session 讓「全新 session 應為空」
# 這條測試失敗。兩個旋鈕都要關掉。
os.environ["CC_SDK_ADOPT_BY_CWD"] = "0"
os.environ["CC_SDK_CLAUDE_PROJECTS"] = os.path.join(_TMP, "projects")
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


class HolderDetectionDirection(unittest.TestCase):
    """`os.kill(pid, 0)` 的兩種失敗**語意完全相反** —— 合在一起就是對話損壞。

    `_live_session_ids()` 回「正被別人持有」的 session。它是唯一的防線:
    SDK 與 CLI 都沒有檔案鎖,兩個寫入者同時接同一條會讓 transcript 的父子鏈
    分岔 = **對話永久損壞**。

      ProcessLookupError → 行程不在     → 沒人佔用 → 可以接
      PermissionError    → **行程存在** → 有人佔用 → 不可以接

    原本兩者都 `continue`(當成沒人佔用),於是會去接別人正拿著的 session。
    這條在 cc2 成為現役預設 provider 之後風險是實際的。
    """

    def _with_registry(self, pid_value, kill_effect):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "x.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": pid_value, "sessionId": "sdk-held"}, f)
        with patch("glob.glob", lambda _p: [os.path.join(d, "x.json")]), \
             patch("os.kill", side_effect=kill_effect):
            return cc_sdk._live_session_ids()

    def test_permission_error_means_occupied(self):
        """別人的行程還活著 —— 這是最危險的那一格,不可以判成空閒。"""
        self.assertIn("sdk-held",
                      self._with_registry(4242, PermissionError()))

    def test_process_gone_means_free(self):
        self.assertNotIn("sdk-held",
                         self._with_registry(4242, ProcessLookupError()))

    def test_unreadable_pid_is_treated_as_occupied(self):
        """判斷不了就當成有人佔用 —— 少接一條只是不方便,接錯是對話損壞。"""
        self.assertIn("sdk-held",
                      self._with_registry("not-a-pid", ValueError()))


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
        for d in ("/tmp/foo", "/Users/you/apps/studio-os"):
            self.assertEqual(cc_sdk._project_key(d), official(d),
                             "必須與 SDK 官方演算法一致(realpath + 非英數轉 -)")

    def test_dotted_path_no_longer_naive(self):
        """舊寫法 replace('/','-') 會把點號留著 → 對不上真實目錄。"""
        key = cc_sdk._project_key("/Users/you/apps/foo.bar")
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
        """排隊項目要**完整**存活:原本只落文字,重啟後附件與 client_id 都掉了
        —— 圖不見了、app 的待送泡泡也對不上(2026-10-29 複查抓到)。"""
        s = self.r.ensure("queue-case", workdir="/tmp")
        atts = [{"path": "/tmp/q.png", "available": True}]
        s.queue = [("排隊的一則", atts, "cid-q")]
        self.r.save()
        r2 = cc_sdk.CCSdkRegistry()
        r2.load()
        self.assertEqual(r2.sessions["queue-case"].queue,
                         [("排隊的一則", atts, "cid-q")])

    def test_old_string_queue_format_still_loads(self):
        """升級當下佇列裡還有舊格式(純字串)不得炸。"""
        json.dump({"sessions": {"oldq": {"workdir": "/tmp", "sdk_session_id": "",
                                         "permission_mode": "acceptEdits",
                                         "queued": ["舊格式那一則"]}}},
                  open(os.environ["CC_SDK_STATE"], "w", encoding="utf-8"))
        r2 = cc_sdk.CCSdkRegistry()
        r2.load()
        self.assertEqual(r2.sessions["oldq"].queue, [("舊格式那一則", [], "")])


class CC2ManagementParityTest(unittest.TestCase):
    """管理操作與推播(2026-10-29 第三批):改名/封存/刪除/回合完成推播。"""

    def setUp(self):
        self.r = cc_sdk.CCSdkRegistry()

    def test_archived_hidden_from_list_but_kept(self):
        s = self.r.ensure("arch-1", workdir="/tmp")
        self.assertTrue(any(r["id"] == "cc2:arch-1" for r in self.r.v2_rows()))
        s.archived = True
        self.assertFalse(any(r["id"] == "cc2:arch-1" for r in self.r.v2_rows()),
                         "封存要從列表消失")
        self.assertIn("arch-1", self.r.sessions, "但對話本身不刪")

    def test_archived_survives_restart(self):
        self.r.ensure("arch-2", workdir="/tmp").archived = True
        self.r.save()
        r2 = cc_sdk.CCSdkRegistry(); r2.load()
        self.assertTrue(r2.sessions["arch-2"].archived)

    def test_turn_end_push_hook_fires(self):
        seen = []
        cc_sdk.configure(turn_end_push=lambda n, s: seen.append((n, s)))
        try:
            s = self.r.ensure("push-1", workdir="/tmp")
            s.digest.turn_begin("t1")
            s.digest.text_delta("回覆內容")
            s.digest.turn_end()
            # 推播是在 _run_turn_safe 的 finally 觸發,這裡直接驗掛鉤可用
            cc_sdk.TURN_END_PUSH("push-1", s.digest.last_assistant_text())
            self.assertEqual(seen[-1][0], "push-1")
            self.assertIn("回覆內容", seen[-1][1])
        finally:
            cc_sdk.TURN_END_PUSH = None

    def test_last_assistant_text_picks_assistant_only(self):
        s = self.r.ensure("push-2", workdir="/tmp")
        s.digest.user_card("我問的")
        s.digest.turn_begin("t1")
        s.digest.text_delta("它答的")
        self.assertEqual(s.digest.last_assistant_text(), "它答的",
                         "摘要不能拿到使用者自己說的話")

    def test_archived_session_does_not_push(self):
        """封存的 session 不該再推播打擾。"""
        s = self.r.ensure("push-3", workdir="/tmp")
        s.archived = True
        self.assertTrue(s.archived)


class CC2AskUserQuestionTest(unittest.TestCase):
    """多選提問(2026-10-29 機主:「沒有多選提問到底怎麼使用,遇到不就卡住了」)。

    CLI 的 AskUserQuestion 是一個**工具**,所以會進 `can_use_tool`。原本 cc2
    把它當一般工具,卡片只有「允許/拒絕」—— 使用者看不到選項、只能盲選,
    而且允許之後工具也拿不到答案。現在:選項攤成卡片按鈕,選擇用
    `PermissionResultAllow(updated_input=...)` 寫回工具輸入。
    """

    def setUp(self):
        self.r = cc_sdk.CCSdkRegistry()
        self.s = self.r.ensure("ask-1", workdir="/tmp")
        self.inp = {"questions": [{
            "question": "要用哪個資料庫?", "header": "DB", "multiSelect": False,
            "options": [{"label": "PostgreSQL", "description": "關聯式"},
                        {"label": "SQLite", "description": "輕量"}]}]}

    def test_options_become_card_buttons(self):
        opts, title, multi = cc_sdk.CCSdkSession._ask_question_options(self.inp)
        labels = [o["label"] for o in opts]
        self.assertIn("PostgreSQL", labels)
        self.assertIn("SQLite", labels)
        self.assertEqual(title, "要用哪個資料庫?")
        self.assertFalse(multi)

    def test_skip_button_always_present(self):
        """CLI 原生 UI 永遠有 Skip —— 不能讓人無路可走。"""
        opts, _, _ = cc_sdk.CCSdkSession._ask_question_options(self.inp)
        self.assertEqual(opts[-1]["key"], "ask:skip")

    def test_multiselect_flag_detected(self):
        inp = {"questions": [{"question": "選多個", "multiSelect": True,
                              "options": [{"label": "A"}, {"label": "B"}]}]}
        _, _, multi = cc_sdk.CCSdkSession._ask_question_options(inp)
        self.assertTrue(multi)

    def test_non_question_tool_unaffected(self):
        """一般工具不受影響,仍走允許/拒絕。"""
        opts, title, _ = cc_sdk.CCSdkSession._ask_question_options(
            {"command": "ls -la"})
        self.assertEqual(opts, [])
        self.assertEqual(title, "")

    def test_malformed_input_does_not_crash(self):
        """壞資料不能炸掉審批流程(會卡住整個回合)。"""
        for bad in ({}, {"questions": []}, {"questions": [{}]},
                    {"questions": "not-a-list"},
                    {"questions": [{"options": []}]}):
            opts, _, _ = cc_sdk.CCSdkSession._ask_question_options(bad)
            self.assertEqual(opts, [], f"壞輸入要安全退回一般審批: {bad}")

    def test_decide_accepts_ask_keys(self):
        """ask:N / ask:skip 要被決議端點接受,否則按了沒反應 = 真的卡住。"""
        import asyncio
        self.r.pending_approvals["aid-1"] = {
            "event": asyncio.Event(), "decision": None,
            "record": {"id": "aid-1"}, "session": "ask-1"}
        self.assertEqual(self.r.decide_approval("aid-1", "ask:0"), "approved")
        self.assertEqual(self.r.pending_approvals["aid-1"]["decision"], "ask:0")

    def test_options_capped(self):
        """選項太多時截斷,卡片不會爆版。"""
        inp = {"questions": [{"question": "多", "options":
                              [{"label": f"opt{i}"} for i in range(20)]}]}
        opts, _, _ = cc_sdk.CCSdkSession._ask_question_options(inp)
        self.assertLessEqual(len(opts), 7, "含略過最多 7 顆")


class CC2HistoryCompactTest(unittest.TestCase):
    """歷史分頁與壓縮(2026-10-29 收尾批)。"""

    def setUp(self):
        self.r = cc_sdk.CCSdkRegistry()
        self.s = self.r.ensure("hist-1", workdir="/tmp")

    def test_history_page_without_session_id(self):
        """全新 session 沒有 transcript → 安靜回空,不炸。"""
        out = self.s.history_page()
        self.assertEqual(out["messages"], [])
        self.assertFalse(out["more"])

    def test_history_page_reads_real_transcript(self):
        """真的讀得到 jsonl,並且回傳 role/text/ts 三欄。"""
        import json as _j
        sid = "test-" + "a" * 8
        d = os.path.join(_TMP, "projects", cc_sdk._project_key("/tmp"))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{sid}.jsonl"), "w", encoding="utf-8") as f:
            for i in range(5):
                f.write(_j.dumps({"type": "user", "timestamp": 1700000000 + i,
                                  "message": {"content": f"第{i}則"}}) + "\n")
        old = cc_sdk._claude_projects_root
        cc_sdk._claude_projects_root = lambda: os.path.join(_TMP, "projects")
        try:
            self.s.sdk_session_id = sid
            out = self.s.history_page(limit=2)
            self.assertEqual(len(out["messages"]), 2, "limit 要生效")
            self.assertTrue(out["more"], "還有更早的要講")
            self.assertEqual(out["total"], 5)
            self.assertIn("role", out["messages"][0])
            # offset 往回翻
            older = self.s.history_page(limit=2, offset=2)
            self.assertNotEqual(older["messages"], out["messages"])
        finally:
            cc_sdk._claude_projects_root = old

    def test_compact_refused_while_busy(self):
        """忙碌時壓縮會變成排隊,語意就不是「現在壓縮」—— 要明說。"""
        import asyncio
        self.s.busy = True
        res = asyncio.run(self.s.compact())
        self.assertFalse(res["ok"])
        self.assertIn("還在跑", res["message"])


class CC2AnswerDeliveryTest(unittest.TestCase):
    """AskUserQuestion 的答案**真的送到模型**(2026-10-29 修正)。

    原本的做法是 `PermissionResultAllow(updated_input=...)` 把答案塞進
    tool_input —— 2026-10-29 用真 SDK + 真 CLI 2.1.207 實測,工具結果一律是
    "The user did not answer the questions."，使用者按了等於白按。
    答案是 CLI 互動式 UI 自己收的,不在 tool_input 裡。
    可行的管道是 deny 的 message:實測它會原文變成 tool_result 進到模型眼前。
    """

    def setUp(self):
        self.r = cc_sdk.CCSdkRegistry()
        self.s = self.r.ensure("ans-1", workdir="/tmp")
        self.s.digest.store.turn_id = "turn-ans"

    _INPUT = {"questions": [{
        "question": "要用哪個資料庫?", "header": "資料庫", "multiSelect": False,
        "options": [{"label": "Postgres"}, {"label": "SQLite"}]}]}

    def _decide(self, key, tool_input=None):
        """跑一次 _can_use_tool,並在它等待時送出決議。回傳 SDK result 物件。"""
        import asyncio

        async def run():
            task = asyncio.ensure_future(self.s._can_use_tool(
                "AskUserQuestion", tool_input or self._INPUT, None))
            for _ in range(200):           # 等卡片/等待者登記好
                await asyncio.sleep(0.005)
                if self.r.pending_approvals:
                    break
            aid = next(iter(self.r.pending_approvals))
            if key is not None:
                self.r.decide_approval(aid, key)
            else:                           # 模擬逾時
                self.r.pending_approvals[aid]["event"].set()
            return await task

        return asyncio.run(run())

    def test_answer_reaches_model_as_tool_result(self):
        res = self._decide("ask:1")
        self.assertEqual(res.behavior, "deny",
                         "必須走 deny 的 message 管道 —— allow 的話工具會回"
                         "「沒人回答」,答案根本到不了模型")
        self.assertIn("SQLite", res.message, "選的那一項要出現在工具結果裡")
        self.assertIn("[User answered", res.message,
                      "用 CLI 原生 TUI 自己的格式,模型 native 看得懂")
        self.assertFalse(getattr(res, "interrupt", False),
                         "回答問題不是危險操作,不得打斷回合")

    def test_answer_uses_header_not_raw_question(self):
        res = self._decide("ask:0")
        self.assertIn("資料庫", res.message)
        self.assertIn("Postgres", res.message)

    def test_skip_returns_cli_native_wording(self):
        res = self._decide("ask:skip")
        self.assertEqual(res.behavior, "deny")
        self.assertEqual(res.message, "The user did not answer the questions.",
                         "略過要回 CLI 原生那句,模型才知道這是「沒答」而非錯誤")
        self.assertFalse(getattr(res, "interrupt", False))

    def test_timeout_is_skip_not_hard_deny(self):
        """逾時 = 略過,不是 fail-closed 打斷 —— 打斷的話整個回合白跑。"""
        res = self._decide(None)
        self.assertEqual(res.message, "The user did not answer the questions.")
        self.assertFalse(getattr(res, "interrupt", False))

    def test_multiselect_says_only_one_picked(self):
        """多選題但卡片一次只收一顆 → 要明講,不能讓模型以為使用者棄選其餘。"""
        inp = json.loads(json.dumps(self._INPUT))
        inp["questions"][0]["multiSelect"] = True
        res = self._decide("ask:1", inp)
        self.assertIn("SQLite", res.message)
        self.assertIn("只選了這一項", res.message)

    def test_malformed_input_falls_back_to_normal_approval(self):
        """壞資料不准炸掉審批流程(炸掉才是真的卡住整個回合)。"""
        for bad in ({}, {"questions": []}, {"questions": [{"options": []}]},
                    {"questions": "nope"}, {"questions": [None]}):
            opts, title, multi = cc_sdk.CCSdkSession._ask_question_options(bad)
            self.assertEqual(opts, [], f"{bad} 應退回一般審批")


class CC2AuditFixesTest(unittest.TestCase):
    """2026-10-29 稽核抓到的其餘缺陷。"""

    def setUp(self):
        self.r = cc_sdk.CCSdkRegistry()
        self.s = self.r.ensure("aud-1", workdir="/tmp",
                               permission_mode="acceptEdits")

    def test_push_summary_skips_tool_and_hint_cards(self):
        """推播摘要不能抓到工具卡或終端接手提示卡。

        兩者 role 都是 assistant,只跑了工具沒出文字的回合會把
        「🔧 Bash: git status」推到手機上。
        """
        self.s.digest.store.turn_id = "t"
        self.s.digest.tool_card("Bash", "git status")
        self.s.sdk_session_id = "abc123"
        self.s._upsert_resume_hint_card()
        self.assertEqual(self.s.digest.last_assistant_text(), "",
                         "沒有真回覆就該回空,讓呼叫端自己決定怎麼說")
        self.s.digest.assistant_final("好了,我改完了。")
        self.s.digest.tool_card("Read", "foo.py")   # 真回覆之後又跑了工具
        self.assertEqual(self.s.digest.last_assistant_text(), "好了,我改完了。")

    def test_ensure_mirrors_mode_same_call(self):
        """同一次 POST 改權限模式,狀態鏡像就要是新值(原本慢一拍)。"""
        self.r.ensure("aud-1", permission_mode="plan")
        self.assertEqual(self.s.permission_mode, "plan")
        self.assertEqual(self.s.digest.mode, "plan",
                         "原本先鏡像再改值 → 要再 POST 一次才追上")

    def test_extra_permission_modes_not_silently_clamped(self):
        """dontAsk/auto 是 SDK 真的支援的模式,不准被吞成 acceptEdits。

        吞掉的後果是使用者選了 dontAsk 卻拿到自動接受編輯 —— 比沒做還糟。
        """
        for mode in ("dontAsk", "auto"):
            self.assertIn(mode, cc_sdk.PERMISSION_MODES)
            self.assertEqual(cc_sdk._clamp_mode(mode), mode)
        self.assertEqual(cc_sdk._clamp_mode("yolo"), "acceptEdits")

    def test_history_spans_self_heal_session_ids(self):
        """自癒換過 sdk_session_id 之後,換 id 前的對話還要翻得回來。"""
        # 不靠模組層的 CC_SDK_CLAUDE_PROJECTS —— 全套跑時別的測試模組會蓋掉它
        # (誰最後 import 誰贏),測試就會依賴載入順序。這裡直接攔函式。
        root = os.path.join(_TMP, "projects")
        d = os.path.join(root, cc_sdk._project_key("/tmp"))
        os.makedirs(d, exist_ok=True)
        for sid, text in (("old-sid", "換id前說的話"), ("new-sid", "換id後說的話")):
            with open(os.path.join(d, f"{sid}.jsonl"), "w", encoding="utf-8") as f:
                f.write(json.dumps({"type": "user", "timestamp": 1700000000,
                                    "message": {"content": text}}) + "\n")
        self.s.prior_session_ids = ["old-sid"]
        self.s.sdk_session_id = "new-sid"
        old = cc_sdk._claude_projects_root
        cc_sdk._claude_projects_root = lambda: root
        try:
            texts = [m["text"] for m in self.s.history_page(limit=10)["messages"]]
        finally:
            cc_sdk._claude_projects_root = old
        self.assertIn("換id前說的話", texts,
                      "不留舊 id 的話那段對話永久失聯")
        self.assertIn("換id後說的話", texts)

    def test_prior_session_ids_persisted(self):
        """舊 id 要落地 —— 只存記憶體的話重啟後歷史就斷了。"""
        self.s.prior_session_ids = ["sid-a", "sid-b"]
        self.r.save()
        blob = json.load(open(os.environ["CC_SDK_STATE"], encoding="utf-8"))
        self.assertEqual(
            blob["sessions"]["aud-1"]["prior_session_ids"], ["sid-a", "sid-b"])

    def test_blocked_flag_in_status(self):
        """卡在等人核准 = blocked(欄位名與 tmux 一致,app 不用分 provider)。"""
        self.s.digest._status()
        self.assertFalse((self.s.digest.store.status or {}).get("blocked"))
        self.s.digest.prompt = "等待核准"
        self.s.digest._status()
        self.assertTrue((self.s.digest.store.status or {}).get("blocked"))

    def test_spawn_opts_whitelist(self):
        """白名單外的鍵不准落地(這份 dict 會進 state 檔並直通 SDK 建構子)。

        `allowed_tools` 曾經在白名單裡(我上一批自己加的),後來查出它會
        **靜默繞過 Pocket 審批閘** → 移出並改為明確拒收,見
        `CC2SpawnPinSafetyTest`。
        """
        out = cc_sdk.sanitize_spawn_opts({
            "effort": "high", "max_thinking_tokens": "4096",
            "cli_path": "/evil/claude", "env": {"X": "1"},
            "can_use_tool": "nope"})
        self.assertEqual(out["effort"], "high")
        self.assertEqual(out["max_thinking_tokens"], 4096, "字串數字要轉型")
        for bad in ("cli_path", "env", "can_use_tool", "allowed_tools"):
            self.assertNotIn(bad, out)

    def test_tool_images_become_attachment_card(self):
        """工具回傳的圖要進卡片庫(原本 cc2 整個不看工具結果 → media 永遠空)。"""
        captured = []

        def sink_factory(session_id):
            def sink(mime, b64):
                captured.append((session_id, mime))
                return {"media_id": "m1", "download_url": "/d/m1",
                        "filename": "shot.png", "mime": mime}
            return sink

        class FakeToolResult:
            def __init__(self):
                self.content = [{"type": "image",
                                 "source": {"type": "base64",
                                            "media_type": "image/png",
                                            "data": "AAAA"}}]
        FakeToolResult.__name__ = "ToolResultBlock"

        class FakeMsg:
            content = [FakeToolResult()]

        old = cc_sdk.MEDIA_SINK
        cc_sdk.MEDIA_SINK = sink_factory
        try:
            self.s.digest.store.turn_id = "t"
            self.s._collect_tool_images(FakeMsg())
        finally:
            cc_sdk.MEDIA_SINK = old
        self.assertEqual(captured, [("cc2:aud-1", "image/png")])
        cards = list(self.s.digest.store.cards.values())
        atts = [c for c in cards
                if (c.get("body") or {}).get("attachments")]
        self.assertEqual(len(atts), 1, "要出一張 attachment 卡")
        self.assertEqual(atts[0]["body"]["attachments"][0]["media_id"], "m1")

    def test_media_sink_failure_does_not_break_turn(self):
        """圖入庫壞掉絕不影響回合(文字回覆比圖重要)。"""
        def sink_factory(session_id):
            def sink(mime, b64):
                raise RuntimeError("media store 掛了")
            return sink

        class FakeToolResult:
            content = [{"type": "image",
                        "source": {"media_type": "image/png", "data": "AAAA"}}]
        FakeToolResult.__name__ = "ToolResultBlock"

        class FakeMsg:
            content = [FakeToolResult()]

        old = cc_sdk.MEDIA_SINK
        cc_sdk.MEDIA_SINK = sink_factory
        try:
            self.s._collect_tool_images(FakeMsg())   # 不該拋
        finally:
            cc_sdk.MEDIA_SINK = old


class CC2ToolResultTest(unittest.TestCase):
    """工具結果要看得到(2026-10-29)。

    cc2 原本只出「🔧 Bash: git status」,**結果從來不顯示** —— 使用者看得到
    它跑了什麼,看不到跑出什麼。tmux 路徑在 pane 裡本來就看得到。
    """

    def setUp(self):
        self.r = cc_sdk.CCSdkRegistry()
        self.s = self.r.ensure("tr-1", workdir="/tmp")
        self.s.digest.store.turn_id = "turn-tr"

    def _result_msg(self, use_id, content, is_error=False):
        class FakeToolResult:
            pass
        FakeToolResult.__name__ = "ToolResultBlock"
        b = FakeToolResult()
        b.tool_use_id = use_id
        b.content = content
        b.is_error = is_error

        class FakeMsg:
            pass
        m = FakeMsg()
        m.content = [b]
        return m

    def _card_text(self, cid_fragment="t"):
        for c in self.s.digest.store.cards.values():
            if (c.get("body") or {}).get("origin") == "cc2.tool":
                return str((c.get("body") or {}).get("text") or "")
        return ""

    def test_result_backfills_same_card(self):
        self.s.digest.tool_card("Bash", "git status", use_id="tu_1")
        before = len(self.s.digest.store.cards)
        self.s._collect_tool_results(
            self._result_msg("tu_1", "On branch main\nnothing to commit"))
        self.assertEqual(len(self.s.digest.store.cards), before,
                         "要回填同一張卡,不得另開新卡(手機上會洗版)")
        txt = self._card_text()
        self.assertIn("git status", txt, "呼叫摘要要留著")
        self.assertIn("nothing to commit", txt, "結果要看得到")

    def test_list_content_text_parts_collected(self):
        self.s.digest.tool_card("Read", "foo.py", use_id="tu_2")
        self.s._collect_tool_results(self._result_msg(
            "tu_2", [{"type": "text", "text": "def main():"},
                     {"type": "image", "source": {"data": "x"}}]))
        self.assertIn("def main():", self._card_text())

    def test_error_result_marked(self):
        self.s.digest.tool_card("Bash", "false", use_id="tu_3")
        self.s._collect_tool_results(
            self._result_msg("tu_3", "command failed", is_error=True))
        self.assertIn("⚠️ 失敗", self._card_text())

    def test_long_result_truncated_and_says_so(self):
        """編譯輸出動輒上萬字:截斷並明說,不要把卡片流撐爆。"""
        self.s.digest.tool_card("Bash", "xcodebuild", use_id="tu_4")
        self.s._collect_tool_results(self._result_msg("tu_4", "x" * 5000))
        txt = self._card_text()
        self.assertLess(len(txt), 1200, "不得塞全文")
        self.assertIn("還有", txt, "要明說截斷了多少")

    def test_empty_result_says_no_output(self):
        self.s.digest.tool_card("Bash", "true", use_id="tu_5")
        self.s._collect_tool_results(self._result_msg("tu_5", "   "))
        self.assertIn("沒有輸出", self._card_text())

    def test_unknown_use_id_ignored(self):
        """對不上的結果安靜跳過,不得炸、不得亂開卡。"""
        before = len(self.s.digest.store.cards)
        self.s._collect_tool_results(self._result_msg("nope", "hi"))
        self.assertEqual(len(self.s.digest.store.cards), before)

    def test_results_inside_assistant_message_also_collected(self):
        """工具結果也會夾在 AssistantMessage 裡(SDK 解析器 case "assistant"
        同樣收 tool_result)。只在 UserMessage 收會安靜漏掉一部分。"""
        class FakeToolResult:
            pass
        FakeToolResult.__name__ = "ToolResultBlock"
        rb = FakeToolResult()
        rb.tool_use_id = "tu_mix"
        rb.content = "mixed-in result"
        rb.is_error = False

        class FakeMsg:
            pass
        m = FakeMsg()
        m.content = [rb]

        self.s.digest.tool_card("Bash", "pwd", use_id="tu_mix")
        self.s._collect_tool_results(m)
        self.assertIn("mixed-in result", self._card_text(),
                      "AssistantMessage 裡的結果也要收")

    def test_map_cleared_on_turn_end(self):
        """對照表不跨回合累積(長 session 會無上限長大)。"""
        self.s.digest.tool_card("Bash", "ls", use_id="tu_6")
        self.assertTrue(self.s.digest._tool_cards)
        self.s.digest.turn_end()
        self.assertFalse(self.s.digest._tool_cards)


class CC2SpawnPinSafetyTest(unittest.TestCase):
    """spawn pin 對齊 tmux 正典 + 拒收會繞過審批的那顆(2026-10-29)。"""

    def setUp(self):
        self.r = cc_sdk.CCSdkRegistry()

    def test_allowed_tools_refused_not_silently_dropped(self):
        """allowed_tools 會**靜默繞過 Pocket 審批閘**(SDK 自己會警告),
        所以拒收 —— 而且要回報理由,不能默默丟掉讓人以為生效了。"""
        self.assertNotIn("allowed_tools", cc_sdk.SPAWN_PIN_KEYS)
        self.assertIn("allowed_tools", cc_sdk.SPAWN_PIN_REFUSED)
        out = cc_sdk.sanitize_spawn_opts({"allowed_tools": ["Bash"]})
        self.assertEqual(out, {})
        refused = cc_sdk.refused_spawn_keys({"allowed_tools": ["Bash"]})
        self.assertIn("allowed_tools", refused)
        self.assertIn("審批", refused["allowed_tools"], "理由要講人話")

    def test_tmux_canonical_pins_supported(self):
        """與 tmux 的 spawn pin 正典同一組,兩條路徑語意才一致。"""
        for k in ("effort", "fallback_model", "max_budget_usd",
                  "append_system_prompt"):
            self.assertIn(k, cc_sdk.SPAWN_PIN_KEYS, k)
        out = cc_sdk.sanitize_spawn_opts({"max_budget_usd": "12.5"})
        self.assertEqual(out["max_budget_usd"], 12.5, "字串數字要轉型")

    def test_byo_api_key_not_in_state_file(self):
        """BYO 憑證不准落進 state JSON(那是明文檔)。"""
        os.environ["CCSESS_SECRET_DIR"] = os.path.join(_TMP, "secret")
        try:
            s = self.r.ensure("byo-1", workdir="/tmp",
                              spawn_opts={"api_key": "sk-super-secret"})
            cc_sdk.write_spawn_secret("byo-1", "sk-super-secret")
            self.r.save()
            blob = open(os.environ["CC_SDK_STATE"], encoding="utf-8").read()
            self.assertNotIn("sk-super-secret", blob,
                             "明文憑證絕不落 state")
            self.assertNotIn("api_key", s.spawn_opts)
            self.assertEqual(cc_sdk.read_spawn_secret("byo-1"),
                             "sk-super-secret")
            # 0600 權限
            path = os.path.join(os.environ["CCSESS_SECRET_DIR"], "byo-1")
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        finally:
            os.environ.pop("CCSESS_SECRET_DIR", None)

    def test_byo_key_reaches_client_env_only(self):
        """key 只經由 env 傳給子行程,不出現在 spawn_opts。

        而且**只傳這一把** —— SDK 的 subprocess transport 本來就繼承
        os.environ,而且刻意濾掉 CLAUDECODE、設 CLAUDE_CODE_ENTRYPOINT=sdk-py;
        `options.env` 是最後覆蓋那層,整份灌 os.environ 會把那兩件事都破壞掉。
        """
        os.environ["CCSESS_SECRET_DIR"] = os.path.join(_TMP, "secret2")
        try:
            s = self.r.ensure("byo-2", workdir="/tmp")
            cc_sdk.write_spawn_secret("byo-2", "sk-abc")
            kw = s._spawn_option_kwargs()
            self.assertEqual(kw["env"], {"ANTHROPIC_API_KEY": "sk-abc"},
                             "只准傳這一把,不得整份灌 os.environ")
        finally:
            os.environ.pop("CCSESS_SECRET_DIR", None)

    def test_no_secret_means_no_env_override(self):
        """沒設 BYO key 就完全不要碰 env(讓 SDK 的預設繼承邏輯生效)。"""
        os.environ["CCSESS_SECRET_DIR"] = os.path.join(_TMP, "secret-empty")
        try:
            s = self.r.ensure("byo-3", workdir="/tmp")
            self.assertNotIn("env", s._spawn_option_kwargs())
        finally:
            os.environ.pop("CCSESS_SECRET_DIR", None)

    def test_context_usage_has_timeout(self):
        """它跑在 turn 收尾的 finally 裡 —— 卡住會讓 session 永遠 busy。"""
        import asyncio

        class WedgedClient:
            async def get_context_usage(self):
                await asyncio.sleep(60)      # 半死的 client:不回

        s = self.r.ensure("ctx-wedged", workdir="/tmp")
        s.client = WedgedClient()
        os.environ["CC_SDK_CONTEXT_TIMEOUT"] = "0.3"   # 測試不必真的等 8 秒

        async def run():
            import time as _t
            t0 = _t.monotonic()
            out = await asyncio.wait_for(s.refresh_context_usage(), timeout=20)
            return out, _t.monotonic() - t0

        try:
            out, elapsed = asyncio.run(run())
        finally:
            os.environ.pop("CC_SDK_CONTEXT_TIMEOUT", None)
        self.assertEqual(out, {}, "逾時要安靜回空,不是拋例外")
        self.assertLess(elapsed, 5, "必須自己逾時,不能等到外層")


class CC2SecondAuditTest(unittest.TestCase):
    """2026-10-29 第二輪複查抓到的缺陷。

    這一輪的教訓:上一輪我的測試**斷言了錯的契約** —— 只驗路由回傳值 200,
    沒驗卡片與 DB 真正記成什麼,所以「回答問題被記成拒絕」一路綠。
    """

    def setUp(self):
        self.r = cc_sdk.CCSdkRegistry()
        self.s = self.r.ensure("a2-1", workdir="/tmp")

    _INPUT = {"questions": [{
        "question": "要用哪個資料庫?", "header": "資料庫",
        "options": [{"label": "Postgres"}, {"label": "SQLite"}]}]}

    def _run_decision(self, key, tool_input=None):
        import asyncio
        marks = []
        old = cc_sdk.APPROVAL_DB_MARK
        cc_sdk.APPROVAL_DB_MARK = lambda aid, st: marks.append((aid, st))

        async def run():
            task = asyncio.ensure_future(self.s._can_use_tool(
                "AskUserQuestion", tool_input or self._INPUT, None))
            for _ in range(200):
                await asyncio.sleep(0.005)
                if self.r.pending_approvals:
                    break
            aid = next(iter(self.r.pending_approvals))
            self.r.decide_approval(aid, key)
            return await task

        try:
            res = asyncio.run(run())
        finally:
            cc_sdk.APPROVAL_DB_MARK = old
        return res, marks

    def test_answering_is_recorded_as_answered_not_denied(self):
        """選了「Postgres」,卡片不能顯示「已拒絕」。

        原本一律套 approve/deny 二元判斷,`ask:N` 不在允許清單 → 算成 denied。
        canonical 字彙:permission→approved|denied、question→answered。
        """
        _res, marks = self._run_decision("ask:0")
        self.assertTrue(marks, "要有 DB 收尾")
        self.assertEqual(marks[-1][1], "answered",
                         "回答問題不是拒絕 —— DB 不得記成 denied")
        cards = [c for c in self.s.digest.store.cards.values()
                 if str(c.get("id", "")).startswith("card-cc2-appr-")]
        self.assertTrue(cards, "要有審批卡")
        resolved = str((cards[-1].get("body") or {}).get("resolved") or "")
        self.assertNotEqual(resolved, "denied",
                            "卡片不得顯示已拒絕(使用者明明選了選項)")

    def test_question_never_enters_allow_all_memory(self):
        """AskUserQuestion 進了「本次全允許」就等於永久關掉提問且無聲。"""
        self._run_decision("approve_for_session")
        self.assertNotIn("AskUserQuestion", self.s.session_allowed_tools)

    def test_allow_all_memory_does_not_swallow_questions(self):
        """就算記憶裡真的有它(舊 state/別的路徑塞進去),也不得走早退放行。"""
        import asyncio
        self.s.session_allowed_tools.add("AskUserQuestion")

        async def run():
            task = asyncio.ensure_future(self.s._can_use_tool(
                "AskUserQuestion", self._INPUT, None))
            for _ in range(200):
                await asyncio.sleep(0.005)
                if self.r.pending_approvals:
                    break
            self.assertTrue(self.r.pending_approvals,
                            "必須照樣出卡問人,不得自動放行")
            aid = next(iter(self.r.pending_approvals))
            self.r.decide_approval(aid, "ask:1")
            return await task

        res = asyncio.run(run())
        self.assertIn("SQLite", res.message)

    def test_multi_question_tells_model_only_first_was_asked(self):
        """多題只答得了第一題 —— 不講模型會以為全部答完了。"""
        inp = {"questions": [
            dict(self._INPUT["questions"][0]),
            {"question": "第二題", "header": "第二", "options": [{"label": "X"}]}]}
        res, _ = self._run_decision("ask:0", inp)
        self.assertIn("only the first", res.message)
        self.assertIn("unanswered", res.message)

    def test_tmux_mode_vocab_accepted(self):
        """app 的模式選擇器送 tmux 字彙(normal);不認的話預設那格直接失敗。"""
        self.assertEqual(cc_sdk.normalize_mode("normal"), "default")
        self.assertEqual(cc_sdk.normalize_mode("manual"), "default")
        self.assertEqual(cc_sdk.normalize_mode("plan"), "plan")
        self.assertEqual(cc_sdk.normalize_mode("yolo"), "",
                         "認不出來要回空,讓呼叫端決定 400 還是退預設")

    def test_secret_path_traversal_refused(self):
        """secret 路徑是 join(dir, name) —— name 含 ../ 就能寫到目錄外。"""
        os.environ["CCSESS_SECRET_DIR"] = os.path.join(_TMP, "sec-trav")
        try:
            self.assertFalse(cc_sdk.write_spawn_secret("../evil", "sk-x"))
            self.assertFalse(cc_sdk.write_spawn_secret("a/b", "sk-x"))
            self.assertEqual(cc_sdk.read_spawn_secret("../evil"), "")
        finally:
            os.environ.pop("CCSESS_SECRET_DIR", None)

    def test_rename_moves_secret_and_fixes_identity(self):
        """改名要連帶搬 key、換 digest 身分、搬 pending 審批歸屬。"""
        import asyncio
        os.environ["CCSESS_SECRET_DIR"] = os.path.join(_TMP, "sec-rn")
        try:
            s = self.r.ensure("rn-old", workdir="/tmp")
            cc_sdk.write_spawn_secret("rn-old", "sk-keep-me")
            aid = "cc2-rn"
            self.r.pending_approvals[aid] = {
                "event": asyncio.Event(), "decision": None,
                "record": {"id": aid}, "session": "rn-old"}
            self.r.rename("rn-old", "rn-new")
            self.assertEqual(cc_sdk.read_spawn_secret("rn-new"), "sk-keep-me",
                             "key 沒搬 → 安靜退回預設憑證")
            self.assertEqual(cc_sdk.read_spawn_secret("rn-old"), "")
            self.assertEqual(s.digest.name, "rn-new")
            self.assertEqual(s.digest.store.media_session_id, "cc2:rn-new",
                             "媒體網址要跟著換,不然已出的圖對不上")
            self.assertIsNotNone(self.r.pending_for("rn-new"),
                                 "審批歸屬要搬,不然那筆變成按不下去")
        finally:
            os.environ.pop("CCSESS_SECRET_DIR", None)

    def test_delete_removes_secret(self):
        """刪掉之後同名新 session 不得悄悄繼承上一個人的憑證。"""
        os.environ["CCSESS_SECRET_DIR"] = os.path.join(_TMP, "sec-del")
        try:
            cc_sdk.write_spawn_secret("del-1", "sk-old-owner")
            cc_sdk.delete_spawn_secret("del-1")
            self.assertEqual(cc_sdk.read_spawn_secret("del-1"), "")
        finally:
            os.environ.pop("CCSESS_SECRET_DIR", None)

    def test_row_exposes_fields_app_needs(self):
        self.s.digest.turn_begin("t")
        row = [r for r in self.r.v2_rows() if r["id"] == "cc2:a2-1"][0]
        self.assertIn("attachments", row["capabilities"],
                      "app 若以 capabilities 決定顯不顯示附件鈕,漏了就等於沒做")
        self.assertEqual(row["meta"]["workdir"], "/tmp")
        self.assertIsNotNone(row["meta"]["busy_since"])
        self.assertEqual(row["meta"]["queue_depth"], 0)

    def test_empty_second_result_does_not_wipe_real_output(self):
        """同一 tool_use_id 回兩次:第二次空的不得蓋掉已收到的真輸出。"""
        self.s.digest.store.turn_id = "t"
        self.s.digest.tool_card("Bash", "make", use_id="tu_x")
        self.s.digest.tool_result("tu_x", "BUILD SUCCEEDED")
        self.s.digest.tool_result("tu_x", "")
        txt = [str((c.get("body") or {}).get("text") or "")
               for c in self.s.digest.store.cards.values()
               if (c.get("body") or {}).get("origin") == "cc2.tool"][0]
        self.assertIn("BUILD SUCCEEDED", txt)
        self.assertNotIn("沒有輸出", txt)

    def test_server_tool_result_dict_rendered(self):
        """server tool(web_search 等)的結果是原始 dict —— 要壓成看得懂的字,
        不能一片空白。"""
        class FakeServerResult:
            pass
        FakeServerResult.__name__ = "ServerToolResultBlock"
        b = FakeServerResult()
        b.tool_use_id = "stu_1"
        b.content = {"type": "web_search_result", "content": "找到 3 筆結果"}
        b.is_error = False

        class FakeMsg:
            pass
        m = FakeMsg()
        m.content = [b]

        self.s.digest.store.turn_id = "t"
        self.s.digest.tool_card("web_search", "pocket app", use_id="stu_1")
        self.s._collect_tool_results(m)
        txt = [str((c.get("body") or {}).get("text") or "")
               for c in self.s.digest.store.cards.values()
               if (c.get("body") or {}).get("origin") == "cc2.tool"][0]
        self.assertIn("找到 3 筆結果", txt)


class CC2ThirdAuditTest(unittest.TestCase):
    """2026-10-29 第三輪盤查:未登入指路、歷史讀取不擋事件圈。"""

    def setUp(self):
        self.r = cc_sdk.CCSdkRegistry()
        self.s = self.r.ensure("a3-1", workdir="/tmp")

    def test_auth_error_becomes_human_readable(self):
        """主機沒登入時原本只會出現一行 SDK 例外字串,使用者不知道要去登入。

        tmux 有 `/ccsessions/{name}/login`(靠 tmux 把 /login 打進 TUI);
        cc2 沒有 TUI 做不到那件事,但**可以明確指路**。
        """
        hint = cc_sdk.CCSdkSession._auth_hint(
            RuntimeError("Invalid API key · Please run /login"))
        self.assertIn("登入", hint)
        self.assertIn("/login", hint, "要給得出可照做的指令")

    def test_quota_error_distinguished_from_auth(self):
        hint = cc_sdk.CCSdkSession._auth_hint(
            RuntimeError("Your credit balance is too low"))
        self.assertIn("額度", hint)
        self.assertNotIn("登入", hint, "額度不足不是叫人去登入")

    def test_unrelated_error_keeps_raw_message(self):
        """不是認證類的錯誤不要硬翻 —— 原文對排查更有用。"""
        self.assertEqual(
            cc_sdk.CCSdkSession._auth_hint(RuntimeError("disk full")), "")

    def test_history_read_is_capped_and_honest(self):
        """超大 transcript 只讀尾端,並誠實標示還有更早的讀不到。"""
        root = os.path.join(_TMP, "projects-big")
        d = os.path.join(root, cc_sdk._project_key("/tmp"))
        os.makedirs(d, exist_ok=True)
        line = json.dumps({"type": "user", "timestamp": 1700000000,
                           "message": {"content": "x" * 900}}) + "\n"
        # 寫到超過 8 MiB 上限
        with open(os.path.join(d, "big-sid.jsonl"), "w", encoding="utf-8") as f:
            for _ in range(11000):
                f.write(line)
        self.s.sdk_session_id = "big-sid"
        old = cc_sdk._claude_projects_root
        cc_sdk._claude_projects_root = lambda: root
        try:
            out = self.s.history_page(limit=5)
        finally:
            cc_sdk._claude_projects_root = old
        self.assertEqual(len(out["messages"]), 5)
        self.assertTrue(out["truncated"], "超過上限要標示截斷")
        self.assertTrue(out["more"], "截斷時 more 必為真(前面還有讀不到的)")

    def test_history_limit_zero_returns_empty(self):
        self.assertEqual(self.s.history_page(limit=0)["messages"], [])
