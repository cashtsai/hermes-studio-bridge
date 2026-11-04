"""Opt-in Codex wire fixture recorder contract (T3Code #3)."""
import _isolation  # noqa: F401
import asyncio
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="fixture-recorder-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class TestFixtureRecorder(unittest.TestCase):
    def test_disabled_by_default(self):
        path = os.path.join(_TMP, "disabled.jsonl")
        with patch.dict(os.environ, {"POCKET_RECORD_FIXTURE": ""}):
            bridge._record_fixture_frame({"method": "turn/started", "params": {}})
        self.assertFalse(os.path.exists(path))

    def test_records_jsonl_with_owner_only_permissions(self):
        path = os.path.join(_TMP, "recorded", "session.jsonl")
        frame = {"method": "item/commandExecution/outputDelta",
                 "params": {"threadId": "t-1", "chunk": "ok"}, "id": 42}
        with patch.dict(os.environ, {"POCKET_RECORD_FIXTURE": path}):
            bridge._record_fixture_frame(frame)

        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600)
        with open(path, encoding="utf-8") as fh:
            recorded = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(recorded, [{
            "method": "item/commandExecution/outputDelta",
            "params": {"threadId": "t-1", "chunk": "ok"},
            "request_id": 42,
        }])

    def test_recording_failure_is_non_fatal(self):
        with patch.dict(os.environ, {"POCKET_RECORD_FIXTURE": "/dev/null/nope"}):
            bridge._record_fixture_frame({"method": "turn/started", "params": {}})

    def test_wire_dispatch_records_before_provider_handler(self):
        path = os.path.join(_TMP, "dispatch.jsonl")

        class FakeClient:
            seen = None

            async def _handle_server_message(self, msg):
                self.seen = msg

        raw = json.dumps({"method": "turn/started", "params": {
            "threadId": "t-1", "turn": {"id": "turn-1"}}})
        client = FakeClient()
        with patch.dict(os.environ, {"POCKET_RECORD_FIXTURE": path}):
            asyncio.run(bridge.CodexAppServerClient._dispatch_wire_message(
                client, raw))

        self.assertEqual(client.seen["method"], "turn/started")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.loads(fh.readline())["method"], "turn/started")


if __name__ == "__main__":
    unittest.main()
