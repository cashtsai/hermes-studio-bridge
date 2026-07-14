"""Explicit CC login recovery endpoint."""

import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

_TMP = tempfile.mkdtemp(prefix="cc-login-canon-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class TestCCSessionLogin(unittest.IsolatedAsyncioTestCase):
    async def test_login_routes_to_ccsess_without_provider_fallback(self):
        runner = AsyncMock(return_value="已送出 /login 到 Ops")
        with (
            patch.object(bridge, "_check_auth"),
            patch.object(bridge, "_cc_conf_rows", return_value=[("Ops", "/tmp/ops", "1")]),
            patch.object(bridge, "_run_ccsess", runner),
        ):
            result = await bridge.cc_session_login("Ops", Mock())

        runner.assert_awaited_once_with("login", "Ops")
        self.assertTrue(result["ok"])
        self.assertEqual(result["session"], "Ops")
        self.assertEqual(result["action"], "login")

    async def test_login_rejects_unknown_session(self):
        with (
            patch.object(bridge, "_check_auth"),
            patch.object(bridge, "_cc_conf_rows", return_value=[]),
        ):
            with self.assertRaises(Exception) as raised:
                await bridge.cc_session_login("missing", Mock())

        self.assertEqual(getattr(raised.exception, "status_code", None), 404)


if __name__ == "__main__":
    unittest.main()
