"""列表 preview 凍結修復:report_events 取最新 + CC transcript 尾巴抽 preview。"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="preview-fix-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class TestReportMessagesNewestFirst(unittest.TestCase):
    def setUp(self):
        bridge._canon_init()

    def test_newest_reports_survive_limit(self):
        con = sqlite3.connect(bridge.CANON_DB)
        for i in range(30):
            con.execute(
                "INSERT OR REPLACE INTO report_events"
                "(id,session,label,name,content,ts,ingested_at) VALUES (?,?,?,?,?,?,?)",
                (f"r{i}", "pantianqing", "測試報告", "", f"內容 {i}",
                 1000.0 + i, 1000.0 + i))
        con.commit()
        con.close()
        msgs = bridge._report_messages("pantianqing", 10)
        ts = [m["ts"] for m in msgs]
        # 舊版 ASC LIMIT 10 只會拿 ts=1000..1009;修復後必須含最新一筆。
        self.assertIn(1029.0, ts)
        self.assertEqual(len(msgs), 10)


class TestCCTailPreview(unittest.TestCase):
    def test_extracts_last_readable_message(self):
        path = os.path.join(_TMP, "aaaa1111-0000-0000-0000-000000000000.jsonl")
        lines = [
            {"type": "user", "message": {"content": "第一句"}},
            {"type": "assistant",
             "message": {"content": [{"type": "text", "text": "最後的助手回覆"}]}},
            {"type": "user",
             "message": {"content": [{"type": "tool_result",
                                      "content": "tool output junk"}]}},
        ]
        with open(path, "w", encoding="utf-8") as f:
            for d in lines:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        mtime, preview = bridge._cc_last_activity(path)
        self.assertGreater(mtime, 0)
        self.assertEqual(preview, "最後的助手回覆")

    def test_cache_by_mtime(self):
        path = os.path.join(_TMP, "bbbb2222-0000-0000-0000-000000000000.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
        m1, p1 = bridge._cc_last_activity(path)
        self.assertEqual(p1, "hi")
        # 同 mtime 直接吃快取(把快取塞髒值驗證沒有重讀)。
        bridge._cc_tail_cache[path] = (m1, "cached")
        _, p2 = bridge._cc_last_activity(path)
        self.assertEqual(p2, "cached")


if __name__ == "__main__":
    unittest.main(verbosity=2)
