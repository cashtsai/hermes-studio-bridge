"""Shared iOS/Android live-update event contract tests."""

import _isolation  # noqa: F401

import unittest

from live_update_event import (LiveUpdateEventError, apns_content_state,
                               LiveUpdateCursor, make_event, next_seq)


class LiveUpdateEventTest(unittest.TestCase):
    def test_event_has_shared_state_and_terminal_semantics(self):
        event = make_event("codex:t1", 42, "running", phase="執行工具",
                           updated_at=1783291200)
        self.assertEqual(event["version"], 1)
        self.assertFalse(event["terminal"])
        self.assertEqual(event["seq"], 42)

        done = make_event("codex:t1", 43, "completed", phase="完成",
                          updated_at=1783291201)
        self.assertTrue(done["terminal"])

    def test_non_terminal_cannot_be_marked_terminal(self):
        with self.assertRaises(LiveUpdateEventError):
            make_event("codex:t1", 1, "running", terminal=True)

    def test_rejects_invalid_state_and_sequence(self):
        with self.assertRaises(LiveUpdateEventError):
            make_event("codex:t1", 0, "running")
        with self.assertRaises(LiveUpdateEventError):
            make_event("codex:t1", 1, "paused")

    def test_apns_state_keeps_ios_fields_and_shared_fields(self):
        event = make_event("codex:t1", 7, "streaming", phase="回覆中",
                           updated_at=1783291200)
        state = apns_content_state(event, busy=True, phase_label="回覆中")
        self.assertTrue(state["busy"])
        self.assertEqual(state["phaseLabel"], "回覆中")
        self.assertEqual(state["sessionID"], "codex:t1")
        self.assertEqual(state["state"], "streaming")
        self.assertFalse(state["terminal"])

    def test_sequence_is_monotonic_even_with_same_floor(self):
        first = next_seq("session-test", floor=100)
        second = next_seq("session-test", floor=100)
        self.assertGreater(second, first)

    def test_cursor_ignores_duplicate_and_out_of_order_events(self):
        cursor = LiveUpdateCursor("codex:t1")
        self.assertTrue(cursor.accept(make_event(
            "codex:t1", 2, "streaming", phase="回覆中", updated_at=1783291200)))
        self.assertFalse(cursor.accept(make_event(
            "codex:t1", 2, "running", phase="舊事件", updated_at=1783291201)))
        self.assertFalse(cursor.accept(make_event(
            "codex:t1", 1, "running", phase="更舊", updated_at=1783291202)))
        self.assertEqual(cursor.snapshot()["state"], "streaming")
        self.assertEqual(cursor.snapshot()["seq"], 2)

    def test_cursor_keeps_terminal_state(self):
        cursor = LiveUpdateCursor("codex:t2")
        self.assertTrue(cursor.accept(make_event(
            "codex:t2", 1, "completed", phase="完成", updated_at=1783291200)))
        self.assertFalse(cursor.accept(make_event(
            "codex:t2", 2, "running", phase="遲到", updated_at=1783291201)))
        self.assertTrue(cursor.snapshot()["terminal"])

    def test_cursor_rejects_other_session(self):
        cursor = LiveUpdateCursor("codex:t3")
        with self.assertRaises(LiveUpdateEventError):
            cursor.accept(make_event("codex:other", 1, "running",
                                     updated_at=1783291200))

    def test_cursor_rejects_unknown_version(self):
        cursor = LiveUpdateCursor("codex:t4")
        event = make_event("codex:t4", 1, "running", updated_at=1783291200)
        event["version"] = 2
        with self.assertRaises(LiveUpdateEventError):
            cursor.accept(event)


if __name__ == "__main__":
    unittest.main(verbosity=2)
