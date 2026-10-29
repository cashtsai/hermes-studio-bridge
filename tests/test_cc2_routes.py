"""cc2 的 **HTTP 路由**層測試(2026-10-29)。

為什麼要單獨一檔:原本 cc2 的 29 條測試全是 in-process 直接驅動 `cc_sdk`,
沒有一條打到真的路由 —— 於是三個只存在於路由層的缺陷整批漏掉:

  ① `POST /app/v2/sessions/cc2:x/approve` **沒有 cc2 分支**。舊形狀 body
     ({"key": "approve"},正是 cc 路徑在送的那種)會一路掉到 Codex 分支,
     拿 cc2 的 session 名當 thread id 去呼 `decide_thread_approval`。
  ② `POST /app/v2/sessions/cc2:x/compact` 對 cc2 回 409,指路去
     `/ccsessions/{name}/compress` —— 那支要求 name 在 `_cc_conf_rows` 裡,
     拿 cc2 名字打一定 404。app 現有的壓縮鈕對 cc2 是死鈕。
  ③ `/health?agents=1` 不算 cc2 的 in-flight 回合,安全重啟腳本會據此
     誤判可以重啟,安靜殺掉在跑的回合。

教訓:**單元測試驗得了模組,驗不了接線。** 路由層要有自己的測試。
"""
import _isolation  # noqa: F401
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="cc2-routes-")
os.environ["CC_SDK_STATE"] = os.path.join(_TMP, "sessions.json")
os.environ["CC_SDK_PROVIDER"] = "1"          # 路由全部 flag-gated,要開才打得到
os.environ["CC_SDK_ADOPT_BY_CWD"] = "0"      # 不准去接機主真實的對話
os.environ["CC_SDK_CLAUDE_PROJECTS"] = os.path.join(_TMP, "projects")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
import cc_sdk  # noqa: E402


