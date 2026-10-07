"""Gemini provider(S5)單元測試。

蓋五層:配置優先序、session 名錄(key↔acp_sid 解耦)、逐字稿、
GeminiDigest 卡形(含 PersonaDigest 前綴重構的回歸)、bridge 接線
(v1 送訊 SSE、update→卡、權限自動拒絕、中斷、v2 rows/agents)。
傳輸層的子程序不真開 —— GEMINI 的呼叫面以 patch 樁住。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

# gemini_provider 的路徑常數在 import 時讀 env —— 必須先導到 tmp 再 import。
_TMP = tempfile.mkdtemp(prefix="gm-test-")
os.environ["GEMINI_STATE_DIR"] = os.path.join(_TMP, "state")
os.environ["GEMINI_CONFIG_FILE"] = os.path.join(_TMP, "gemini.json")
os.environ["GEMINI_WORKDIR"] = os.path.join(_TMP, "workspace")
os.environ.pop("GEMINI_API_KEY", None)
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
import carddigest  # noqa: E402
import gemini_provider  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(bridge.app)
AUTH = {"Authorization": "Bearer " + os.environ.get("BRIDGE_TOKEN", "test-unit-token")}


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class ConfigTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("GEMINI_API_KEY", None)
        try:
            os.remove(os.environ["GEMINI_CONFIG_FILE"])
        except FileNotFoundError:
            pass

    def test_unconfigured_by_default(self):
        self.assertEqual(gemini_provider.load_config(),
                         {"api_key": "", "source": "none"})
        self.assertFalse(bridge.GEMINI.configured())

    def test_env_wins_over_file(self):
        gemini_provider.save_config("file-key")
        self.assertEqual(gemini_provider.load_config()["source"], "file")
        os.environ["GEMINI_API_KEY"] = "env-key"
        cfg = gemini_provider.load_config()
        self.assertEqual((cfg["api_key"], cfg["source"]), ("env-key", "env"))

    def test_save_config_file_mode_0600(self):
        gemini_provider.save_config("k")
        mode = os.stat(os.environ["GEMINI_CONFIG_FILE"]).st_mode & 0o777
        self.assertEqual(mode, 0o600)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        try:
            os.remove(os.path.join(os.environ["GEMINI_STATE_DIR"], "sessions.json"))
        except FileNotFoundError:
            pass

    def test_remember_touch_list_forget(self):
        gemini_provider.remember_session("k1", "acp-1", title="第一句話當標題")
        time.sleep(0.01)
        gemini_provider.remember_session("k2", "acp-2")
        gemini_provider.touch_session("k2", title_hint="hello world")
        rows = gemini_provider.list_sessions()
        self.assertEqual([r["id"] for r in rows], ["k2", "k1"])   # last 新→舊
        self.assertEqual(rows[0]["title"], "hello world")
        # 已有標題不被 touch 覆蓋
        gemini_provider.touch_session("k1", title_hint="別蓋我")
        self.assertEqual(gemini_provider.get_session("k1")["title"], "第一句話當標題")
        gemini_provider.forget_session("k1")
        self.assertIsNone(gemini_provider.get_session("k1"))

    def test_v2_row_shape(self):
        row = gemini_provider.session_v2_row(
            {"id": "k1", "title": "T", "last": 123.0}, busy=True)
        self.assertEqual(row["id"], "gemini:k1")
        self.assertEqual(row["provider"], "gemini")
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["last_event_at"], 123.0)
        self.assertIn("input", row["capabilities"])
        self.assertIn("interrupt", row["capabilities"])


class TranscriptTests(unittest.TestCase):
    def test_append_read_roundtrip(self):
        mid = gemini_provider.transcript_append("tkey", "user", "哈囉")
        gemini_provider.transcript_append("tkey", "assistant", "你好")
        msgs = gemini_provider.transcript_read("tkey")
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual(msgs[0]["id"], mid)
        self.assertTrue(mid.startswith("gm-"))


class GeminiDigestTests(unittest.TestCase):
    def test_card_prefix_and_turn_flow(self):
        d = carddigest.GeminiDigest()
        d.message_card({"id": "m1", "role": "user", "content": "hi", "ts": 1.0})
        snap = d.store.snapshot(limit=10)
        self.assertEqual(snap["cards"][0]["id"], "card-gm-m1")
        d.turn_begin("c1")
        d.turn_delta("c1", "PO")
        d.turn_delta("c1", "NG")
        cards = {c["id"]: c for c in d.store.snapshot(limit=10)["cards"]}
        draft = cards["card-gm-turn-c1"]
        self.assertEqual(draft["body"]["text"], "PONG")
        self.assertFalse(draft.get("final", True))
        d.turn_end("c1", "PONG", reply_mid="m2")
        cards = {c["id"]: c for c in d.store.snapshot(limit=10)["cards"]}
        self.assertTrue(cards["card-gm-turn-c1"].get("final"))
        self.assertIn("m2", d.known_mids)   # follower 補掃不雙出

    def test_persona_digest_prefix_regression(self):
        """前綴改 class attr 後,hermes 卡 id 一個字都不能變。"""
        d = carddigest.PersonaDigest()
        d.message_card({"id": "m1", "role": "assistant", "content": "x", "ts": 1.0})
        d.turn_begin("c9")
        d.turn_delta("c9", "y")
        ids = {c["id"] for c in d.store.snapshot(limit=10)["cards"]}
        self.assertIn("card-hp-m1", ids)
        self.assertIn("card-hp-turn-c9", ids)


class BridgeGlueTests(unittest.TestCase):
    def setUp(self):
        os.environ["GEMINI_API_KEY"] = "test-key"
        bridge._GM_CARD_DIGESTS.clear()
        bridge._GM_TURNS.clear()
        try:
            os.remove(os.path.join(os.environ["GEMINI_STATE_DIR"], "sessions.json"))
        except FileNotFoundError:
            pass

    def tearDown(self):
        os.environ.pop("GEMINI_API_KEY", None)

    def test_key_from_session_id(self):
        f = bridge._gemini_key_from_session_id
        self.assertEqual(f("gemini:abc"), "abc")
        self.assertIsNone(f("codex:abc"))
        self.assertIsNone(f("gemini:"))
        self.assertIsNone(f(""))

    def test_v2_card_source_routing(self):
        self.assertEqual(bridge._v2_card_source("gemini:k1"), ("gm", "k1"))
        os.environ.pop("GEMINI_API_KEY", None)
        with self.assertRaises(Exception):
            bridge._v2_card_source("gemini:k1")

    def test_on_update_routes_only_registered_turns(self):
        d = _run(bridge._gm_card_digest("k1"))
        # 沒登記 turn(session/load replay)→ 忽略
        bridge._gm_on_update("acp-x", {"sessionUpdate": "agent_message_chunk",
                                       "content": {"text": "ghost"}})
        self.assertEqual(d.turn_text, {})
        d.turn_begin("c1")
        bridge._GM_TURNS["acp-x"] = {"cid": "c1", "text": "", "key": "k1"}
        bridge._gm_on_update("acp-x", {"sessionUpdate": "agent_message_chunk",
                                       "content": {"text": "hi"}})
        self.assertEqual(bridge._GM_TURNS["acp-x"]["text"], "hi")
        cards = {c["id"] for c in d.store.snapshot(limit=10)["cards"]}
        self.assertIn("card-gm-turn-c1", cards)

    def test_permission_autodenied_with_visible_card(self):
        d = _run(bridge._gm_card_digest("k1"))
        d.turn_begin("c1")
        bridge._GM_TURNS["acp-x"] = {"cid": "c1", "text": "", "key": "k1"}
        out = _run(bridge._gm_on_request({
            "id": 9, "method": "session/request_permission",
            "params": {"sessionId": "acp-x",
                       "toolCall": {"title": "run_shell"}}}))
        self.assertEqual(out, {"outcome": {"outcome": "cancelled"}})
        texts = [c["body"].get("text", "")
                 for c in d.store.snapshot(limit=20)["cards"]]
        self.assertTrue(any("已自動拒絕" in t for t in texts))
        bridge._GM_TURNS.clear()

    def test_v1_post_message_accepted_and_turn_runs(self):
        ran = {}

        async def fake_ensure(key):
            ran["key"] = key
            return "acp-77"

        async def fake_call(method, params=None, timeout=0):
            # prompt 期間模擬兩個 chunk 進來
            bridge._gm_on_update("acp-77", {"sessionUpdate": "agent_message_chunk",
                                            "content": {"text": "PO"}})
            bridge._gm_on_update("acp-77", {"sessionUpdate": "agent_message_chunk",
                                            "content": {"text": "NG"}})
            return {"stopReason": "end_turn"}

        with patch.object(bridge.GEMINI, "ensure_session", side_effect=fake_ensure), \
                patch.object(bridge.GEMINI, "call", side_effect=fake_call):
            r = client.post("/app/v1/messages", headers=AUTH,
                            json={"session": "gemini:k9", "content": "ping"})
            self.assertEqual(r.status_code, 200)
            body = r.text
            self.assertIn('"accepted": true', body.replace("true,", "true,"))
            self.assertIn("[DONE]", body)
            # 背景 turn 要跑完(TestClient 的 loop 已結束;同步等卡片落地)
            deadline = time.time() + 5
            while time.time() < deadline:
                msgs = gemini_provider.transcript_read("k9")
                if [m["role"] for m in msgs] == ["user", "assistant"]:
                    break
                time.sleep(0.05)
        msgs = gemini_provider.transcript_read("k9")
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual(msgs[1]["content"], "PONG")
        d = bridge._GM_CARD_DIGESTS["k9"]
        cards = {c["id"]: c for c in d.store.snapshot(limit=20)["cards"]}
        finals = [c for c in cards.values() if c["id"].startswith("card-gm-turn-")]
        self.assertTrue(finals and finals[0]["body"]["text"] == "PONG")
        self.assertFalse(bridge._GM_TURNS)

    def test_v1_post_message_busy_409(self):
        bridge._GM_TURNS["acp-z"] = {"cid": "c", "text": "", "key": "kbusy"}
        r = client.post("/app/v1/messages", headers=AUTH,
                        json={"session": "gemini:kbusy", "content": "x"})
        self.assertEqual(r.status_code, 409)
        bridge._GM_TURNS.clear()

    def test_v1_interrupt_dispatch(self):
        with patch.object(bridge.GEMINI, "cancel",
                          new=AsyncMock()) as cancel:
            r = client.post("/app/v1/messages/interrupt", headers=AUTH,
                            json={"session": "gemini:k1"})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["ok"])
            cancel.assert_awaited_once_with("k1")

    def test_v2_rows_default_entry_and_agents(self):
        rows = _run(bridge._gemini_v2_rows())
        self.assertEqual(rows[0]["id"], "gemini:default")   # 空名錄給預設入口
        gemini_provider.remember_session("k1", "acp-1", title="T")
        rows = _run(bridge._gemini_v2_rows())
        self.assertEqual(rows[0]["id"], "gemini:k1")
        r = client.get("/app/v2/agents", headers=AUTH)
        provs = [a["provider"] for a in r.json()["agents"]]
        self.assertIn("gemini", provs)
        os.environ.pop("GEMINI_API_KEY", None)
        r = client.get("/app/v2/agents", headers=AUTH)
        provs = [a["provider"] for a in r.json()["agents"]]
        self.assertNotIn("gemini", provs)   # 未配置=靜默缺席

    def test_config_endpoints(self):
        os.environ.pop("GEMINI_API_KEY", None)
        r = client.get("/app/v1/gemini/config", headers=AUTH)
        self.assertFalse(r.json()["configured"])
        with patch.object(bridge.GEMINI, "_drop_proc", new=AsyncMock()):
            r = client.put("/app/v1/gemini/config", headers=AUTH,
                           json={"api_key": "abc123"})
        self.assertTrue(r.json()["configured"])
        self.assertEqual(gemini_provider.load_config()["source"], "file")
        os.remove(os.environ["GEMINI_CONFIG_FILE"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
