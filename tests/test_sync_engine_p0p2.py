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


def _make_tg_home(rows):
    """假 persona home:state.db schema 對齊 _persona_history 的 query
    (sessions.source='telegram' JOIN messages)。rows=[(role, content, ts)]。"""
    home = tempfile.mkdtemp(prefix="syncengine-home-")
    con = sqlite3.connect(os.path.join(home, "state.db"))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT)")
    con.execute("CREATE TABLE messages(session_id TEXT, role TEXT, "
                "content TEXT, timestamp REAL)")
    con.execute("INSERT INTO sessions VALUES('tg-main','telegram')")
    for role, content, ts in rows:
        con.execute("INSERT INTO messages VALUES('tg-main',?,?,?)",
                    (role, content, ts))
    con.commit()
    con.close()
    return home


class TestP1Mirrors(unittest.TestCase):
    def test_canon_add_mirrors_message_event(self):
        mid, ok = bridge._canon_add("p1-app", "user", "你好,事件日誌")
        self.assertTrue(ok)
        evs = bridge._event_since("p1-app", 0)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["type"], "message.upsert")
        m = evs[0]["data"]["message"]
        self.assertEqual((m["id"], m["role"], m["content"], m["source"]),
                         (mid, "user", "你好,事件日誌", "app"))

    def test_canon_add_same_mid_content_dedups(self):
        mid, _ = bridge._canon_add("p1-retry", "user", "同一則")
        bridge._canon_add("p1-retry", "user", "同一則", mid=mid)  # replay
        self.assertEqual(len(bridge._event_since("p1-retry", 0)), 1)

    def test_report_upsert_mirrors_and_rewrites(self):
        sess = "p1-rep"
        rid = bridge._report_upsert(sess, {
            "id": "r-1", "label": "晨報", "name": "morning",
            "content": "第一版內容", "ts": 1000.0})
        self.assertEqual(rid, "r-1")
        evs = bridge._event_since(sess, 0)
        self.assertEqual(len(evs), 1)
        m = evs[0]["data"]["message"]
        self.assertEqual(m["id"], "rep-r-1")
        self.assertEqual(m["source"], "report")
        self.assertIn("第一版內容", m["content"])
        # 內容沒變 → upsert 短路 → 不追加事件
        bridge._report_upsert(sess, {
            "id": "r-1", "label": "晨報", "name": "morning",
            "content": "第一版內容", "ts": 1000.0})
        self.assertEqual(len(bridge._event_since(sess, 0)), 1)
        # 改稿 → 新事件、同 message id(client 端以 id 覆蓋)
        bridge._report_upsert(sess, {
            "id": "r-1", "label": "晨報", "name": "morning",
            "content": "改稿後內容", "ts": 1000.0})
        evs = bridge._event_since(sess, 0)
        self.assertEqual(len(evs), 2)
        self.assertEqual(evs[1]["data"]["message"]["id"], "rep-r-1")
        self.assertIn("改稿後內容", evs[1]["data"]["message"]["content"])

    def test_tg_scan_mirrors_and_backfills(self):
        sess = "p1-tg"
        home = _make_tg_home([("user", "早安", 2000.0),
                              ("assistant", "早安,今天天氣不錯", 2001.0)])
        bridge.PERSONAS[sess] = (f"測試人格 ({sess})", home)
        try:
            # event_log 出生前的舊 canonical 訊息(直接落庫,不經 _canon_add)
            con = sqlite3.connect(bridge.CANON_DB)
            con.execute("INSERT INTO messages(id,session,role,content,attachments,"
                        "created_at,status) VALUES('old-1',?,?,?,?,?,?)",
                        (sess, "user", "歷史訊息", "[]", 1999.0, "done"))
            con.commit()
            con.close()
            merged = bridge._hp_merged_messages(sess, 80)
            self.assertEqual([m["content"] for m in merged],
                             ["歷史訊息", "早安", "早安,今天天氣不錯"])
            evs = bridge._event_since(sess, 0)
            self.assertEqual([e["data"]["message"]["content"] for e in evs],
                             ["歷史訊息", "早安", "早安,今天天氣不錯"])
            self.assertEqual(evs[1]["data"]["message"]["source"], "telegram")
            # 重掃冪等:不重複
            bridge._hp_merged_messages(sess, 80)
            self.assertEqual(len(bridge._event_since(sess, 0)), 3)
        finally:
            bridge.PERSONAS.pop(sess, None)

    def test_event_sync_session_throttle(self):
        sess = "p1-sync"
        home = _make_tg_home([("user", "節流測試", 3000.0)])
        bridge.PERSONAS[sess] = (f"測試人格 ({sess})", home)
        try:
            bridge._event_sync_session(sess, force=True)
            self.assertEqual(len(bridge._event_since(sess, 0)), 1)
            # 節流窗內再叫不掃(塞第二筆 TG 進 state.db 也看不到)
            con = sqlite3.connect(os.path.join(home, "state.db"))
            con.execute("INSERT INTO messages VALUES('tg-main','user','第二筆',3001.0)")
            con.commit()
            con.close()
            bridge._event_sync_session(sess)
            self.assertEqual(len(bridge._event_since(sess, 0)), 1)
            bridge._event_sync_session(sess, force=True)
            self.assertEqual(len(bridge._event_since(sess, 0)), 2)
        finally:
            bridge.PERSONAS.pop(sess, None)


class _FakeReq:
    def __init__(self, body=None):
        self._b = body or {}
        self.headers = {"authorization": f"bearer {os.environ['BRIDGE_TOKEN']}"}

    async def json(self):
        return self._b


