"""Codex archive endpoint must use the matching app-server lifecycle verb."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="codex-archive-canon-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class FakeRequest:
    def __init__(self, archived):
        self.archived = archived

    async def json(self):
        return {"archived": self.archived}


class FakeCodexApp:
    def __init__(self, terminal_method):
        self.terminal_method = terminal_method
        self.calls = []

    async def call(self, method, params, timeout):
        self.calls.append((method, params, timeout))
        if method != self.terminal_method:
            raise RuntimeError("unsupported")
        return {"ok": True}


class TestCodexSessionArchive(unittest.IsolatedAsyncioTestCase):
    async def test_unarchive_falls_back_to_official_unarchive_method(self):
        app = FakeCodexApp("thread/unarchive")
        with patch.object(bridge, "CODEX_APP", app), patch.object(
            bridge, "_check_auth", return_value=None
        ):
            result = await bridge.codex_session_archive(
                "thread-123", FakeRequest(False)
            )

        self.assertEqual(result["method"], "thread/unarchive")
        self.assertEqual(
            [method for method, _, _ in app.calls],
            ["thread/archive/set", "thread/setArchived", "thread/unarchive"],
        )
        self.assertNotIn("thread/archive", [method for method, _, _ in app.calls])

    async def test_archive_falls_back_to_official_archive_method(self):
        app = FakeCodexApp("thread/archive")
        with patch.object(bridge, "CODEX_APP", app), patch.object(
            bridge, "_check_auth", return_value=None
        ):
            result = await bridge.codex_session_archive(
                "thread-456", FakeRequest(True)
            )

        self.assertEqual(result["method"], "thread/archive")
        self.assertEqual(
            [method for method, _, _ in app.calls],
            ["thread/archive/set", "thread/setArchived", "thread/archive"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
