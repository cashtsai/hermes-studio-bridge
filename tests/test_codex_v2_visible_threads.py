"""The v2 Codex list must not let guardian threads hide operator sessions."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="codex-v2-list-canon-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class FakeCodexApp:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    async def call(self, method, params, timeout):
        self.calls.append((method, dict(params), timeout))
        return self.pages.pop(0)


class FailingCodexApp:
    def __init__(self):
        self.loaded_threads = set()
        self.token_usage = {}
        self.thread_errors = {}
        self.last_event_at = {}
        self.provider_status = {}

    async def call(self, method, params, timeout):
        raise RuntimeError("codex app-server unavailable")

    def note_provider_status(self, thread_id, status):
        # 真的那顆會把 provider 回報的 ThreadStatus 收進 provider_status;
        # 這個 stub 模擬「app-server 整顆掛掉」,有這支、不炸就夠。
        return None

    def is_active(self, thread_id):
        return False

    def is_server_alive(self):
        return False

    def runtime_status(self, thread_id, provider_status):
        return provider_status or "idle"

    def pending_approval_for_thread(self, thread_id):
        return None

    def thread_lock_info(self, thread_id):
        # thread-store 寫入鎖(fix/codex-thread-store-conflict):summary 疊加
        # 會問這一個。這個 stub 模擬的是「app-server 整顆掛掉」,不是鎖。
        return None

    def thread_lock_retry_due(self, thread_id):
        return False


class TestCodexV2VisibleThreads(unittest.IsolatedAsyncioTestCase):
    async def test_paginates_past_guardian_burst(self):
        child_rows = [
            {"id": f"child-{i}", "source": {"subagent": {"other": "guardian"}}}
            for i in range(40)
        ]
        xcash = {
            "id": "xcash-thread",
            "name": "XCash",
            "source": "exec",
            "cwd": "/Users/xcash",
        }
        app = FakeCodexApp([
            {"data": child_rows, "nextCursor": "next-page"},
            {"data": [xcash], "nextCursor": None},
        ])

        with patch.object(bridge, "CODEX_APP", app):
            rows = await bridge._codex_v2_visible_threads(1)

        self.assertEqual(rows, [xcash])
        self.assertEqual(len(app.calls), 2)
        self.assertEqual(app.calls[0][1]["limit"], 40)
        self.assertEqual(app.calls[1][1]["cursor"], "next-page")

    async def test_uses_last_v2_list_when_provider_is_temporarily_down(self):
        xcash = {
            "id": "xcash-thread",
            "name": "XCash",
            "source": "exec",
            "cwd": "/Users/xcash",
            "status": "idle",
        }
        with patch.object(bridge, "CODEX_APP", FailingCodexApp()), patch.object(
            bridge, "_CODEX_V2_VISIBLE_CACHE", [xcash]
        ):
            rows = await bridge._codex_v2_visible_threads(20)

        self.assertEqual(rows, [xcash])

    async def test_legacy_list_returns_stale_cache_instead_of_empty(self):
        xcash = {
            "id": "xcash-thread",
            "name": "XCash",
            "source": "exec",
            "cwd": "/Users/xcash",
            "status": "idle",
        }
        cache_key = (False, "", False)
        with patch.object(bridge, "CODEX_APP", FailingCodexApp()), patch.object(
            bridge, "_CODEX_SESSION_LIST_CACHE",
            {cache_key: {"data": [xcash], "hidden_children": 0}},
        ), patch.object(bridge, "_check_auth", return_value=None):
            result = await bridge.codex_sessions(object(), limit=40)

        self.assertTrue(result["stale"])
        self.assertEqual(result["sessions"][0]["thread_id"], "xcash-thread")


if __name__ == "__main__":
    unittest.main(verbosity=2)
