"""OpenClaw 兩個缺陷的回歸測試(fix/openclaw-attachments-approvals)。

缺陷 1(資料遺失):`_oc_input_core` 在「有文字 + 有附件」時把附件靜默丟掉 ——
app 顯示送出成功、自己的泡泡有圖,OpenClaw 端根本沒收到。現在附件真的進
`chat.send.attachments`(gateway 實際受理形狀 `{type,mimeType,fileName,content}`,
content 是 base64),送不出去的一律報錯。

缺陷 2(功能缺席):`exec.approval.*` / `plugin.approval.*` 事件在 digest
`handle()` 被靜默丟棄 → agent 觸發審批時 Pocket 端永遠卡住不動;`/approve`
對 openclaw 也硬拒。現在翻成 approval 卡 + 進審核中心,`/approve` 回覆
gateway。附帶:tool 分支補 tool_result 卡、純圖片訊息不再整則消失。
"""
import asyncio
import base64
import json
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="oc-att-appr-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["OPENCLAW_CONFIG_FILE"] = os.path.join(_TMP, "openclaw.json")
os.environ.pop("OPENCLAW_BASE_URL", None)
os.environ.pop("OPENCLAW_TOKEN", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carddigest as cd  # noqa: E402
import bridge  # noqa: E402
from fastapi import HTTPException  # noqa: E402

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg==")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeReq:
    def __init__(self, body=None):
        # 用 bridge 匯入時真正綁到的 token,不用 env —— discover 跑全套時
        # bridge 可能已被別的測試模組先匯入(env 早就定住了)。
        self.headers = {"authorization": f"Bearer {bridge.BRIDGE_TOKEN}"}
        self.client = type("C", (), {"host": "127.0.0.1"})()
        self._body = json.dumps(body or {}).encode()

    class _URL:
        path = "/test"
    url = _URL()

    async def json(self):
        return json.loads(self._body)

    async def body(self):
        return self._body


class _FakeOC:
    def __init__(self, configured=True):
        self._configured = configured
        self.calls = []
        self.responses = {}
        self.raises = {}

    def configured(self):
        return self._configured

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append((method, params or {}))
        if method in self.raises:
            raise self.raises[method]
        return self.responses.get(method, {})


def _upload(name: str, data: bytes) -> str:
    """把 bytes 寫進 UPLOAD_DIR(`_save_attachment` 只認這個根)。"""
    bridge.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    p = bridge.UPLOAD_DIR / name
    p.write_bytes(data)
    return str(p)


# ─────────────────────────── 缺陷 1:附件 ────────────────────────────────────

class AttachmentInputTests(unittest.TestCase):
    def setUp(self):
        self._orig = bridge.OPENCLAW
        bridge._OC_CARD_DIGESTS.clear()
        self.fake = _FakeOC()
        self.fake.responses["chat.send"] = {"runId": "run-a", "status": "started"}
        bridge.OPENCLAW = self.fake

    def tearDown(self):
        bridge.OPENCLAW = self._orig
        bridge._OC_CARD_DIGESTS.clear()

    def test_text_plus_attachment_actually_ships_the_attachment(self):
        """這就是那個資料遺失缺陷:以前 chat.send 只帶 message。"""
        path = _upload("oc-att-1.png", _PNG)
        res = _run(bridge._oc_input_core(
            "agent:main:main", "openclaw:agent:main:main",
            {"content": "看這張", "client_id": "c-1",
             "attachments": [{"kind": "image", "filename": "shot.png",
                              "mime": "image/png", "path": path}]}))
        self.assertTrue(res["accepted"])
        self.assertEqual(res["attachments"], 1)
        method, params = self.fake.calls[0]
        self.assertEqual(method, "chat.send")
        self.assertEqual(params["message"], "看這張")
        atts = params["attachments"]
        self.assertEqual(len(atts), 1)
        # gateway 只讀 {type, mimeType, fileName, content};content 必須是
        # base64,沒有 content 的件會被 gateway 靜默 filter 掉。
        self.assertEqual(set(atts[0]), {"type", "mimeType", "fileName", "content"})
        self.assertEqual(atts[0]["mimeType"], "image/png")
        self.assertEqual(atts[0]["fileName"], "shot.png")
        self.assertEqual(base64.b64decode(atts[0]["content"]), _PNG)

    def test_attachment_only_is_accepted(self):
        """gateway 的 chat.send 只要 message 或 attachments 其一有值即受理。"""
        path = _upload("oc-att-2.png", _PNG)
        res = _run(bridge._oc_input_core(
            "agent:main:main", "x",
            {"attachments": [{"kind": "image", "filename": "a.png",
                              "mime": "image/png", "path": path}]}))
        self.assertTrue(res["accepted"])
        params = self.fake.calls[0][1]
        self.assertEqual(params["message"], "")
        self.assertEqual(len(params["attachments"]), 1)

    def test_data_uri_attachment_is_decoded_and_shipped(self):
        b64 = base64.b64encode(_PNG).decode()
        res = _run(bridge._oc_input_core(
            "agent:main:main", "x",
            {"content": "hi",
             "attachments": [{"kind": "image", "filename": "d.png",
                              "data": f"data:image/png;base64,{b64}"}]}))
        self.assertEqual(res["attachments"], 1)
        self.assertEqual(
            base64.b64decode(self.fake.calls[0][1]["attachments"][0]["content"]),
            _PNG)

    def test_unreadable_attachment_raises_instead_of_silently_dropping(self):
        with self.assertRaises(HTTPException) as cm:
            _run(bridge._oc_input_core(
                "agent:main:main", "x",
                {"content": "hi",
                 "attachments": [{"kind": "image", "url": "https://x/y.png"}]}))
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(cm.exception.code, "ATTACHMENT_UNREADABLE")
        self.assertEqual(self.fake.calls, [])   # 一件都送不出就整包不送

    def test_oversized_image_is_413_not_a_dropped_frame(self):
        path = _upload("oc-att-big.png", b"\x00" * (bridge._OC_ATT_MAX_IMAGE_BYTES + 1))
        with self.assertRaises(HTTPException) as cm:
            _run(bridge._oc_input_core(
                "agent:main:main", "x",
                {"content": "hi",
                 "attachments": [{"kind": "image", "filename": "big.png",
                                  "mime": "image/png", "path": path}]}))
        self.assertEqual(cm.exception.status_code, 413)
        self.assertEqual(cm.exception.code, "ATTACHMENT_TOO_LARGE")

    def test_user_echo_card_carries_the_attachment_summary(self):
        path = _upload("oc-att-3.png", _PNG)
        d = bridge._OC_CARD_DIGESTS["agent:main:main"] = cd.OpenClawDigest()
        _run(bridge._oc_input_core(
            "agent:main:main", "x",
            {"client_id": "c-9",
             "attachments": [{"kind": "image", "filename": "e.png",
                              "mime": "image/png", "path": path}]}))
        cards = d.store.snapshot()["cards"]
        self.assertEqual(len(cards), 1)          # 純附件也要出卡
        self.assertEqual(cards[0]["body"]["attachments"][0]["filename"], "e.png")
        self.assertTrue(cards[0]["body"]["fallback_text"])


class AttachmentCardTests(unittest.TestCase):
    def test_image_only_message_still_makes_a_card(self):
        """`_oc_msg_text` 只留 text → 純圖片訊息以前整則消失。"""
        d = cd.OpenClawDigest()
        d.seed_messages([{
            "role": "user", "timestamp": 1785119281390,
            "__openclaw": {"id": "img1"},
            "content": [{"type": "image", "omitted": True, "bytes": 4096,
                         "mimeType": "image/png"}]}])
        cards = d.store.snapshot()["cards"]
        self.assertEqual(len(cards), 1)
        body = cards[0]["body"]
        self.assertEqual(body["text"], "")
        self.assertTrue(body["fallback_text"])
        self.assertEqual(body["attachments"][0]["kind"], "image")
        self.assertEqual(body["attachments"][0]["size"], 4096)
        self.assertTrue(body["attachments"][0]["omitted"])

    def test_mixed_text_and_image_keeps_both(self):
        d = cd.OpenClawDigest()
        d.handle("chat", {"runId": "r1", "state": "final", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "看圖"},
                        {"type": "image", "omitted": True, "bytes": 9}]}})
        body = d.store.snapshot()["cards"][0]["body"]
        self.assertEqual(body["text"], "看圖")
        self.assertEqual(len(body["attachments"]), 1)


