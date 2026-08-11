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
import asyncio
import datetime
import os
import sys
import tempfile
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

    `_check_auth` 每次認證失敗就往模組級的 `_AUTH_FAILS` deque 記一筆，
    同一個 60s 視窗內超過 `_AUTH_FAIL_MAX`(12) 之後改回 **429**。那個 deque
    是行程全域的，所以全套 `unittest discover` 一起跑時，前面任何打過認證
    失敗路徑的測試(例如 test_upload_file_endpoint)都會先把額度用掉，輪到
    後面的測試時預期的 401/400/409 全變成 429 —— 過不過取決於執行順序。

    正式限流行為不動(那是對的),只在測試側把狀態清乾淨。
    """
    with bridge._AUTH_LOCK:
        bridge._AUTH_FAILS.clear()
        bridge._AUTH_FAIL_AGG.clear()


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
    def test_answers_rfc3339_utc(self):
        client = RecordingClient()
        _handle(client, {"jsonrpc": "2.0", "id": 5,
                         "method": "currentTime/read", "params": {}})
        result = client.replies_for(5)[0]["result"]
        # CurrentTimeReadResponse = {currentTimeAt}（binary: 1 element）
        self.assertEqual(list(result.keys()), ["currentTimeAt"])
        value = result["currentTimeAt"]
        self.assertTrue(value.endswith("Z"), value)
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        self.assertEqual(parsed.tzinfo.utcoffset(parsed),
                         datetime.timedelta(0))


class TestPermissionsRequestApproval(unittest.TestCase):
    def test_grants_nothing_but_keeps_turn_alive(self):
        client = RecordingClient()
        _handle(client, {"jsonrpc": "2.0", "id": 6,
                         "method": "item/permissions/requestApproval",
                         "params": {"threadId": "t-1", "turnId": "u-1"}})
        reply = client.replies_for(6)[0]
        self.assertNotIn("error", reply)
        self.assertEqual(reply["result"], {"permissions": {}, "scope": "turn"})


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
        self.assertEqual(bridge._codex_safe_question_result("nope"), {})


if __name__ == "__main__":
    unittest.main()
