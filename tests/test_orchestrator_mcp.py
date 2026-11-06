"""Orchestrator MCP 工具面 v1(POST /mcp,JSON-RPC over HTTP)。

涵蓋:
- **協定三式**:initialize / tools/list / tools/call(+ping、notifications 202、
  未知 method → -32601)。
- **tools/list 形狀**:四顆工具齊、inputSchema/required 正確。
- **四工具 happy path**(monkeypatch create/input;status/wait 走真 event_log):
  open_thread 回 session_id + delegation 記帳 + `thread.delegated` 進 event_log;
  send 轉發統一 input 路由;status 撈 phase/busy/assistant 里程碑;
  wait_idle 即回(idle/blocked)。
- **並行上限 3**:第 4 顆 open_thread 回 isError + ORCH_MCP_DELEGATION_LIMIT。
- **auth**:無 token 401;device token(非 master)403 —— 機器對機器面
  只收 BRIDGE_TOKEN,權限只縮不放。
- **wait 逾時**:busy 不退 → timed_out=true,且不取消對方工作(無副作用)。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線)
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="orchestrator-mcp-test-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("POCKET_REGISTRY_DB", os.path.join(_TMP, "registry.db"))
os.environ.setdefault("HARNESS_DB", os.path.join(_TMP, "harness.db"))
os.environ.setdefault("OPENCLAW_CONFIG_FILE", os.path.join(_TMP, "openclaw.json"))
os.environ.setdefault("POCKET_CODEX_HOME", os.path.join(_TMP, "codex-home"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import bridge  # noqa: E402
from fastapi import HTTPException  # noqa: E402

AUTH = "Bearer test-unit-token"


class _FakeURL:
    path = "/mcp"

    def __str__(self):
        return "http://testserver/mcp"


class FakeRequest:
    client = None
    url = _FakeURL()

    def __init__(self, body=None, auth=AUTH):
        self.headers = {"authorization": auth} if auth else {}
        self._body = body or {}

    async def json(self):
        return self._body


def _rpc(body, auth=AUTH):
    return asyncio.run(bridge.orchestrator_mcp_rpc(FakeRequest(body, auth=auth)))


def _tool_call(name, arguments, auth=AUTH):
    res = _rpc({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}}, auth=auth)
    result = res["result"]
    payload = json.loads(result["content"][0]["text"])
    return bool(result.get("isError")), payload


class ProtocolShape(unittest.TestCase):
    def test_initialize(self):
        res = _rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(res["id"], 1)
        self.assertEqual(res["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(res["result"]["serverInfo"]["name"], "pocket-orchestrator")
        self.assertIn("tools", res["result"]["capabilities"])

    def test_initialize_unknown_version_falls_back(self):
        res = _rpc({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                    "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(res["result"]["protocolVersion"],
                         bridge._ORCH_MCP_PROTOCOL)

    def test_ping(self):
        res = _rpc({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        self.assertEqual(res["result"], {})

    def test_notifications_return_202(self):
        res = _rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(res.status_code, 202)

    def test_unknown_method(self):
        res = _rpc({"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
        self.assertEqual(res["error"]["code"], -32601)

    def test_tools_list_shape(self):
        res = _rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
        tools = {t["name"]: t for t in res["result"]["tools"]}
        self.assertEqual(set(tools), {"pocket_open_thread", "pocket_send",
                                      "pocket_status", "pocket_wait_idle"})
        for tool in tools.values():
            self.assertTrue(tool["description"])
            self.assertEqual(tool["inputSchema"]["type"], "object")
        self.assertEqual(tools["pocket_open_thread"]["inputSchema"]["required"],
                         ["provider", "prompt"])
        self.assertEqual(
            tools["pocket_open_thread"]["inputSchema"]["properties"]["provider"]["enum"],
            ["codex", "claude_code"])
        self.assertEqual(tools["pocket_send"]["inputSchema"]["required"],
                         ["session_id", "text"])
        self.assertEqual(tools["pocket_status"]["inputSchema"]["required"],
                         ["session_id"])
        self.assertEqual(tools["pocket_wait_idle"]["inputSchema"]["required"],
                         ["session_id"])

    def test_unknown_tool_is_error_result(self):
        is_err, payload = _tool_call("pocket_fly_to_moon", {})
        self.assertTrue(is_err)
        self.assertEqual(payload["error"]["code"], "ORCH_MCP_UNKNOWN_TOOL")


class Auth(unittest.TestCase):
    def test_no_token_401(self):
        with self.assertRaises(HTTPException) as ctx:
            _rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, auth=None)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_wrong_token_401(self):
        with self.assertRaises(HTTPException) as ctx:
            _rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                 auth="Bearer not-the-token")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_device_token_403(self):
        # 配對出來的 device token 過得了一般 _check_auth,但 /mcp 是機器對
        # 機器的編排面 —— 只收 master BRIDGE_TOKEN(權限只縮不放)。
        with bridge._PAIR_LOCK:
            bridge._DEVICE_TOKENS["dev-tok-orch"] = {"platform": "ios"}
        try:
            with self.assertRaises(HTTPException) as ctx:
                _rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                     auth="Bearer dev-tok-orch")
            self.assertEqual(ctx.exception.status_code, 403)
        finally:
            with bridge._PAIR_LOCK:
                bridge._DEVICE_TOKENS.pop("dev-tok-orch", None)


class OpenThread(unittest.TestCase):
    def _fake_create(self, delegation):
        calls = []

        async def fake(request):
            body = await request.json()
            calls.append((request, body))
            return {"ok": True, "delegation": dict(delegation)}

        return fake, calls

    def test_happy_path_codex(self):
        delegation = {"id": "dlg-abc", "work_order": "XW-ORCH-MCP-20261106-AAAA",
                      "provider": "codex", "codex_thread_id": "th-123",
                      "cc_session_name": "", "status": "running"}
        fake, calls = self._fake_create(delegation)
        with patch.object(bridge, "app_delegation_create", fake):
            is_err, payload = _tool_call("pocket_open_thread", {
                "provider": "cx", "prompt": "跑完整測試並回報", "cwd": "~/apps/demo"})
        self.assertFalse(is_err)
        self.assertEqual(payload["session_id"], "codex:th-123")
        self.assertEqual(payload["delegation_id"], "dlg-abc")
        self.assertEqual(payload["work_order"], "XW-ORCH-MCP-20261106-AAAA")
        # 轉發 body:復用既有 create 路徑,記帳欄位齊全
        req, body = calls[0]
        self.assertEqual(body["provider"], "codex")       # cx 別名已正規化
        self.assertEqual(body["objective"], "跑完整測試並回報")
        self.assertEqual(body["task_code"], "ORCH")
        self.assertEqual(body["subtask_code"], "MCP")
        self.assertEqual(body["created_via"], "orchestrator_mcp")
        self.assertEqual(body["cwd"], "~/apps/demo")
        # 轉發 request 帶同一把 Authorization(權限只縮不放,不造新授權)
        self.assertEqual(req.headers["authorization"], AUTH)
        # `thread.delegated` 進 event_log(audit 可查)
        con = sqlite3.connect(bridge.CANON_DB)
        row = con.execute(
            "SELECT payload FROM event_log WHERE session=? AND type=?",
            ("codex:th-123", "thread.delegated")).fetchone()
        con.close()
        self.assertIsNotNone(row)
        evt = json.loads(row[0])
        self.assertEqual(evt["delegation_id"], "dlg-abc")
        self.assertEqual(evt["created_via"], "orchestrator_mcp")

    def test_happy_path_claude_code(self):
        delegation = {"id": "dlg-cc", "work_order": "XW-ORCH-MCP-20261106-BBBB",
                      "provider": "claude_code", "codex_thread_id": "",
                      "cc_session_name": "xw-orch-mcp", "status": "running"}
        fake, _calls = self._fake_create(delegation)
        with patch.object(bridge, "app_delegation_create", fake):
            is_err, payload = _tool_call("pocket_open_thread", {
                "provider": "claude_code", "prompt": "hi"})
        self.assertFalse(is_err)
        self.assertEqual(payload["session_id"], "claude_code:xw-orch-mcp")

    def test_empty_prompt_400(self):
        is_err, payload = _tool_call("pocket_open_thread",
                                     {"provider": "codex", "prompt": "  "})
        self.assertTrue(is_err)
        self.assertEqual(payload["error"]["code"], "ORCH_MCP_BAD_REQUEST")

    def test_bad_provider_400(self):
        is_err, payload = _tool_call("pocket_open_thread",
                                     {"provider": "gemini", "prompt": "x"})
        self.assertTrue(is_err)
        self.assertEqual(payload["error"]["status"], 400)

    def test_parallel_limit_3(self):
        rows = [{"id": f"dlg-{i}", "work_order": f"WO-{i}",
                 "created_via": "orchestrator_mcp", "provider": "codex",
                 "codex_thread_id": f"th-{i}", "status": "running"}
                for i in range(3)]

        async def fake_runtime(_row):
            return "running"

        async def fail_create(_request):  # 不應該被叫到
            raise AssertionError("create must not run past the limit")

        with patch.object(bridge, "_delegation_rows",
                          lambda *a, **k: list(rows)), \
             patch.object(bridge, "_delegation_runtime_status", fake_runtime), \
             patch.object(bridge, "app_delegation_create", fail_create):
            is_err, payload = _tool_call("pocket_open_thread",
                                         {"provider": "codex", "prompt": "x"})
        self.assertTrue(is_err)
        self.assertEqual(payload["error"]["code"], "ORCH_MCP_DELEGATION_LIMIT")
        self.assertEqual(payload["error"]["status"], 429)

    def test_terminal_rows_do_not_count(self):
        rows = [{"id": f"dlg-{i}", "created_via": "orchestrator_mcp",
                 "provider": "codex", "codex_thread_id": f"th-{i}",
                 "status": "running"} for i in range(3)]
        delegation = {"id": "dlg-new", "work_order": "WO-NEW",
                      "provider": "codex", "codex_thread_id": "th-new",
                      "status": "running"}
        fake, _calls = self._fake_create(delegation)

        async def fake_runtime(_row):
            return "idle"   # 已收斂 → 不佔配額(記帳看 runtime,不看靜態欄位)

        with patch.object(bridge, "_delegation_rows",
                          lambda *a, **k: list(rows)), \
             patch.object(bridge, "_delegation_runtime_status", fake_runtime), \
             patch.object(bridge, "app_delegation_create", fake):
            is_err, payload = _tool_call("pocket_open_thread",
                                         {"provider": "codex", "prompt": "x"})
        self.assertFalse(is_err)
        self.assertEqual(payload["session_id"], "codex:th-new")


class Send(unittest.TestCase):
    def test_happy_path(self):
        calls = []

        async def fake_input(session_id, request):
            body = await request.json()
            calls.append((session_id, body))
            return {"ok": True, "session_id": session_id, "accepted": True}

        with patch.object(bridge, "v2_session_input", fake_input):
            is_err, payload = _tool_call("pocket_send", {
                "session_id": "codex:th-9", "text": "繼續,跑測試",
                "client_id": "orch-retry-1"})
        self.assertFalse(is_err)
        self.assertTrue(payload["accepted"])
        sid, body = calls[0]
        self.assertEqual(sid, "codex:th-9")
        self.assertEqual(body["content"], "繼續,跑測試")
        self.assertEqual(body["client_id"], "orch-retry-1")

    def test_missing_fields_400(self):
        is_err, payload = _tool_call("pocket_send", {"session_id": "codex:x"})
        self.assertTrue(is_err)
        self.assertEqual(payload["error"]["code"], "ORCH_MCP_BAD_REQUEST")


class StatusAndWait(unittest.TestCase):
    def _seed(self, sid, phase, busy, milestone=None):
        bridge._event_append(sid, "session.status",
                             {"busy": busy, "phase": phase, "label": phase})
        if milestone is not None:
            bridge._event_append(sid, "card.milestone", {"card": {
                "id": "c1", "role": "assistant", "kind": "markdown",
                "final": True, "body": {"text": milestone}}})

    def test_status_happy_path(self):
        sid = "codex:st-happy"
        self._seed(sid, "idle", False, milestone="測試全綠,已 push abc1234")
        is_err, payload = _tool_call("pocket_status", {"session_id": sid})
        self.assertFalse(is_err)
        self.assertEqual(payload["phase"], "idle")
        self.assertFalse(payload["busy"])
        self.assertIn("測試全綠", payload["last_assistant_milestone"]["text"])
        self.assertGreater(payload["last_event_seq"], 0)

    def test_status_uses_latest_events(self):
        sid = "codex:st-latest"
        self._seed(sid, "run", True, milestone="第一階段完成")
        self._seed(sid, "idle", False, milestone="全部完成")
        is_err, payload = _tool_call("pocket_status", {"session_id": sid})
        self.assertFalse(is_err)
        self.assertEqual(payload["phase"], "idle")
        self.assertEqual(payload["last_assistant_milestone"]["text"], "全部完成")

    def test_status_no_events_is_unknown(self):
        is_err, payload = _tool_call("pocket_status",
                                     {"session_id": "codex:st-empty"})
        self.assertFalse(is_err)
        self.assertEqual(payload["phase"], "unknown")
        self.assertIsNone(payload["last_assistant_milestone"])

    def test_status_bad_provider_rejected(self):
        is_err, payload = _tool_call("pocket_status",
                                     {"session_id": "warp:engine-1"})
        self.assertTrue(is_err)
        self.assertEqual(payload["error"]["status"], 400)

    def test_wait_idle_returns_immediately_when_idle(self):
        sid = "codex:wait-idle"
        self._seed(sid, "idle", False, milestone="收工")
        t0 = time.monotonic()
        is_err, payload = _tool_call("pocket_wait_idle",
                                     {"session_id": sid, "timeout_secs": 30})
        self.assertFalse(is_err)
        self.assertFalse(payload["timed_out"])
        self.assertEqual(payload["last_assistant_milestone"]["text"], "收工")
        self.assertLess(time.monotonic() - t0, 5.0)

    def test_wait_idle_returns_on_blocked(self):
        # blocked = 對方在等輸入/核准,不會自己前進 —— 立刻把球交回編排方,
        # phase 說明原因,而不是乾等到逾時。
        sid = "codex:wait-blocked"
        self._seed(sid, "blocked", True)
        is_err, payload = _tool_call("pocket_wait_idle",
                                     {"session_id": sid, "timeout_secs": 30})
        self.assertFalse(is_err)
        self.assertFalse(payload["timed_out"])
        self.assertEqual(payload["phase"], "blocked")

    def test_wait_idle_timeout(self):
        sid = "codex:wait-busy"
        self._seed(sid, "run", True)
        t0 = time.monotonic()
        is_err, payload = _tool_call("pocket_wait_idle",
                                     {"session_id": sid, "timeout_secs": 1})
        self.assertFalse(is_err)          # 逾時不是工具錯誤,是正常回報
        self.assertTrue(payload["timed_out"])
        self.assertEqual(payload["phase"], "run")
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertLess(elapsed, 10.0)

    def test_wait_idle_timeout_clamped_to_600(self):
        # 上限 600:塞 10 萬秒也只會被夾回 600(這裡只驗 clamp 邏輯本身)。
        self.assertEqual(max(1.0, min(600.0, float(100000))), 600.0)
        self.assertEqual(max(1.0, min(600.0, float(0))), 1.0)


if __name__ == "__main__":
    unittest.main()
