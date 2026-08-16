"""S4 OpenClaw provider 測試(mock 層)。

三塊:
1. carddigest.OpenClawDigest — seed/delta/final/error/lifecycle/tool 不變量。
2. openclaw_provider 純函式 — 設定載入優先序、ws_url 歸一、v2 row 對映。
3. bridge 接線 — 未配置全靜默缺席(v2 sessions/agents/dashboard/card_source),
   配置後(假 client 注入)v2 sessions 列 openclaw、input/interrupt 路由、
   config 端點,推播 heartbeat 過濾。

真靶機層(需本機 OpenClaw gateway)在 test_openclaw_live.py,預設 skip。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
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

# 上面那些 os.environ 只在「本檔是第一個 import 這些模組的人」時算數:
# `_CONFIG_FILE` / `BRIDGE_TOKEN` 都是模組層常數,import 當下就定死了。全套
# `unittest discover` 一起跑時 bridge / openclaw_provider 早被別的測試檔
# import 過,於是本檔設的 env 變成 no-op —— 測試讀到的是真實機器上的
# `~/.pocket/openclaw.json`(這台有配置 → source 是 "file" 不是 "none"),
# 而且 `save_config()` 會把真的設定檔覆蓋掉。
#
# 正式行為不動,只在測試側把模組常數綁回本檔的 tmp:順序無關,也不再碰
# 使用者家目錄。
ocp._CONFIG_FILE = os.environ["OPENCLAW_CONFIG_FILE"]


class _FakeReq:
    def __init__(self, token=None):
        # 讀 `bridge.BRIDGE_TOKEN` 而不是 `os.environ["BRIDGE_TOKEN"]`:
        # 兩者只有在「本檔是第一個 import bridge 的人」時才相等 —— bridge 的
        # `BRIDGE_TOKEN` 是模組層常數(import 當下由 env 定死),而本檔開頭是
        # `setdefault`,別人先 import 過就變 no-op,於是 env 與 bridge 實際在
        # 比對的值發散,本模組多支測試會誤紅。
        #
        # 這裡曾有一段註解說「不能改讀 bridge.BRIDGE_TOKEN,否則會改變全域
        # `_AUTH_FAILS` 的累積量,讓 test_robustness_pack 的
        # test_4xx_flood_is_throttled 翻紅」。那個顧慮在 per-client 分桶
        # (`fix/auth-throttle-per-client`:全域 `_AUTH_FAILS` deque →
        # `_AUTH_FAILS_BY_CLIENT` + `_auth_fail_bump_locked`)之後已經不成立:
        # 節流桶以 client host 為 key,本檔的 `_FakeReq` 是 "127.0.0.1"、
        # test_robustness_pack 用的 TestClient 是 "testclient",兩者落在不同
        # 桶裡,本檔的認證失敗次數再也影響不到那支測試。已實測確認
        # (兩檔同行程跑,test_4xx_flood_is_throttled 綠)。
        #
        # 這也是 main 現行的統一寫法:test_codex_server_requests.py、
        # test_cx_thread_lock.py、test_dashboard_v1.py 等都已改讀
        # bridge.BRIDGE_TOKEN,理由與此處相同。
        tok = token if token is not None else bridge.BRIDGE_TOKEN
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

    def test_late_delta_after_run_end_does_not_resurrect_busy(self):
        """實測坑(卡死在「回覆中」):同一 run 收尾後 gateway 仍會遲送 delta,
        原本 delta 的 busy 自癒沒有 run 對位 → 把待命拉回回覆中,直到下一
        回合才解。實測 seq:turn end → idle → final 卡 → 遲送 delta → 卡死。"""
        d = cd.OpenClawDigest()
        d.handle("agent", {"runId": "r1", "stream": "lifecycle",
                           "data": {"phase": "start"}})
        d.handle("chat", {"runId": "r1", "state": "delta", "deltaText": "1"})
        d.handle("agent", {"runId": "r1", "stream": "lifecycle",
                           "data": {"phase": "end", "stopReason": "stop"}})
        self.assertFalse(d.busy)
        d.handle("chat", {"runId": "r1", "state": "final",
                          "message": {"role": "assistant",
                                      "content": [{"type": "text", "text": "1\n2"}]}})
        # ↓ 收尾後的遲送 delta
        d.handle("chat", {"runId": "r1", "state": "delta", "deltaText": "2",
                          "message": {"role": "assistant",
                                      "content": [{"type": "text", "text": "1\n2"}]}})
        self.assertFalse(d.busy)
        self.assertEqual(d.active_run, "")
        self.assertEqual(d.store.status.get("phase"), "idle")
        self.assertEqual(d.store.status.get("label"), "待命")

    def test_late_delta_after_abort_does_not_resurrect_busy(self):
        """中斷後 gateway 會把殘留 delta 補送 —— 同樣不能把 busy 拉回來,
        否則使用者按了中斷卻永遠停在「回覆中」。"""
        d = cd.OpenClawDigest()
        d.handle("chat", {"runId": "rf", "state": "delta", "deltaText": "ha"})
        self.assertTrue(d.busy)
        d.handle("chat", {"runId": "rf", "state": "aborted"})
        self.assertFalse(d.busy)
        d.handle("chat", {"runId": "rf", "state": "delta", "deltaText": "i"})
        self.assertFalse(d.busy)
        # 帶 message 的 final 同樣算收尾(SPEC §3 的完成語意)
        self.assertIn("rf", d.ended_runs)
        d2 = cd.OpenClawDigest()
        d2.handle("chat", {"runId": "rg", "state": "final",
                           "message": {"role": "assistant",
                                       "content": [{"type": "text", "text": "hi"}]}})
        self.assertIn("rg", d2.ended_runs)

    def test_bare_final_does_not_end_run(self):
        """裸 final(無 message)只是 ack(SPEC §3),不是收尾 —— 之後的 delta
        仍要能自癒 busy,不可過度修正成永遠不顯示「回覆中」。"""
        d = cd.OpenClawDigest()
        d.handle("chat", {"runId": "rb", "state": "final"})
        d.handle("chat", {"runId": "rb", "state": "delta", "deltaText": "yo"})
        self.assertTrue(d.busy)
        self.assertEqual(d.active_run, "rb")

    def test_new_run_first_delta_still_sets_busy(self):
        """對位守衛只擋已收尾的那個 run:新 run 的第一則 delta 照樣要顯示忙碌
        (lifecycle start 漏接時的自癒路徑不能被鎖死)。"""
        d = cd.OpenClawDigest()
        d.handle("agent", {"runId": "r1", "stream": "lifecycle",
                           "data": {"phase": "start"}})
        d.handle("agent", {"runId": "r1", "stream": "lifecycle",
                           "data": {"phase": "end", "stopReason": "stop"}})
        self.assertFalse(d.busy)
        d.handle("chat", {"runId": "r2", "state": "delta", "deltaText": "新回合"})
        self.assertTrue(d.busy)
        self.assertEqual(d.active_run, "r2")
        self.assertEqual(d.store.status.get("phase"), "run")
        # runId 缺失(無法對位)時也要保留自癒
        d2 = cd.OpenClawDigest()
        d2.handle("chat", {"state": "delta", "deltaText": "x"})
        self.assertTrue(d2.busy)

    def test_aborted_with_partial_text_shows_interrupted(self):
        """實測坑:abort payload 挾帶半截正文,錯誤卡原本會顯示「⚠️ 1\\n2」
        (那份文字主卡已經有了)→ 讀起來像假錯誤。中斷一律講「已中斷」。"""
        d = cd.OpenClawDigest()
        d.handle("chat", {"runId": "ra", "state": "delta", "deltaText": "1\n2"})
        d.handle("chat", {"runId": "ra", "state": "aborted",
                          "message": {"role": "assistant",
                                      "content": [{"type": "text", "text": "1\n2"}]}})
        err = [c for c in d.store.snapshot()["cards"] if c["role"] == "system"]
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0]["body"]["text"], "⚠️ 已中斷")
        self.assertNotIn("1\n2", err[0]["body"]["text"])
        self.assertFalse(d.busy)

    def test_aborted_with_real_error_message_kept(self):
        d = cd.OpenClawDigest()
        d.handle("chat", {"runId": "ra2", "state": "aborted",
                          "errorMessage": "aborted by operator",
                          "message": {"role": "assistant",
                                      "content": [{"type": "text", "text": "半截"}]}})
        err = [c for c in d.store.snapshot()["cards"] if c["role"] == "system"][0]
        self.assertIn("aborted by operator", err["body"]["text"])

    def test_error_path_unchanged(self):
        """真錯誤不受影響:errorMessage 優先,缺了才用 message 正文,再缺才預設。"""
        d = cd.OpenClawDigest()
        d.handle("chat", {"runId": "e1", "state": "error",
                          "errorMessage": "model not found",
                          "message": {"role": "assistant",
                                      "content": [{"type": "text", "text": "⚠️ 內文"}]}})
        d.handle("chat", {"runId": "e2", "state": "error",
                          "message": {"role": "assistant",
                                      "content": [{"type": "text",
                                                   "text": "ollama 連不上"}]}})
        d.handle("chat", {"runId": "e3", "state": "error"})
        texts = [c["body"]["text"] for c in d.store.snapshot()["cards"]
                 if c["role"] == "system"]
        self.assertEqual(texts, ["⚠️ model not found", "⚠️ ollama 連不上",
                                 "⚠️ OpenClaw 回合失敗"])

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
    def setUp(self):
        # 進來先把「未配置」這個前提自己建立好,不靠別人的 tearDown:
        # 同檔的 `BridgeWiringTests.test_config_endpoints_roundtrip` 會透過
        # PUT /openclaw/config 落一份設定檔,而 unittest 是照類別名排序跑的
        # (BridgeWiring… 在 ProviderHelper… 前面),於是 `test_unconfigured`
        # 會讀到別人留下的檔案 → source "file" 而不是 "none"。
        self._reset_openclaw_config()

    def tearDown(self):
        self._reset_openclaw_config()

    @staticmethod
    def _reset_openclaw_config():
        os.environ.pop("OPENCLAW_BASE_URL", None)
        os.environ.pop("OPENCLAW_TOKEN", None)
        try:
            os.unlink(ocp._CONFIG_FILE)
        except FileNotFoundError:
            pass

    def test_ws_url(self):
        self.assertEqual(ocp.ws_url("http://h:1/"), "ws://h:1")
        self.assertEqual(ocp.ws_url("https://h"), "wss://h")
        self.assertEqual(ocp.ws_url("ws://h:19801"), "ws://h:19801")
        self.assertEqual(ocp.ws_url("h:19801"), "ws://h:19801")

    def test_config_priority_env_over_file(self):
        # 注意:openclaw_provider 讀模組常數 _CONFIG_FILE(import 時已定),
        # 本檔在 import 後已把它綁回 tmp(見檔頭),所以這裡寫檔不會碰到
        # 真的 ~/.pocket/openclaw.json。
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
                         ["input", "interrupt", "attachments", "replay",
                          "follow", "approve"])
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
        # openclaw key 本身含冒號,partition 後整段保留(不截斷)。用非撞名的
        # sub-key 驗這件事;撞 default lane 的改道另見 MainLaneCollisionTests。
        bridge.OPENCLAW = _FakeOC(configured=True)
        src = bridge._v2_card_source("openclaw:agent:main:dev")
        self.assertEqual(src, ("oc", "agent:main:dev"))

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

    def test_input_unreadable_attachment_400(self):
        """取不到本機檔案的附件 → 明確報錯(不再靜默丟棄後送純文字)。"""
        bridge.OPENCLAW = _FakeOC(configured=True)
        with self.assertRaises(HTTPException) as cm:
            _run(bridge._oc_input_core("agent:main:main", "x",
                                       {"attachments": [{"kind": "image"}]}))
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(cm.exception.code, "ATTACHMENT_UNREADABLE")

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
        d = bridge._OC_CARD_DIGESTS["agent:main:dev"] = cd.OpenClawDigest()
        d.busy = True
        res = _run(bridge.v2_session_interrupt("openclaw:agent:main:dev",
                                               _FakeReq()))
        self.assertTrue(res["interrupted"])
        self.assertEqual(fake.calls[0][0], "chat.abort")
        self.assertEqual(fake.calls[0][1], {"sessionKey": "agent:main:dev"})

    # approve:已支援(exec/plugin.approval.*),但 approval_id 必填

    def test_approve_requires_approval_id(self):
        bridge.OPENCLAW = _FakeOC(configured=True)
        req = _FakeReq().set_json({"approve": True})
        with self.assertRaises(HTTPException) as cm:
            _run(bridge.v2_session_approve("openclaw:agent:main:main", req))
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(cm.exception.code, "APPROVAL_ID_REQUIRED")

    # 事件 feed:heartbeat 推播過濾 + digest 分流

    def test_events_feed_routes_and_heartbeat_filter(self):
        bridge.OPENCLAW = _FakeOC(configured=True)
        # digest 照 production 建在**改道後**的 key 上(`_v2_card_source` →
        # `_oc_safe_session_key`);gateway 事件帶的才是原始 key。
        safe = bridge._oc_safe_session_key("agent:main:main")
        d = bridge._OC_CARD_DIGESTS[safe] = cd.OpenClawDigest()
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
            self.assertEqual(pushed, [(safe, "r-9")])
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


class MainLaneCollisionTests(unittest.TestCase):
    """sub-key 撞 agent default lane 的防呆(2026-08-02 rakutai 空回應事故)。

    `agent:<a>:<a>` 會讓 gateway 一次 chat.send 起兩條 lane(default + session)
    各跑一個 prompt,互相 takeover → 使用者送一則卻雙泡泡、回覆被吞成空。
    bridge 在唯一入口 _v2_card_source 改道到安全 key,send/讀/abort 一致。
    """

    def test_safe_key_redirects_collision_only(self):
        f = bridge._oc_safe_session_key
        self.assertEqual(f("agent:main:main"), "agent:main:pocket")
        self.assertEqual(f("agent:xcash:xcash"), "agent:xcash:pocket")
        # 不撞名的一律原樣
        self.assertEqual(f("agent:main:pocket"), "agent:main:pocket")
        self.assertEqual(f("agent:main:pocket-e2e"), "agent:main:pocket-e2e")
        self.assertEqual(f("agent:main:main2"), "agent:main:main2")
        self.assertEqual(f("weird"), "weird")

    def test_card_source_routes_collision_key(self):
        # 進 _v2_card_source 的 openclaw 撞名 key → src[1] 已被改道
        import unittest.mock as mock
        with mock.patch.object(bridge.OPENCLAW, "configured", return_value=True):
            src = bridge._v2_card_source("openclaw:agent:main:main")
        self.assertEqual(src, ("oc", "agent:main:pocket"))
        with mock.patch.object(bridge.OPENCLAW, "configured", return_value=True):
            src2 = bridge._v2_card_source("openclaw:agent:main:dev")
        self.assertEqual(src2, ("oc", "agent:main:dev"))   # 不撞名不動


if __name__ == "__main__":
    unittest.main(verbosity=1)