class CC2RouteTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        try:
            from tests.test_codex_server_requests import reset_auth_throttle
            reset_auth_throttle()
        except Exception:  # noqa: BLE001
            pass
        self.auth = {"Authorization": "Bearer " + bridge.BRIDGE_TOKEN}
        self.http = TestClient(bridge.app)
        cc_sdk.reset_registry_for_tests()
        self.reg = cc_sdk.registry()
        self.sess = self.reg.ensure("r1", workdir="/tmp")

    # ── ① approve 不准掉進 Codex 分支 ──

    def test_legacy_approve_body_handled_by_cc2_not_codex(self):
        """只帶 {"key": "approve"} 的舊 body 要被 cc2 自己收掉。

        漏掉的話會呼到 CODEX_APP.decide_thread_approval(thread_id="r1") ——
        決議送錯子系統。這裡把 CODEX_APP 換成會爆的替身,真的掉過去就炸。
        """
        import asyncio

        class Boom:
            async def decide_thread_approval(self, *a, **k):
                raise AssertionError("cc2 的審批決議掉進 Codex 分支了")

        aid = "cc2-route-test1"
        waiter = {"event": asyncio.Event(), "decision": None,
                  "record": {"id": aid, "session_id": "cc2:r1"},
                  "session": "r1"}
        self.reg.pending_approvals[aid] = waiter
        real, bridge.CODEX_APP = bridge.CODEX_APP, Boom()
        try:
            r = self.http.post("/app/v2/sessions/cc2:r1/approve",
                               headers=self.auth, json={"key": "approve"})
        finally:
            bridge.CODEX_APP = real
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json().get("status"), "approved")
        self.assertEqual(waiter["decision"], "approve", "決議要真的叫醒等待者")

    def test_approve_without_pending_is_409_not_codex(self):
        """沒有待決審批時要回 409,而不是悄悄轉給 Codex。"""
        r = self.http.post("/app/v2/sessions/cc2:r1/approve",
                           headers=self.auth, json={"key": "approve"})
        self.assertEqual(r.status_code, 409, r.text)

    def test_question_key_accepted_over_http(self):
        """提問的 ask:N key 走統一 `{approval_id, key}` 路由也要通。

        這條走的是 `_approval_decide_core`(契約 §3.3 的標準形狀),它會先查
        approvals DB 的那一列才分 provider —— 所以 DB 列**必須**一起建,
        否則查不到列會掉到最後的泛用分支回 409。production 裡這列是
        `_db_hook(APPROVAL_DB_UPSERT, record)` 在出卡時寫的。
        """
        import asyncio
        import time
        aid = "cc2-route-q1"
        record = {
            "id": aid, "title": "要選哪個?", "source": "cc2:r1",
            "risk": "low", "detail": "AskUserQuestion", "created_at": time.time(),
            "expires_at": time.time() + 120, "status": "pending",
            "decided_at": None, "result": None, "session_id": "cc2:r1",
            "provider": "cc2", "kind": "question",
            "options": [{"key": "ask:0", "label": "A", "style": "primary"},
                        {"key": "ask:2", "label": "C", "style": "secondary"},
                        {"key": "ask:skip", "label": "略過", "style": "danger"}],
        }
        bridge._cc2_approval_db_upsert(record)
        waiter = {"event": asyncio.Event(), "decision": None,
                  "record": record, "session": "r1"}
        self.reg.pending_approvals[aid] = waiter
        r = self.http.post("/app/v2/sessions/cc2:r1/approve",
                           headers=self.auth,
                           json={"approval_id": aid, "key": "ask:2"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(waiter["decision"], "ask:2",
                         "ask:N 要原樣帶回等待者,它靠這個 index 找答案")

    # ── ② 壓縮鈕不准是死鈕 ──

    def test_unified_compact_route_reaches_cc2(self):
        """統一 compact 路由對 cc2 要真的做事,不是 409 指路到打不到的地方。"""
        sent = {}

        async def fake_submit(text, attachments=None, client_id=""):
            sent["text"] = text
            return {"delivery": "accepted"}

        self.sess.submit = fake_submit
        r = self.http.post("/app/v2/sessions/cc2:r1/compact",
                           headers=self.auth, json={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("started"))
        self.assertEqual(sent.get("text"), "/compact",
                         "壓縮是把 /compact 斜線指令當訊息送進去")

    def test_compact_while_busy_is_409(self):
        """忙碌中壓縮會變成排隊,語意就不是「現在壓縮」—— 要明確拒絕。"""
        self.sess.busy = True
        try:
            r = self.http.post("/app/v2/sessions/cc2:r1/compact",
                               headers=self.auth, json={})
        finally:
            self.sess.busy = False
        self.assertEqual(r.status_code, 409, r.text)

    # ── ③ 安全重啟看得到 cc2 ──

    def test_health_agents_counts_cc2_inflight(self):
        """/health?agents=1 要吐 cc2 的忙碌數,否則安全重啟會殺掉在跑的回合。"""
        r = self.http.get("/health?agents=1", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("cc2", r.json().get("agents_in_flight") or {})

        self.sess.busy = True
        try:
            r2 = self.http.get("/health?agents=1", headers=self.auth)
        finally:
            self.sess.busy = False
        self.assertEqual((r2.json().get("agents_in_flight") or {}).get("cc2"), 1,
                         "有回合在跑就要數出來")

    def test_host_capabilities_declares_cc2(self):
        """app 要能問出「這台支不支援 cc2」,而不是試了才知道。"""
        r = self.http.get("/health", headers=self.auth)
        self.assertIn("cc2", r.json().get("capabilities") or {})

    # ── 熱改設定 / 上下文用量 ──

    def test_settings_route_records_when_offline(self):
        """client 沒在線:值要記下來、誠實回 applied_live=false。"""
        r = self.http.post("/app/v2/cc2/r1/settings", headers=self.auth,
                           json={"model": "claude-sonnet-5",
                                 "permission_mode": "plan"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body.get("applied_live"), "沒有在線 client 不該說已生效")
        self.assertEqual(self.sess.model, "claude-sonnet-5")
        self.assertEqual(self.sess.permission_mode, "plan")
        self.assertEqual(self.sess.digest.mode, "plan", "狀態鏡像要同步")

    def test_settings_route_hot_applies_to_live_client(self):
        """client 在線:要真的呼 SDK 的 set_model/set_permission_mode。

        光改欄位是不夠的 —— ClaudeAgentOptions 只在 connect 當下讀一次,
        不熱改的話畫面顯示新模式、實際仍跑舊的,比沒做還糟。
        """
        calls = []

        class FakeClient:
            async def set_model(self, m=None):
                calls.append(("model", m))

            async def set_permission_mode(self, mode):
                calls.append(("mode", mode))

        self.sess.client = FakeClient()
        try:
            r = self.http.post("/app/v2/cc2/r1/settings", headers=self.auth,
                               json={"model": "claude-opus-4-8",
                                     "permission_mode": "dontAsk"})
        finally:
            self.sess.client = None
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("applied_live"))
        self.assertIn(("mode", "dontAsk"), calls)
        self.assertIn(("model", "claude-opus-4-8"), calls)

    def test_settings_rejects_unknown_permission_mode(self):
        """不認得的模式要擋在門口,不能被 _clamp_mode 悄悄吞成 acceptEdits。"""
        r = self.http.post("/app/v2/cc2/r1/settings", headers=self.auth,
                           json={"permission_mode": "yolo"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertNotEqual(self.sess.permission_mode, "acceptEdits-yolo")

    def test_create_accepts_model_and_spawn_pins(self):
        """建立時釘模型與其他啟動選項(tmux 有一整個 pin 目錄,cc2 原本只有
        workdir/permission_mode)。白名單外的鍵要被丟掉。"""
        r = self.http.post("/app/v2/cc2/r2", headers=self.auth,
                           json={"workdir": "/tmp", "model": "claude-sonnet-5",
                                 "spawn_opts": {
                                     "effort": "high",
                                     "append_system_prompt": "講中文",
                                     "max_budget_usd": 5,
                                     "evil_key": "rm -rf /"}})
        self.assertEqual(r.status_code, 200, r.text)
        pins = r.json().get("spawn_opts") or {}
        self.assertEqual(pins.get("effort"), "high")
        self.assertEqual(pins.get("max_budget_usd"), 5.0)
        self.assertNotIn("evil_key", pins, "白名單外的鍵不准落地")
        self.assertEqual(r.json().get("model"), "claude-sonnet-5")

    def test_create_reports_refused_pins(self):
        """拒收的 pin 要回報理由,不能靜默丟掉 —— 靜默的話使用者會以為生效了。"""
        r = self.http.post("/app/v2/cc2/r4", headers=self.auth,
                           json={"workdir": "/tmp",
                                 "spawn_opts": {"allowed_tools": ["Bash"]}})
        self.assertEqual(r.status_code, 200, r.text)
        refused = r.json().get("refused_spawn_opts") or {}
        self.assertIn("allowed_tools", refused,
                      "會繞過審批閘的 pin 必須明確回報被拒")
        self.assertNotIn("allowed_tools", r.json().get("spawn_opts") or {})

    def test_create_does_not_echo_api_key(self):
        """BYO 憑證只回布林,絕不回明文。"""
        import os as _os
        _os.environ["CCSESS_SECRET_DIR"] = _os.path.join(_TMP, "rsecret")
        try:
            r = self.http.post("/app/v2/cc2/r5", headers=self.auth,
                               json={"workdir": "/tmp",
                                     "spawn_opts": {"api_key": "sk-leak-me"}})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertNotIn("sk-leak-me", r.text, "明文憑證不准出現在回應裡")
            self.assertTrue(r.json().get("has_api_key"))
        finally:
            _os.environ.pop("CCSESS_SECRET_DIR", None)

    def test_append_system_prompt_keeps_claude_code_preset(self):
        """append_system_prompt 要轉成官方 preset+append 形狀。

        直接塞 system_prompt 字串會**整個換掉**預設提示,Claude Code 會失去
        它原本的工具使用規範 —— 那是靜默的行為退化。
        """
        sess = self.reg.ensure("r3", workdir="/tmp",
                               spawn_opts={"append_system_prompt": "講中文"})
        kw = sess._spawn_option_kwargs()
        self.assertEqual(kw["system_prompt"],
                         {"type": "preset", "preset": "claude_code",
                          "append": "講中文"})

    def test_context_route_reports_live_false_when_offline(self):
        """不為了上下文數字去憑空開一個 claude 行程。"""
        r = self.http.get("/app/v2/cc2/r1/context", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json().get("live"))

    def test_context_usage_lands_in_status(self):
        """上下文用量要進 status,app 才顯示得出「快滿了」。"""
        import asyncio

        class FakeClient:
            async def get_context_usage(self):
                return {"totalTokens": 120000, "maxTokens": 200000,
                        "percentage": 60.0}

        self.sess.client = FakeClient()
        try:
            asyncio.run(self.sess.refresh_context_usage())
        finally:
            self.sess.client = None
        self.sess.digest._status()
        ctx = (self.sess.digest.store.status or {}).get("context") or {}
        self.assertEqual(ctx.get("used"), 120000)
        self.assertEqual(ctx.get("size"), 200000)
        self.assertEqual(ctx.get("pct"), 60.0)

    def test_status_has_no_cost_field(self):
        """刻意不放累計花費:機主 2026-10-28 要求拿掉費用預估
        (「那個數字很不精準常常看的很奇怪」)。"""
        self.sess.digest._status()
        st = self.sess.digest.store.status or {}
        for k in st:
            self.assertNotIn("cost", k.lower())


if __name__ == "__main__":
    unittest.main()


class CC2ModeVocabRouteTest(unittest.TestCase):
    """模式字彙別名要走得通(2026-10-29 第二輪複查)。

    tmux 的 `_CC_MODES` 用 `normal`,SDK 用 `default`。app 的模式選擇器送的是
    tmux 字彙 —— 路由只認 SDK 字彙的話,預設那一格在 cc2 上直接 400。
    """

    def setUp(self):
        from fastapi.testclient import TestClient
        self.auth = {"Authorization": "Bearer " + bridge.BRIDGE_TOKEN}
        self.http = TestClient(bridge.app)
        cc_sdk.reset_registry_for_tests()
        cc_sdk.registry().ensure("mv1", workdir="/tmp")

    def test_create_accepts_tmux_vocab(self):
        r = self.http.post("/app/v2/cc2/mv2", headers=self.auth,
                           json={"workdir": "/tmp", "permission_mode": "normal"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["permission_mode"], "default",
                         "要轉成 SDK 字彙")

    def test_settings_accepts_tmux_vocab(self):
        r = self.http.post("/app/v2/cc2/mv1/settings", headers=self.auth,
                           json={"permission_mode": "normal"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(cc_sdk.registry().get("mv1").permission_mode, "default")

    def test_still_rejects_nonsense(self):
        r = self.http.post("/app/v2/cc2/mv1/settings", headers=self.auth,
                           json={"permission_mode": "yolo"})
        self.assertEqual(r.status_code, 400, r.text)


class CC2HistoryResumeRouteTest(unittest.TestCase):
    """把過去的對話接回成 cc2 session(2026-10-29 複查列為 MISSING)。

    `/cchistory` 的列表與逐字稿讀 `~/.claude/projects`,**與 cc2 同一個儲存**,
    所以那兩支本來就通;缺的只有接回 —— 既有的 `/cchistory/{sid}/resume` 開的是
    tmux session。
    """

    def setUp(self):
        from fastapi.testclient import TestClient
        self.auth = {"Authorization": "Bearer " + bridge.BRIDGE_TOKEN}
        self.http = TestClient(bridge.app)
        cc_sdk.reset_registry_for_tests()
        self._find = bridge._cchist_find
        self._meta = bridge._cchist_meta

    def tearDown(self):
        bridge._cchist_find = self._find
        bridge._cchist_meta = self._meta

    def _stub(self, cwd="/tmp", found=True):
        bridge._cchist_find = lambda sid: ("/fake/path.jsonl" if found else None)
        bridge._cchist_meta = lambda path: {"cwd": cwd}

    def test_resume_creates_cc2_session_bound_to_old_transcript(self):
        self._stub()
        r = self.http.post("/app/v2/cc2/_resume/abc12345def", headers=self.auth,
                           json={"name": "revived"})
        self.assertEqual(r.status_code, 200, r.text)
        sess = cc_sdk.registry().get("revived")
        self.assertIsNotNone(sess)
        self.assertEqual(sess.sdk_session_id, "abc12345def",
                         "要綁到那條舊逐字稿,不然接回來是空的")
        self.assertEqual(sess.workdir, "/tmp")

    def test_resume_refuses_when_cwd_gone(self):
        """原本的工作目錄不在了就不要接 —— 相對路徑會全錯。"""
        self._stub(cwd="/no/such/dir/anywhere")
        r = self.http.post("/app/v2/cc2/_resume/zzz", headers=self.auth, json={})
        self.assertEqual(r.status_code, 409, r.text)

    def test_resume_unknown_sid_is_404(self):
        self._stub(found=False)
        r = self.http.post("/app/v2/cc2/_resume/nope", headers=self.auth, json={})
        self.assertEqual(r.status_code, 404, r.text)

    def test_resume_refuses_session_held_elsewhere(self):
        """正被終端機持有的那條不准接 —— 兩個寫入者會讓逐字稿父子鏈分岔。"""
        self._stub()
        old = cc_sdk._live_session_ids
        cc_sdk._live_session_ids = lambda: {"held-sid"}
        try:
            r = self.http.post("/app/v2/cc2/_resume/held-sid",
                               headers=self.auth, json={})
        finally:
            cc_sdk._live_session_ids = old
        self.assertEqual(r.status_code, 409, r.text)


class CC2InterruptTest(unittest.TestCase):
    """停止鈕在「卡在審批/提問」時要真的有用(2026-10-29 複查)。"""

    def setUp(self):
        cc_sdk.reset_registry_for_tests()
        self.r = cc_sdk.registry()
        self.s = self.r.ensure("int-1", workdir="/tmp")

    def test_interrupt_wakes_pending_question_as_skip(self):
        """提問掛著時按停止 → 當「略過」叫醒,不是等 110 秒 fail-closed。"""
        import asyncio
        aid = "cc2-int-q"
        w = {"event": asyncio.Event(), "decision": None, "session": "int-1",
             "record": {"id": aid, "options": [
                 {"key": "ask:0"}, {"key": "ask:skip"}]}}
        self.r.pending_approvals[aid] = w
        self.s.busy = True
        self.assertTrue(asyncio.run(self.s.interrupt()))
        self.assertEqual(w["decision"], "ask:skip",
                         "提問要當略過 —— 回答問題不是危險操作")
        self.assertTrue(w["event"].is_set())

    def test_interrupt_denies_pending_permission(self):
        import asyncio
        aid = "cc2-int-p"
        w = {"event": asyncio.Event(), "decision": None, "session": "int-1",
             "record": {"id": aid, "options": [
                 {"key": "approve"}, {"key": "deny"}]}}
        self.r.pending_approvals[aid] = w
        self.s.busy = True
        asyncio.run(self.s.interrupt())
        self.assertEqual(w["decision"], "deny")

    def test_interrupt_idle_is_false(self):
        import asyncio
        self.assertFalse(asyncio.run(self.s.interrupt()))

    def test_interrupt_reports_failure_honestly(self):
        """原本無條件回 True,連 client 已死都說成功。"""
        import asyncio

        class DeadClient:
            async def interrupt(self):
                raise RuntimeError("client 已死")

        self.s.busy = True
        self.s.client = DeadClient()
        try:
            self.assertFalse(asyncio.run(self.s.interrupt()),
                             "送不出去又沒叫醒任何等待者 → 不得回報成功")
        finally:
            self.s.client = None
