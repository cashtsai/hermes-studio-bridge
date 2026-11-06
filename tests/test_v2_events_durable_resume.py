"""T3#1 持久補洞(2026-11-06):per-session 卡片流重連改走 event_log,消滅 410。

被測契約(docs/APP_BRIDGE_CONTRACT.md §5/§7):
1. 舊 client(無 after_durable_seq):ring 斷層照樣 410 SEQ_GONE —— 釘死不回歸。
2. snapshot 加欄 durable_seq(event_log 該 session 最大 id;沒有=0),既有欄位不動。
3. 新 client:ring 清空(模擬 bridge 重啟)+ after_durable_seq=0 → 收到持久
   里程碑補洞(card.milestone→card.upsert / session.status / turn)+ resync 信封
   {durable_seq, ring_seq},之後接 ring 續流。
4. 串流 delta(final=False)不經持久路徑重放;message.upsert 等非里程碑型別
   不漏進卡片流,但持久游標要跨過它們。
5. 補洞段 durable_seq 單調遞增;續流 ring seq 單調遞增且接在 resync.ring_seq 之後。
6. SSE 信封帶 durable_seq:有對應持久事件 = event_log id;純 delta / 去重重放 = null。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(見 tests/_isolation.py)
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="dres-canon-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402  (import 觸發 _canon_init → event_log 建表)
import carddigest  # noqa: E402


def _store(sid: str | None = None) -> "carddigest.SessionCardStore":
    st = carddigest.SessionCardStore()
    if sid:
        st.media_session_id = sid
    return st


def _final_card(cid: str, text: str) -> dict:
    return carddigest.make_card(cid, "t1", "assistant", "text",
                                {"text": text}, final=True)


class _Req:
    """最小 Request 替身:v2 cards/events 只用 headers + is_disconnected。"""

    def __init__(self):
        self.headers = {"authorization": "Bearer " + bridge.BRIDGE_TOKEN}
        self.disconnected = False

    async def is_disconnected(self):
        return self.disconnected


async def _open_events(store, *, since_seq: int, after_durable_seq=None):
    """monkeypatch _v2_card_store → 直呼端點,回 (req, SSE async gen)。"""
    orig = bridge._v2_card_store

    async def fake(_sid):
        return store

    bridge._v2_card_store = fake
    try:
        req = _Req()
        resp = await bridge.v2_session_events(
            "codex:dres", req, since_seq=since_seq, profile="phone",
            after_durable_seq=after_durable_seq)
        return req, resp.body_iterator
    finally:
        bridge._v2_card_store = orig


async def _next_frame(agen, deadline: float = 5.0) -> dict:
    chunk = await asyncio.wait_for(agen.__anext__(), deadline)
    assert chunk.startswith("data: "), chunk
    return json.loads(chunk[len("data: "):].strip())


async def _drain(req, agen):
    req.disconnected = True
    try:
        while True:
            await asyncio.wait_for(agen.__anext__(), 10.0)
    except StopAsyncIteration:
        pass


class TestOldClient410(unittest.TestCase):
    """驗收 1:無新參數的舊 client,領先 seq 照樣 410(不回歸)。"""

    def test_ahead_seq_still_410_without_param(self):
        async def run():
            st = _store("codex:dres-410")      # 空 ring = 剛重啟
            orig = bridge._v2_card_store

            async def fake(_sid):
                return st

            bridge._v2_card_store = fake
            try:
                with self.assertRaises(bridge.BridgeError) as cm:
                    await bridge.v2_session_events(
                        "codex:dres-410", _Req(), since_seq=42, profile="phone")
            finally:
                bridge._v2_card_store = orig
            self.assertEqual(cm.exception.status_code, 410)
            self.assertEqual(cm.exception.code, "SEQ_GONE")
        asyncio.run(run())

    def test_route_exposes_after_durable_seq_param(self):
        """FastAPI 端點簽名有 after_durable_seq(query 參數接線存在)。"""
        import inspect
        route = next(r for r in bridge.app.routes
                     if getattr(r, "path", "") == "/app/v2/sessions/{session_id}/events")
        params = inspect.signature(route.endpoint).parameters
        self.assertIn("after_durable_seq", params)
        self.assertIsNone(params["after_durable_seq"].default)


class TestSnapshotDurableSeq(unittest.TestCase):
    """驗收 2:snapshot 加欄 durable_seq,既有欄位一個不動。"""

    def test_snapshot_carries_durable_seq(self):
        async def run():
            sid = "codex:dres-snap"
            st = _store(sid)
            st.upsert_card(_final_card("c-snap", "里程碑一"))
            want = bridge._event_latest_seq(sid)
            self.assertGreater(want, 0)
            orig = bridge._v2_card_store

            async def fake(_s):
                return st

            bridge._v2_card_store = fake
            try:
                snap = await bridge.v2_session_cards(sid, _Req())
            finally:
                bridge._v2_card_store = orig
            self.assertEqual(snap["durable_seq"], want)
            # 既有欄位不動
            self.assertIn("cards", snap)
            self.assertIn("latest_seq", snap)
            self.assertIn("status", snap)
            self.assertEqual(snap["latest_seq"], st.seq)
        asyncio.run(run())

    def test_snapshot_without_identity_is_zero(self):
        async def run():
            st = _store()                      # 沒 media_session_id
            st.upsert_card(_final_card("c-anon", "孤兒卡"))
            orig = bridge._v2_card_store

            async def fake(_s):
                return st

            bridge._v2_card_store = fake
            try:
                snap = await bridge.v2_session_cards("cc:anon", _Req())
            finally:
                bridge._v2_card_store = orig
            self.assertEqual(snap["durable_seq"], 0)
        asyncio.run(run())


class TestDurableBackfill(unittest.TestCase):
    """驗收 3/4/5:重啟後持久補洞 + resync + 續流,delta 不重放,seq 單調。"""

    def test_backfill_resync_and_resume(self):
        async def run():
            sid = "codex:dres-main"
            # ── 重啟前:舊 store 產生里程碑(delta 刻意穿插,不准落地)──
            pre = _store(sid)
            pre.upsert_card(carddigest.make_card(
                "c1", "t1", "assistant", "text", {"text": "打字"}, final=False))
            pre.upsert_card(carddigest.make_card(
                "c1", "t1", "assistant", "text", {"text": "打字更多"}, final=False))
            pre.upsert_card(_final_card("c1", "完整回覆一"))
            pre.set_status({"state": "idle", "label": "待命"})
            pre.push_turn("end", "t1")
            # 非里程碑型別(/app/v2/events 的世界)不准漏進卡片流,
            # 但持久游標要跨過它。
            tail_id = bridge._event_append(sid, "message.upsert",
                                           {"message": {"id": "m1", "content": "x"}})
            self.assertGreater(tail_id, 0)

            # ── 重啟:新 store,ring 清空;client 帶舊 ring 游標 + 持久游標 0 ──
            post = _store(sid)
            req, agen = await _open_events(post, since_seq=42, after_durable_seq=0)

            frames = [await _next_frame(agen) for _ in range(4)]
            types = [f["type"] for f in frames]
            self.assertEqual(types, ["card.upsert", "session.status", "turn", "resync"])

            # 補洞卡 = 終態那張;delta 不經持久路徑重放
            self.assertEqual(frames[0]["data"]["card"]["id"], "c1")
            self.assertEqual(frames[0]["data"]["card"]["body"]["text"], "完整回覆一")
            self.assertTrue(frames[0]["data"]["card"]["final"])
            self.assertEqual(frames[1]["data"], {"state": "idle", "label": "待命"})
            self.assertEqual(frames[2]["data"], {"state": "end", "turn_id": "t1"})

            # 補洞段 durable_seq 單調遞增;信封 seq=0(非 ring 游標)
            backfill_ds = [f["durable_seq"] for f in frames[:3]]
            self.assertTrue(all(isinstance(d, int) and d > 0 for d in backfill_ds))
            self.assertEqual(backfill_ds, sorted(backfill_ds))
            self.assertEqual(len(set(backfill_ds)), len(backfill_ds))
            self.assertTrue(all(f["seq"] == 0 for f in frames[:3]))

            # resync:durable 游標跨過 message.upsert;ring 游標 = 新 store 尾端
            resync = frames[3]
            self.assertEqual(resync["data"]["durable_seq"], tail_id)
            self.assertEqual(resync["data"]["ring_seq"], post.seq)
            self.assertIsNone(resync["durable_seq"])

            # ── 續流:resync 之後 ring 新事件照常到,seq 接在 ring_seq 後 ──
            post.upsert_card(_final_card("c2", "重啟後的新回覆"))
            live = await _next_frame(agen)
            self.assertEqual(live["type"], "card.upsert")
            self.assertEqual(live["data"]["card"]["id"], "c2")
            self.assertEqual(live["seq"], resync["data"]["ring_seq"] + 1)
            # 續流的持久游標也單調(新里程碑的 event_log id 更大)
            self.assertIsInstance(live["durable_seq"], int)
            self.assertGreater(live["durable_seq"], backfill_ds[-1])

            await _drain(req, agen)
        asyncio.run(run())

    def test_backfill_empty_log_still_resyncs(self):
        """沒有任何持久里程碑的 session:不 410、收到空補洞 + resync。"""
        async def run():
            post = _store("codex:dres-empty")
            req, agen = await _open_events(post, since_seq=7, after_durable_seq=0)
            resync = await _next_frame(agen)
            self.assertEqual(resync["type"], "resync")
            self.assertEqual(resync["data"], {"durable_seq": 0, "ring_seq": 0})
            await _drain(req, agen)
        asyncio.run(run())

    def test_cursor_skips_already_seen_milestones(self):
        """client 帶上次的 durable 游標 → 只補游標之後的里程碑。"""
        async def run():
            sid = "codex:dres-cursor"
            pre = _store(sid)
            pre.upsert_card(_final_card("c-old", "舊里程碑"))
            seen = bridge._event_latest_seq(sid)
            pre.upsert_card(_final_card("c-new", "新里程碑"))

            post = _store(sid)
            req, agen = await _open_events(post, since_seq=99,
                                           after_durable_seq=seen)
            f1 = await _next_frame(agen)
            f2 = await _next_frame(agen)
            self.assertEqual(f1["data"]["card"]["id"], "c-new")
            self.assertEqual(f2["type"], "resync")
            await _drain(req, agen)
        asyncio.run(run())


class TestEnvelopeDurableSeq(unittest.TestCase):
    """驗收 6:信封 durable_seq —— 里程碑 = event_log id;delta/去重 = null。"""

    def test_envelope_durable_seq_semantics(self):
        sid = "codex:dres-env"
        st = _store(sid)
        delta = st.upsert_card(carddigest.make_card(
            "ce", "te", "assistant", "text", {"text": "打"}, final=False))
        self.assertIsNone(delta["durable_seq"], "串流 delta 不落持久 log")
        final = st.upsert_card(_final_card("ce", "完整"))
        self.assertIsInstance(final["durable_seq"], int)
        self.assertEqual(final["durable_seq"], bridge._event_latest_seq(sid))
        replay = st.upsert_card(_final_card("ce", "完整"))   # reseed 重放:去重
        self.assertIsNone(replay["durable_seq"], "去重略過 → null,不給假游標")
        status = st.set_status({"state": "busy"})
        self.assertIsInstance(status["durable_seq"], int)
        self.assertGreater(status["durable_seq"], final["durable_seq"])
        turn = st.push_turn("begin", "te2")
        self.assertIsInstance(turn["durable_seq"], int)

    def test_anonymous_store_envelope_is_null(self):
        st = _store()                          # 沒 media_session_id → 不鏡射
        ev = st.upsert_card(_final_card("cx", "孤兒"))
        self.assertIsNone(ev["durable_seq"])

    def test_ping_has_null_durable_seq(self):
        st = _store("codex:dres-ping")
        p = st.ping()
        self.assertIn("durable_seq", p)
        self.assertIsNone(p["durable_seq"])


if __name__ == "__main__":
    unittest.main()
