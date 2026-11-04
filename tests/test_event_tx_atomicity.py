"""事件+正典同交易(2026-11-03,抄 t3code「事件+投影同一交易」)。

病灶:_canon_add / _report_upsert 原本 commit 完才另開連線鏡射 event_log,
中間斷掉 = 「canonical 有、event_log 沒有」—— 列表預覽/未讀落後的結構性
來源之一。釘死三件事:同交易寫入、commit 失敗兩邊一起消失、通知在
commit 之後(訂閱者醒來一定查得到)。
"""
import _isolation  # noqa: F401
import os
import sqlite3
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="evttx-canon-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


def _count(table: str, session: str) -> int:
    con = sqlite3.connect(bridge.CANON_DB)
    try:
        return con.execute(
            f"SELECT COUNT(*) FROM {table} WHERE session=?", (session,)
        ).fetchone()[0]
    finally:
        con.close()


class TestCanonAddAtomicity(unittest.TestCase):
    def test_message_and_event_commit_together(self):
        sess = "tx-sess-1"
        mid, ok = bridge._canon_add(sess, "assistant", "hello tx", push=False)
        self.assertTrue(ok)
        self.assertEqual(_count("messages", sess), 1)
        self.assertEqual(_count("event_log", sess), 1)
        evs = bridge._event_since(sess, 0)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["type"], "message.upsert")
        self.assertEqual(evs[0]["data"]["message"]["id"], mid)

    def test_commit_failure_loses_both(self):
        """commit 炸掉 → messages 與 event_log 一起消失,不准只留一邊。"""
        sess = "tx-sess-fail"
        real_connect = sqlite3.connect

        class BoomCon:
            def __init__(self, con):
                self._con = con

            def execute(self, *a, **kw):
                return self._con.execute(*a, **kw)

            def commit(self):
                raise sqlite3.OperationalError("boom: simulated commit failure")

            def close(self):
                self._con.rollback()
                self._con.close()

        def fake_connect(path, *a, **kw):
            con = real_connect(path, *a, **kw)
            return BoomCon(con)

        sqlite3.connect = fake_connect
        try:
            mid, ok = bridge._canon_add(sess, "assistant", "doomed", push=False)
        finally:
            sqlite3.connect = real_connect
        self.assertFalse(ok)
        self.assertEqual(_count("messages", sess), 0, "commit 失敗不准留訊息")
        self.assertEqual(_count("event_log", sess), 0, "commit 失敗不准留事件")

    def test_event_append_with_caller_con_does_not_commit(self):
        """con= 模式:append 不自行 commit —— rollback 後事件不存在。"""
        sess = "tx-sess-rollback"
        con = sqlite3.connect(bridge.CANON_DB)
        seq = bridge._event_append(sess, "message.upsert", {"n": 1},
                                   external_id="x-1", con=con)
        self.assertGreater(seq, 0)
        con.rollback()
        con.close()
        self.assertEqual(_count("event_log", sess), 0)
        # rollback 不毒化冪等:同 external_id 再寫(自管交易)必須成功。
        seq2 = bridge._event_append(sess, "message.upsert", {"n": 1},
                                    external_id="x-1")
        self.assertGreater(seq2, 0)


class TestReportUpsertAtomicity(unittest.TestCase):
    def test_report_and_event_commit_together(self):
        sess = "tx-rep-1"
        rid = bridge._report_upsert(sess, {
            "label": "晨報", "name": "morning", "content": "today",
            "ts": 1234.0})
        self.assertTrue(rid)
        self.assertEqual(_count("report_events", sess), 1)
        self.assertEqual(_count("event_log", sess), 1)


if __name__ == "__main__":
    unittest.main()
