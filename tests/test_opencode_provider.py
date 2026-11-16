"""opencode_sdk 配置/偵測/名錄形狀 + bridge S7 的列表、設定 op、失敗翻譯。

偵測鐵則(照 Gemini/grok/agy 模式):binary 不在 → configured() False →
bridge 全部靜默缺席(caps 不宣告、v2 列表無列、agents 無項)——
CI/測試環境沒裝 opencode 一樣全綠。
"""
import _isolation  # noqa: F401
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="oce-provider-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import opencode_sdk  # noqa: E402
import bridge  # noqa: E402


class _Req:
    headers = {"authorization": "Bearer test-unit-token"}
    query_params = {}

    def __init__(self, body=None):
        self._body = body or {}

    async def json(self):
        return self._body


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="oce-cfg-")
        self.p = patch.object(opencode_sdk, "_CONFIG_FILE",
                              os.path.join(self._dir, "opencode.json"))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        os.environ.pop("OPENROUTER_API_KEY", None)

    def test_env_key_beats_file(self):
        opencode_sdk.save_config(api_key="file-key")
        os.environ["OPENROUTER_API_KEY"] = "env-key"
        cfg = opencode_sdk.load_config()
        self.assertEqual(cfg["api_key"], "env-key")
        self.assertEqual(cfg["source"], "env")

    def test_merge_write_keeps_other_field(self):
        """只帶 model 的寫入不可以把 api_key 洗掉(gemini 踩過)。"""
        opencode_sdk.save_config(api_key="k1")
        opencode_sdk.save_config(model="openrouter/x/y")
        cfg = opencode_sdk.load_config()
        self.assertEqual(cfg["api_key"], "k1")
        self.assertEqual(cfg["model"], "openrouter/x/y")
        self.assertEqual(cfg["model_source"], "file")

    def test_config_file_is_0600(self):
        opencode_sdk.save_config(api_key="secret")
        mode = os.stat(opencode_sdk._CONFIG_FILE).st_mode & 0o777
        self.assertEqual(mode, 0o600)


class DetectTest(unittest.TestCase):
    def test_env_bin_must_exist(self):
        with patch.dict(os.environ,
                        {"POCKET_OPENCODE_BIN": "/no/such/opencode"}):
            self.assertEqual(opencode_sdk.opencode_bin(), "")

    def test_env_bin_used_when_exists(self):
        f = os.path.join(_TMP, "fake-opencode")
        open(f, "w").close()
        with patch.dict(os.environ, {"POCKET_OPENCODE_BIN": f}):
            self.assertEqual(opencode_sdk.opencode_bin(), f)

    def test_not_installed_means_silent_absence(self):
        """binary 不在 → configured False → v2 列表整段缺席。"""
        with patch.object(opencode_sdk, "opencode_bin", lambda: ""):
            client = opencode_sdk.OpenCodeClient()
            self.assertFalse(client.configured())
        with patch.object(bridge.OPENCODE, "configured", lambda: False):
            rows = asyncio.run(bridge._opencode_v2_rows())
            self.assertEqual(rows, [])

    def test_auth_state_never_required(self):
        """這家的 auth 是加值不是閘門 —— required 永遠 False。"""
        with patch.object(opencode_sdk, "_AUTH_FILE",
                          os.path.join(_TMP, "nope-auth.json")), \
             patch.object(opencode_sdk, "_CONFIG_FILE",
                          os.path.join(_TMP, "nope-cfg.json")):
            st = opencode_sdk.auth_state()
            self.assertEqual(st, {"logged_in": False, "key_set": False,
                                  "required": False, "method": "builtin-free"})

    def test_auth_state_login_detected_from_credentials_content(self):
        """auth.json **存在但是空的**不算登入(opencode auth list 顯示
        0 credentials 的狀態)—— 只看檔案在不在會把「沒憑證」報成已登入。"""
        empty = os.path.join(_TMP, "auth-empty.json")
        with open(empty, "w") as f:
            f.write("{}")
        with patch.object(opencode_sdk, "_AUTH_FILE", empty):
            self.assertFalse(opencode_sdk.login_available())
        full = os.path.join(_TMP, "auth-full.json")
        with open(full, "w") as f:
            f.write('{"openrouter": {"type": "api"}}')
        with patch.object(opencode_sdk, "_AUTH_FILE", full):
            self.assertTrue(opencode_sdk.login_available())
            self.assertEqual(opencode_sdk.auth_state()["method"], "login")


