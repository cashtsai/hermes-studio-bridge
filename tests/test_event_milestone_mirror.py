"""里程碑鏡射(2026-11-04,t3code P3-lite):卡片終態/狀態/turn 入持久 log。

鐵律:**串流 delta 不落地** —— final=False 的 card.upsert 每個打字機
tick 都來一次,全鏡射會灌爆 sqlite;只收 final=True、session.status、
turn。重放同一終態卡(reseed)靠 external_id 冪等。
"""
import _isolation  # noqa: F401
import os
import sqlite3
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="milestone-canon-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402  (import 時注入 carddigest.MILESTONE_SINK)
import carddigest  # noqa: E402


def _events(session: str) -> list:
    con = sqlite3.connect(bridge.CANON_DB)
    try:
        return con.execute(
            "SELECT type FROM event_log WHERE session=? ORDER BY id",
            (session,)).fetchall()
    finally:
        con.close()


def _store(sid: str) -> "carddigest.SessionCardStore":
    st = carddigest.SessionCardStore()
    st.media_session_id = sid
    return st


class TestMilestoneMirror(unittest.TestCase):
    def test_sink_is_wired_on_import(self):
        self.assertIs(carddigest.MILESTONE_SINK, bridge._milestone_sink)

    def test_final_card_mirrors_delta_does_not(self):
        sid = "codex:ms-1"
        st = _store(sid)
        st.upsert_card(carddigest.make_card(
            "c1", "t1", "assistant", "text", {"text": "部分"}, final=False))
        st.upsert_card(carddigest.make_card(
            "c1", "t1", "assistant", "text", {"text": "部分更多"}, final=False))
        self.assertEqual(_events(sid), [], "串流 delta 不准落地")
        st.upsert_card(carddigest.make_card(
            "c1", "t1", "assistant", "text", {"text": "完整回覆"}, final=True))
        self.assertEqual([r[0] for r in _events(sid)], ["card.milestone"])

    def test_final_card_replay_is_idempotent(self):
        sid = "codex:ms-2"
        st = _store(sid)
        card = carddigest.make_card(
            "c9", "t9", "assistant", "text", {"text": "同一張"}, final=True)
        st.upsert_card(dict(card))
        st.upsert_card(dict(card))      # reseed 重放:rev 會 ++,內容相同
        self.assertEqual(len(_events(sid)), 1, "同內容終態卡只落一筆")

    def test_status_and_turn_mirror(self):
        sid = "claude_code:ms-3"
        st = _store(sid)
        st.set_status({"state": "working", "label": "跑步中"})
        st.set_status({"state": "working", "label": "跑步中"})   # 無變化不發
        st.push_turn("completed", "turn-1")
        st.push_turn("completed", "turn-1")                       # 冪等
        types = [r[0] for r in _events(sid)]
        self.assertEqual(types, ["session.status", "turn"])

    def test_store_without_identity_mirrors_nothing(self):
        st = carddigest.SessionCardStore()     # 沒 media_session_id
        st.upsert_card(carddigest.make_card(
            "cx", "tx", "assistant", "text", {"text": "孤兒"}, final=True))
        # 沒 id 的 store 連 session 鍵都沒有 —— 驗全表沒有這張卡的事件。
        con = sqlite3.connect(bridge.CANON_DB)
        try:
            n = con.execute(
                "SELECT COUNT(*) FROM event_log WHERE payload LIKE '%孤兒%'"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
