import _isolation  # noqa: F401
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge
import carddigest
from line_sessions import ReplyTracker


def event(mid, parent, kind, text=""):
    row = {"uuid": mid, "parentUuid": parent, "type": kind}
    if kind in {"user", "assistant"}:
        row["message"] = {"role": kind, "content": text if kind == "user" else [{"type": "text", "text": text}]}
    return row


class NativeAncestryTests(unittest.TestCase):
    def digest(self, store, rows):
        with patch.object(bridge, "_schedule_media_capture"), \
             patch.object(bridge, "_schedule_media_attach"):
            bridge._cc_digest_lines(store, [json.dumps(row) for row in rows], "test.jsonl", 0)

    def tracker(self, store):
        user = next(c for c in store.cards.values() if c["body"].get("native_message_id") == "u")
        user["body"]["client_id"] = "line-test"
        store.upsert_card(user)
        store.set_status({"busy": False, "phase": "idle"})
        tracker = ReplyTracker("line-test", native_cc=True)
        for item in store.since(0):
            tracker.feed(item)
        return tracker

    def test_hidden_attachments_across_tail_batches_preserve_raw_parent(self):
        store = carddigest.SessionCardStore()
        self.digest(store, [event("u", "", "user", "question"), event("x", "u", "attachment")])
        self.digest(store, [event("y", "x", "attachment"), event("a", "y", "assistant", "answer")])
        answer = next(c for c in store.cards.values() if c["role"] == "assistant")
        self.assertEqual(answer["body"]["native_parent_id"], "y")
        self.assertEqual(answer["body"]["native_link_parent_id"], "u")
        self.assertEqual(len(store.cards), 2)
        self.assertEqual(self.tracker(store).result(), "answer")

    def test_intervening_user_is_a_boundary(self):
        store = carddigest.SessionCardStore()
        self.digest(store, [event("u", "", "user", "question"),
            event("x", "u", "attachment"), event("other", "x", "user", "other question"),
            event("y", "other", "progress"), event("a", "y", "assistant", "other answer")])
        self.assertIsNone(self.tracker(store).result())

    def test_missing_ancestor_does_not_guess(self):
        store = carddigest.SessionCardStore()
        self.digest(store, [event("u", "", "user", "question"),
            event("x", "missing", "attachment"), event("a", "x", "assistant", "unproven")])
        self.assertIsNone(self.tracker(store).result())

    def test_unknown_nonrendered_and_user_events_are_not_skipped(self):
        ancestry = carddigest.CCNativeAncestry()
        for kind in ("user", "assistant", "new-unknown-type"):
            ancestry.observe(event("x", "u", kind), rendered=False)
            self.assertEqual(ancestry.parent("x"), "x")

    def test_rendered_metadata_is_not_skipped(self):
        ancestry = carddigest.CCNativeAncestry()
        ancestry.observe(event("x", "u", "system"), rendered=True)
        self.assertEqual(ancestry.parent("x"), "x")

    def test_cycles_depth_and_eviction_fail_closed(self):
        ancestry = carddigest.CCNativeAncestry(limit=3)
        ancestry.observe(event("x", "y", "attachment"), False)
        ancestry.observe(event("y", "x", "attachment"), False)
        self.assertEqual(ancestry.parent("x"), "x")
        ancestry.observe(event("z", "u", "attachment"), False)
        ancestry.observe(event("w", "z", "attachment"), False)
        self.assertEqual(len(ancestry.hidden), 3)
        self.assertEqual(ancestry.parent("x"), "x")
        self.assertEqual(ancestry.parent("w"), "u")
        ancestry = carddigest.CCNativeAncestry()
        for i in range(300):
            ancestry.observe(event(str(i), str(i + 1), "attachment"), False)
        self.assertEqual(ancestry.parent("0"), "0")


if __name__ == "__main__":
    unittest.main()
