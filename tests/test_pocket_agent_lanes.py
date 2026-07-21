"""Pocket fixed CC/CX tmux lanes."""

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
             bridge.CLAUDE_BIN, "--resume", sid),
            [call.args for call in tmux.await_args_list],
        )

    async def test_activate_codex_lane_resumes_thread_into_fixed_tmux(self):
        cwd = tempfile.mkdtemp(prefix="pocket-cx-cwd-")
        tmux = AsyncMock(return_value=(0, "", ""))
        with (
            patch.object(bridge, "_check_auth"),
            patch.object(bridge, "POCKET_CX_TMUX", "pocket-cx-test"),
            patch.object(bridge, "_resolve_codex_bin", return_value="/bin/codex"),
            patch.object(bridge, "_tmux_alive", AsyncMock(return_value=False)),
            patch.object(bridge, "_tmux_run", tmux),
            patch.object(bridge, "_pocket_lane_bindings", return_value={}),
            patch.object(bridge, "_pocket_lane_note"),
        ):
            res = await bridge.app_agent_lane_activate(
                "codex", FakeRequest({"thread_id": "thread-123", "workdir": cwd, "name": "CX"})
            )

        self.assertTrue(res["ok"])
        self.assertEqual(res["tmux"], "pocket-cx-test")
        self.assertEqual(res["session"]["thread_id"], "thread-123")
        self.assertIn(
            ("new-session", "-d", "-s", "pocket-cx-test", "-c", os.path.realpath(cwd),
             "/bin/codex", "resume", "thread-123"),
            [call.args for call in tmux.await_args_list],
        )

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
