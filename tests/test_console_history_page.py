import unittest

from carddigest import SessionCardStore, make_card


class ConsoleHistoryPageTests(unittest.TestCase):
    def test_page_boundary_is_stable_across_edits_and_live_appends(self):
        store = SessionCardStore()
        def put(i, text):
            store.upsert_card(make_card(str(i), "t", "assistant", "text", {"text": text}, ts=i))
        for i in range(75):
            put(i, "original")
        latest = store.snapshot(limit=24, paged=True)
        self.assertEqual(latest["older_cursor"], "51")
        put(50, "edited after snapshot")
        put(75, "new")
        older = store.snapshot(limit=48, before_id=latest["older_cursor"], paged=True)
        self.assertEqual(older["cards"][-1]["body"]["text"], "edited after snapshot")
        first = store.snapshot(limit=48, before_id=older["older_cursor"], paged=True)
        self.assertIsNone(first["older_cursor"])
        self.assertEqual([c["id"] for p in (first, older, latest) for c in p["cards"]],
                         [str(i) for i in range(75)])
        self.assertNotIn("older_cursor", store.snapshot())