class SessionIdParseTest(unittest.TestCase):
    """`opencode:` 與 `openclaw:` 只差三個字母 —— 前綴不准互吃。"""

    def test_opencode_id_parses(self):
        self.assertEqual(
            bridge._opencode_key_from_session_id("opencode:k1"), "k1")

    def test_openclaw_id_is_not_opencode(self):
        self.assertIsNone(
            bridge._opencode_key_from_session_id("openclaw:agent:main:main"))
        self.assertIsNone(bridge._opencode_key_from_session_id("opencode:"))

    def test_approval_provider_mapping_does_not_confuse_the_two(self):
        self.assertEqual(bridge._approval_provider_of("opencode:k1"),
                         "opencode")
        self.assertEqual(bridge._approval_provider_of("openclaw:agent:main"),
                         "openclaw")


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
            {"update": {"sessionUpdate": "usage_update", "used": 1}},
            {"update": {"sessionUpdate": "user_message_chunk",
                        "content": {"type": "text", "text": "追問"}}},
        ]
        msgs = opencode_sdk.project_updates_to_messages(frames)
        self.assertEqual([(m["role"], m["content"]) for m in msgs],
                         [("user", "問題"), ("assistant", "答案"),
                          ("user", "追問")])

    def test_unknown_and_malformed_frames_are_skipped(self):
        frames = ["junk", {"update": {"sessionUpdate": "future_kind"}},
                  {"no": "update"}, None]
        self.assertEqual(opencode_sdk.project_updates_to_messages(frames), [])

    def test_id_prefix_is_distinct_from_grok(self):
        """兩家共用 acp_rail 的投影,id 前綴必須不同(卡 id 會撞)。"""
        import grok_sdk
        frame = [{"update": {"sessionUpdate": "agent_message_chunk",
                             "content": {"type": "text", "text": "a"}}}]
        self.assertTrue(
            opencode_sdk.project_updates_to_messages(frame)[0]["id"]
            .startswith("oce-"))
        self.assertTrue(
            grok_sdk.project_updates_to_messages(frame)[0]["id"]
            .startswith("gk-"))


class ConfigContractTest(unittest.TestCase):
    """configOptions → 契約形狀(app 的 SessionControlSheet 吃這個)。"""

    OPTS = [{"id": "model", "name": "Model", "type": "select",
             "currentValue": "opencode/big-pickle",
             "options": [{"value": "opencode/big-pickle", "name": "Big Pickle"},
                         {"value": "opencode/beam-free", "name": "Beam Free"}]},
            {"id": "mode", "name": "Session Mode", "type": "select",
             "currentValue": "build",
             "options": [{"value": "build", "name": "build"},
                         {"value": "plan", "name": "plan"}]}]

    def test_flat_snake_case_with_option_lists(self):
        out = opencode_sdk.config_to_contract(self.OPTS)
        self.assertEqual(out["model"], "opencode/big-pickle")
        self.assertEqual(out["mode"], "build")
        self.assertEqual([o["value"] for o in out["mode_options"]],
                         ["build", "plan"])

    def test_unknown_config_id_passes_through_without_guessing(self):
        out = opencode_sdk.config_to_contract(
            [{"id": "future_knob", "currentValue": 7}])
        self.assertEqual(out["future_knob"], 7)

    def test_allowed_values_drive_write_validation(self):
        self.assertEqual(
            opencode_sdk.config_allowed_values(self.OPTS, "mode"),
            ["build", "plan"])
        self.assertEqual(
            opencode_sdk.config_allowed_values(self.OPTS, "nope"), [],
            "沒有 options 回空 = 不驗(不自己發明白名單)")


class RowShapeTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="oce-row-")
        self.p1 = patch.object(opencode_sdk, "_STATE_DIR", self._dir)
        self.p2 = patch.object(opencode_sdk, "_SESSIONS_FILE",
                               os.path.join(self._dir, "sessions.json"))
        self.p1.start(); self.p2.start()

    def tearDown(self):
        self.p1.stop(); self.p2.stop()

    def test_v2_row_shape_declares_config_capability(self):
        opencode_sdk.remember_session("k1", "ses_1", title="任務 A")
        opencode_sdk.transcript_append("k1", "assistant", "最後一句")
        row = opencode_sdk.session_v2_row(opencode_sdk.get_session("k1"))
        self.assertEqual(row["id"], "opencode:k1")
        self.assertEqual(row["provider"], "opencode")
        self.assertEqual(row["title"], "任務 A")
        self.assertEqual(row["subtitle"], "最後一句")
        self.assertIn("interrupt", row["capabilities"])
        self.assertIn("approve", row["capabilities"])
        self.assertIn("config", row["capabilities"])

    def test_row_align_iso_timestamp_and_waiting_approval(self):
        opencode_sdk.remember_session("k1", "ses_1")
        row = opencode_sdk.session_v2_row(opencode_sdk.get_session("k1"))
        out = bridge._oce_row_align(row, {"opencode:k1": {"id": "oce-x"}})
        self.assertTrue(str(out["last_event_at"]).endswith("Z"),
                        f"要 ISO8601 UTC,不是 {out['last_event_at']!r}")
        self.assertEqual(out["status"], "waiting_approval")
        self.assertEqual(out["meta"]["approval"], {"id": "oce-x"})

    def test_default_row_offered_when_ledger_empty(self):
        with patch.object(bridge.OPENCODE, "configured", lambda: True):
            rows = asyncio.run(bridge._opencode_v2_rows())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "opencode:default",
                         "新裝也要有入口(第一則訊息才真正開 ACP session)")

    def test_archive_hides_without_deleting(self):
        opencode_sdk.remember_session("k1", "ses_1", title="x")
        opencode_sdk.set_archived("k1", True)
        self.assertEqual(opencode_sdk.list_sessions(), [])
        self.assertIsNotNone(opencode_sdk.get_session("k1"),
                             "封存=不列出來,不是刪")
        opencode_sdk.set_archived("k1", False)
        self.assertEqual(len(opencode_sdk.list_sessions()), 1)

    def test_history_page_shape_matches_other_providers(self):
        for i in range(5):
            opencode_sdk.transcript_append("k1", "user", f"m{i}")
        page = opencode_sdk.transcript_page("k1", limit=2, offset=0)
        self.assertEqual([m["content"] for m in page["messages"]],
                         ["m3", "m4"], "offset 從最新往回數,messages 舊→新")
        self.assertTrue(page["more"])
        self.assertEqual(page["total"], 5)