async def _collect_sse(resp):
    """StreamingResponse → [事件dict](follow=False 模式,讀到 [DONE] 為止)。"""
    out = []
    async for chunk in resp.body_iterator:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        for line in text.splitlines():
            if not line.startswith("data: "):
                continue
            body = line[6:]
            if body == "[DONE]":
                return out
            out.append(json.loads(body))
    return out


class TestP2EventsEndpoint(unittest.TestCase):
    def setUp(self):
        self._auth = bridge._check_auth
        bridge._check_auth = lambda r: None

    def tearDown(self):
        bridge._check_auth = self._auth

    def test_unknown_session_rejected(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(bridge.app_v2_events("no-such-persona", _FakeReq()))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_backlog_since_seq_and_tg_pull(self):
        sess = "p2-ev"
        home = _make_tg_home([("user", "TG那邊說的話", 5000.0)])
        bridge.PERSONAS[sess] = (f"測試人格 ({sess})", home)
        try:
            mid, _ = bridge._canon_add(sess, "user", "app 這邊的訊息")
            # follow=False:回放 event_log 積壓後 [DONE]。連線時的 force 同步
            # 應把 TG 洞補進 event_log(app 訊息已由 _canon_add 鏡射,去重)。
            resp = asyncio.run(bridge.app_v2_events(sess, _FakeReq(),
                                                    since_seq=0, follow=False))
            evs = asyncio.run(_collect_sse(resp))
            self.assertEqual({e["type"] for e in evs}, {"message.upsert"})
            contents = [e["data"]["message"]["content"] for e in evs]
            self.assertIn("app 這邊的訊息", contents)
            self.assertIn("TG那邊說的話", contents)
            self.assertEqual(len(contents), 2)
            seqs = [e["seq"] for e in evs]
            self.assertEqual(seqs, sorted(seqs))
            # since_seq 補洞:從第一筆之後訂閱,只收到之後的事件
            resp = asyncio.run(bridge.app_v2_events(sess, _FakeReq(),
                                                    since_seq=seqs[0],
                                                    follow=False))
            evs2 = asyncio.run(_collect_sse(resp))
            self.assertEqual([e["seq"] for e in evs2], seqs[1:])
        finally:
            bridge.PERSONAS.pop(sess, None)


class TestP2ReadCursor(unittest.TestCase):
    def setUp(self):
        self._auth = bridge._check_auth
        bridge._check_auth = lambda r: None
        # session 每個測試獨立:游標單調不退,共用 session 會互相污染
        self.sess = f"p2-read-{self._testMethodName}"
        bridge.PERSONAS[self.sess] = (f"測試人格 ({self.sess})",
                                      tempfile.mkdtemp(prefix="syncengine-rc-"))

    def tearDown(self):
        bridge._check_auth = self._auth
        bridge.PERSONAS.pop(self.sess, None)

    def _post(self, body):
        return asyncio.run(bridge.app_v2_read_post(_FakeReq(body)))

    def test_post_get_roundtrip_and_event(self):
        r = self._post({"session": self.sess, "device_id": "iphone",
                        "last_read_seq": 42, "last_read_ts": 5100.0,
                        "message_id": "m-42"})
        self.assertTrue(r["ok"] and r["moved"])
        self.assertGreater(r["seq"], 0)
        # read_cursor.update 事件進了 event_log(其他裝置訂閱得到)
        evs = bridge._event_since(self.sess, 0)
        self.assertEqual(evs[-1]["type"], "read_cursor.update")
        self.assertEqual(evs[-1]["data"]["device_id"], "iphone")
        self.assertEqual(evs[-1]["data"]["last_read_seq"], 42)
        g = asyncio.run(bridge.app_v2_read_get(self.sess, _FakeReq()))
        self.assertEqual(len(g["cursors"]), 1)
        self.assertEqual(g["cursors"][0]["last_read_seq"], 42)
        self.assertEqual(g["cursors"][0]["message_id"], "m-42")

    def test_cursor_monotonic_and_idempotent(self):
        self._post({"session": self.sess, "device_id": "iphone",
                    "last_read_seq": 42})
        n_events = len(bridge._event_since(self.sess, 0))
        # 倒退/重送 → 不動、不追加事件(冪等)
        r = self._post({"session": self.sess, "device_id": "iphone",
                        "last_read_seq": 7})
        self.assertFalse(r["moved"])
        self.assertEqual(r["cursor"]["last_read_seq"], 42)
        self.assertEqual(len(bridge._event_since(self.sess, 0)), n_events)
        # 前進 → 動、追加事件
        r = self._post({"session": self.sess, "device_id": "iphone",
                        "last_read_seq": 99})
        self.assertTrue(r["moved"])
        self.assertEqual(len(bridge._event_since(self.sess, 0)), n_events + 1)

    def test_per_device_rows(self):
        self._post({"session": self.sess, "device_id": "iphone",
                    "last_read_seq": 10})
        self._post({"session": self.sess, "device_id": "ipad",
                    "last_read_seq": 3})
        g = asyncio.run(bridge.app_v2_read_get(self.sess, _FakeReq()))
        by_dev = {c["device_id"]: c["last_read_seq"] for c in g["cursors"]}
        self.assertEqual(by_dev, {"iphone": 10, "ipad": 3})

    def test_validation(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self._post({"session": self.sess, "last_read_seq": 1})
        self.assertEqual(ctx.exception.status_code, 400)   # device_id 必填
        with self.assertRaises(HTTPException) as ctx:
            self._post({"session": self.sess, "device_id": "iphone"})
        self.assertEqual(ctx.exception.status_code, 400)   # 游標至少一個
        with self.assertRaises(HTTPException) as ctx:
            self._post({"session": "nope", "device_id": "iphone",
                        "last_read_seq": 1})
        self.assertEqual(ctx.exception.status_code, 400)   # unknown session


if __name__ == "__main__":
    unittest.main(verbosity=2)
