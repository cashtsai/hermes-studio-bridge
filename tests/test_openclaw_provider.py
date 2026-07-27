"""S4 OpenClaw provider 測試(mock 層)。

三塊:
1. carddigest.OpenClawDigest — seed/delta/final/error/lifecycle/tool 不變量。
2. openclaw_provider 純函式 — 設定載入優先序、ws_url 歸一、v2 row 對映。
3. bridge 接線 — 未配置全靜默缺席(v2 sessions/agents/dashboard/card_source),
   配置後(假 client 注入)v2 sessions 列 openclaw、input/interrupt 路由、
   config 端點,推播 heartbeat 過濾。

真靶機層(需本機 OpenClaw gateway)在 test_openclaw_live.py,預設 skip。
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="oc-test-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["OPENCLAW_CONFIG_FILE"] = os.path.join(_TMP, "openclaw.json")
os.environ.pop("OPENCLAW_BASE_URL", None)
os.environ.pop("OPENCLAW_TOKEN", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carddigest as cd  # noqa: E402
import openclaw_provider as ocp  # noqa: E402
import bridge  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _FakeReq:
    def __init__(self, token=None):
        tok = token if token is not None else os.environ["BRIDGE_TOKEN"]
        self.headers = {"authorization": f"Bearer {tok}"}
        self.client = type("C", (), {"host": "127.0.0.1"})()
        self._body = b"{}"

    class _URL:
        path = "/test"
    url = _URL()

    def set_json(self, obj):
        self._body = json.dumps(obj).encode()
        return self

    async def json(self):
        return json.loads(self._body)

    async def body(self):
        return self._body


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ───────────────────────── 1. OpenClawDigest ────────────────────────────────

class DigestTests(unittest.TestCase):
    def test_seed_skips_internal_and_dedupes(self):
        d = cd.OpenClawDigest()
        msgs = [
            {"role": "system", "content": [{"type": "text", "text": "Compaction"}],
             "__openclaw": {"kind": "compaction", "id": "c1"}},
            {"role": "user", "content": "哈囉", "timestamp": 1785119281390,
             "__openclaw": {"id": "u1"}},
            {"role": "assistant", "content": [{"type": "text", "text": "你好"}],
             "timestamp": 1785119324877, "__openclaw": {"id": "a1"}},
        ]
        d.seed_messages(msgs)
        d.seed_messages(msgs)   # 重放 → known_mids 去重,不得雙份
        snap = d.store.snapshot()
        self.assertEqual(len(snap["cards"]), 2)
        self.assertEqual(snap["cards"][0]["role"], "user")
        self.assertEqual(snap["cards"][0]["kind"], "text")
        self.assertEqual(snap["cards"][1]["kind"], "markdown")
        # 毫秒 timestamp → 秒
        self.assertLess(snap["cards"][0]["ts"], 1e11)

    def test_chat_delta_final_same_card(self):
        d = cd.OpenClawDigest()
        d.handle("chat", {"runId": "r1", "state": "delta", "deltaText": "PRO",
                          "message": {"role": "assistant",
                                      "content": [{"type": "text", "text": "PRO"}]}})
        d.handle("chat", {"runId": "r1", "state": "delta", "deltaText": "BE",
                          "message": {"role": "assistant",
                                      "content": [{"type": "text", "text": "PROBE"}]}})
        d.handle("chat", {"runId": "r1", "state": "final",
                          "message": {"role": "assistant",
                                      "content": [{"type": "text", "text": "PROBE_OK"}],
                                      "timestamp": 1785119370729}})
        snap = d.store.snapshot()
        self.assertEqual(len(snap["cards"]), 1)
        card = snap["cards"][0]
        self.assertEqual(card["body"]["text"], "PROBE_OK")
        self.assertTrue(card["final"])
        self.assertGreaterEqual(card["rev"], 3)

    def test_chat_delta_without_message_accumulates(self):
        d = cd.OpenClawDigest()
        d.handle("chat", {"runId": "r2", "state": "delta", "deltaText": "he"})
        d.handle("chat", {"runId": "r2", "state": "delta", "deltaText": "llo"})
        d.handle("chat", {"runId": "r2", "state": "final", "message": {}})
        card = d.store.snapshot()["cards"][0]
        self.assertEqual(card["body"]["text"], "hello")
        self.assertTrue(card["final"])

    def test_error_and_aborted_cards(self):
        d = cd.OpenClawDigest()
        d.handle("chat", {"runId": "r3", "state": "error",
                          "errorMessage": "model not found"})
        d.handle("chat", {"runId": "r4", "state": "aborted"})
        kinds = [(c["kind"], c["body"]["text"]) for c in d.store.snapshot()["cards"]]
        self.assertEqual(len(kinds), 2)
        self.assertIn("model not found", kinds[0][1])
        self.assertIn("已中斷", kinds[1][1])

    def test_lifecycle_turn_and_status(self):
        d = cd.OpenClawDigest()
        d.handle("agent", {"runId": "r5", "stream": "lifecycle",
                           "data": {"phase": "start"}})
        self.assertTrue(d.busy)
        self.assertEqual(d.store.status.get("phase"), "run")
        d.handle("agent", {"runId": "r5", "stream": "lifecycle",
                           "data": {"phase": "end", "stopReason": "stop"}})
        self.assertFalse(d.busy)
        self.assertEqual(d.store.status.get("phase"), "idle")
        turn_events = [e for e in d.store.events if e["type"] == "turn"]
        self.assertEqual([e["data"]["state"] for e in turn_events], ["begin", "end"])

    def test_lifecycle_error_emits_card(self):
        d = cd.OpenClawDigest()
        d.handle("agent", {"runId": "r6", "stream": "lifecycle",
                           "data": {"phase": "start"}})
        d.handle("agent", {"runId": "r6", "stream": "lifecycle",
                           "data": {"phase": "error", "error": "boom"}})
        cards = d.store.snapshot()["cards"]
        self.assertTrue(any("boom" in c["body"]["text"] for c in cards))
        self.assertFalse(d.busy)

    def test_stale_run_end_does_not_clear_busy(self):
        """實測坑:舊 run 被 abort 後,其 lifecycle end 會在新 run start 之後
        才到 — 不能把新 run 的 busy 誤清(interrupt 會因此 409)。"""
        d = cd.OpenClawDigest()
        d.handle("agent", {"runId": "old", "stream": "lifecycle",
                           "data": {"phase": "start"}})
        d.handle("agent", {"runId": "new", "stream": "lifecycle",
                           "data": {"phase": "start"}})
        d.handle("agent", {"runId": "old", "stream": "lifecycle",
                           "data": {"phase": "end", "stopReason": "stop"}})
        self.assertTrue(d.busy)          # 舊 run 的 end 不得誤清
        self.assertEqual(d.active_run, "new")
        # chat delta 也要能自癒 busy(lifecycle start 漏接時)
        d2 = cd.OpenClawDigest()
        d2.handle("chat", {"runId": "r", "state": "delta", "deltaText": "x"})
        self.assertTrue(d2.busy)
        d.handle("agent", {"runId": "new", "stream": "lifecycle",
                           "data": {"phase": "end", "stopReason": "stop"}})
        self.assertFalse(d.busy)

    def test_tool_stream_card(self):
        d = cd.OpenClawDigest()
        d.handle("agent", {"runId": "r7", "stream": "tool",
                           "data": {"name": "exec", "args": {"command": "ls -la"},
                                    "toolCallId": "t1"}})
        card = d.store.snapshot()["cards"][0]
        self.assertEqual(card["kind"], "tool_call")
        self.assertEqual(card["body"]["tool"], "exec")
        self.assertEqual(card["body"]["summary"], "ls -la")
        self.assertTrue(card["body"]["fallback_text"].startswith("› 🔧"))

    def test_assistant_stream_not_double_carded(self):
        d = cd.OpenClawDigest()
        d.handle("agent", {"runId": "r8", "stream": "assistant",
                           "data": {"text": "hi", "delta": "hi"}})
        self.assertEqual(len(d.store.snapshot()["cards"]), 0)

    def test_user_card_idempotent(self):
        d = cd.OpenClawDigest()
        d.user_card("測試", "idem-1")
        d.user_card("測試", "idem-1")
        cards = d.store.snapshot()["cards"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["role"], "user")


# ───────────────────────── 2. openclaw_provider 純函式 ──────────────────────

class ProviderHelperTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("OPENCLAW_BASE_URL", None)
        os.environ.pop("OPENCLAW_TOKEN", None)
        try:
            os.unlink(os.environ["OPENCLAW_CONFIG_FILE"])
        except FileNotFoundError:
            pass

    def test_ws_url(self):
        self.assertEqual(ocp.ws_url("http://h:1/"), "ws://h:1")
        self.assertEqual(ocp.ws_url("https://h"), "wss://h")
        self.assertEqual(ocp.ws_url("ws://h:19801"), "ws://h:19801")
        self.assertEqual(ocp.ws_url("h:19801"), "ws://h:19801")

    def test_config_priority_env_over_file(self):
        # 注意:openclaw_provider 讀模組常數 _CONFIG_FILE(import 時已定),
        # 測試檔開頭已把 OPENCLAW_CONFIG_FILE 指到 tmp。
        ocp.save_config("ws://file-host:1", "file-token")
        self.assertEqual(ocp.load_config()["source"], "file")
        os.environ["OPENCLAW_BASE_URL"] = "ws://env-host:2"
        cfg = ocp.load_config()
        self.assertEqual(cfg["source"], "env")
        self.assertEqual(cfg["base_url"], "ws://env-host:2")

    def test_unconfigured(self):
        cfg = ocp.load_config()
        self.assertEqual(cfg["source"], "none")
        self.assertEqual(cfg["base_url"], "")

    def test_session_row_mapping(self):
        row = {"key": "agent:main:main", "model": "qwen3:4b",
               "updatedAt": 1785119370803, "hasActiveRun": False}
        v2 = ocp.session_v2_row(row)
        self.assertEqual(v2["id"], "openclaw:agent:main:main")
        self.assertEqual(v2["provider"], "openclaw")
        self.assertEqual(v2["title"], "main")
        self.assertEqual(v2["status"], "idle")
        self.assertEqual(v2["capabilities"],
                         ["input", "interrupt", "replay", "follow"])
        self.assertLess(v2["last_event_at"], 1e11)
        row["hasActiveRun"] = True
        self.assertEqual(ocp.session_v2_row(row)["status"], "running")

    def test_short_name(self):
        self.assertEqual(ocp.session_short_name("agent:main:main"), "main")
        self.assertEqual(ocp.session_short_name("agent:ops:deploy"), "ops/deploy")
        self.assertEqual(ocp.session_short_name("main"), "main")


# ───────────────────────── 3. bridge 接線 ───────────────────────────────────

class _FakeOC:
    """注入 bridge.OPENCLAW 的假 client。"""

    def __init__(self, configured=True):
        self._configured = configured
        self.calls = []
        self.responses = {}
        self.raises = None

    def configured(self):
        return self._configured

    async def call(self, method, params=None, timeout=30.0):
        self.calls.append((method, params or {}))
        if self.raises:
            raise self.raises
        return self.responses.get(method, {})


class BridgeWiringTests(unittest.TestCase):
    def setUp(self):
        self._orig = bridge.OPENCLAW
        bridge._OC_CARD_DIGESTS.clear()
        bridge._OC_HEARTBEAT_RUNS.clear()

    def tearDown(self):
        bridge.OPENCLAW = self._orig
        bridge._OC_CARD_DIGESTS.clear()
        bridge._OC_HEARTBEAT_RUNS.clear()

    # 未配置:全部靜默缺席

    def test_unconfigured_card_source_404(self):
        bridge.OPENCLAW = _FakeOC(configured=False)
        with self.assertRaises(HTTPException) as cm:
            bridge._v2_card_source("openclaw:agent:main:main")
        self.assertEqual(cm.exception.status_code, 404)

    def test_unconfigured_agents_absent(self):
        bridge.OPENCLAW = _FakeOC(configured=False)
        payload = _run(bridge.v2_agents(_FakeReq()))
        provs = [a["provider"] for a in payload["agents"]]
        self.assertNotIn("openclaw", provs)

    def test_configured_agents_present(self):
        bridge.OPENCLAW = _FakeOC(configured=True)
        payload = _run(bridge.v2_agents(_FakeReq()))
        provs = [a["provider"] for a in payload["agents"]]
        self.assertIn("openclaw", provs)

    # card source 路由:sessionKey 含冒號要整段保留

    def test_card_source_keeps_full_key(self):
        bridge.OPENCLAW = _FakeOC(configured=True)
        src = bridge._v2_card_source("openclaw:agent:main:main")
        self.assertEqual(src, ("oc", "agent:main:main"))

    # input 路由

    def test_input_sends_chat_send_with_idempotency(self):
        fake = _FakeOC(configured=True)
        fake.responses["chat.send"] = {"runId": "run-1", "status": "started"}
        bridge.OPENCLAW = fake
        res = _run(bridge._oc_input_core(
            "agent:main:main", "openclaw:agent:main:main",
            {"content": "哈囉", "client_id": "c-123"}))
        self.assertTrue(res["accepted"])
        self.assertEqual(res["run_id"], "run-1")
        method, params = fake.calls[0]
        self.assertEqual(method, "chat.send")
        self.assertEqual(params["sessionKey"], "agent:main:main")
        self.assertEqual(params["idempotencyKey"], "pocket-c-123")

    def test_input_empty_400(self):
        bridge.OPENCLAW = _FakeOC(configured=True)
        with self.assertRaises(HTTPException) as cm:
            _run(bridge._oc_input_core("agent:main:main", "x", {"content": ""}))
        self.assertEqual(cm.exception.status_code, 400)

    def test_input_attachments_only_400(self):
        bridge.OPENCLAW = _FakeOC(configured=True)
        with self.assertRaises(HTTPException) as cm:
            _run(bridge._oc_input_core("agent:main:main", "x",
                                       {"attachments": [{"kind": "image"}]}))
        self.assertEqual(cm.exception.status_code, 400)

    def test_input_echoes_user_card_to_subscribed_digest(self):
        fake = _FakeOC(configured=True)
        fake.responses["chat.send"] = {"runId": "run-2"}
        bridge.OPENCLAW = fake
        d = bridge._OC_CARD_DIGESTS["agent:main:main"] = cd.OpenClawDigest()
        _run(bridge._oc_input_core("agent:main:main", "x",
                                   {"content": "hi", "client_id": "c1"}))
        cards = d.store.snapshot()["cards"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["role"], "user")

    # v2 sessions 清單 + degraded

    def _v2_light_stubs(self):
        async def _no_deleg():
            return []
        async def _no_cc():
            return []
        return _no_deleg, _no_cc

    def test_v2_sessions_lists_openclaw(self):
        fake = _FakeOC(configured=True)
        fake.responses["sessions.list"] = {"sessions": [
            {"key": "agent:main:main", "model": "qwen3:4b",
             "updatedAt": 1785119370803, "hasActiveRun": True}]}
        bridge.OPENCLAW = fake
        rows = _run(self._collect_v2_sessions())
        oc = [s for s in rows["sessions"] if s["provider"] == "openclaw"]
        self.assertEqual(len(oc), 1)
        self.assertEqual(oc[0]["id"], "openclaw:agent:main:main")
        self.assertEqual(oc[0]["status"], "running")
        self.assertNotIn("openclaw", rows["degraded_providers"])

    def test_v2_sessions_degraded_on_failure(self):
        fake = _FakeOC(configured=True)
        fake.raises = ocp.OpenClawError("boom", code="CONNECT_FAILED")
        bridge.OPENCLAW = fake
        rows = _run(self._collect_v2_sessions())
        self.assertIn("openclaw", rows["degraded_providers"])

    def test_v2_sessions_unconfigured_absent(self):
        bridge.OPENCLAW = _FakeOC(configured=False)
        rows = _run(self._collect_v2_sessions())
        provs = {s["provider"] for s in rows["sessions"]}
        self.assertNotIn("openclaw", provs)

    async def _collect_v2_sessions(self):
        # 把 cc/persona/codex 分支換成便宜假件,只驗 openclaw 段。
        orig = {}
        async def _empty_deleg():
            return []
        def _no_cc_rows():
            return []
        class _DeadCodex:
            async def call(self, *a, **k):
                raise RuntimeError("codex off in test")
        orig["deleg"] = bridge._delegation_v2_sessions
        orig["cc"] = bridge._cc_conf_rows
        orig["cx"] = bridge.CODEX_APP
        orig["hp"] = bridge._hermes_pending_by_session
        orig["personas"] = bridge.PERSONAS
        bridge._delegation_v2_sessions = _empty_deleg
        bridge._cc_conf_rows = _no_cc_rows
        bridge.CODEX_APP = _DeadCodex()
        bridge._hermes_pending_by_session = lambda: {}
        bridge.PERSONAS = {}
        try:
            return await bridge.v2_sessions(_FakeReq())
        finally:
            bridge._delegation_v2_sessions = orig["deleg"]
            bridge._cc_conf_rows = orig["cc"]
            bridge.CODEX_APP = orig["cx"]
            bridge._hermes_pending_by_session = orig["hp"]
            bridge.PERSONAS = orig["personas"]

    # interrupt 路由

    def test_interrupt_409_when_idle(self):
        bridge.OPENCLAW = _FakeOC(configured=True)
        d = bridge._OC_CARD_DIGESTS["agent:main:main"] = cd.OpenClawDigest()
        d.busy = False
        with self.assertRaises(HTTPException) as cm:
            _run(bridge.v2_session_interrupt("openclaw:agent:main:main",
                                             _FakeReq()))
        self.assertEqual(cm.exception.status_code, 409)

    def test_interrupt_calls_abort_when_busy(self):
        fake = _FakeOC(configured=True)
        bridge.OPENCLAW = fake
        d = bridge._OC_CARD_DIGESTS["agent:main:main"] = cd.OpenClawDigest()
        d.busy = True
        res = _run(bridge.v2_session_interrupt("openclaw:agent:main:main",
                                               _FakeReq()))
        self.assertTrue(res["interrupted"])
        self.assertEqual(fake.calls[0][0], "chat.abort")
        self.assertEqual(fake.calls[0][1], {"sessionKey": "agent:main:main"})

    # approve 明示不支援

    def test_approve_unsupported(self):
        bridge.OPENCLAW = _FakeOC(configured=True)
        req = _FakeReq().set_json({"approve": True})
        with self.assertRaises(HTTPException) as cm:
            _run(bridge.v2_session_approve("openclaw:agent:main:main", req))
        self.assertEqual(cm.exception.status_code, 400)

    # 事件 feed:heartbeat 推播過濾 + digest 分流

    def test_events_feed_routes_and_heartbeat_filter(self):
        bridge.OPENCLAW = _FakeOC(configured=True)
        d = bridge._OC_CARD_DIGESTS["agent:main:main"] = cd.OpenClawDigest()
        pushed = []
        orig_push = bridge._oc_push_final
        bridge._oc_push_final = lambda key, payload: pushed.append(
            (key, payload.get("runId")))
        try:
            bridge._oc_events_feed("agent", {
                "sessionKey": "agent:main:main", "runId": "hb-1",
                "isHeartbeat": True, "stream": "lifecycle",
                "data": {"phase": "start"}})
            self.assertIn("hb-1", bridge._OC_HEARTBEAT_RUNS)
            bridge._oc_events_feed("chat", {
                "sessionKey": "agent:main:main", "runId": "r-9",
                "state": "final",
                "message": {"role": "assistant",
                            "content": [{"type": "text", "text": "done"}]}})
            self.assertEqual(pushed, [("agent:main:main", "r-9")])
            self.assertTrue(d.store.snapshot()["cards"])
        finally:
            bridge._oc_push_final = orig_push

    def test_push_final_skips_heartbeat_runs(self):
        bridge._OC_HEARTBEAT_RUNS["hb-2"] = True
        sent = []
        orig = bridge.push_notify
        async def _fake_push(*a, **k):
            sent.append(a)
            return {}
        bridge.push_notify = _fake_push
        try:
            async def _t():
                bridge._oc_push_final("agent:main:main", {
                    "runId": "hb-2", "state": "final",
                    "message": {"content": [{"type": "text", "text": "hb"}]}})
                await asyncio.sleep(0)
            _run(_t())
            self.assertEqual(sent, [])
        finally:
            bridge.push_notify = orig

    # config 端點

    def test_config_endpoints_roundtrip(self):
        bridge.OPENCLAW = self._orig   # 真 client(但不連線)
        req = _FakeReq().set_json({"base_url": "ws://127.0.0.1:19801",
                                   "token": "sekrit"})
        async def _t():
            put = await bridge.openclaw_config_put(req)
            got = await bridge.openclaw_config_get(_FakeReq())
            return put, got
        put, got = _run(_t())
        try:
            self.assertTrue(put["configured"])
            self.assertEqual(got["base_url"], "ws://127.0.0.1:19801")
            self.assertTrue(got["token_set"])
            self.assertNotIn("token", got)   # 明文 token 永不回傳
            # 清除配置 → 回到缺席
            req2 = _FakeReq().set_json({"base_url": "", "token": ""})
            cleared = _run(bridge.openclaw_config_put(req2))
            self.assertFalse(cleared["configured"])
        finally:
            try:
                os.unlink(os.environ["OPENCLAW_CONFIG_FILE"])
            except FileNotFoundError:
                pass

    # dashboard 計數

    def test_dashboard_openclaw_counts_and_absence(self):
        async def _dash(fake):
            bridge.OPENCLAW = fake
            orig_cc = bridge._cc_sessions
            orig_hp = bridge._hermes_pending_by_session
            orig_p = bridge.PERSONAS
            orig_cx = bridge.CODEX_APP
            class _DeadCodex:
                async def call(self, *a, **k):
                    raise RuntimeError("off")
            async def _no_cc():
                return []
            bridge._cc_sessions = _no_cc
            bridge._hermes_pending_by_session = lambda: {}
            bridge.PERSONAS = {}
            bridge.CODEX_APP = _DeadCodex()
            try:
                return await bridge._dashboard_sessions()
            finally:
                bridge._cc_sessions = orig_cc
                bridge._hermes_pending_by_session = orig_hp
                bridge.PERSONAS = orig_p
                bridge.CODEX_APP = orig_cx
        fake = _FakeOC(configured=True)
        fake.responses["sessions.list"] = {"sessions": [
            {"key": "agent:main:main", "hasActiveRun": True},
            {"key": "agent:main:dev", "hasActiveRun": False}]}
        out = _run(_dash(fake))
        self.assertEqual(out["openclaw"], {"working": 1, "idle": 1})
        out2 = _run(_dash(_FakeOC(configured=False)))
        self.assertNotIn("openclaw", out2)   # 未配置 → 鍵缺席
        fake3 = _FakeOC(configured=True)
        fake3.raises = ocp.OpenClawError("down", code="CONNECT_FAILED")
        out3 = _run(_dash(fake3))
        self.assertNotIn("openclaw", out3)
        self.assertIn("openclaw", out3["degraded"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