class ConfigOpTest(unittest.TestCase):
    """S7 的分水嶺:per-session config_read/config_write 的實際行為。"""

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="oce-cfgop-")
        self._patches = [
            patch.object(opencode_sdk, "_STATE_DIR", self._dir),
            patch.object(opencode_sdk, "_SESSIONS_FILE",
                         os.path.join(self._dir, "sessions.json")),
            patch.object(opencode_sdk, "_CONFIG_FILE",
                         os.path.join(self._dir, "opencode.json")),
        ]
        for p in self._patches:
            p.start()
        opencode_sdk.remember_session("k1", "ses_1")
        self.opts = [{"id": "model", "currentValue": "opencode/big-pickle",
                      "options": [{"value": "opencode/big-pickle"},
                                  {"value": "opencode/beam-free"}]},
                     {"id": "mode", "currentValue": "build",
                      "options": [{"value": "build"}, {"value": "plan"}]}]
        self.sets = []

        class _Fake:
            configured = staticmethod(lambda: True)

            def session_loaded(_self, sid):
                return sid == "ses_1"

            def config_options(_self, sid):
                return self.opts

            def current_config(_self, sid):
                return opencode_sdk.config_to_contract(self.opts)

            async def ensure_session(_self, key):
                return "ses_1"

            async def set_config(_self, sid, cid, val):
                self.sets.append((sid, cid, val))
                for o in self.opts:
                    if o["id"] == cid:
                        o["currentValue"] = val
                return opencode_sdk.config_to_contract(self.opts)

        self._orig = bridge.OPENCODE
        bridge.OPENCODE = _Fake()

    def tearDown(self):
        bridge.OPENCODE = self._orig
        for p in self._patches:
            p.stop()

    def test_config_read_returns_current_values_and_options(self):
        out = asyncio.run(bridge._oce_op_config_read("k1", _Req()))
        self.assertEqual(out["model"], "opencode/big-pickle")
        self.assertEqual(out["mode"], "build")
        self.assertIn("model_options", out)

    def test_config_write_applies_and_persists_model_preference(self):
        out = asyncio.run(bridge._oce_op_config_write(
            "k1", _Req({"model": "opencode/beam-free"})))
        self.assertEqual(out["applied"], {"model": "opencode/beam-free"})
        self.assertEqual(self.sets, [("ses_1", "model", "opencode/beam-free")])
        self.assertEqual(opencode_sdk.load_config()["model"],
                         "opencode/beam-free",
                         "模型選擇要黏住(下一條新 session 也用它)")

    def test_config_write_mode_is_session_scoped_not_persisted(self):
        """mode(build/plan)是這條對話當下的選擇,不該變成全域偏好。"""
        asyncio.run(bridge._oce_op_config_write("k1", _Req({"mode": "plan"})))
        self.assertEqual(self.sets, [("ses_1", "mode", "plan")])
        self.assertEqual(opencode_sdk.load_config()["model"], "")

    def test_unknown_value_is_400_with_the_allowed_list(self):
        """盲寫會把 session 釘在不存在的模型上(codex 那條病史)——
        400 並列出可用值,app 直接顯示 detail 就知道該填什麼。"""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(bridge._oce_op_config_write(
                "k1", _Req({"model": "does/not/exist"})))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("opencode/beam-free", str(ctx.exception.detail),
                      "要列出可用值")
        self.assertEqual(self.sets, [], "驗不過不准真的寫下去")

    def test_empty_body_is_400(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(bridge._oce_op_config_write("k1", _Req({})))
        self.assertEqual(ctx.exception.status_code, 400)


class FailureTextTest(unittest.TestCase):
    def test_credential_error_points_at_models_not_login(self):
        """**不可以**翻成「去登入」—— 這家沒有登入牆,401 的意思是某個
        provider 的 key/模型 id 不對,指引要指向模型設定。"""
        e = opencode_sdk.OpenCodeError("401 Unauthorized", code="-32000")
        text = bridge._opencode_failure_text(e)
        self.assertEqual(bridge._opencode_failure_kind(e), "credential")
        self.assertIn("免費模型", text)
        self.assertNotIn("登入後再回來", text)

    def test_no_binary_maps_to_install_hint(self):
        e = opencode_sdk.OpenCodeError(
            "opencode 不在這台主機(curl …)", code="NO_BINARY")
        self.assertEqual(bridge._opencode_failure_kind(e), "no_binary")
        self.assertIn("opencode.ai/install",
                      bridge._opencode_failure_text(e))

    def test_unknown_error_keeps_raw_message(self):
        e = opencode_sdk.OpenCodeError("weird explosion xyz")
        self.assertIn("weird explosion xyz",
                      bridge._opencode_failure_text(e))

    def test_quota_and_timeout_kinds(self):
        self.assertEqual(bridge._opencode_failure_kind(
            opencode_sdk.OpenCodeError("429 rate limit")), "quota")
        self.assertEqual(bridge._opencode_failure_kind(
            opencode_sdk.OpenCodeError("opencode call timeout: session/prompt",
                                       code="TIMEOUT")), "timeout")
        self.assertEqual(bridge._opencode_failure_kind(
            opencode_sdk.OpenCodeError("agent process lost",
                                       code="PROCESS_LOST")), "unavailable")


class CapsDeclarationTest(unittest.TestCase):
    def test_opencode_caps_payload_includes_five_ops(self):
        payload = bridge._provider_caps_payload()
        self.assertIn("opencode", payload)
        self.assertEqual(payload["opencode"]["ops"],
                         ["archive", "config_read", "config_write",
                          "history", "rename"])
        self.assertTrue(payload["opencode"]["interrupt"])
        self.assertTrue(payload["opencode"]["approvals"])
        self.assertTrue(payload["opencode"]["approval_callbacks_live_only"])
        self.assertEqual(payload["opencode"]["terminal_status_quality"],
                         "strong")

    def test_compact_is_not_claimed_anywhere(self):
        """availableCommands 實測沒有 compact → 不准出現在 ops
        (宣告了就是 app 畫出來、按下去 409 的死鈕)。"""
        self.assertNotIn("compact", bridge._provider_caps_payload()
                         ["opencode"]["ops"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
