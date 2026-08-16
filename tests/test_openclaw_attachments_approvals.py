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
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import asyncio
import base64
import builtins
import json
import os
import sqlite3
import sys
import tempfile
import time
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

# UPLOAD_DIR 是模組層常數,硬編碼指向 production 的
# ~/apps/hermes-agent/home/uploads —— 這支測試會真的落檔(_save_attachment /
# data URI decode),不綁回 tmp 就是每跑一次污染一次正式 uploads 目錄
# (隔離閂上線後直接 ProductionWriteBlocked,這裡是修根因不是關警報)。
from pathlib import Path as _Path  # noqa: E402
bridge.UPLOAD_DIR = _Path(_TMP) / "uploads"

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


def _digest(session_key: str):
    """照 **production 的做法** 建 digest。

    正式路徑是 `_v2_card_source("openclaw:<key>")` → `_oc_safe_session_key()`
    改道 → 用改道後的 key 建 digest。測試如果自己拿原始 key 直接塞
    `_OC_CARD_DIGESTS`，剛好會繞過改道，把 M-1 那個缺陷測不出來
    （`agent:main:main` 正是會被改道的撞名 lane）。
    """
    key = bridge._oc_safe_session_key(session_key)
    d = bridge._OC_CARD_DIGESTS.get(key)
    if d is None:
        d = bridge._OC_CARD_DIGESTS[key] = cd.OpenClawDigest()
    return d


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
        d = _digest("agent:main:main")
        # `_oc_input_core` 在正式路徑拿到的一定是 `_v2_card_source` 改道後的
        # key（送 gateway 的 sessionKey 也是它），所以測試也照樣傳。
        _run(bridge._oc_input_core(
            bridge._oc_safe_session_key("agent:main:main"), "x",
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
        d = _digest("agent:main:main")
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
        d = _digest("agent:main:main")
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
        self.assertEqual(row["session_id"], "openclaw:agent:main:pocket")
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
        d = _digest("agent:main:main")
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
        d = _digest("agent:main:main")
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
        d = _digest("agent:main:main")
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


# ══════════════════ 修 review 抓到的阻斷缺陷（H-1 / H-2 / M-1）══════════════

class AttachmentSizeCapTests(unittest.TestCase):
    """H-1(live bridge 存活):上限要在**讀進記憶體之前**就擋下來。

    舊碼 `raw = Path(path).read_bytes()` 在前、`len(raw) > cap` 在後 ——
    宣告 2GiB × 12 件 = 最多 24GB 灌進 RAM 才回 413，production bridge
    早被 OOM killer 收走了。這裡直接把「有沒有真的讀進來」量出來。
    """

    def setUp(self):
        self._orig = bridge.OPENCLAW
        self.fake = _FakeOC()
        self.fake.responses["chat.send"] = {"runId": "run-cap"}
        bridge.OPENCLAW = self.fake
        bridge._OC_CARD_DIGESTS.clear()
        self._reads = []
        self._real_open = builtins.open

        def _spy_open(file, mode="r", *a, **kw):
            fh = self._real_open(file, mode, *a, **kw)
            if "b" in str(mode) and "r" in str(mode):
                real_read = fh.read

                def read(n=-1):
                    data = real_read(n)
                    self._reads.append(len(data))
                    return data
                fh.read = read
            return fh
        builtins.open = _spy_open

    def tearDown(self):
        builtins.open = self._real_open
        bridge.OPENCLAW = self._orig
        bridge._OC_CARD_DIGESTS.clear()

    def _oversized(self, name="huge.bin", extra=1):
        """做一個「宣告很大」的檔:用稀疏檔，不用真的佔 20MB 磁碟。"""
        bridge.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        p = bridge.UPLOAD_DIR / name
        with self._real_open(p, "wb") as fh:
            fh.truncate(bridge._OC_ATT_MAX_FILE_BYTES + extra)
        return str(p)

    def test_oversized_file_is_rejected_without_reading_it(self):
        path = self._oversized()
        with self.assertRaises(HTTPException) as cm:
            _run(bridge._oc_input_core(
                "agent:main:pocket", "x",
                {"content": "hi",
                 "attachments": [{"filename": "huge.bin",
                                  "mime": "application/octet-stream",
                                  "path": path}]}))
        self.assertEqual(cm.exception.status_code, 413)
        self.assertEqual(cm.exception.code, "ATTACHMENT_TOO_LARGE")
        # 關鍵斷言:一個 byte 都沒讀進來
        self.assertEqual(sum(self._reads), 0, self._reads)
        self.assertEqual(self.fake.calls, [])

    def test_a_read_never_exceeds_the_cap_even_if_stat_lies(self):
        """TOCTOU:stat 之後檔案長大也不准吞超過上限（讀取封頂 cap+1）。"""
        path = self._oversized(name="grow.bin", extra=1)   # 比上限多 1 byte
        real_getsize = os.path.getsize

        def _lying_getsize(p):
            return 1 if str(p) == path else real_getsize(p)
        os.path.getsize = _lying_getsize
        try:
            with self.assertRaises(HTTPException) as cm:
                _run(bridge._oc_input_core(
                    "agent:main:pocket", "x",
                    {"content": "hi",
                     "attachments": [{"filename": "grow.bin", "path": path}]}))
        finally:
            os.path.getsize = real_getsize
        # stat 說 1 byte，實際超標 → 只有「讀取封頂」擋得住；而且吞進來的
        # 量必須 ≤ cap+1，絕不是「整包無上限讀」。
        self.assertEqual(cm.exception.status_code, 413)
        self.assertLessEqual(max(self._reads or [0]),
                             bridge._OC_ATT_MAX_FILE_BYTES + 1)

    def test_data_uri_over_cap_never_touches_the_disk(self):
        """data URI 用 base64 長度估算就能擋 —— 連落盤都不做。"""
        before = set(os.listdir(bridge.UPLOAD_DIR)) if bridge.UPLOAD_DIR.exists() else set()
        big = "data:image/png;base64," + "A" * (bridge._OC_ATT_MAX_IMAGE_BYTES * 2)
        with self.assertRaises(HTTPException) as cm:
            _run(bridge._oc_input_core(
                "agent:main:pocket", "x",
                {"attachments": [{"filename": "big.png", "mime": "image/png",
                                  "data": big}]}))
        self.assertEqual(cm.exception.status_code, 413)
        self.assertIn("未落盤", str(cm.exception.detail))
        after = set(os.listdir(bridge.UPLOAD_DIR)) if bridge.UPLOAD_DIR.exists() else set()
        self.assertEqual(after - before, set())

    def test_total_budget_stops_before_reading_every_file(self):
        """12 件各 19MB:總額度 20MB → 第 2 件就該停，不是全讀完再算。"""
        chunk = 2 * 1024 * 1024
        paths = [_upload(f"oc-bulk-{i}.bin", b"\x00" * chunk) for i in range(12)]
        with self.assertRaises(HTTPException) as cm:
            _run(bridge._oc_input_core(
                "agent:main:pocket", "x",
                {"attachments": [{"filename": f"b{i}.bin", "path": p}
                                 for i, p in enumerate(paths)]}))
        self.assertEqual(cm.exception.status_code, 413)
        # 最多只讀到剛好超過總額度的那一件為止，不是 12 件 × 2MB
        self.assertLessEqual(sum(self._reads),
                             bridge._OC_ATT_MAX_TOTAL_BYTES + chunk + 16)

    def test_filename_is_sanitised_before_it_leaves_the_bridge(self):
        path = _upload("oc-safe.png", _PNG)
        _run(bridge._oc_input_core(
            "agent:main:pocket", "x",
            {"attachments": [{"filename": "../../etc/passwd", "mime": "image/png",
                              "path": path}]}))
        sent = self.fake.calls[0][1]["attachments"][0]
        self.assertEqual(sent["fileName"], "passwd")   # 路徑成分被剝掉
        self.assertNotIn("/", sent["fileName"])
        self.assertNotIn("..", sent["fileName"])


class EventLoopStaysFreeTests(unittest.TestCase):
    """H-2(live bridge 存活):`_oc_events_feed` 是 WS reader 的同步 callback，
    跑在 FastAPI 的事件圈上。本 PR 在裡面新加了 `sqlite3.connect(timeout=30)`
    + INSERT/UPDATE —— 撞到寫鎖就凍結整個服務 30 秒（main 的
    `_oc_events_feed` 完全沒有 DB I/O）。這裡驗證 DB 工作被搬離事件圈。"""

    def setUp(self):
        self._orig = bridge.OPENCLAW
        self.fake = _FakeOC()
        bridge.OPENCLAW = self.fake
        bridge._OC_CARD_DIGESTS.clear()
        bridge._OC_APPROVAL_METHODS.clear()
        bridge._OC_DB_QUEUE = None
        bridge._OC_DB_WORKER = None
        ApprovalEventTests._wipe()

    def tearDown(self):
        bridge.OPENCLAW = self._orig
        bridge._OC_CARD_DIGESTS.clear()
        bridge._OC_APPROVAL_METHODS.clear()
        bridge._OC_DB_QUEUE = None
        bridge._OC_DB_WORKER = None
        ApprovalEventTests._wipe()

    def test_feed_does_not_block_the_loop_and_still_lands_the_row(self):
        """事件圈被佔住的時間必須遠小於 DB 工作本身。"""
        real_connect = sqlite3.connect

        def _slow_connect(*a, **kw):
            time.sleep(0.35)              # 模擬「等別人的寫鎖」
            return real_connect(*a, **kw)

        async def scenario():
            d = _digest("agent:main:main")
            sqlite3.connect = _slow_connect
            try:
                t0 = time.monotonic()
                bridge._oc_events_feed("exec.approval.requested",
                                       _requested(aid="ap-loop"))
                blocked = time.monotonic() - t0
                # worker 在背景做完
                await bridge._OC_DB_QUEUE.join()
            finally:
                sqlite3.connect = real_connect
            return blocked, d

        blocked, d = _run(scenario())
        self.assertLess(blocked, 0.1,
                        f"事件圈被 DB 卡住 {blocked:.3f}s（應該幾乎是 0）")
        # 而且工作真的有做完:DB 落列 + 卡片出來
        self.assertEqual(bridge._approval_get_row("ap-loop")["status"], "pending")
        self.assertEqual(d.store.snapshot()["cards"][0]["kind"], "approval")

    def test_requested_then_resolved_keep_their_order(self):
        """序列化 worker（而不是各自 create_task）:resolved 不會搶在
        requested 落庫之前跑，否則 UPDATE 撲空、那一列永遠 pending。"""
        async def scenario():
            _digest("agent:main:main")
            bridge._oc_events_feed("exec.approval.requested",
                                   _requested(aid="ap-order"))
            bridge._oc_events_feed("exec.approval.resolved", {
                "id": "ap-order", "decision": "deny",
                "request": {"sessionKey": "agent:main:main"}})
            await bridge._OC_DB_QUEUE.join()
        _run(scenario())
        self.assertEqual(bridge._approval_get_row("ap-order")["status"], "denied")

    def test_sync_fallback_still_works_without_a_loop(self):
        """沒有 running loop（單元測試 / 匯入期）時原地同步跑，行為不變。"""
        _digest("agent:main:main")
        bridge._oc_events_feed("exec.approval.requested", _requested(aid="ap-sync"))
        self.assertEqual(bridge._approval_get_row("ap-sync")["status"], "pending")


class ReroutedSessionKeyTests(unittest.TestCase):
    """M-1:gateway 事件帶的是**原始** sessionKey，bridge 這側的 digest 用的
    是 `_oc_safe_session_key()` 改道後的 key。不改道 → `agent:<a>:<a>` 這種
    撞名 lane 的審批卡靜默消失，使用者只看到「等待審批」卻沒有任何按鈕。"""

    def setUp(self):
        self._orig = bridge.OPENCLAW
        self.fake = _FakeOC()
        bridge.OPENCLAW = self.fake
        bridge._OC_CARD_DIGESTS.clear()
        bridge._OC_APPROVAL_METHODS.clear()
        ApprovalEventTests._wipe()

    def tearDown(self):
        bridge.OPENCLAW = self._orig
        bridge._OC_CARD_DIGESTS.clear()
        bridge._OC_APPROVAL_METHODS.clear()
        ApprovalEventTests._wipe()

    def test_colliding_lane_still_gets_its_approval_card(self):
        # digest 照正式路徑建（= 改道後的 key）
        provider, key = bridge._v2_card_source("openclaw:agent:main:main")
        self.assertEqual((provider, key), ("oc", "agent:main:pocket"))
        d = bridge._OC_CARD_DIGESTS[key] = cd.OpenClawDigest()
        # gateway 事件帶原始 key
        bridge._oc_events_feed("exec.approval.requested",
                               _requested(aid="ap-rr", key="agent:main:main"))
        cards = d.store.snapshot()["cards"]
        self.assertEqual(len(cards), 1, "改道 lane 的審批卡不見了（M-1）")
        self.assertEqual(cards[0]["kind"], "approval")
        self.assertEqual(cards[0]["body"]["approval_id"], "ap-rr")
        # DB 的 session_id 也要是 app 認得的那個（v2 session id 同源）
        self.assertEqual(bridge._approval_get_row("ap-rr")["session_id"],
                         "openclaw:agent:main:pocket")

    def test_colliding_lane_resolution_closes_the_same_card(self):
        key = bridge._oc_safe_session_key("agent:main:main")
        d = bridge._OC_CARD_DIGESTS[key] = cd.OpenClawDigest()
        bridge._oc_events_feed("exec.approval.requested",
                               _requested(aid="ap-rr2", key="agent:main:main"))
        bridge._oc_events_feed("exec.approval.resolved", {
            "id": "ap-rr2", "decision": "deny",
            "request": {"sessionKey": "agent:main:main"}})
        cards = d.store.snapshot()["cards"]
        self.assertEqual(len(cards), 1, "收尾卡落到別的 key 去了")
        self.assertEqual(cards[0]["body"]["resolved"], "denied")

    def test_non_colliding_lane_is_untouched(self):
        d = bridge._OC_CARD_DIGESTS["agent:main:pocket2"] = cd.OpenClawDigest()
        bridge._oc_events_feed("exec.approval.requested",
                               _requested(aid="ap-rr3", key="agent:main:pocket2"))
        self.assertEqual(len(d.store.snapshot()["cards"]), 1)

    def test_chat_events_also_route_through_the_safe_key(self):
        key = bridge._oc_safe_session_key("agent:main:main")
        d = bridge._OC_CARD_DIGESTS[key] = cd.OpenClawDigest()
        bridge._oc_events_feed("chat", {
            "sessionKey": "agent:main:main", "runId": "r-rr", "state": "final",
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "哈囉"}]}})
        self.assertTrue(d.store.snapshot()["cards"])