# ─────────────────────────── tool_result 卡 ─────────────────────────────────

class ToolStreamTests(unittest.TestCase):
    def test_start_then_result_makes_two_distinct_cards(self):
        d = cd.OpenClawDigest()
        d.handle("agent", {"runId": "r1", "stream": "tool", "data": {
            "phase": "start", "name": "bash", "toolCallId": "t1",
            "args": {"command": "ls -la"}}})
        d.handle("agent", {"runId": "r1", "stream": "tool", "data": {
            "phase": "result", "name": "bash", "toolCallId": "t1",
            "isError": False, "result": "total 0\nfoo"}})
        cards = d.store.snapshot()["cards"]
        kinds = [c["kind"] for c in cards]
        self.assertEqual(kinds, ["tool_call", "tool_result"])
        self.assertEqual(cards[0]["body"]["summary"], "ls -la")
        self.assertIn("total 0", cards[1]["body"]["text"])
        self.assertFalse(cards[1]["body"]["is_error"])

    def test_error_result_is_marked(self):
        d = cd.OpenClawDigest()
        d.handle("agent", {"runId": "r1", "stream": "tool", "data": {
            "phase": "result", "name": "bash", "toolCallId": "t2",
            "isError": True, "toolErrorSummary": "exit 1"}})
        body = d.store.snapshot()["cards"][0]["body"]
        self.assertTrue(body["is_error"])
        self.assertIn("exit 1", body["text"])
        self.assertIn("⚠️", body["fallback_text"])

    def test_native_item_status_counts_as_error(self):
        d = cd.OpenClawDigest()
        d.handle("agent", {"runId": "r1", "stream": "tool", "data": {
            "phase": "result", "name": "edit", "itemId": "i9",
            "status": "failed", "result": {"output": "nope"}}})
        body = d.store.snapshot()["cards"][0]["body"]
        self.assertTrue(body["is_error"])
        self.assertEqual(body["text"], "nope")

    def test_unknown_result_shape_falls_back_to_json(self):
        d = cd.OpenClawDigest()
        d.handle("agent", {"runId": "r1", "stream": "tool", "data": {
            "phase": "result", "name": "x", "toolCallId": "t3",
            "result": {"weird": [1, 2]}}})
        self.assertIn("weird", d.store.snapshot()["cards"][0]["body"]["text"])


