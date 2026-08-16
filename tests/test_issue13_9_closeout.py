"""Issue #13/#9 收尾切片測試。

涵蓋:
1. #13 契約 body {"approval_id","decision":"approve"|"deny"} — decision 字串
   在 _approval_decide_core 入口折成 approve bool(hermes fallback 分支驗證)。
2. #13 v2 統一路由:POST /app/v2/sessions/{id}/approve 帶 {approval_id,decision}
   走 uni path(不再落到 provider 分支要求 key)。
3. #9 _APP_TURN_INFLIGHT:claim → launch 之間失敗要釋放 claim,同 client_id
   重試不得被 in_flight 擋到 600s TTL。

跑法同其他測試:POCKET_CANON_DB 指到 tmp 庫再 import bridge。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="i139-canon-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


def _insert_pending(aid, session_id="hermes:xcash", provider="hermes",
                    kind="permission", options=None):
    con = sqlite3.connect(bridge.CANON_DB)
    now = time.time()
    con.execute(
        "INSERT OR REPLACE INTO approvals"
        "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result,callback,"
        "session_id,provider,kind,options) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (aid, f"t-{aid}", session_id, "high", "d", now, now + 600, "pending",
         None, None, None, session_id, provider, kind,
         json.dumps(options, ensure_ascii=False) if options else None))
    con.commit()
    con.close()


def _status_of(aid):
    con = sqlite3.connect(bridge.CANON_DB)
    r = con.execute("SELECT status FROM approvals WHERE id=?", (aid,)).fetchone()
    con.close()
    return r[0] if r else None


class _FakeReq:
    client = None

    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


class TestDecisionStringContract(unittest.TestCase):
    """#13:decision 字串與 approve bool 等價。"""

    def test_decision_approve(self):
        _insert_pending("dc-ap")
        r = asyncio.run(bridge._approval_decide_core("dc-ap", {"decision": "approve"}))
        self.assertEqual(r["status"], "approved")
        self.assertEqual(_status_of("dc-ap"), "approved")

    def test_decision_deny(self):
        _insert_pending("dc-dn")
        r = asyncio.run(bridge._approval_decide_core("dc-dn", {"decision": "deny"}))
        self.assertEqual(r["status"], "denied")
        self.assertEqual(_status_of("dc-dn"), "denied")

    def test_approve_bool_untouched(self):
        # 既有 {approve: bool} 語意不受 normalization 影響。
        _insert_pending("dc-b1")
        r = asyncio.run(bridge._approval_decide_core("dc-b1", {"approve": True}))
        self.assertEqual(r["status"], "approved")

    def test_key_wins_over_decision(self):
        # 同時給 key 與 decision 時 key 是第一公民(A1 spec §3.2)。
        _insert_pending("dc-k1", options=[
            {"key": "go", "label": "Go", "style": "primary"},
            {"key": "stop", "label": "Stop", "style": "danger"}])
        r = asyncio.run(bridge._approval_decide_core(
            "dc-k1", {"key": "stop", "decision": "approve"}))
        self.assertEqual(r["status"], "denied")
        self.assertEqual(r["key"], "stop")


class TestV2ApproveUniPath(unittest.TestCase):
    """#13:v2 approve 帶 {approval_id, decision} 直接走 uni path。"""

    def test_uni_path_decision(self):
        _insert_pending("v2-uni")
        orig = bridge._check_auth
        bridge._check_auth = lambda r: None
        try:
            r = asyncio.run(bridge.v2_session_approve(
                "hermes:xcash",
                _FakeReq({"approval_id": "v2-uni", "decision": "deny"})))
        finally:
            bridge._check_auth = orig
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "denied")
        self.assertEqual(_status_of("v2-uni"), "denied")


class TestInflightReleaseOnPrelaunchError(unittest.TestCase):
    """#9:claim → launch 之間失敗要釋放 _APP_TURN_INFLIGHT claim。"""

    def test_v2_input_releases_claim(self):
        session, client_id = "tp-i9", "cid-i9"
        key = (session, client_id)

        async def _boom(*a, **kw):
            raise RuntimeError("prepare failed (test)")

        orig_prepare = bridge._persona_prepare_turn
        bridge._persona_prepare_turn = _boom
        try:
            with self.assertRaises(RuntimeError):
                asyncio.run(bridge._v2_persona_input(
                    session, f"hermes:{session}",
                    {"content": "hi", "client_id": client_id}, _FakeReq({})))
            # claim 已釋放:同 client_id 立刻重試不會拿到 in_flight。
            self.assertNotIn(key, bridge._APP_TURN_INFLIGHT)
            with self.assertRaises(RuntimeError):
                asyncio.run(bridge._v2_persona_input(
                    session, f"hermes:{session}",
                    {"content": "hi", "client_id": client_id}, _FakeReq({})))
            self.assertNotIn(key, bridge._APP_TURN_INFLIGHT)
        finally:
            bridge._persona_prepare_turn = orig_prepare
            bridge._APP_TURN_INFLIGHT.pop(key, None)

    def test_v2_input_dup_still_bounced_while_live(self):
        # 對照組:entry 活著時第二發仍回 in_flight(冪等擋重複執行不回退)。
        session, client_id = "tp-i9b", "cid-i9b"
        key = (session, client_id)
        bridge._APP_TURN_INFLIGHT[key] = {"ts": time.monotonic(),
                                          "task": None, "state": None}
        try:
            r = asyncio.run(bridge._v2_persona_input(
                session, f"hermes:{session}",
                {"content": "hi", "client_id": client_id}, _FakeReq({})))
            self.assertTrue(r.get("in_flight"))
        finally:
            bridge._APP_TURN_INFLIGHT.pop(key, None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
