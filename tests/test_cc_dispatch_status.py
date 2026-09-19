"""CC accepted-input status must stay visible across the idle/background gap."""
import _isolation  # noqa: F401

import os
import sys
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="cc-dispatch-status-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
import carddigest  # noqa: E402


class CcDispatchStatusTest(unittest.TestCase):
    def _store(self, client_id: str):
        store = carddigest.SessionCardStore()
        accepted = store.upsert_card(carddigest.make_input_accepted_card(
            "claude_code", client_id, "run the build"))
        store.dispatch_pending = True
        store.dispatch_started_at = time.time() - 10
        store.dispatch_card_seq = accepted["seq"]
        return store

    def test_idle_gap_is_dispatching_not_idle(self):
        status = bridge._cc_dispatch_status(
            self._store("c1"), {"busy": False, "mode": "normal", "prompt": None})
        self.assertEqual(status["phase"], "dispatching")
        self.assertEqual(status["label"], "已送達，等待 Claude Code 開始…")

    def test_long_idle_gap_explains_background_wait(self):
        store = self._store("c2")
        store.dispatch_started_at = time.time() - bridge._CC_DISPATCH_LONG_WAIT_SECS - 1
        status = bridge._cc_dispatch_status(
            store, {"busy": False, "mode": None, "prompt": None})
        self.assertEqual(status["phase"], "dispatching")
        self.assertEqual(status["label"], "等待背景工作結果…")

    def test_later_card_clears_waiting_state(self):
        store = self._store("c3")
        store.upsert_card(carddigest.make_card(
            "tool-1", "", "assistant", "tool_result",
            {"text": "ok", "fallback_text": "ok"}))
        self.assertIsNone(bridge._cc_dispatch_status(
            store, {"busy": False, "mode": None, "prompt": None}))
        self.assertFalse(store.dispatch_pending)


if __name__ == "__main__":
    unittest.main(verbosity=2)
