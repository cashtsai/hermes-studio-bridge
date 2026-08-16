"""Cron report sync must ignore assistant tool-call progress rows."""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest


_TMP = tempfile.mkdtemp(prefix="cron-report-finality-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class TestCronReportFinality(unittest.TestCase):
    def test_tool_call_progress_is_not_indexed_before_final_reply(self):
        home = os.path.join(_TMP, "home")
        os.makedirs(os.path.join(home, "cron"), exist_ok=True)
        with open(os.path.join(home, "cron", "jobs.json"), "w", encoding="utf-8") as f:
            json.dump({"jobs": [{"id": "abcdef", "name": "morning-test"}]}, f)

        db_path = os.path.join(home, "state.db")
        con = sqlite3.connect(db_path)
        con.executescript(
            """
            CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT NOT NULL);
            CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL NOT NULL,
                finish_reason TEXT
            );
            """
        )
        session_id = "cron_abcdef_20260808_070000"
        con.execute("INSERT INTO sessions VALUES(?,'cron')", (session_id,))
        con.execute(
            "INSERT INTO messages(session_id,role,content,timestamp,finish_reason) "
            "VALUES(?,?,?,?,?)",
            (session_id, "assistant", "Now gathering calendar and weather.", time.time(), "tool_calls"),
        )
        con.commit()
        con.close()

        persona = "cron-finality-test"
        bridge.PERSONAS[persona] = ("測試人格", home)
        bridge.PERSONA_REPORTS[persona] = {"morning-test": "晨報"}
        try:
            self.assertEqual(bridge._persona_reports(persona, 20), [])

            con = sqlite3.connect(db_path)
            con.execute(
                "INSERT INTO messages(session_id,role,content,timestamp,finish_reason) "
                "VALUES(?,?,?,?,?)",
                (session_id, "assistant", "🌅 善彰早安\n正式晨報正文", time.time(), "stop"),
            )
            con.commit()
            con.close()

            reports = bridge._persona_reports(persona, 20)
            self.assertEqual(len(reports), 1)
            self.assertIn("正式晨報正文", reports[0]["content"])
            self.assertNotIn("Now gathering", reports[0]["content"])
        finally:
            bridge.PERSONA_REPORTS.pop(persona, None)
            bridge.PERSONAS.pop(persona, None)

    def test_legacy_null_finish_reason_still_counts_as_final(self):
        # hermes 早期不寫 finish_reason(live DB 有數千筆 NULL/''),
        # 過濾只能排除明確的 tool_calls —— NULL/'' 一律視為最終回覆。
        home = os.path.join(_TMP, "home-legacy")
        os.makedirs(os.path.join(home, "cron"), exist_ok=True)
        with open(os.path.join(home, "cron", "jobs.json"), "w", encoding="utf-8") as f:
            json.dump({"jobs": [{"id": "beef01", "name": "legacy-test"}]}, f)

        db_path = os.path.join(home, "state.db")
        con = sqlite3.connect(db_path)
        con.executescript(
            """
            CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT NOT NULL);
            CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL NOT NULL,
                finish_reason TEXT
            );
            """
        )
        session_id = "cron_beef01_20260807_070000"
        con.execute("INSERT INTO sessions VALUES(?,'cron')", (session_id,))
        con.execute(
            "INSERT INTO messages(session_id,role,content,timestamp,finish_reason) "
            "VALUES(?,?,?,?,NULL)",
            (session_id, "assistant", "舊版晨報正文(沒有 finish_reason)", time.time()),
        )
        con.execute(
            "INSERT INTO messages(session_id,role,content,timestamp,finish_reason) "
            "VALUES(?,?,?,?,'')",
            (session_id, "assistant", "空字串版晨報正文", time.time() + 1),
        )
        con.commit()
        con.close()

        persona = "cron-finality-legacy"
        bridge.PERSONAS[persona] = ("測試人格", home)
        bridge.PERSONA_REPORTS[persona] = {"legacy-test": "晨報"}
        try:
            reports = bridge._persona_reports(persona, 20)
            self.assertEqual(len(reports), 1)
            self.assertIn("空字串版晨報正文", reports[0]["content"])
        finally:
            bridge.PERSONA_REPORTS.pop(persona, None)
            bridge.PERSONAS.pop(persona, None)

    def test_old_schema_without_finish_reason_column_falls_back(self):
        # 更老的 schema 連 finish_reason 欄位都沒有 —— 過濾必須退回舊行為
        # (取最新一筆),絕不能讓整個函式吃 OperationalError 回空表。
        home = os.path.join(_TMP, "home-oldschema")
        os.makedirs(os.path.join(home, "cron"), exist_ok=True)
        with open(os.path.join(home, "cron", "jobs.json"), "w", encoding="utf-8") as f:
            json.dump({"jobs": [{"id": "cafe02", "name": "oldschema-test"}]}, f)

        db_path = os.path.join(home, "state.db")
        con = sqlite3.connect(db_path)
        con.executescript(
            """
            CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT NOT NULL);
            CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL NOT NULL
            );
            """
        )
        session_id = "cron_cafe02_20260806_070000"
        con.execute("INSERT INTO sessions VALUES(?,'cron')", (session_id,))
        con.execute(
            "INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)",
            (session_id, "assistant", "老 schema 晨報正文", time.time()),
        )
        con.commit()
        con.close()

        persona = "cron-finality-oldschema"
        bridge.PERSONAS[persona] = ("測試人格", home)
        bridge.PERSONA_REPORTS[persona] = {"oldschema-test": "晨報"}
        try:
            reports = bridge._persona_reports(persona, 20)
            self.assertEqual(len(reports), 1)
            self.assertIn("老 schema 晨報正文", reports[0]["content"])
        finally:
            bridge.PERSONA_REPORTS.pop(persona, None)
            bridge.PERSONAS.pop(persona, None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
