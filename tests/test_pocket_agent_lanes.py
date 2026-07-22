"""Pocket CC/CX agent bindings."""

import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

_TMP = tempfile.mkdtemp(prefix="pocket-agent-lanes-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class FakeRequest:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


class TestPocketAgentLanes(unittest.IsolatedAsyncioTestCase):
    async def test_activate_cc_lane_resumes_history_sid_into_fixed_tmux(self):
        cwd = tempfile.mkdtemp(prefix="pocket-cc-cwd-")
        sid = "12345678-1234-1234-1234-123456789abc"
        tmux = AsyncMock(return_value=(0, "", ""))
        remote_log = "/tmp/pocket-cc-test.remote.log"
        with (
            patch.object(bridge, "_check_auth"),
            patch.object(bridge, "POCKET_CC_TMUX", "pocket-cc-test"),
            patch.object(bridge, "_cchist_find", return_value="/tmp/session.jsonl"),
            patch.object(bridge, "_cchist_meta", return_value={"cwd": cwd, "title": "resume me"}),
            patch.object(bridge, "_tmux_alive", AsyncMock(return_value=False)),
            patch.object(bridge, "_tmux_run", tmux),
            patch.object(bridge, "_run_ccsess", AsyncMock(return_value="")),
            patch.object(bridge, "_cc_wait_ready", AsyncMock(return_value=True)),
            patch.object(bridge, "_pocket_lane_note"),
            patch.object(bridge, "_cc_write_resume_pin"),
            patch.object(bridge, "_cc_write_remote_control_pin") as remote_pin,
            patch.object(bridge, "_cc_remote_debug_path", return_value=remote_log),
            patch.object(bridge, "_cc_register_explicit_resume", AsyncMock()),
            patch.object(bridge, "_cc_cache_sid"),
            patch.object(bridge, "_cc_mark_app_owned"),
        ):
            res = await bridge.app_agent_lane_activate(
                "cc", FakeRequest({"session_id": sid})
            )

        self.assertTrue(res["ok"])
        self.assertEqual(res["session"]["name"], "pocket-cc-test")
        self.assertEqual(res["session"]["sessionId"], sid)
        self.assertIn(
            ("new-session", "-d", "-s", "pocket-cc-test", "-c", os.path.realpath(cwd),
             bridge.CLAUDE_BIN, "--resume", sid,
             "--remote-control", "pocket-cc-test", "--debug-file", remote_log),
            [call.args for call in tmux.await_args_list],
        )
        remote_pin.assert_called_once_with("pocket-cc-test")

    async def test_activate_existing_cc_lane_restarts_when_remote_control_is_missing(self):
        cwd = tempfile.mkdtemp(prefix="pocket-cc-existing-cwd-")
        sid = "23456789-2345-2345-2345-23456789abcd"
        replace = AsyncMock()
        register = AsyncMock()
        remote_log = "/tmp/pocket-cc-existing.remote.log"
        with (
            patch.object(bridge, "_check_auth"),
            patch.object(bridge, "POCKET_CC_TMUX", "pocket-cc-existing"),
            patch.object(bridge, "_tmux_alive", AsyncMock(return_value=True)),
            patch.object(bridge, "_cc_pane_session_id", AsyncMock(return_value=sid)),
            patch.object(bridge, "_cc_pane_has_remote_control", AsyncMock(return_value=False)),
            patch.object(bridge, "_pocket_tmux_replace", replace),
            patch.object(bridge, "_cc_wait_ready", AsyncMock(return_value=True)),
            patch.object(bridge, "_cc_register_explicit_resume", register),
            patch.object(bridge, "_cc_remote_debug_path", return_value=remote_log),
            patch.object(bridge, "_cc_write_remote_control_pin"),
            patch.object(bridge, "_cc_write_resume_pin"),
            patch.object(bridge, "_cc_cache_sid"),
            patch.object(bridge, "_cc_mark_app_owned"),
            patch.object(bridge, "_pocket_lane_note"),
        ):
            res = await bridge.app_agent_lane_activate(
                "cc", FakeRequest({"session_id": sid, "cwd": cwd, "adopt_source": False})
            )

        self.assertEqual(res["session"]["status"], "running")
        replace.assert_awaited_once_with(
            "pocket-cc-existing", os.path.realpath(cwd),
            [bridge.CLAUDE_BIN, "--resume", sid,
             "--remote-control", "pocket-cc-existing", "--debug-file", remote_log],
        )
        register.assert_awaited_once_with("pocket-cc-existing", os.path.realpath(cwd))

    async def test_cc_question_choice_sends_digit_then_enter(self):
        tmux = AsyncMock(return_value=(0, "", ""))
        pane = "Choose a release lane\n  1. Stable\n  2. Preview\nEnter to select"
        with (
            patch.object(bridge, "_tmux_alive", AsyncMock(return_value=True)),
            patch.object(bridge, "_tmux_capture_cached", AsyncMock(return_value=pane)),
            patch.object(bridge, "_cc_conf_rows", return_value=[]),
            patch.object(bridge, "_tmux_run", tmux),
        ):
            res = await bridge._cc_key_core("pocket-cc-test", "2")

        self.assertTrue(res["ok"])
        self.assertEqual(
            [call.args for call in tmux.await_args_list],
            [("send-keys", "-t", "pocket-cc-test", "-l", "2"),
             ("send-keys", "-t", "pocket-cc-test", "Enter")],
        )

    async def test_live_source_session_is_reused_in_place(self):
        cwd = tempfile.mkdtemp(prefix="pocket-cc-source-cwd-")
        sid = "34567890-3456-3456-3456-34567890abcd"
        write_pin = Mock()
        replace = AsyncMock()
        with (
            patch.object(bridge, "_tmux_alive", AsyncMock(return_value=True)),
            patch.object(bridge, "_cc_pane_session_id", AsyncMock(return_value=sid)),
            patch.object(bridge, "_cc_pane_has_remote_control", AsyncMock(return_value=True)),
            patch.object(bridge, "_pocket_tmux_replace", replace),
            patch.object(bridge, "_cc_register_explicit_resume", AsyncMock()),
            patch.object(bridge, "_cc_write_remote_control_pin"),
            patch.object(bridge, "_cc_write_resume_pin", write_pin),
            patch.object(bridge, "_cc_cache_sid"),
            patch.object(bridge, "_cc_mark_app_owned"),
            patch.object(bridge, "_pocket_lane_note"),
        ):
            res = await bridge._pocket_bind_cc_source(
                "cc-source", sid, cwd, "Existing Claude App session"
            )

        self.assertEqual(res["name"], "cc-source")
        self.assertEqual(res["sessionId"], sid)
        write_pin.assert_called_once_with("cc-source", sid)
        replace.assert_not_awaited()

    async def test_activate_live_source_returns_original_tmux_name(self):
        sid = "45678901-4567-4567-4567-45678901abcd"
        session = {
            "name": "cc-original", "workdir": "/tmp", "status": "running",
            "sessionId": sid, "sessionTitle": "Original",
        }
        bind = AsyncMock(return_value=session)
        with (
            patch.object(bridge, "_check_auth"),
            patch.object(bridge, "_pocket_selected_cc", AsyncMock(
                return_value=(sid, "/tmp", "Original", "cc-original"))),
            patch.object(bridge, "_pocket_bind_cc_source", bind),
        ):
            res = await bridge.app_agent_lane_activate(
                "cc", FakeRequest({"name": "cc-original", "session_id": sid})
            )

        self.assertEqual(res["tmux"], "cc-original")
        self.assertEqual(res["session"]["name"], "cc-original")
        bind.assert_awaited_once_with("cc-original", sid, "/tmp", "Original")

    async def test_activate_codex_lane_uses_app_server_without_tmux(self):
        cwd = tempfile.mkdtemp(prefix="pocket-cx-cwd-")
        replace = AsyncMock()
        note = Mock()
        with (
            patch.object(bridge, "_check_auth"),
            patch.object(bridge, "_pocket_tmux_replace", replace),
            patch.object(bridge, "_pocket_lane_note", note),
        ):
            res = await bridge.app_agent_lane_activate(
                "codex", FakeRequest({"thread_id": "thread-123", "workdir": cwd, "name": "CX"})
            )

        self.assertTrue(res["ok"])
        self.assertIsNone(res["tmux"])
        self.assertEqual(res["session"]["thread_id"], "thread-123")
        self.assertEqual(res["session"]["source"], "codex-app-server")
        replace.assert_not_awaited()
        note.assert_called_once_with("codex", "", "thread-123", os.path.realpath(cwd), "CX")

    async def test_cc_history_resume_registers_without_unpacking_ccsess_output(self):
        cwd = tempfile.mkdtemp(prefix="pocket-history-cwd-")
        sid = "87654321-4321-4321-4321-cba987654321"
        with (
            patch.object(bridge, "_check_auth"),
            patch.object(bridge, "_cchist_find", return_value="/tmp/session.jsonl"),
            patch.object(bridge, "_cchist_meta", return_value={"cwd": cwd}),
            patch.object(bridge, "_cc_conf_rows", return_value=[]),
            patch.object(bridge, "_tmux_alive", AsyncMock(return_value=False)),
            patch.object(bridge, "_tmux_run", AsyncMock(return_value=(0, "", ""))),
            patch.object(bridge, "_run_ccsess", AsyncMock(return_value="registered")),
        ):
            res = await bridge.cc_history_resume(sid, FakeRequest({}))

        self.assertTrue(res["ok"])
        self.assertEqual(res["name"], f"cc-{sid[:8]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
