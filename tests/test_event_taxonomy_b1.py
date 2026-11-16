"""B1 事件三分類紀律(grok-build steal #2)。

釘三件事:
  1. 登記表完整性 —— 掃源碼所有事件發射點(_event_append 字面型別、
     _mirror_milestone 的 sink 型別、SessionCardStore._push 字面型別、
     手工組裝的 wire 信封),每個型別都必須在登記表裡;**新增事件型別
     漏登記 → 這裡紅**。
  2. seq 蓋章單一 chokepoint —— event_log 的 INSERT 只准出現在
     _event_append 本體;persisted 事件必有全域遞增 seq。
  3. transient 絕不蓋全域 seq —— ring 信封的 durable_seq 只有里程碑影子
     才有;ping/resync/非終態 delta 一律 None;未登記型別 runtime 留痕
     (fail-open)而 emit_transient 直接拒收。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import os
import re
import sqlite3
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="b1-taxonomy-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import bridge      # noqa: E402  (import 觸發 _canon_init → 建表)
import carddigest  # noqa: E402


def _src(name: str) -> str:
    with open(os.path.join(_ROOT, name), encoding="utf-8") as f:
        return f.read()


class TestRegistryCompleteness(unittest.TestCase):
    """掃源碼發射點 → 型別必須登記。新事件型別漏登記在這裡紅。"""

    def test_event_append_literals_registered(self):
        # _event_append(session, "literal", …) 的字面型別
        types = set(re.findall(
            r"_event_append\(\s*[^,]+,\s*\"([a-z_.]+)\"", _src("bridge.py")))
        self.assertTrue(types, "找不到任何 _event_append 字面型別(regex 壞了?)")
        allowed = set(bridge.EVENT_PERSISTED) | set(bridge.EVENT_PERSIST_ONLY)
        self.assertLessEqual(
            types, allowed,
            f"未登記的 event_log 型別:{types - allowed} — "
            "登記進 bridge.EVENT_PERSISTED / EVENT_PERSIST_ONLY(標理由)")

    def test_milestone_sink_literals_registered(self):
        # _mirror_milestone 經 MILESTONE_SINK 落地的型別(sink(sid, "literal", …))
        types = set(re.findall(
            r"sink\(\s*sid,\s*\"([a-z_.]+)\"", _src("carddigest.py")))
        self.assertTrue(types)
        self.assertLessEqual(
            types, set(bridge.EVENT_PERSISTED),
            f"里程碑鏡射出現未登記 persisted 型別:{types - set(bridge.EVENT_PERSISTED)}")

    def test_ring_push_literals_registered(self):
        types = set(re.findall(
            r"_push\(\s*\"([a-z_.]+)\"", _src("carddigest.py")))
        self.assertTrue(types)
        self.assertLessEqual(
            types, set(carddigest.RING_TRANSIENT),
            f"未登記的 ring transient 型別:{types - set(carddigest.RING_TRANSIENT)}")

    def test_handbuilt_wire_envelopes_registered(self):
        # 手工組裝(不經 _push)的 wire 信封型別:resync / ping。
        for t in ("resync", "ping"):
            self.assertIn(t, carddigest.RING_TRANSIENT)

    def test_no_type_registered_on_both_rails_without_shadow_note(self):
        # persisted 與 transient 名字重疊只允許「ring 活流 + 持久影子」那三種
        # (card.upsert 的影子改名 card.milestone,不重疊)。
        overlap = set(carddigest.RING_TRANSIENT) & (
            set(bridge.EVENT_PERSISTED) | set(bridge.EVENT_PERSIST_ONLY))
        self.assertEqual(overlap, {"session.status", "turn"},
                         "軌道重疊的型別變了 — 想清楚影子關係再改這條測試")


class TestSingleChokepoint(unittest.TestCase):
    def test_event_log_insert_only_inside_event_append(self):
        src = _src("bridge.py")
        hits = [m.start() for m in re.finditer(r"INTO event_log", src)]
        # CREATE TABLE 那句不算寫入
        writes = [i for i in hits if "INSERT" in src[max(0, i - 40):i]]
        self.assertTrue(writes, "event_log 寫入點消失了?")
        fn = src.index("def _event_append(")
        # _event_append 函式結束 = 下一個模組層 def
        fn_end = src.index("\ndef ", fn + 10)
        for i in writes:
            self.assertTrue(fn < i < fn_end,
                            "event_log 的 INSERT 出現在 _event_append 之外 — "
                            "seq 蓋章必須單一 chokepoint")

    def test_persisted_events_get_global_monotonic_seq(self):
        s1 = bridge._event_append("b1-test-a", "session.status", {"state": "idle"})
        s2 = bridge._event_append("b1-test-b", "turn", {"state": "started"})
        s3 = bridge._event_append("b1-test-a", "turn", {"state": "ended"})
        self.assertTrue(0 < s1 < s2 < s3, f"全域 seq 不遞增:{(s1, s2, s3)}")
        rows = bridge._event_since("b1-test-a", 0)
        self.assertEqual([r["seq"] for r in rows], [s1, s3])
        for r in rows:
            self.assertIn(r["type"], set(bridge.EVENT_PERSISTED))

    def test_unregistered_persisted_type_is_loud_but_fail_open(self):
        calls = []
        orig = bridge._log_event
        bridge._log_event = lambda ev, **kw: calls.append((ev, kw))
        try:
            seq = bridge._event_append("b1-test-c", "bogus.type", {"x": 1})
        finally:
            bridge._log_event = orig
        self.assertGreater(seq, 0, "fail-open:未登記型別仍要寫入(但要留痕)")
        self.assertIn("event_type_unregistered", [c[0] for c in calls])
        # 同型別第二次不再洗版
        calls.clear()
        bridge._log_event = lambda ev, **kw: calls.append((ev, kw))
        try:
            bridge._event_append("b1-test-c", "bogus.type", {"x": 2})
        finally:
            bridge._log_event = orig
        self.assertNotIn("event_type_unregistered", [c[0] for c in calls])


class TestTransientNeverStampsSeq(unittest.TestCase):
    def _store(self, sink_calls: list) -> "carddigest.SessionCardStore":
        store = carddigest.SessionCardStore(ring_max=64, cards_max=64)
        store.media_session_id = "test:b1"
        carddigest.MILESTONE_SINK = (
            lambda sid, etype, payload, ext: sink_calls.append(etype) or 7)
        self.addCleanup(lambda: setattr(carddigest, "MILESTONE_SINK", None))
        return store

    def test_streaming_delta_has_no_durable_seq(self):
        sink_calls = []
        store = self._store(sink_calls)
        ev = store.upsert_card(carddigest.make_card(
            "c1", "t1", "assistant", "message", {"text": "hi", "fallback_text": "hi"},
            final=False))
        self.assertIsNone(ev["durable_seq"], "非終態 delta 不准有持久影子")
        self.assertEqual(sink_calls, [])
        # 終態卡才落影子(走單一 chokepoint)
        ev2 = store.upsert_card(carddigest.make_card(
            "c1", "t1", "assistant", "message", {"text": "hi!", "fallback_text": "hi!"},
            final=True))
        self.assertEqual(ev2["durable_seq"], 7)
        self.assertEqual(sink_calls, ["card.milestone"])

    def test_ping_and_resync_never_persist(self):
        sink_calls = []
        store = self._store(sink_calls)
        ping = store.ping()
        self.assertIsNone(ping["durable_seq"])
        self.assertEqual(store.seq, 0, "ping 不佔 ring seq")
        self.assertIsNone(store._mirror_milestone("resync", {"durable_seq": 1}))
        self.assertIsNone(store._mirror_milestone("ping", {}))
        self.assertEqual(sink_calls, [])

    def test_emit_transient_rejects_unregistered(self):
        store = carddigest.SessionCardStore(ring_max=8, cards_max=8)
        with self.assertRaises(ValueError):
            carddigest.emit_transient(store, "made.up", {})
        ev = carddigest.emit_transient(store, "sessions.changed",
                                       {"upserted": [], "removed": []})
        self.assertEqual(ev["type"], "sessions.changed")
        self.assertIsNone(ev["durable_seq"], "transient 絕不帶持久 id")

    def test_push_unregistered_leaves_a_trace(self):
        store = carddigest.SessionCardStore(ring_max=8, cards_max=8)
        carddigest.UNREGISTERED_RING_TYPES.clear()
        store._push("sneaky.type", {})
        self.assertIn("sneaky.type", carddigest.UNREGISTERED_RING_TYPES)


if __name__ == "__main__":
    unittest.main()
