"""grok_sdk 配置/偵測/名錄形狀 + bridge S6 的列表與失敗翻譯。

偵測鐵則(照 Gemini/agy 模式):binary 不在 → configured() False →
bridge 全部靜默缺席(caps 不宣告、v2 列表無列、agents 無項)——
CI/測試環境沒裝 grok 一樣全綠。
"""
import _isolation  # noqa: F401
import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="gk-provider-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grok_sdk  # noqa: E402
import bridge  # noqa: E402


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="gk-cfg-")
        self.p = patch.object(grok_sdk, "_CONFIG_FILE",
                              os.path.join(self._dir, "grok.json"))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        os.environ.pop("XAI_API_KEY", None)

    def test_env_key_beats_file(self):
        grok_sdk.save_config(api_key="file-key")
        os.environ["XAI_API_KEY"] = "env-key"
        cfg = grok_sdk.load_config()
        self.assertEqual(cfg["api_key"], "env-key")
        self.assertEqual(cfg["source"], "env")

    def test_merge_write_keeps_other_field(self):
        """只帶 model 的寫入不可以把 api_key 洗掉(gemini 踩過)。"""
        grok_sdk.save_config(api_key="k1")
        grok_sdk.save_config(model="grok-4.6")
        cfg = grok_sdk.load_config()
        self.assertEqual(cfg["api_key"], "k1")
        self.assertEqual(cfg["model"], "grok-4.6")
        self.assertEqual(cfg["model_source"], "file")

    def test_config_file_is_0600(self):
        grok_sdk.save_config(api_key="secret")
        mode = os.stat(grok_sdk._CONFIG_FILE).st_mode & 0o777
        self.assertEqual(mode, 0o600)


class DetectTest(unittest.TestCase):
    def test_env_bin_must_exist(self):
        with patch.dict(os.environ, {"POCKET_GROK_BIN": "/no/such/grok"}):
            self.assertEqual(grok_sdk.grok_bin(), "")

    def test_env_bin_used_when_exists(self):
        f = os.path.join(_TMP, "fake-grok")
        open(f, "w").close()
        with patch.dict(os.environ, {"POCKET_GROK_BIN": f}):
            self.assertEqual(grok_sdk.grok_bin(), f)

    def test_not_installed_means_silent_absence(self):
        """binary 不在 → configured False → v2 列表整段缺席、agents 無項。"""
        with patch.object(grok_sdk, "grok_bin", lambda: ""):
            client = grok_sdk.GrokClient()
            self.assertFalse(client.configured())
        with patch.object(bridge.GROK, "configured", lambda: False):
            rows = asyncio.run(bridge._grok_v2_rows())
            self.assertEqual(rows, [])

    def test_auth_state_shapes(self):
        with patch.object(grok_sdk, "_AUTH_FILE",
                          os.path.join(_TMP, "nope-auth.json")), \
             patch.object(grok_sdk, "_CONFIG_FILE",
                          os.path.join(_TMP, "nope-cfg.json")):
            st = grok_sdk.auth_state()
            self.assertEqual(st, {"logged_in": False, "key_set": False,
                                  "method": "none"})
        authf = os.path.join(_TMP, "auth-present.json")
        open(authf, "w").close()
        with patch.object(grok_sdk, "_AUTH_FILE", authf):
            self.assertEqual(grok_sdk.auth_state()["method"], "login")


