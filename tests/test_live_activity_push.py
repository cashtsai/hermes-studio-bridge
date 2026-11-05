"""ActivityKit APNs sender guardrails.

These tests exercise the bridge-side contract without contacting Apple.  They
pin the two operational guarantees that are easy to regress: high-frequency
phase updates are throttled, while terminal updates and stale 410 tokens are
handled immediately.
"""
import _isolation  # noqa: F401

import asyncio
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["APNS_LIVE_ACTIVITY_MIN_UPDATE_SECS"] = "8"

import bridge  # noqa: E402


class _Response:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    responses = []
    calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.responses.pop(0)


class LiveActivityPushTest(unittest.TestCase):
    def setUp(self):
        bridge._LIVE_ACTIVITY_LAST_SENT.clear()
        _FakeClient.calls.clear()
        _FakeClient.responses.clear()
        self._old = {
            "configured": bridge.apns_configured,
            "problem": bridge._push_http2_problem,
            "jwt": bridge._apns_jwt,
            "httpx": sys.modules.get("httpx"),
        }
        bridge.apns_configured = lambda: True
        bridge._push_http2_problem = lambda: None
        bridge._apns_jwt = lambda: "test-jwt"
        sys.modules["httpx"] = types.SimpleNamespace(AsyncClient=_FakeClient)
        bridge.t3code_runtime.register_live_activity(
            bridge.CANON_DB, "codex:test-thread", "activity-token",
            "com.pocketagent.kernel")

    def tearDown(self):
        bridge._LIVE_ACTIVITY_LAST_SENT.clear()
        bridge.apns_configured = self._old["configured"]
        bridge._push_http2_problem = self._old["problem"]
        bridge._apns_jwt = self._old["jwt"]
        if self._old["httpx"] is None:
            sys.modules.pop("httpx", None)
        else:
            sys.modules["httpx"] = self._old["httpx"]
        bridge.t3code_runtime.remove_live_activity(
            bridge.CANON_DB, "codex:test-thread")

    def test_busy_updates_are_throttled_but_terminal_update_is_sent(self):
        _FakeClient.responses.extend([_Response(200), _Response(200)])

        first = asyncio.run(bridge._apns_send_live_activity(
            "codex:test-thread", True, phase_label="工作中"))
        second = asyncio.run(bridge._apns_send_live_activity(
            "codex:test-thread", True, phase_label="整理中"))
        terminal = asyncio.run(bridge._apns_send_live_activity(
            "codex:test-thread", False, ended_label="已完成"))

        self.assertTrue(first["sent"])
        self.assertEqual(second["reason"], "throttled")
        self.assertTrue(terminal["sent"])
        self.assertEqual(len(_FakeClient.calls), 2)
        self.assertEqual(_FakeClient.calls[0]["headers"]["apns-push-type"],
                         "liveactivity")
        self.assertEqual(_FakeClient.calls[1]["json"]["aps"]["event"], "end")

    def test_410_removes_stale_activity_registration(self):
        _FakeClient.responses.append(_Response(410, "Unregistered"))

        result = asyncio.run(bridge._apns_send_live_activity(
            "codex:test-thread", True, phase_label="工作中"))

        self.assertFalse(result["sent"])
        self.assertEqual(result["status"], 410)
        self.assertIsNone(bridge.t3code_runtime.get_live_activity(
            bridge.CANON_DB, "codex:test-thread"))
        self.assertNotIn("codex:test-thread", bridge._LIVE_ACTIVITY_LAST_SENT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
