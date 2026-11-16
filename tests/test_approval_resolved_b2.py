"""B2 審批 blocking-request 語義補強(grok-build steal #1)。

釘三件事:
  1. resolved 廣播 —— 任何把 approvals 翻成終局的 chokepoint(決議路由、
     TTL 過期掃除、provider 死亡掃除、waiter RAII)都發 persisted
     `approval.resolved`;同終局重複 mark 靠 external_id 去重只有一筆。
  2. 多端搶答 —— first-wins,第二端拿冪等收據(不是 409 嚇人)。
  3. waiter key 穩定性 + RAII —— cc2 的 aid 是 (session,tool,參數) 持久
     雜湊,重啟後重問復活同列不開新卡;waiter 被取消(provider 死)必
     留 expired 終局,絕不懸空。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="b2-approvals-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ["CC_SDK_STATE"] = os.path.join(_TMP, "cc-sdk-sessions.json")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge      # noqa: E402  (import 即接好 cc_sdk 的 DB 掛鉤)
import cc_sdk      # noqa: E402


def _insert(aid: str, session_id: str, provider: str = "hermes",
            kind: str = "permission", status: str = "pending",
            expires: float | None = None) -> None:
    con = sqlite3.connect(bridge.CANON_DB)
    con.execute(
        "INSERT OR REPLACE INTO approvals(id,title,source,risk,detail,"
        "created_at,expires_at,status,decided_at,result,callback,"
        "session_id,provider,kind,options) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (aid, f"t-{aid}", session_id, "low", "d", time.time(),
         expires if expires is not None else time.time() + 600,
         status, None, None, None, session_id, provider, kind, None))
    con.commit()
    con.close()


def _resolved_events(session: str) -> list:
    return [e for e in bridge._event_since(session, 0)
            if e["type"] == "approval.resolved"]


class TestResolvedBroadcast(unittest.TestCase):
    def test_type_registered_as_persisted(self):
        self.assertIn("approval.resolved", bridge.EVENT_PERSISTED)

    def test_local_decide_broadcasts_once_and_second_gets_receipt(self):
        _insert("b2-hp-1", "hermes:boss")
        r1 = asyncio.run(bridge._approval_decide_core("b2-hp-1", {"approve": True}))
        self.assertEqual(r1["status"], "approved")
        evs = _resolved_events("boss")   # persona 走裸 mid(全域訂閱吃得到)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["data"]["approval_id"], "b2-hp-1")
        self.assertEqual(evs[0]["data"]["status"], "approved")
        self.assertEqual(evs[0]["data"]["session_id"], "hermes:boss")
        # 第二端晚一步 → 冪等收據(不是 409),且不多發事件
        r2 = asyncio.run(bridge._approval_decide_core("b2-hp-1", {"approve": False}))
        self.assertTrue(r2.get("idempotent"))
        self.assertEqual(r2["status"], "approved")
        self.assertEqual(len(_resolved_events("boss")), 1)

    def test_ttl_expiry_broadcasts_expired(self):
        _insert("b2-hp-2", "hermes:boss2", expires=time.time() - 5)
        con = sqlite3.connect(bridge.CANON_DB)
        bridge._approvals_expire(con)
        con.commit()
        con.close()
        evs = _resolved_events("boss2")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["data"]["status"], "expired")
        d = bridge._approval_get_row("b2-hp-2")
        self.assertEqual(d["status"], "expired")

    def test_codex_provider_death_sweep_broadcasts(self):
        _insert("b2-cx-1", "codex:th1", provider="codex")
        bridge.CODEX_APP._expire_stale_codex_approvals()
        evs = _resolved_events("codex:th1")   # provider 用完整 v2 id
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["data"]["status"], "expired")

    def test_cc2_restart_orphan_sweep_broadcasts(self):
        _insert("b2-cc2-orph", "cc2:gamma", provider="cc2")
        n = bridge._cc2_reconcile_orphan_approvals()
        self.assertGreaterEqual(n, 1)
        evs = [e for e in _resolved_events("cc2:gamma")
               if e["data"]["approval_id"] == "b2-cc2-orph"]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["data"]["status"], "expired")

    def test_generic_mark_dedupes_double_terminal(self):
        _insert("b2-oc-1", "openclaw:main", provider="openclaw")
        self.assertTrue(bridge._oc_approval_mark("b2-oc-1", "expired"))
        self.assertFalse(bridge._oc_approval_mark("b2-oc-1", "expired"))
        self.assertEqual(len(_resolved_events("openclaw:main")), 1)


class TestWaiterKeyStability(unittest.TestCase):
    def test_same_question_same_key_across_restart(self):
        a = cc_sdk.approval_key("Ops", "Bash", {"command": "ls"})
        b = cc_sdk.approval_key("Ops", "Bash", {"command": "ls"})
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("cc2-"))
        self.assertNotEqual(a, cc_sdk.approval_key("Ops", "Bash", {"command": "pwd"}))
        self.assertNotEqual(a, cc_sdk.approval_key("Dev", "Bash", {"command": "ls"}))

    def test_reask_after_restart_revives_same_row_not_new_card(self):
        aid = cc_sdk.approval_key("Ops", "Bash", {"command": "ls"})
        rec = {"id": aid, "title": "t", "source": "cc2:Ops", "risk": "medium",
               "detail": "d", "created_at": time.time(),
               "expires_at": time.time() + 115, "session_id": "cc2:Ops",
               "provider": "cc2", "kind": "permission",
               "options": [{"key": "approve", "label": "允許", "style": "primary"}]}
        bridge._cc2_approval_db_upsert(rec)
        # 第一問逾時(bridge 重啟前的世界)
        self.assertTrue(bridge._oc_approval_mark(aid, "expired"))
        # 重啟後 CLI 重問同一個工具呼叫 → 同 aid 復活同列,不開新列
        rec2 = dict(rec, created_at=time.time() + 1, expires_at=time.time() + 120)
        bridge._cc2_approval_db_upsert(rec2)
        con = sqlite3.connect(bridge.CANON_DB)
        rows = con.execute("SELECT status FROM approvals WHERE id=?",
                           (aid,)).fetchall()
        n = con.execute("SELECT COUNT(*) FROM approvals WHERE session_id='cc2:Ops'"
                        ).fetchone()[0]
        con.close()
        self.assertEqual(rows, [("pending",)], "重問要把同列復活回 pending")
        self.assertEqual(n, 1, "不准開第二列")
        # 復活後決議要走得通(冪等收據不可攔住活 waiter 的新問題)——
        # 這裡只驗 DB 路由層:pending 列的 cc2 分支需要活 waiter,沒有就 409。
        d = bridge._approval_get_row(aid)
        self.assertEqual(d["status"], "pending")


class TestWaiterRAII(unittest.TestCase):
    def setUp(self):
        cc_sdk.reset_registry_for_tests()
        cc_sdk._sdk_mod = types.SimpleNamespace(
            PermissionResultAllow=lambda **kw: types.SimpleNamespace(
                behavior="allow", **kw),
            PermissionResultDeny=lambda **kw: types.SimpleNamespace(
                behavior="deny", **kw))
        self.addCleanup(lambda: setattr(cc_sdk, "_sdk_mod", None))
        self.addCleanup(cc_sdk.reset_registry_for_tests)

    def test_cancelled_waiter_leaves_expired_terminal_and_broadcast(self):
        async def run():
            sess = cc_sdk.registry().ensure("b2raii", workdir=_TMP)
            with patch.dict(os.environ, {"CC_SDK_APPROVAL_TIMEOUT": "60"}):
                task = asyncio.get_event_loop().create_task(
                    sess._can_use_tool("Bash", {"command": "x"},
                                       type("Ctx", (), {})()))
                while not cc_sdk.registry().pending_approvals:
                    await asyncio.sleep(0.005)
                aid = next(iter(cc_sdk.registry().pending_approvals))
                task.cancel()   # 回合被取消 / provider 收攤
                with self.assertRaises(asyncio.CancelledError):
                    await task
                return sess, aid

        sess, aid = asyncio.run(run())
        self.assertEqual(cc_sdk.registry().pending_approvals, {},
                         "waiter 消亡必須離場")
        d = bridge._approval_get_row(aid)
        self.assertEqual(d["status"], "expired", "RAII:消亡必留終局")
        evs = _resolved_events("cc2:b2raii")
        self.assertEqual([e["data"]["approval_id"] for e in evs], [aid])
        self.assertEqual(evs[0]["data"]["status"], "expired")
        # 卡片流那張也要收尾(不留可按的殭屍卡)
        appr = [e["data"]["card"] for e in sess.digest.store.events
                if e["type"] == "card.upsert"
                and e["data"]["card"]["kind"] == "approval"]
        self.assertEqual(appr[-1]["body"]["resolved"], "expired")

    def test_double_answer_first_wins_second_409_while_live(self):
        async def run():
            sess = cc_sdk.registry().ensure("b2race", workdir=_TMP)
            with patch.dict(os.environ, {"CC_SDK_APPROVAL_TIMEOUT": "60"}):
                task = asyncio.get_event_loop().create_task(
                    sess._can_use_tool("Edit", {"file_path": "/tmp/x"},
                                       type("Ctx", (), {})()))
                while not cc_sdk.registry().pending_approvals:
                    await asyncio.sleep(0.005)
                aid = next(iter(cc_sdk.registry().pending_approvals))
                first = cc_sdk.registry().decide_approval(aid, "approve")
                second = cc_sdk.registry().decide_approval(aid, "deny")
                res = await task
                return aid, first, second, res

        aid, first, second, res = asyncio.run(run())
        self.assertEqual(first, "approved")
        self.assertIsNone(second, "second-answer 在 waiter 層讓路(路由層回收據/409)")
        self.assertEqual(res.behavior, "allow", "first-wins:生效的是第一個決定")
        # DB 終局 approved + 廣播一筆;第二端此後走冪等收據
        d = bridge._approval_get_row(aid)
        self.assertEqual(d["status"], "approved")
        r2 = asyncio.run(bridge._approval_decide_core(aid, {"approve": False}))
        self.assertTrue(r2.get("idempotent"))
        self.assertEqual(r2["status"], "approved")
        evs = [e for e in _resolved_events("cc2:b2race")
               if e["data"]["approval_id"] == aid]
        self.assertEqual(len(evs), 1)


if __name__ == "__main__":
    unittest.main()
