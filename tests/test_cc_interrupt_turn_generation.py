"""CC interrupt turn-generation race guards."""

import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

_TMP = tempfile.mkdtemp(prefix="cc-interrupt-turn-gen-")
os.environ["HOME"] = _TMP
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class FakeHookRequest:
    def __init__(self, body):
        self._body = body
        self.headers = {"authorization": f"Bearer {bridge.BRIDGE_TOKEN}"}
        self.url = type("Url", (), {"path": "/ccsessions/_hook"})()
        self.client = type("Client", (), {"host": "127.0.0.1"})()

    async def json(self):
        return self._body


class FakeAppRequest:
    def __init__(self, path, body=None, token=None):
        self._body = body or {}
        self.headers = {"authorization": f"Bearer {token or bridge.BRIDGE_TOKEN}"}
        self.url = type("Url", (), {"path": path})()
        self.client = type("Client", (), {"host": "127.0.0.1"})()

    async def json(self):
        return self._body


class AutoBumpGenDict(dict):
    """Bump a generation immediately after a selected .get() call."""

    def __init__(self, bump_after_nth_get: int):
        super().__init__()
        self._calls = 0
        self._bump_after = bump_after_nth_get

    def get(self, key, default=None):
        self._calls += 1
        value = super().get(key, default)
        if self._calls == self._bump_after:
            self[key] = value + 1
        return value


class TestCCInterruptTurnGeneration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_turn_gen = bridge._CC_TURN_GEN
        self.old_hook_state = bridge._CC_HOOK_STATE
        self.old_pane_cache = bridge._PANE_CACHE
        bridge._CC_TURN_GEN = {}
        bridge._CC_HOOK_STATE = {}
        bridge._PANE_CACHE = {}

    async def asyncTearDown(self):
        bridge._CC_TURN_GEN = self.old_turn_gen
        bridge._CC_HOOK_STATE = self.old_hook_state
        bridge._PANE_CACHE = self.old_pane_cache

    async def test_user_prompt_submit_increments_turn_generation(self):
        name = "Ops"
        cwd = "/tmp/ops"
        request = FakeHookRequest({
            "hook_event_name": "UserPromptSubmit",
            "cwd": cwd,
        })

        with (
            patch.object(bridge, "_client_host", return_value="127.0.0.1"),
            patch.object(bridge, "_cc_names_for_cwd", return_value=[name]),
            patch.object(bridge, "_log_event", Mock()),
        ):
            first = await bridge.cc_session_hook(request)
            second = await bridge.cc_session_hook(request)

        self.assertTrue(first["busy"])
        self.assertTrue(second["busy"])
        self.assertEqual(bridge._CC_TURN_GEN[name], 2)

    async def test_idle_hook_short_circuits_without_escape(self):
        name = "Ops"
        bridge._CC_HOOK_STATE[name] = {
            "busy": False,
            "updated_at": bridge.time.time(),
            "source": "interrupt",
        }
        tmux_run = AsyncMock(return_value=(0, "", ""))

        with (
            patch.object(bridge, "_tmux_alive", AsyncMock(return_value=True)),
            patch.object(bridge, "_tmux_run", tmux_run),
            patch.object(bridge, "_log_event", Mock()),
        ):
            result = await bridge._cc_interrupt_core(name)

        self.assertTrue(result["ok"])
        self.assertTrue(result["interrupted"])
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(result["reason"], "already_idle")
        tmux_run.assert_not_awaited()

    async def test_generation_change_before_escape_marks_stale_and_sends_nothing(self):
        name = "Ops"
        bridge._CC_TURN_GEN = AutoBumpGenDict(bump_after_nth_get=1)
        tmux_run = AsyncMock(return_value=(0, "", ""))

        with (
            patch.object(bridge, "_tmux_alive", AsyncMock(return_value=True)),
            patch.object(bridge, "_cc_fresh_hook_state", Mock(return_value=None)),
            patch.object(bridge, "_tmux_run", tmux_run),
            patch.object(bridge, "_log_event", Mock()),
        ):
            result = await bridge._cc_interrupt_core(name)

        self.assertFalse(result["interrupted"])
        self.assertTrue(result["stale_turn"])
        self.assertEqual(result["attempts"], 0)
        tmux_run.assert_not_awaited()

    async def test_generation_change_after_escape_marks_stale_and_stops_retry(self):
        name = "Ops"
        bridge._CC_TURN_GEN[name] = 7

        async def send_escape(*_args, **_kwargs):
            bridge._CC_TURN_GEN[name] += 1
            return 0, "", ""

        capture_pane = AsyncMock(return_value="")

        with (
            patch.object(bridge, "_tmux_alive", AsyncMock(return_value=True)),
            patch.object(bridge, "_cc_fresh_hook_state", Mock(return_value=None)),
            patch.object(bridge, "_tmux_run", AsyncMock(side_effect=send_escape)) as tmux_run,
            patch.object(bridge, "_cc_capture_pane_fresh", capture_pane),
            patch.object(bridge.asyncio, "sleep", AsyncMock(return_value=None)),
            patch.object(bridge, "_log_event", Mock()),
        ):
            result = await bridge._cc_interrupt_core(name)

        self.assertFalse(result["interrupted"])
        self.assertTrue(result["stale_turn"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(tmux_run.await_count, 1)
        capture_pane.assert_not_awaited()


class TestAppContractSmoke(unittest.IsolatedAsyncioTestCase):
    async def test_health_capabilities_and_message_dry_run(self):
        health = await bridge.health()
        self.assertTrue(health["ok"])

        caps = await bridge.capabilities(FakeAppRequest("/capabilities"))
        self.assertIn("message_dry_run", caps["features"])
        self.assertIn("/app/v1/messages", caps["endpoints"])

        request = FakeAppRequest(
            "/app/v1/messages",
            {"session": "xcash", "content": "dry smoke", "dry_run": True},
        )
        with patch.object(bridge, "_log_event", Mock()):
            response = await bridge.app_post_message(request)

        body = b""
        async for chunk in response.body_iterator:
            body += chunk.encode("utf-8") if isinstance(chunk, str) else chunk
        text = body.decode("utf-8")
        self.assertIn("dry-run ok", text)
        self.assertIn("data: [DONE]", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