# ─────────────────────────── 缺陷 2:審批 ────────────────────────────────────

def _requested(aid="ap-1", key="agent:main:main", allowed=None):
    return {"id": aid, "createdAtMs": 1785119281390,
            "expiresAtMs": 1785119281390 + 60000,
            "request": {"command": "rm -rf /tmp/x", "cwd": "/tmp",
                        "host": "gateway", "agentId": "main",
                        "sessionKey": key, "ask": "on-miss",
                        "warningText": "危險指令",
                        "allowedDecisions": allowed or
                        ["allow-once", "allow-always", "deny"]}}


class ApprovalEventTests(unittest.TestCase):
    def setUp(self):
        self._orig = bridge.OPENCLAW
        self.fake = _FakeOC()
        bridge.OPENCLAW = self.fake
        bridge._OC_CARD_DIGESTS.clear()
        bridge._OC_APPROVAL_METHODS.clear()
        self._wipe()

    def tearDown(self):
        bridge.OPENCLAW = self._orig
        bridge._OC_CARD_DIGESTS.clear()
        bridge._OC_APPROVAL_METHODS.clear()
        self._wipe()

    @staticmethod
    def _wipe():
        import sqlite3
        con = sqlite3.connect(bridge.CANON_DB, timeout=30)
        try:
            con.execute("DELETE FROM approvals WHERE provider='openclaw'")
            con.commit()
        finally:
            con.close()

    def test_requested_event_makes_an_approval_card(self):
        d = bridge._OC_CARD_DIGESTS["agent:main:main"] = cd.OpenClawDigest()
        bridge._oc_events_feed("exec.approval.requested", _requested())
        cards = d.store.snapshot()["cards"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["kind"], "approval")
        body = cards[0]["body"]
        self.assertEqual(body["approval_id"], "ap-1")
        self.assertEqual(body["source"], "openclaw")
        self.assertEqual([o["key"] for o in body["options"]],
                         ["allow-once", "allow-always", "deny"])
        self.assertIn("rm -rf /tmp/x", body["title"])

    def test_allowed_decisions_narrow_the_options(self):
        d = bridge._OC_CARD_DIGESTS["agent:main:main"] = cd.OpenClawDigest()
        bridge._oc_events_feed("exec.approval.requested",
                               _requested(aid="ap-2",
                                          allowed=["allow-once", "deny"]))
        body = d.store.snapshot()["cards"][0]["body"]
        self.assertEqual([o["key"] for o in body["options"]],
                         ["allow-once", "deny"])

    def test_pending_row_lands_in_the_approval_hub(self):
        bridge._oc_events_feed("exec.approval.requested", _requested(aid="ap-3"))
        row = bridge._approval_get_row("ap-3")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["provider"], "openclaw")
        self.assertEqual(row["session_id"], "openclaw:agent:main:main")
        self.assertEqual(row["kind"], "permission")

    def test_plugin_approval_uses_its_own_resolve_method(self):
        bridge._oc_events_feed("plugin.approval.requested", {
            "id": "plugin:abc", "createdAtMs": 1785119281390,
            "expiresAtMs": 1785119341390,
            "request": {"pluginId": "p1", "title": "外掛想做事",
                        "description": "細節", "severity": "critical",
                        "sessionKey": "agent:main:main",
                        "allowedDecisions": ["allow-once", "deny"]}})
        row = bridge._approval_get_row("plugin:abc")
        self.assertEqual(row["title"], "外掛想做事")
        self.assertEqual(row["risk"], "high")
        self.assertEqual(bridge._OC_APPROVAL_METHODS["plugin:abc"],
                         "plugin.approval.resolve")

    def test_resolved_event_closes_the_card_and_the_row(self):
        d = bridge._OC_CARD_DIGESTS["agent:main:main"] = cd.OpenClawDigest()
        bridge._oc_events_feed("exec.approval.requested", _requested(aid="ap-4"))
        bridge._oc_events_feed("exec.approval.resolved", {
            "id": "ap-4", "decision": "deny", "resolvedBy": "cli",
            "ts": 1785119291390,
            "request": {"sessionKey": "agent:main:main", "command": "rm -rf /tmp/x"}})
        self.assertEqual(bridge._approval_get_row("ap-4")["status"], "denied")
        body = d.store.snapshot()["cards"][0]["body"]
        self.assertEqual(body["resolved"], "denied")
        self.assertEqual(body["options"], [])

    def test_approval_without_session_key_still_reaches_the_hub(self):
        """非 session 觸發的 exec 沒有可歸屬的對話,但不能就此消失。"""
        p = _requested(aid="ap-5", key="")
        p["request"]["sessionKey"] = None
        bridge._oc_events_feed("exec.approval.requested", p)
        row = bridge._approval_get_row("ap-5")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["source"], "openclaw")

    def test_malformed_event_does_not_explode(self):
        bridge._oc_events_feed("exec.approval.requested", {"request": {}})
        bridge._oc_events_feed("exec.approval.resolved", {})

    # ── /approve 路由 ──

    def test_approve_resolves_via_gateway(self):
        d = bridge._OC_CARD_DIGESTS["agent:main:main"] = cd.OpenClawDigest()
        bridge._oc_events_feed("exec.approval.requested", _requested(aid="ap-6"))
        res = _run(bridge.v2_session_approve(
            "openclaw:agent:main:main",
            _FakeReq({"approval_id": "ap-6", "key": "allow-once"})))
        self.assertEqual(res["status"], "approved")
        self.assertEqual(self.fake.calls[-1],
                         ("exec.approval.resolve",
                          {"id": "ap-6", "decision": "allow-once"}))
        self.assertEqual(bridge._approval_get_row("ap-6")["status"], "approved")
        # 決議者收不到 gateway 的 resolved 廣播 → 卡片必須自己收尾
        self.assertEqual(d.store.snapshot()["cards"][0]["body"]["resolved"],
                         "approved")

    def test_approve_deny_maps_to_denied(self):
        bridge._oc_events_feed("exec.approval.requested", _requested(aid="ap-7"))
        res = _run(bridge.v2_session_approve(
            "openclaw:agent:main:main",
            _FakeReq({"approval_id": "ap-7", "key": "deny"})))
        self.assertEqual(res["status"], "denied")

    def test_approve_bool_sugar_picks_a_real_decision(self):
        bridge._oc_events_feed("exec.approval.requested", _requested(aid="ap-8"))
        _run(bridge.v2_session_approve(
            "openclaw:agent:main:main",
            _FakeReq({"approval_id": "ap-8", "approve": True})))
        self.assertEqual(self.fake.calls[-1][1]["decision"], "allow-once")

    def test_approve_rejects_a_decision_the_gateway_never_offered(self):
        bridge._oc_events_feed("exec.approval.requested",
                               _requested(aid="ap-9",
                                          allowed=["allow-once", "deny"]))
        with self.assertRaises(HTTPException) as cm:
            _run(bridge.v2_session_approve(
                "openclaw:agent:main:main",
                _FakeReq({"approval_id": "ap-9", "key": "allow-always"})))
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(cm.exception.code, "UNKNOWN_KEY")
        self.assertEqual(self.fake.calls, [])   # 沒打 gateway

    def test_gateway_refusal_leaves_the_row_pending(self):
        """gateway 是真相:resolve 失敗就不能把 DB 標成已核准。"""
        import openclaw_provider as ocp
        bridge._oc_events_feed("exec.approval.requested", _requested(aid="ap-10"))
        self.fake.raises["exec.approval.resolve"] = ocp.OpenClawError(
            "gateway down", code="CONNECT_FAILED")
        with self.assertRaises(HTTPException):
            _run(bridge.v2_session_approve(
                "openclaw:agent:main:main",
                _FakeReq({"approval_id": "ap-10", "key": "deny"})))
        self.assertEqual(bridge._approval_get_row("ap-10")["status"], "pending")

    def test_already_resolved_upstream_is_409(self):
        import openclaw_provider as ocp
        bridge._oc_events_feed("exec.approval.requested", _requested(aid="ap-11"))
        self.fake.raises["exec.approval.resolve"] = ocp.OpenClawError(
            "approval already resolved", code="INVALID_REQUEST")
        with self.assertRaises(HTTPException) as cm:
            _run(bridge.v2_session_approve(
                "openclaw:agent:main:main",
                _FakeReq({"approval_id": "ap-11", "key": "deny"})))
        self.assertEqual(cm.exception.status_code, 409)

    # ── 重連補洞 ──

    def test_reseed_pulls_pending_approvals_after_reconnect(self):
        d = bridge._OC_CARD_DIGESTS["agent:main:main"] = cd.OpenClawDigest()
        # `*.approval.list` 回的是裸陣列(不是 {approvals: […]})
        self.fake.responses["exec.approval.list"] = [_requested(aid="ap-12")]
        self.fake.responses["plugin.approval.list"] = []
        _run(bridge._oc_approvals_reseed())
        self.assertEqual(bridge._approval_get_row("ap-12")["status"], "pending")
        self.assertEqual(d.store.snapshot()["cards"][0]["kind"], "approval")

    def test_reseed_survives_a_missing_method_family(self):
        import openclaw_provider as ocp
        self.fake.raises["plugin.approval.list"] = ocp.OpenClawError(
            "unknown method", code="INVALID_REQUEST")
        self.fake.responses["exec.approval.list"] = [_requested(aid="ap-13")]
        _run(bridge._oc_approvals_reseed())
        self.assertEqual(bridge._approval_get_row("ap-13")["status"], "pending")


if __name__ == "__main__":
    unittest.main()