class DigestStateLeakTests(unittest.TestCase):
    """M(狀態殘留):回合被中斷/失敗時 gateway 不補送 approval.resolved，
    `self.prompt` 沒清就永遠掛著「等待核准」標籤。"""

    def _pending(self):
        d = cd.OpenClawDigest()
        d.handle_approval({"id": "a1", "title": "要跑指令", "options": []})
        self.assertEqual(d.prompt, "要跑指令")
        return d

    def test_chat_aborted_clears_the_pending_label(self):
        d = self._pending()
        d.handle("chat", {"sessionKey": "k", "runId": "r1", "state": "aborted"})
        self.assertIsNone(d.prompt)
        self.assertNotIn("核准", d.store.status.get("label") or "")

    def test_chat_error_clears_the_pending_label(self):
        d = self._pending()
        d.handle("chat", {"sessionKey": "k", "runId": "r1", "state": "error",
                          "errorMessage": "boom"})
        self.assertIsNone(d.prompt)

    def test_lifecycle_error_clears_the_pending_label(self):
        d = self._pending()
        d.handle("agent", {"sessionKey": "k", "runId": "r1",
                           "stream": "lifecycle",
                           "data": {"phase": "error", "error": "boom"}})
        self.assertIsNone(d.prompt)


class NonMediaBlocksAreNotAttachmentsTests(unittest.TestCase):
    """「非 text 就是附件」的黑名單會把 thinking / tool_use 變成假附件，
    卡片上冒出「📎 thinking」。改成媒體白名單。"""

    def test_thinking_and_tool_blocks_are_ignored(self):
        atts = cd._oc_msg_nontext([
            {"type": "text", "text": "hi"},
            {"type": "thinking", "thinking": "嗯…"},
            {"type": "redacted_thinking", "data": "xx"},
            {"type": "tool_use", "name": "bash", "input": {"cmd": "ls"}},
            {"type": "tool_result", "content": "ok"},
        ])
        self.assertEqual(atts, [])

    def test_real_media_blocks_still_come_through(self):
        atts = cd._oc_msg_nontext([
            {"type": "image", "source": {"media_type": "image/png",
                                         "omitted": True, "bytes": 1234}},
            {"type": "document", "fileName": "a.pdf"},
        ])
        self.assertEqual([a["kind"] for a in atts], ["image", "document"])
        self.assertEqual(atts[0]["mime"], "image/png")
        self.assertTrue(atts[0]["omitted"])

    def test_unknown_block_with_media_hints_is_kept(self):
        atts = cd._oc_msg_nontext([
            {"type": "future_media", "mimeType": "audio/ogg"},
            {"type": "future_noise", "score": 1},
        ])
        self.assertEqual([a["kind"] for a in atts], ["future_media"])

    def test_message_card_is_not_created_for_thinking_only_content(self):
        d = cd.OpenClawDigest()
        d.message_card({"role": "assistant", "__openclaw": {"id": "m1"},
                        "content": [{"type": "thinking", "thinking": "…"}]})
        self.assertEqual(d.store.snapshot()["cards"], [])


if __name__ == "__main__":
    unittest.main()