class ProjectionTest(unittest.TestCase):
    def test_merges_consecutive_same_role_chunks(self):
        frames = [
            {"update": {"sessionUpdate": "user_message_chunk",
                        "content": {"type": "text", "text": "問題"}}},
            {"update": {"sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": "(略過)"}}},
            {"update": {"sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "答"}}},
            {"update": {"sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "案"}}},
            {"update": {"sessionUpdate": "tool_call", "title": "Bash"}},
            {"update": {"sessionUpdate": "user_message_chunk",
                        "content": {"type": "text", "text": "追問"}}},
        ]
        msgs = grok_sdk.project_updates_to_messages(frames)
        self.assertEqual([(m["role"], m["content"]) for m in msgs],
                         [("user", "問題"), ("assistant", "答案"),
                          ("user", "追問")])

    def test_unknown_and_malformed_frames_are_skipped(self):
        frames = ["junk", {"update": {"sessionUpdate": "future_kind"}},
                  {"no": "update"}, None]
        self.assertEqual(grok_sdk.project_updates_to_messages(frames), [])

    def test_accepts_bare_update_and_params_wrapped(self):
        """x.ai/session/updates 的幀可能是 {update:…} 或原始通知
        {params:{update:…}} 或裸 update —— 三種都要吃。"""
        bare = {"sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "a"}}
        wrapped = {"params": {"update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "b"}}}}
        msgs = grok_sdk.project_updates_to_messages([bare, wrapped])
        self.assertEqual(msgs[0]["content"], "ab")


class RowShapeTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="gk-row-")
        self.p1 = patch.object(grok_sdk, "_STATE_DIR", self._dir)
        self.p2 = patch.object(grok_sdk, "_SESSIONS_FILE",
                               os.path.join(self._dir, "sessions.json"))
        self.p1.start(); self.p2.start()

    def tearDown(self):
        self.p1.stop(); self.p2.stop()

    def test_v2_row_shape(self):
        grok_sdk.remember_session("k1", "acp-1", title="任務 A")
        grok_sdk.transcript_append("k1", "assistant", "最後一句")
        row = grok_sdk.session_v2_row(grok_sdk.get_session("k1"))
        self.assertEqual(row["id"], "grok:k1")
        self.assertEqual(row["provider"], "grok")
        self.assertEqual(row["title"], "任務 A")
        self.assertEqual(row["subtitle"], "最後一句")
        self.assertIn("interrupt", row["capabilities"])
        self.assertIn("approve", row["capabilities"])

    def test_row_align_iso_timestamp_and_waiting_approval(self):
        """S5 的兩個教訓直接釘住:float epoch → ISO 字串;pending →
        waiting_approval + meta.approval。"""
        grok_sdk.remember_session("k1", "acp-1")
        row = grok_sdk.session_v2_row(grok_sdk.get_session("k1"))
        out = bridge._gk_row_align(row, {"grok:k1": {"id": "gk-x"}})
        self.assertTrue(str(out["last_event_at"]).endswith("Z"),
                        f"要 ISO8601 UTC,不是 {out['last_event_at']!r}")
        self.assertEqual(out["status"], "waiting_approval")
        self.assertEqual(out["meta"]["approval"], {"id": "gk-x"})

    def test_default_row_offered_when_ledger_empty(self):
        with patch.object(bridge.GROK, "configured", lambda: True):
            rows = asyncio.run(bridge._grok_v2_rows())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "grok:default",
                         "新裝也要有入口(第一則訊息才真正開 ACP session)")


class FailureTextTest(unittest.TestCase):
    def test_not_logged_in_maps_to_login_guidance(self):
        e = grok_sdk.GrokError(grok_sdk.LOGIN_GUIDANCE, code="NOT_LOGGED_IN")
        self.assertEqual(bridge._grok_failure_text(e), grok_sdk.LOGIN_GUIDANCE)
        self.assertEqual(bridge._grok_failure_kind(e), "auth")

    def test_real_wire_auth_error_maps_to_login_guidance(self):
        """真機錄到的 session/new 錯誤字串要翻成登入指引。"""
        e = grok_sdk.GrokError("Authentication required: "
                               "no auth method id provided", code="-32000")
        self.assertEqual(bridge._grok_failure_text(e), grok_sdk.LOGIN_GUIDANCE)

    def test_unknown_error_keeps_raw_message(self):
        e = grok_sdk.GrokError("weird explosion xyz")
        self.assertIn("weird explosion xyz", bridge._grok_failure_text(e))

    def test_quota_and_timeout_kinds(self):
        self.assertEqual(bridge._grok_failure_kind(
            grok_sdk.GrokError("429 rate limit")), "quota")
        self.assertEqual(bridge._grok_failure_kind(
            grok_sdk.GrokError("grok call timeout: session/prompt",
                               code="TIMEOUT")), "timeout")


class CapsDeclarationTest(unittest.TestCase):
    def test_grok_caps_payload_includes_three_ops(self):
        payload = bridge._provider_caps_payload()
        self.assertIn("grok", payload)
        self.assertEqual(payload["grok"]["ops"],
                         ["archive", "history", "rename"])
        self.assertTrue(payload["grok"]["interrupt"])
        self.assertTrue(payload["grok"]["approvals"])
        self.assertTrue(payload["grok"]["approval_callbacks_live_only"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
