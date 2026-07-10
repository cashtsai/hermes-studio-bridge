"""Approval Hub A1 切片測試(spec §6 A1 / §7 可離線驗項)。

跑法:POCKET_CANON_DB 指到 tmp 庫再 import bridge — module import 會執行
_canon_init()(migration 就在裡面),所以 import 本身就是被測物之一。
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="a1-canon-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402  (import 觸發 _canon_init → 建表 + migration)
from fastapi import HTTPException  # noqa: E402


def _cols():
    con = sqlite3.connect(bridge.CANON_DB)
    cols = [r[1] for r in con.execute("PRAGMA table_info(approvals)").fetchall()]
    con.close()
    return cols


def _insert_legacy(aid, source, status="pending", callback=None):
    """塞一列「遷移前形狀」的舊列(新欄位 NULL)模擬歷史資料。"""
    con = sqlite3.connect(bridge.CANON_DB)
    now = time.time()
    con.execute(
        "INSERT OR REPLACE INTO approvals"
        "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result,callback,"
        "session_id,provider,kind,options) VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL)",
        (aid, f"t-{aid}", source, "high", "d", now, now + 600, status, None, None, callback))
    con.commit()
    con.close()


class TestMigration(unittest.TestCase):
    def test_columns_added_and_idempotent(self):
        self.assertTrue({"session_id", "provider", "kind", "options"} <= set(_cols()))
        bridge._canon_init()          # 第二次跑不炸 = 冪等
        bridge._canon_init()
        self.assertTrue({"session_id", "provider", "kind", "options"} <= set(_cols()))

    def test_backfill(self):
        _insert_legacy("bf-cc", "claude_code:Ops")
        _insert_legacy("bf-cx", "codex:thread-1")
        _insert_legacy("bf-hp", "tg-post")
        bridge._canon_init()          # 回填在 init 裡
        con = sqlite3.connect(bridge.CANON_DB)
        rows = {r[0]: r for r in con.execute(
            "SELECT id,provider,session_id,kind FROM approvals WHERE id LIKE 'bf-%'")}
        con.close()
        self.assertEqual(rows["bf-cc"][1:], ("claude_code", "claude_code:Ops", "permission"))
        self.assertEqual(rows["bf-cx"][1:], ("codex", "codex:thread-1", "permission"))
        # hermes 舊列:session_id 不硬造(拍板 → NULL),kind 補 permission
        self.assertEqual(rows["bf-hp"][1], "hermes")
        self.assertIsNone(rows["bf-hp"][2])
        self.assertEqual(rows["bf-hp"][3], "permission")


class TestWireShape(unittest.TestCase):
    def test_legacy_row_unified(self):
        _insert_legacy("wire-1", "claude_code:Ops")
        d = bridge._approval_get_row("wire-1")
        for k in ("id", "session_id", "provider", "kind", "options",
                  "title", "source", "risk", "detail", "created_at",
                  "expires_at", "status", "decided_at", "result"):
            self.assertIn(k, d)
        self.assertEqual(d["provider"], "claude_code")
        self.assertEqual(d["kind"], "permission")
        # options 缺席 → 預設 approve/deny,deny 標 danger
        styles = {o["key"]: o.get("style") for o in d["options"]}
        self.assertEqual(styles.get("deny"), "danger")

    def test_notice_default_single_ack(self):
        opts = bridge._approval_default_options("notice")
        self.assertEqual([o["key"] for o in opts], ["ack"])


class TestDecideCore(unittest.TestCase):
    def _decide(self, aid, body):
        return asyncio.run(bridge._approval_decide_core(aid, body))

    def _mk(self, aid, kind, options=None, callback=None):
        con = sqlite3.connect(bridge.CANON_DB)
        now = time.time()
        con.execute(
            "INSERT OR REPLACE INTO approvals"
            "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result,callback,"
            "session_id,provider,kind,options) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, aid, "hermes:xcash", "", "", now, now + 600, "pending", None, None,
             callback, "hermes:xcash", "hermes", kind,
             json.dumps(options, ensure_ascii=False) if options else None))
        con.commit()
        con.close()

    def test_permission_key_and_409(self):
        self._mk("p1", "permission")
        r = self._decide("p1", {"key": "approve"})
        self.assertEqual(r["status"], "approved")
        with self.assertRaises(HTTPException) as cm:
            self._decide("p1", {"key": "approve"})
        self.assertEqual(cm.exception.status_code, 409)

    def test_permission_deny_writes_denied(self):
        self._mk("p2", "permission")
        r = self._decide("p2", {"key": "deny"})
        self.assertEqual(r["status"], "denied")     # 拍板:新字彙

    def test_bool_compat_sugar(self):
        self._mk("p3", "permission")
        r = self._decide("p3", {"approve": True})
        self.assertEqual((r["status"], r["key"]), ("approved", "approve"))
        self._mk("p4", "permission")
        r = self._decide("p4", {"approve": False})
        self.assertEqual((r["status"], r["key"]), ("denied", "deny"))

    def test_question_answered_result_key(self):
        self._mk("q1", "question", options=[{"key": "1", "label": "甲"},
                                            {"key": "2", "label": "乙"}])
        r = self._decide("q1", {"key": "2"})
        self.assertEqual(r["status"], "answered")
        d = bridge._approval_get_row("q1")
        self.assertEqual(d["result"], "2")

    def test_notice_ack(self):
        self._mk("n1", "notice")
        r = self._decide("n1", {"key": "ack"})
        self.assertEqual(r["status"], "acknowledged")

    def test_unknown_key_400(self):
        self._mk("q2", "question", options=[{"key": "1", "label": "甲"}])
        with self.assertRaises(HTTPException) as cm:
            self._decide("q2", {"key": "9"})
        self.assertEqual(cm.exception.status_code, 400)

    def test_callback_carries_kind_status(self):
        fired = []

        async def fake_cb(aid, cb, status, result):
            fired.append((aid, cb, status, result))
        orig = bridge._approval_fire_callback
        bridge._approval_fire_callback = fake_cb
        try:
            self._mk("cb1", "question", options=[{"key": "a", "label": "A"}],
                     callback="http://127.0.0.1:9/x")

            async def run():
                r = await bridge._approval_decide_core("cb1", {"key": "a"})
                await asyncio.sleep(0)      # 讓 create_task 的 callback 跑完
                return r
            r = asyncio.run(run())
            self.assertEqual(r["status"], "answered")
            self.assertEqual(fired, [("cb1", "http://127.0.0.1:9/x", "answered", "a")])
        finally:
            bridge._approval_fire_callback = orig


class TestWritersKeepColumns(unittest.TestCase):
    def test_cc_create_and_reupsert_keeps_new_cols(self):
        prompt = {"kind": "menu", "semantic": "question", "title": "選一個",
                  "options": [{"key": "1", "label": "甲"}, {"key": "2", "label": "乙"}]}
        aid = bridge._cc_approval_create("Ops", prompt)
        d = bridge._approval_get_row(aid)
        self.assertEqual((d["provider"], d["kind"], d["session_id"]),
                         ("claude_code", "question", "claude_code:Ops"))
        # question 無 danger 語意
        self.assertTrue(all(o.get("style") is None for o in d["options"]))

    def test_cc_permission_styles(self):
        prompt = {"kind": "menu", "semantic": "permission", "title": "wants to run",
                  "options": [{"key": "1", "label": "Allow"},
                              {"key": "2", "label": "Allow always"},
                              {"key": "3", "label": "Don't allow"}]}
        aid = bridge._cc_approval_create("Ops", prompt)
        styles = {o["key"]: o.get("style") for o in bridge._approval_get_row(aid)["options"]}
        self.assertEqual(styles, {"1": "primary", "2": "primary", "3": "danger"})

    def test_codex_upsert_style_canonicalized(self):
        rec = {"id": "codex-t1", "title": "t", "source": "codex:th-9", "risk": "high",
               "detail": "d", "created_at": time.time(),
               "options": [{"key": "approve", "label": "允許執行", "style": "primary"},
                           {"key": "deny", "label": "拒絕", "style": "deny"}]}
        bridge.CODEX_APP._approval_db_upsert(rec)
        d = bridge._approval_get_row("codex-t1")
        styles = {o["key"]: o.get("style") for o in d["options"]}
        self.assertEqual(styles["deny"], "danger")           # DB 收斂為規範字彙
        self.assertEqual(rec["options"][1]["style"], "deny")  # 記憶體 record 不動
        self.assertEqual((d["provider"], d["session_id"]), ("codex", "codex:th-9"))
        bridge.CODEX_APP._approval_db_upsert(rec)             # re-upsert 不清欄
        d2 = bridge._approval_get_row("codex-t1")
        self.assertEqual(d2["kind"], "permission")
        self.assertEqual(d2["session_id"], "codex:th-9")


class TestHermesPending(unittest.TestCase):
    def test_pending_by_session_oldest_first(self):
        con = sqlite3.connect(bridge.CANON_DB)
        now = time.time()
        for i, aid in enumerate(("hp-b", "hp-a")):
            con.execute(
                "INSERT OR REPLACE INTO approvals"
                "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result,callback,"
                "session_id,provider,kind,options) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,NULL)",
                (aid, aid, "hermes:shuijing", "", "", now - i, now + 600, "pending",
                 None, None, "hermes:shuijing", "hermes", "notice"))
        con.commit()
        con.close()
        m = bridge._hermes_pending_by_session()
        self.assertIn("hermes:shuijing", m)
        self.assertEqual(m["hermes:shuijing"]["id"], "hp-a")   # 最早的那筆


if __name__ == "__main__":
    unittest.main(verbosity=2)
