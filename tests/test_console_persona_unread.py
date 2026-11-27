"""Persona metadata and scoped receipts share the existing durable cursor."""
import _isolation  # noqa: F401
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge
from fastapi.testclient import TestClient


class PersonaReadTest(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(bridge, "CANON_DB", os.path.join(tmp, "canon.db")))
        self.enterContext(patch.object(bridge, "PERSONAS", {"reader": ("Reader", tmp)}))
        self.enterContext(patch.object(bridge, "_PERSONA_READ_STATE_CACHE", {}))
        self.enterContext(patch.object(bridge, "_CONSOLE_GRANTS", {}))
        self.enterContext(patch.object(bridge, "_console_uses_relay_policy", return_value=False))
        self.token = "pdev-unread-test"
        self.enterContext(patch.dict(bridge._DEVICE_TOKENS, {self.token: {
            "scope": "console-remote", "last_seen": time.time(), "expires": time.time() + 600}}))
        bridge._canon_init()
        self.http = TestClient(bridge.app)
        self.addCleanup(self.http.close)
        self.headers = {"Authorization": "Bearer " + self.token}

    def message(self, mid, ts, role="assistant"):
        return bridge._event_append("reader", "message.upsert", {"message": {
            "id": mid, "ts": ts, "role": role, "content": "private message"}})

    def grant(self):
        rows = [{"id": "hermes:reader", "provider": "hermes", "capabilities": ["replay", "follow"],
                 "read_state": bridge._persona_read_state("reader")}]
        bridge._console_remote_list(rows, self.token, time.monotonic(), [])
        return rows[0]

    def post(self, **body):
        return self.http.post("/app/v2/sessions/hermes:reader/read", headers=self.headers,
                              json={"last_read_ts": 100, **body})

    def test_content_free_snapshot_and_late_edit(self):
        first = self.message("first", 100)
        latest = self.message("latest", 200)
        self.message("self", 300, "user")
        self.message("tool", 400, "tool")
        self.message("first", 100)  # Old edit must not replace the latest inbound.
        snapshot = bridge._persona_read_state("reader")
        self.assertEqual(snapshot["incoming_seq"], latest)
        self.assertEqual(snapshot["incoming_ts"], 200)
        self.assertEqual(snapshot["last_message_ts"], 300)
        self.assertNotIn("private", str(snapshot))
        snapshot["last_read_seq"] = first
        self.assertEqual(bridge._persona_read_state("reader")["last_read_seq"], 0)

    def test_remote_receipt_is_persisted_and_idempotent(self):
        self.message("first", 100)
        self.assertIn("read_cursor", self.grant()["capabilities"])
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["global"]["last_read_ts"], 100)
        self.assertEqual(response.json()["read_state"]["last_read_ts"], 100)
        version = response.json()["read_state"]["version"]
        self.assertFalse(self.post().json()["moved"])
        self.assertEqual(bridge._persona_read_state("reader")["version"], version)
        self.assertEqual(len(bridge._read_cursor_rows("reader")), 1)

    def test_receipt_requires_current_grant_and_cannot_select_another_session(self):
        self.message("first", 100)
        self.assertEqual(self.post().status_code, 403)
        self.grant()
        for body in [{"session": "someone-else"}, {"last_read_ts": 10000}, {"last_read_seq": 900}]:
            self.assertEqual(self.post(**body).status_code, 400)
        self.assertEqual(self.http.post("/app/v2/read", headers=self.headers,
                                       json={"session": "reader"}).status_code, 403)
        self.assertEqual(self.http.get("/app/v2/events", headers=self.headers).status_code, 403)
        self.assertEqual(bridge._read_cursor_rows("reader"), [])

    def test_timestamp_read_does_not_clear_newer_message(self):
        self.message("first", 100)
        self.message("new", 200)
        self.grant()
        self.assertEqual(self.post().status_code, 200)
        snapshot = bridge._persona_read_state("reader")
        self.assertGreater(snapshot["incoming_ts"], snapshot["last_read_ts"])


if __name__ == "__main__":
    unittest.main()
