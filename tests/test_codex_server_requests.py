"""codex app-server 的 server → client request 處理（`fix/codex-server-requests`）。

背景:`_handle_server_message` 只認 4 個 approval method，其餘一律回
JSON-RPC -32601「server request not implemented」。實機 log 抓到
`item/tool/call`（DynamicToolCall）被拒 3 次 → 使用者 config.toml 裡的
plugin 工具在 Pocket 開的 thread 永遠失敗，而且是**整個 turn 失敗**。

這裡驗的是「不再用 -32601 打死 turn」與各 method 的回覆形狀正確
（形狀取自 codex 二進位的 ServerRequest/Response serde 表 + OpenClaw
codex app-server v2 client 的實作）。

跑法:
    python -m unittest tests.test_codex_server_requests
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="cxreq-canon-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class FakeProc:
    returncode = None


class RecordingClient(bridge.CodexAppServerClient):
    """真的 client，但把 stdio 寫入換成錄音；不會 spawn codex。"""

    def __init__(self):
        super().__init__()
        self.proc = FakeProc()
        self.sent = []

    async def _write_locked(self, msg: dict):
        self.sent.append(msg)

    def replies_for(self, request_id):
        return [m for m in self.sent if m.get("id") == request_id]


def reset_auth_throttle():
    """把「認證失敗限流器」的行程全域狀態歸零。

    `_check_auth` 每次認證失敗就往模組級的表記一筆，同一個 60s 視窗內超過
    `_AUTH_FAIL_MAX` 之後改回 **429**。那些表是行程全域的，所以全套
    `unittest discover` 一起跑時，前面任何打過認證失敗路徑的測試(例如
    test_upload_file_endpoint)都會先把額度用掉，輪到後面的測試時預期的
    401/400/409 全變成 429 —— 過不過取決於執行順序。

    正式限流行為不動(那是對的),只在測試側把狀態清乾淨。
    容器名稱隨 main 演進過(全域 `_AUTH_FAILS` deque → per-client 分桶
    `_AUTH_FAILS_BY_CLIENT`),所以兩種名字都清、都用 getattr 取。
    """
    with bridge._AUTH_LOCK:
        for attr in ("_AUTH_FAILS", "_AUTH_FAILS_BY_CLIENT", "_AUTH_FAIL_AGG"):
            container = getattr(bridge, attr, None)
            if container is not None:
                container.clear()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _handle(client, msg):
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(client._handle_server_message(msg))
        # auto-resolution 之類的背景 task 這裡不跑，測試各自控制
    finally:
        loop.close()


class TestDynamicToolCall(unittest.TestCase):
    """item/tool/call — 有實機 log 的那一個。"""

    def _call(self):
        client = RecordingClient()
        _handle(client, {
            "jsonrpc": "2.0", "id": 41, "method": "item/tool/call",
            "params": {"threadId": "t-1", "turnId": "u-1", "callId": "c-1",
                       "tool": "create_document", "namespace": "documents",
                       "arguments": {"title": "x"}},
        })
        return client

    def test_never_answers_with_method_not_found(self):
        client = self._call()
        replies = client.replies_for(41)
        self.assertEqual(len(replies), 1)
        self.assertNotIn("error", replies[0], "-32601 會讓整個 turn 失敗")

    def test_returns_structured_failed_tool_result(self):
        result = self._call().replies_for(41)[0]["result"]
        # DynamicToolCallResponse = {contentItems, success}（binary: 2 elements）
        self.assertEqual(set(result.keys()), {"contentItems", "success"})
        self.assertFalse(result["success"])
        self.assertEqual(result["contentItems"][0]["type"], "inputText")
        self.assertIn("documents/create_document", result["contentItems"][0]["text"])

    def test_notes_the_thread_once_per_tool(self):
        client = RecordingClient()
        for _ in range(3):
            _handle(client, {
                "jsonrpc": "2.0", "id": 7, "method": "item/tool/call",
                "params": {"threadId": "t-9", "turnId": "u", "callId": "c",
                           "tool": "browse", "arguments": {}},
            })
        notes = [e for e in client.thread_events["t-9"]
                 if e[0] == "text" and "browse" in e[1]]
        self.assertEqual(len(notes), 1, "同工具連打不該洗版")


class TestCurrentTimeRead(unittest.TestCase):
    def test_answers_whole_unix_seconds(self):
        """CurrentTimeReadResponse.currentTimeAt 是 **number**（整數 Unix 秒）。

        來源:`codex app-server generate-json-schema` 與二進位 serde 表
        （`CurrentTimeReadResponse.ts / currentTimeAt / : number,` +
        doc「Current time as whole Unix seconds」）。之前送 RFC3339 字串
        會 deserialize 失敗 —— 跟回 -32601 一樣打死 turn。
        """
        client = RecordingClient()
        before = int(time.time())
        _handle(client, {"jsonrpc": "2.0", "id": 5,
                         "method": "currentTime/read", "params": {}})
        result = client.replies_for(5)[0]["result"]
        # CurrentTimeReadResponse = {currentTimeAt}（binary: 1 element）
        self.assertEqual(list(result.keys()), ["currentTimeAt"])
        value = result["currentTimeAt"]
        self.assertIsInstance(value, int)
        self.assertNotIsInstance(value, bool)
        self.assertGreaterEqual(value, before)
        self.assertLessEqual(value, int(time.time()) + 1)


class TestPermissionsRequestApproval(unittest.TestCase):
    def test_grants_nothing_but_keeps_turn_alive(self):
        """PermissionsRequestApprovalResponse 是 **3 elements**。

        二進位 serde 表:`struct PermissionsRequestApprovalResponse with 3
        element`;JSON Schema 同源:permissions(GrantedPermissionProfile)
        / scope(PermissionGrantScope = "turn"|"session") / strictAutoReview
        (bool|null)。只送 2 欄是在賭 serde 有沒有替 strictAutoReview 補
        default —— 賭輸就是 deserialize 失敗、turn 陣亡（本 PR 要修的病）。
        """
        client = RecordingClient()
        _handle(client, {"jsonrpc": "2.0", "id": 6,
                         "method": "item/permissions/requestApproval",
                         "params": {"threadId": "t-1", "turnId": "u-1"}})
        reply = client.replies_for(6)[0]
        self.assertNotIn("error", reply)
        result = reply["result"]
        self.assertEqual(sorted(result.keys()),
                         ["permissions", "scope", "strictAutoReview"])
        # 一項權限都不加授
        self.assertEqual(result["permissions"],
                         {"network": None, "fileSystem": None})
        self.assertEqual(result["scope"], "turn")
        self.assertIsInstance(result["strictAutoReview"], bool)


class TestRequestUserInput(unittest.TestCase):
    PARAMS = {
        "threadId": "t-2", "turnId": "u-2", "itemId": "i-2",
        "isBlocking": True,
        "questions": [{
            "id": "q1", "header": "選一個環境", "question": "要部署到哪裡?",
            "isOther": False, "isSecret": False,
            "options": [{"label": "staging", "description": "測試機"},
                        {"label": "production", "description": ""}],
        }],
    }

    def _pending(self):
        client = RecordingClient()
        _handle(client, {"jsonrpc": "2.0", "id": 21,
                         "method": "item/tool/requestUserInput",
                         "params": self.PARAMS})
        return client

    def test_creates_question_card_instead_of_answering_immediately(self):
        client = self._pending()
        self.assertEqual(client.replies_for(21), [], "應該先問人，不是立刻回覆")
        record = client.pending_question_for_thread("t-2")
        self.assertIsNotNone(record)
        self.assertEqual(record["kind"], "question")
        self.assertEqual(record["title"], "選一個環境")
        keys = [o["key"] for o in record["options"]]
        self.assertEqual(keys, ["opt0", "opt1", "deny"])
        self.assertEqual([o["label"] for o in record["options"]],
                         ["staging", "production", "略過"])

    def test_answer_by_option_key(self):
        client = self._pending()
        record = client.pending_question_for_thread("t-2")
        out = _run(client.answer_question(record["id"], key="opt1"))
        self.assertEqual(out["status"], "answered")
        # ToolRequestUserInputResponse = {answers: {qid: {answers: [...]}}}
        self.assertEqual(client.replies_for(21)[0]["result"],
                         {"answers": {"q1": {"answers": ["production"]}}})
        self.assertIsNone(client.pending_question_for_thread("t-2"))

    def test_skip_returns_empty_answers(self):
        client = self._pending()
        record = client.pending_question_for_thread("t-2")
        _run(client.answer_question(record["id"], key="deny"))
        self.assertEqual(client.replies_for(21)[0]["result"],
                         {"answers": {"q1": {"answers": []}}})

    def test_free_text_only_accepted_when_question_allows_it(self):
        client = self._pending()
        record = client.pending_question_for_thread("t-2")
        # 有固定選項且 isOther=False → 自由文字不該偷渡成答案
        _run(client.answer_question(record["id"], key="", text="whatever"))
        self.assertEqual(client.replies_for(21)[0]["result"],
                         {"answers": {"q1": {"answers": []}}})

    def test_free_text_used_for_other_questions(self):
        params = {**self.PARAMS,
                  "questions": [{**self.PARAMS["questions"][0],
                                 "isOther": True}]}
        client = RecordingClient()
        _handle(client, {"jsonrpc": "2.0", "id": 22,
                         "method": "item/tool/requestUserInput",
                         "params": params})
        record = client.pending_question_for_thread("t-2")
        _run(client.answer_question(record["id"], key="", text="  canary  "))
        self.assertEqual(client.replies_for(22)[0]["result"],
                         {"answers": {"q1": {"answers": ["canary"]}}})

    def test_multi_question_keeps_unanswered_slots(self):
        params = {**self.PARAMS,
                  "questions": self.PARAMS["questions"] + [{
                      "id": "q2", "header": "第二題", "question": "?",
                      "isOther": True, "isSecret": False, "options": []}]}
        client = RecordingClient()
        _handle(client, {"jsonrpc": "2.0", "id": 23,
                         "method": "item/tool/requestUserInput",
                         "params": params})
        record = client.pending_question_for_thread("t-2")
        _run(client.answer_question(record["id"], key="opt0"))
        self.assertEqual(client.replies_for(23)[0]["result"],
                         {"answers": {"q1": {"answers": ["staging"]},
                                      "q2": {"answers": []}}})

    def test_unreadable_params_fall_back_instead_of_32601(self):
        client = RecordingClient()
        _handle(client, {"jsonrpc": "2.0", "id": 24,
                         "method": "item/tool/requestUserInput",
                         "params": {"threadId": "t-3"}})
        reply = client.replies_for(24)[0]
        self.assertNotIn("error", reply)
        self.assertEqual(reply["result"], {"answers": {}})


class TestMcpElicitation(unittest.TestCase):
    def _pending(self, params):
        client = RecordingClient()
        _handle(client, {"jsonrpc": "2.0", "id": 31,
                         "method": "mcpServer/elicitation/request",
                         "params": params})
        return client

    def test_creates_card(self):
        client = self._pending({"threadId": "t-4", "turnId": "u-4",
                                "serverName": "node_repl", "mode": "form",
                                "message": "允許執行?"})
        self.assertEqual(client.replies_for(31), [])
        record = client.pending_question_for_thread("t-4")
        self.assertEqual([o["key"] for o in record["options"]],
                         ["approve", "deny"])
        self.assertIn("node_repl", record["title"])

    def test_decline_shape(self):
        client = self._pending({"threadId": "t-4", "serverName": "node_repl"})
        record = client.pending_question_for_thread("t-4")
        _run(client.answer_question(record["id"], key="deny"))
        # McpServerElicitationRequestResponse = {action, content, _meta}
        self.assertEqual(client.replies_for(31)[0]["result"],
                         {"action": "decline", "content": None, "_meta": None})

    def test_accept_when_schema_has_no_fields(self):
        client = self._pending({"threadId": "t-4", "serverName": "node_repl",
                                "requestedSchema": {"type": "object",
                                                    "properties": {}}})
        record = client.pending_question_for_thread("t-4")
        _run(client.answer_question(record["id"], key="approve"))
        self.assertEqual(client.replies_for(31)[0]["result"],
                         {"action": "accept", "content": None, "_meta": None})

    def test_accept_declines_when_form_fields_cannot_be_filled(self):
        client = self._pending({
            "threadId": "t-4", "serverName": "node_repl", "mode": "form",
            "requestedSchema": {"type": "object",
                                "properties": {"token": {"type": "string"}}}})
        record = client.pending_question_for_thread("t-4")
        _run(client.answer_question(record["id"], key="approve"))
        self.assertEqual(client.replies_for(31)[0]["result"]["action"], "decline")


class TestStillUnimplemented(unittest.TestCase):
    """沒能力代做的仍回 -32601，但 log 要夠診斷。"""

    def test_attestation_and_token_refresh_still_error(self):
        for method in ("attestation/generate",
                       "account/chatgptAuthTokens/refresh"):
            client = RecordingClient()
            _handle(client, {"jsonrpc": "2.0", "id": 99, "method": method,
                             "params": {"threadId": "t-5"}})
            reply = client.replies_for(99)[0]
            self.assertEqual(reply["error"]["code"], -32601, method)

    def test_unhandled_log_carries_enough_to_diagnose(self):
        seen = {}
        original = bridge._log_event

        def spy(event, **kw):
            if event == "codex_app_server_unhandled_request":
                seen.update(kw)
            return original(event, **kw)

        bridge._log_event = spy
        try:
            client = RecordingClient()
            _handle(client, {"jsonrpc": "2.0", "id": 98,
                             "method": "attestation/generate",
                             "params": {"threadId": "t-5", "turnId": "u-5",
                                        "nonce": "abc"}})
        finally:
            bridge._log_event = original
        self.assertEqual(seen.get("method"), "attestation/generate")
        self.assertIn("thread_id_hash", seen)
        self.assertIn("turn_id_hash", seen)
        self.assertEqual(seen.get("param_keys"), "nonce,threadId,turnId")


class TestAnswerEndpoint(unittest.TestCase):
    """POST /codexsessions/{thread_id}/answer —— body 形狀刻意與 CC 那條
    `POST /ccsessions/{name}/answer` 對齊({keys:[...], submit:true})。"""

    def setUp(self):
        from fastapi.testclient import TestClient
        # 全套跑時額度可能已被別的測試耗光 → 401/400/409 會變 429。
        reset_auth_throttle()
        # 讀 bridge.BRIDGE_TOKEN 而不是 os.environ:全套跑時 bridge 可能已被
        # 別的測試模組先 import 過，那時的 env 才是生效值，本檔的
        # setdefault 只在「本檔第一個 import bridge」時算數。
        self.auth = {"Authorization": "Bearer " + bridge.BRIDGE_TOKEN}
        self.client = RecordingClient()
        self._real = bridge.CODEX_APP
        bridge.CODEX_APP = self.client
        self.http = TestClient(bridge.app)
        _handle(self.client, {"jsonrpc": "2.0", "id": 51,
                              "method": "item/tool/requestUserInput",
                              "params": TestRequestUserInput.PARAMS})

    def tearDown(self):
        bridge.CODEX_APP = self._real
        # 本檔自己打出來的認證失敗也不要留給後面的測試。
        reset_auth_throttle()

    def test_answer_with_keys_list(self):
        r = self.http.post("/codexsessions/t-2/answer",
                           json={"keys": ["opt0"], "submit": True},
                           headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "answered")
        self.assertEqual(self.client.replies_for(51)[0]["result"],
                         {"answers": {"q1": {"answers": ["staging"]}}})

    def test_unknown_key_rejected(self):
        r = self.http.post("/codexsessions/t-2/answer",
                           json={"keys": ["nope"]}, headers=self.auth)
        self.assertEqual(r.status_code, 400)

    def test_missing_answer_rejected(self):
        r = self.http.post("/codexsessions/t-2/answer", json={},
                           headers=self.auth)
        self.assertEqual(r.status_code, 400)

    def test_no_pending_question_is_409(self):
        r = self.http.post("/codexsessions/t-nope/answer",
                           json={"keys": ["opt0"]}, headers=self.auth)
        self.assertEqual(r.status_code, 409)

    def test_requires_auth(self):
        r = self.http.post("/codexsessions/t-2/answer", json={"keys": ["opt0"]},
                           headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)

    def test_approval_center_decision_route_answers_the_question(self):
        """既有卡片流的決議端點(/app/v1/approvals/{id}/decision)也要通 ——
        question 類要落 answered,而不是被當二元核准。"""
        record = self.client.pending_question_for_thread("t-2")
        r = self.http.post(f"/app/v1/approvals/{record['id']}/decision",
                           json={"key": "opt1"}, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "answered")
        self.assertEqual(self.client.replies_for(51)[0]["result"],
                         {"answers": {"q1": {"answers": ["production"]}}})

    def test_question_row_lands_in_approvals_db_as_question_kind(self):
        record = self.client.pending_question_for_thread("t-2")
        row = bridge._approval_get_row(record["id"])
        self.assertEqual(row["kind"], "question")
        self.assertEqual(row["provider"], "codex")
        self.assertEqual([o["key"] for o in row["options"]],
                         ["opt0", "opt1", "deny"])
        # deny style 落庫要收斂成 canonical 字彙(A1)
        self.assertEqual(row["options"][-1]["style"], "danger")


class TestApprovalRegressions(unittest.TestCase):
    """既有 4 個 approval method 的行為不能被這次重構動到。"""

    def test_command_approval_still_creates_permission_record(self):
        client = RecordingClient()
        _handle(client, {"jsonrpc": "2.0", "id": 11,
                         "method": "item/commandExecution/requestApproval",
                         "params": {"threadId": "t-6", "command": ["ls", "-l"],
                                    "cwd": "/tmp"}})
        self.assertEqual(client.replies_for(11), [])
        record = client.pending_approval_for_thread("t-6")
        self.assertIsNotNone(record)
        self.assertIsNone(client.pending_question_for_thread("t-6"))
        self.assertEqual([o["key"] for o in record["options"]],
                         ["approve", "approve_for_session", "deny"])
        out = _run(client.decide_approval(record["id"], True, for_session=True))
        self.assertEqual(out["status"], "approved")
        self.assertEqual(client.replies_for(11)[0]["result"],
                         {"decision": "acceptForSession"})

    def test_safe_defaults_match_protocol(self):
        self.assertEqual(bridge._codex_safe_question_result("item/tool/requestUserInput"),
                         {"answers": {}})
        self.assertEqual(
            bridge._codex_safe_question_result("mcpServer/elicitation/request"),
            {"action": "decline", "content": None, "_meta": None})
        self.assertEqual(
            sorted(bridge._codex_safe_question_result(
                "item/permissions/requestApproval").keys()),
            ["permissions", "scope", "strictAutoReview"])
        self.assertEqual(bridge._codex_safe_question_result("nope"), {})


# ═══════════════════ 修 review 抓到的阻斷缺陷（B1 / H1 / H2）═══════════════════

def _question_client(request_id=61, params=None):
    client = RecordingClient()
    _handle(client, {"jsonrpc": "2.0", "id": request_id,
                     "method": "item/tool/requestUserInput",
                     "params": params or TestRequestUserInput.PARAMS})
    return client


class TestQuestionKindGuard(unittest.TestCase):
    """B1（BLOCKER）:thread 層的二元審批不准回到 question 類 server request。

    以前 `decide_thread_approval()` 只做「這個 thread 有沒有 pending」，
    抓到 `item/tool/requestUserInput` 也照樣送 `{"decision": "approved"}`。
    codex 端期待的是 `ToolRequestUserInputResponse{answers}`，deserialize
    直接失敗 → **turn 死掉**（正是本 PR 要修的那個病），而且 approvals DB
    那一列已經被寫成 approved，使用者看到「已允許」但實際上回合陣亡。
    """

    def test_binary_approve_does_not_corrupt_a_question(self):
        client = _question_client()
        record = client.pending_question_for_thread("t-2")
        with self.assertRaises(bridge.CodexAppServerError) as ctx:
            _run(client.decide_thread_approval("t-2", True))
        self.assertEqual(ctx.exception.code, 409)
        self.assertIn("問答", str(ctx.exception))
        # 一個 frame 都不准送出去 —— turn 必須完好如初
        self.assertEqual(client.replies_for(61), [])
        # 卡片還在（使用者可以改用 /answer 作答），DB 也不准被標成 approved
        self.assertIsNotNone(client.pending_question_for_thread("t-2"))
        row = bridge._approval_get_row(record["id"])
        self.assertEqual(row["status"], "pending")

    def test_decide_approval_by_id_is_guarded_too(self):
        """走 approval_id 的那條路（/app/v1/approvals/{id}/decision 的
        fallback 分支）同樣要擋，否則換個入口一樣能弄壞 turn。"""
        client = _question_client(request_id=62)
        record = client.pending_question_for_thread("t-2")
        with self.assertRaises(bridge.CodexAppServerError) as ctx:
            _run(client.decide_approval(record["id"], True))
        self.assertEqual(ctx.exception.code, 409)
        self.assertEqual(client.replies_for(62), [])
        self.assertIsNotNone(client.pending_question_for_thread("t-2"))

    def test_answer_question_refuses_an_approval_record(self):
        """反方向也要擋:拿作答介面去回二元審批。"""
        client = RecordingClient()
        _handle(client, {"jsonrpc": "2.0", "id": 63,
                         "method": "item/commandExecution/requestApproval",
                         "params": {"threadId": "t-9", "command": ["ls"]}})
        record = client.pending_approval_for_thread("t-9")
        with self.assertRaises(bridge.CodexAppServerError) as ctx:
            _run(client.answer_question(record["id"], key="opt0"))
        self.assertEqual(ctx.exception.code, 409)
        self.assertEqual(client.replies_for(63), [])
        self.assertIsNotNone(client.pending_approval_for_thread("t-9"))

    def test_deny_is_routed_to_the_right_response_shape(self):
        """「拒絕」在 requestUserInput 有等價語意（略過不答），可以無損路由。"""
        client = _question_client(request_id=64)
        out = _run(client.decide_thread_approval("t-2", False))
        self.assertEqual(out["status"], "answered")
        self.assertEqual(client.replies_for(64)[0]["result"],
                         {"answers": {"q1": {"answers": []}}})

    def test_elicitation_maps_binary_decision_losslessly(self):
        """MCP elicitation 本來就是允許/拒絕二選一 → 兩個方向都路由得過去。"""
        for approved, expect in ((True, "accept"), (False, "decline")):
            client = RecordingClient()
            _handle(client, {"jsonrpc": "2.0", "id": 65,
                             "method": "mcpServer/elicitation/request",
                             "params": {"threadId": "t-8", "serverName": "svc",
                                        "message": "ok?", "requestedSchema": {}}})
            out = _run(client.decide_thread_approval("t-8", approved))
            self.assertEqual(out["status"], "answered")
            self.assertEqual(client.replies_for(65)[0]["result"]["action"], expect)

    def test_real_approval_wins_over_a_pending_question(self):
        """同一個 thread 同時有審批卡與問答卡時，thread 層的允許要落在
        審批卡上（以前是看 dict 順序，抓到誰算誰）。"""
        client = RecordingClient()
        _handle(client, {"jsonrpc": "2.0", "id": 66,
                         "method": "item/tool/requestUserInput",
                         "params": {**TestRequestUserInput.PARAMS,
                                    "threadId": "t-7"}})
        _handle(client, {"jsonrpc": "2.0", "id": 67,
                         "method": "item/commandExecution/requestApproval",
                         "params": {"threadId": "t-7", "command": ["ls"]}})
        out = _run(client.decide_thread_approval("t-7", True))
        self.assertEqual(out["status"], "approved")
        self.assertEqual(client.replies_for(67)[0]["result"], {"decision": "accept"})
        self.assertEqual(client.replies_for(66), [])          # 問答卡不准被動到
        self.assertIsNotNone(client.pending_question_for_thread("t-7"))


class TestSingleResponsePerRequestId(unittest.TestCase):
    """H1:同一個 JSON-RPC id 只准有一個 response（協定違規會被 codex 當
    client 壞掉）。以前是「先寫 response、再 pop pending」，兩條決議路徑
    （卡片按鈕 / autoResolution timer / thread 層 API / 兩台裝置）撞在一起
    就會各寫一個 result frame。"""

    def test_second_decision_gets_404_not_a_second_frame(self):
        client = _question_client(request_id=71)
        record = client.pending_question_for_thread("t-2")
        _run(client.answer_question(record["id"], key="opt0"))
        with self.assertRaises(bridge.CodexAppServerError) as ctx:
            _run(client.answer_question(record["id"], key="opt1"))
        self.assertEqual(ctx.exception.code, 404)
        self.assertEqual(len(client.replies_for(71)), 1)

    def test_concurrent_answers_only_produce_one_frame(self):
        """兩台裝置同一瞬間按下去（同一個 loop 內併發）。"""
        client = _question_client(request_id=72)
        record = client.pending_question_for_thread("t-2")

        async def both():
            return await asyncio.gather(
                client.answer_question(record["id"], key="opt0"),
                client.answer_question(record["id"], key="opt1"),
                return_exceptions=True)

        out = _run(both())
        ok = [x for x in out if isinstance(x, dict)]
        err = [x for x in out if isinstance(x, bridge.CodexAppServerError)]
        self.assertEqual(len(ok), 1, out)
        self.assertEqual(len(err), 1, out)
        self.assertEqual(err[0].code, 404)
        self.assertEqual(len(client.replies_for(72)), 1)

    def test_auto_resolution_racing_a_manual_answer(self):
        """autoResolutionMs 到期的自動作答 vs 使用者手動作答。"""
        client = _question_client(request_id=73)
        record = client.pending_question_for_thread("t-2")

        async def race():
            return await asyncio.gather(
                client.answer_question(record["id"], key="opt0"),
                client.answer_question(record["id"], key="", text="", auto=True),
                return_exceptions=True)

        _run(race())
        self.assertEqual(len(client.replies_for(73)), 1)

    def test_approval_side_is_single_response_too(self):
        client = RecordingClient()
        _handle(client, {"jsonrpc": "2.0", "id": 74,
                         "method": "item/commandExecution/requestApproval",
                         "params": {"threadId": "t-10", "command": ["ls"]}})
        record = client.pending_approval_for_thread("t-10")
        _run(client.decide_approval(record["id"], True))
        with self.assertRaises(bridge.CodexAppServerError):
            _run(client.decide_approval(record["id"], False))
        self.assertEqual(len(client.replies_for(74)), 1)

    def test_write_failure_puts_the_card_back(self):
        """pop-before-write 不能讓「寫不出去」變成死卡:要放回 pending。"""
        client = _question_client(request_id=75)
        record = client.pending_question_for_thread("t-2")
        client.proc = None          # app-server 掉線 → _write_server_result 丟例外
        with self.assertRaises(bridge.CodexAppServerError):
            _run(client.answer_question(record["id"], key="opt0"))
        self.assertIsNotNone(client.pending_question_for_thread("t-2"))
        client.proc = FakeProc()
        _run(client.answer_question(record["id"], key="opt0"))
        self.assertEqual(len(client.replies_for(75)), 1)


class TestSecretAnswersNeverPersisted(unittest.TestCase):
    """H2:`ToolRequestUserInputQuestion.isSecret` 之前只被解析、沒被使用，
    使用者貼進來的 API key 直接以明文寫進 CANON_DB 的 `approvals.result`。"""

    SECRET = "sk-live-51H8ZqREGRESSION0000"
    PARAMS = {
        "threadId": "t-secret", "turnId": "u-1", "itemId": "i-1",
        "questions": [{
            "id": "qk", "header": "貼上 API key", "question": "OpenAI key?",
            "isOther": True, "isSecret": True, "options": [],
        }, {
            "id": "qn", "header": "環境", "question": "哪個環境?",
            "isOther": True, "isSecret": False, "options": [],
        }],
    }

    def _answered(self, request_id=81):
        client = _question_client(request_id=request_id, params=self.PARAMS)
        record = client.pending_question_for_thread("t-secret")
        out = _run(client.answer_question(record["id"], key="", text=self.SECRET))
        return client, record, out

    def test_plaintext_reaches_app_server_but_nothing_else(self):
        client, record, out = self._answered(81)
        # app-server 那個 frame 必須拿到真答案，否則功能是壞的
        self.assertEqual(client.replies_for(81)[0]["result"],
                         {"answers": {"qk": {"answers": [self.SECRET]},
                                      "qn": {"answers": []}}})
        # 回給 caller 的、以及落庫的，都只能是佔位字串
        self.assertEqual(out["result"]["answers"]["qk"]["answers"],
                         [bridge.CODEX_SECRET_ANSWER_PLACEHOLDER])

    def test_canon_db_has_no_plaintext_secret(self):
        client, record, _ = self._answered(82)
        con = sqlite3.connect(bridge.CANON_DB)
        try:
            row = con.execute("SELECT status, result FROM approvals WHERE id=?",
                              (record["id"],)).fetchone()
        finally:
            con.close()
        self.assertEqual(row[0], "answered")
        self.assertNotIn(self.SECRET, row[1] or "")
        self.assertIn(bridge.CODEX_SECRET_ANSWER_PLACEHOLDER, row[1] or "")
        # 整張表掃一遍，確定沒有從別的欄位漏出去
        con = sqlite3.connect(bridge.CANON_DB)
        try:
            dump = json.dumps(con.execute("SELECT * FROM approvals").fetchall(),
                              ensure_ascii=False, default=str)
        finally:
            con.close()
        self.assertNotIn(self.SECRET, dump)

    def test_non_secret_answers_are_untouched(self):
        client = _question_client(request_id=83, params=self.PARAMS)
        record = client.pending_question_for_thread("t-secret")
        params2 = {**self.PARAMS,
                   "questions": [{**self.PARAMS["questions"][1]}]}
        client2 = _question_client(request_id=84, params=params2)
        rec2 = client2.pending_question_for_thread("t-secret")
        out = _run(client2.answer_question(rec2["id"], key="", text="staging"))
        self.assertEqual(out["result"], {"answers": {"qn": {"answers": ["staging"]}}})

    def test_secret_stays_out_of_logs(self):
        seen = []
        real = bridge._log_event
        bridge._log_event = lambda ev, **kw: (seen.append((ev, kw)), real(ev, **kw))[1]
        try:
            self._answered(85)
        finally:
            bridge._log_event = real
        blob = json.dumps(seen, ensure_ascii=False, default=str)
        self.assertNotIn(self.SECRET, blob)
        self.assertTrue(any(ev == "codex_question_decision" and kw.get("secret") is True
                            for ev, kw in seen), seen)


if __name__ == "__main__":
    unittest.main()
