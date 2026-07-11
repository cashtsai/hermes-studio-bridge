"""Sync engine P0〜P2 測試(docs/SYNC_ENGINE_REWRITE_PLAN_20260711.md)。

跑法(repo 慣例):POCKET_CANON_DB 指到 tmp 庫再 import bridge —
module import 會執行 _canon_init()(event_log/read_cursors 建表就在裡面),
所以 import 本身就是被測物之一。pytest 與 python3 直跑皆可。
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="syncengine-canon-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402  (import 觸發 _canon_init → 建表)


def _tables():
    con = sqlite3.connect(bridge.CANON_DB)
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    con.close()
    return names


class TestP0EventLog(unittest.TestCase):
    def test_table_created_and_init_idempotent(self):
        self.assertIn("event_log", _tables())
        bridge._canon_init()   # 第二次跑不炸 = 冪等
        self.assertIn("event_log", _tables())

    def test_append_returns_increasing_seq(self):
        s1 = bridge._event_append("p0-sess", "message.upsert", {"n": 1})
        s2 = bridge._event_append("p0-sess", "message.upsert", {"n": 2})
        self.assertGreater(s1, 0)
        self.assertGreater(s2, s1)

    def test_since_filters_by_session_and_seq(self):
        a = bridge._event_append("p0-a", "t", {"v": "a1"})
        bridge._event_append("p0-b", "t", {"v": "b1"})
        a2 = bridge._event_append("p0-a", "t", {"v": "a2"})
        evs = bridge._event_since("p0-a", 0)
        self.assertEqual([e["data"]["v"] for e in evs], ["a1", "a2"])
        evs = bridge._event_since("p0-a", a)
        self.assertEqual([e["seq"] for e in evs], [a2])
        # 信封形狀:{seq, ts, type, data}
        self.assertEqual(set(evs[0].keys()), {"seq", "ts", "type", "data"})

    def test_external_id_dedup(self):
        s1 = bridge._event_append("p0-dedup", "t", {"v": 1}, external_id="ext-1")
        s2 = bridge._event_append("p0-dedup", "t", {"v": 1}, external_id="ext-1")
        self.assertGreater(s1, 0)
        self.assertEqual(s2, 0)
        # 記憶體快取清空後(模擬 bridge 重啟),DB UNIQUE 仍然守住
        bridge._EVENT_SEEN.pop("p0-dedup", None)
        s3 = bridge._event_append("p0-dedup", "t", {"v": 1}, external_id="ext-1")
        self.assertEqual(s3, 0)
        self.assertEqual(len(bridge._event_since("p0-dedup", 0)), 1)

    def test_version_bumped_only_on_real_insert(self):
        ver0 = bridge._EVENT_VER.get("p0-ver", 0)
        bridge._event_append("p0-ver", "t", {}, external_id="v-1")
        self.assertEqual(bridge._EVENT_VER.get("p0-ver", 0), ver0 + 1)
        bridge._event_append("p0-ver", "t", {}, external_id="v-1")  # dedup
        self.assertEqual(bridge._EVENT_VER.get("p0-ver", 0), ver0 + 1)

    def test_latest_seq(self):
        self.assertEqual(bridge._event_latest_seq("p0-empty"), 0)
        s = bridge._event_append("p0-latest", "t", {})
        self.assertEqual(bridge._event_latest_seq("p0-latest"), s)

    def test_event_wait_wakes_on_append(self):
        async def _run():
            ver = bridge._EVENT_VER.get("p0-wait", 0)
            waiter = asyncio.create_task(bridge._event_wait("p0-wait", ver))
            await asyncio.sleep(0.05)
            self.assertFalse(waiter.done())
            bridge._event_append("p0-wait", "t", {})
            await asyncio.wait_for(waiter, timeout=2.0)
        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
