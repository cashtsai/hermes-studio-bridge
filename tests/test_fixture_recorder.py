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


class TestDigestFixtureRecorder(unittest.TestCase):
    """POCKET_RECORD_FIXTURE=**目錄** → digest 入口 per-thread 錄製。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="digest-rec-", dir=_TMP)

    @staticmethod
    def _frames(path):
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_cx_feed_records_frames_verbatim_per_thread(self):
        frames = [
            ("turn/started", {"threadId": "t-rec-1", "turn": {"id": "turn-1"}}),
            ("item/agentMessage/delta",
             {"threadId": "t-rec-1", "itemId": "i-1", "delta": "你好"}),
            ("turn/completed", {"threadId": "t-rec-1", "turn": {"id": "turn-1"}}),
        ]
        with patch.dict(os.environ, {"POCKET_RECORD_FIXTURE": self.dir}):
            for method, params in frames:
                bridge._cx_cards_feed(method, params)
        got = self._frames(os.path.join(self.dir, "t-rec-1.jsonl"))
        self.assertEqual(got, [{"method": m, "params": p} for m, p in frames],
                         "jsonl 內容要與餵進去的幀逐幀相等")

    def test_threads_are_split_into_separate_files(self):
        with patch.dict(os.environ, {"POCKET_RECORD_FIXTURE": self.dir}):
            bridge._cx_cards_feed("turn/started", {"threadId": "t-a"})
            bridge._cx_cards_feed("turn/started", {"threadId": "t-b"})
        self.assertEqual(
            sorted(p for p in os.listdir(self.dir) if p.endswith(".jsonl")),
            ["t-a.jsonl", "t-b.jsonl"])

    def test_unset_env_writes_nothing(self):
        with patch.dict(os.environ, {"POCKET_RECORD_FIXTURE": ""}):
            bridge._cx_cards_feed("turn/started", {"threadId": "t-quiet"})
            bridge._record_digest_fixture_frame("t-quiet", "turn/started", {})
        self.assertEqual(os.listdir(self.dir), [])

    def test_file_mode_path_does_not_trigger_digest_recorder(self):
        """env 指到檔案(wire 模式)時,digest 鉤必須完全缺席。"""
        path = os.path.join(self.dir, "wire.jsonl")
        with patch.dict(os.environ, {"POCKET_RECORD_FIXTURE": path}):
            bridge._record_digest_fixture_frame("t-x", "turn/started", {})
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "t-x.jsonl")))

    def test_directory_mode_silences_wire_recorder(self):
        """反向:env 指到目錄時,wire recorder 讓位(不逐幀刷失敗 log)。"""
        with patch.dict(os.environ, {"POCKET_RECORD_FIXTURE": self.dir}):
            bridge._record_fixture_frame(
                {"method": "turn/started", "params": {"threadId": "t-1"}})
        self.assertEqual(os.listdir(self.dir), [])

    def test_openclaw_feed_records_with_sanitized_session_key(self):
        payload = {"sessionKey": "agent:main:worker", "runId": "run-1",
                   "stream": "lifecycle", "data": {"phase": "start"}}
        with patch.dict(os.environ, {"POCKET_RECORD_FIXTURE": self.dir}):
            bridge._oc_events_feed("agent", dict(payload))
        got = self._frames(os.path.join(self.dir, "agent_main_worker.jsonl"))
        self.assertEqual(got, [{"method": "agent", "params": payload}])

    def test_gemini_update_records_session_update(self):
        key = "gm-rec-1"
        bridge._GM_TURNS["acp-rec-1"] = {"key": key, "cid": "c-1", "text": ""}
        update = {"sessionUpdate": "agent_message_chunk",
                  "content": {"text": "GEMINI_OK"}}
        try:
            with patch.dict(os.environ, {"POCKET_RECORD_FIXTURE": self.dir}):
                bridge._gm_on_update("acp-rec-1", dict(update))
        finally:
            bridge._GM_TURNS.pop("acp-rec-1", None)
        got = self._frames(os.path.join(self.dir, "gm-rec-1.jsonl"))
        self.assertEqual(got, [{"method": "session/update", "params": update}])

    def test_recorded_file_feeds_the_replay_runner(self):
        """錄出來的檔案直接餵 CodexThreadDigest —— 這就是 fixture 的用途。"""
        import carddigest
        from replay_fixture import load_jsonl
        with patch.dict(os.environ, {"POCKET_RECORD_FIXTURE": self.dir}):
            bridge._cx_cards_feed("turn/started",
                                  {"threadId": "t-run", "turn": {"id": "t1"}})
            bridge._cx_cards_feed("item/agentMessage/delta",
                                  {"threadId": "t-run", "itemId": "i-1",
                                   "delta": "OK"})
            bridge._cx_cards_feed("turn/completed",
                                  {"threadId": "t-run", "turn": {"id": "t1"}})
        d = carddigest.CodexThreadDigest()
        for f in load_jsonl(os.path.join(self.dir, "t-run.jsonl")):
            d.handle(f["method"], f.get("params") or {})
        self.assertFalse(d.busy)

    def test_secretlike_frame_is_rejected_whole(self):
        with patch.dict(os.environ, {"POCKET_RECORD_FIXTURE": self.dir}):
            bridge._record_digest_fixture_frame(
                "t-sec", "turn/started", {"api_key": "sk-aaaaaaaaaaaaaaaaaaaa"})
        self.assertFalse(os.path.exists(os.path.join(self.dir, "t-sec.jsonl")),
                         "secret-like 幀整幀拒收,不落盤")


if __name__ == "__main__":
    unittest.main()
