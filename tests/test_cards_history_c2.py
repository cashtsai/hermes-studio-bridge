"""C2 歷史拉取契約(grok-build steal #4;APP_BRIDGE_CONTRACT.md §7.1)。

釘四件事:
  1. 負 offset 取尾(Python slicing 語義)+ window_start/total_cards/
     has_more/has_more_after。
  2. turn_index 按使用者回合切片(含負值倒數、裁切後的無主第 0 片、
     超範圍 IndexError→端點 409)。
  3. has_more 升格為 snapshot 所有模式的一等欄位。
  4. 端點參數互斥(offset/turn_index vs before_seq/before_id)與既有
     路徑相容(before_seq/paged 回應不變形)。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import os
import sys
import tempfile
import unittest
import unittest.mock

_TMP = tempfile.mkdtemp(prefix="c2-cards-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge      # noqa: E402
import carddigest  # noqa: E402


def _store_with_turns(n_turns=3, cards_per_turn=4):
    """store:每回合 1 張 user 卡 + (cards_per_turn-1) 張 assistant 卡。"""
    store = carddigest.SessionCardStore(ring_max=512, cards_max=512)
    k = 0
    for t in range(n_turns):
        store.upsert_card(carddigest.make_card(
            f"u{t}", f"turn-{t}", "user", "message",
            {"text": f"問題 {t}", "fallback_text": f"問題 {t}"}, ts=1000.0 + k))
        k += 1
        for j in range(cards_per_turn - 1):
            store.upsert_card(carddigest.make_card(
                f"a{t}-{j}", f"turn-{t}", "assistant", "message",
                {"text": f"回 {t}-{j}", "fallback_text": f"回 {t}-{j}"},
                ts=1000.0 + k))
            k += 1
    return store


class TestNegativeOffsetTail(unittest.TestCase):
    def test_tail_window(self):
        store = _store_with_turns(3, 4)   # 12 張
        page = store.page(limit=50, offset=-5)
        self.assertEqual([c["id"] for c in page["cards"]],
                         ["a1-2", "u2", "a2-0", "a2-1", "a2-2"])
        self.assertEqual(page["window_start"], 7)
        self.assertEqual(page["total_cards"], 12)
        self.assertTrue(page["has_more"])
        self.assertFalse(page["has_more_after"])

    def test_tail_larger_than_store_clamps_to_zero(self):
        store = _store_with_turns(1, 3)
        page = store.page(limit=50, offset=-999)
        self.assertEqual(page["window_start"], 0)
        self.assertEqual(len(page["cards"]), 3)
        self.assertFalse(page["has_more"])

    def test_forward_offset_paging(self):
        store = _store_with_turns(3, 4)
        page = store.page(limit=4, offset=4)
        self.assertEqual([c["id"] for c in page["cards"]],
                         ["u1", "a1-0", "a1-1", "a1-2"])
        self.assertTrue(page["has_more"])
        self.assertTrue(page["has_more_after"])

    def test_latest_seq_and_status_shape_preserved(self):
        store = _store_with_turns(1, 2)
        store.set_status({"state": "idle"})
        page = store.page(limit=10, offset=-10)
        self.assertEqual(page["latest_seq"], store.seq)
        self.assertEqual(page["status"], {"state": "idle"})


class TestTurnSlicing(unittest.TestCase):
    def test_slices_follow_user_boundaries(self):
        store = _store_with_turns(3, 4)
        slices = store.turn_slices()
        self.assertEqual(len(slices), 3)
        self.assertTrue(all(s[0].startswith("u") for s in slices))

    def test_orphan_head_is_its_own_slice(self):
        store = carddigest.SessionCardStore(ring_max=64, cards_max=64)
        store.upsert_card(carddigest.make_card(
            "a-orphan", "t0", "assistant", "message",
            {"text": "裁切殘留", "fallback_text": "x"}, ts=1.0))
        store.upsert_card(carddigest.make_card(
            "u0", "t1", "user", "message",
            {"text": "問", "fallback_text": "問"}, ts=2.0))
        slices = store.turn_slices()
        self.assertEqual([s[0] for s in slices], ["a-orphan", "u0"])

    def test_negative_turn_index_counts_from_latest(self):
        store = _store_with_turns(3, 4)
        page = store.page(limit=50, turn_index=-1)
        self.assertEqual([c["id"] for c in page["cards"]],
                         ["u2", "a2-0", "a2-1", "a2-2"])
        self.assertEqual(page["turn_index"], 2)
        self.assertEqual(page["turn_count"], 3)
        self.assertTrue(page["has_more"], "前面還有兩個回合")
        first = store.page(limit=50, turn_index=0)
        self.assertFalse(first["has_more"])

    def test_out_of_range_raises(self):
        store = _store_with_turns(2, 3)
        with self.assertRaises(IndexError):
            store.page(turn_index=5)
        with self.assertRaises(IndexError):
            store.page(turn_index=-3)


class TestSnapshotHasMore(unittest.TestCase):
    def test_has_more_first_class_in_all_modes(self):
        store = _store_with_turns(3, 4)
        self.assertTrue(store.snapshot(limit=5)["has_more"])
        self.assertFalse(store.snapshot(limit=100)["has_more"])
        paged = store.snapshot(limit=5, paged=True)
        self.assertTrue(paged["has_more"])
        self.assertIsNotNone(paged["older_cursor"])


class TestEndpointWiring(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        self.client = TestClient(bridge.app)
        self.auth = {"Authorization": f"Bearer {bridge.BRIDGE_TOKEN}"}
        self._store = _store_with_turns(3, 4)
        self._store.media_session_id = ""

        async def fake_store(_sid):
            return self._store
        self._patch = unittest.mock.patch.object(
            bridge, "_v2_card_store", fake_store)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _get(self, qs: str):
        return self.client.get(f"/app/v2/sessions/cc2:x/cards?{qs}",
                               headers=self.auth)

    def test_tail_fetch_over_http(self):
        r = self._get("offset=-3&limit=3")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual([c["id"] for c in body["cards"]],
                         ["a2-0", "a2-1", "a2-2"])
        self.assertIn("durable_seq", body, "持久游標欄位不因新視窗消失")

    def test_turn_fetch_over_http(self):
        r = self._get("turn_index=-1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["turn_count"], 3)

    def test_turn_index_gone_is_409(self):
        r = self._get("turn_index=99")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "TURN_INDEX_GONE")

    def test_mutual_exclusion(self):
        self.assertEqual(self._get("offset=-3&before_seq=5").status_code, 400)
        self.assertEqual(self._get("offset=-3&turn_index=-1").status_code, 400)
        self.assertEqual(self._get("turn_index=-1&history_page=true").status_code, 400)

    def test_legacy_paths_unchanged(self):
        r = self._get("limit=5")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["cards"]), 5)
        self.assertIn("has_more", body)
        r2 = self._get("limit=5&history_page=true")
        self.assertEqual(r2.status_code, 200)
        self.assertIn("older_cursor", r2.json())


if __name__ == "__main__":
    unittest.main()
