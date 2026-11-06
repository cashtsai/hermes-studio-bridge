"""Guest 互動版(2026-11-06,E3,t3code 旁觀者範式)。

guest 擬稿:率限/長度/轉義/落表;owner 核准 = 走該 session 既有 input 路徑
(monkeypatch),忽略 = 不送;revoke/過期 token 一律 410。
"""
import _isolation  # noqa: F401
import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="guest-interactive-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["HOME"] = _TMP      # guest secret 落 ~/.pocket,測試隔離
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class _Req:
    """極簡 request 替身(guest 端點只碰 json body)。"""
    client = None
    query_params: dict = {}

    def __init__(self, body=None):
        self._b = body or {}

    async def json(self):
        return self._b


class _FakeStore:
    def snapshot(self, limit=100, before_seq=None):
        return {"cards": [], "latest_seq": 0, "status": {}}


async def _fake_card_store(sid):
    return _FakeStore()


class _GuestBase(unittest.TestCase):
    def setUp(self):
        self._orig_store = bridge._v2_card_store
        self._orig_input = bridge.v2_session_input
        bridge._v2_card_store = _fake_card_store
        bridge._GUEST_SUGGEST_RL.clear()

    def tearDown(self):
        bridge._v2_card_store = self._orig_store
        bridge.v2_session_input = self._orig_input

    @staticmethod
    def _suggest(token, text):
        return asyncio.run(bridge.guest_suggest(token, _Req({"text": text})))

    @staticmethod
    def _db():
        return sqlite3.connect(bridge.CANON_DB)


class TestSuggest(_GuestBase):
    def test_lands_in_table_and_approval(self):
        tok, _ = bridge._guest_token_make("hermes:guesttest", 3600)
        out = self._suggest(tok, "幫我跑一下測試")
        self.assertTrue(out["ok"])
        con = self._db()
        row = con.execute(
            "SELECT session, text, status, approval_id FROM guest_suggestions"
            " WHERE id=?", (out["id"],)).fetchone()
        con.close()
        self.assertEqual(row[0], "hermes:guesttest")
        self.assertEqual(row[1], "幫我跑一下測試")
        self.assertEqual(row[2], "pending")
        appr = bridge._approval_get_row(row[3])
        self.assertEqual(appr["kind"], "guest_message")
        self.assertEqual(appr["status"], "pending")
        self.assertEqual([o["key"] for o in appr["options"]],
                         ["send", "ignore"])
        # DB-backed:pending 必答得動,不走 provider 等待者探活
        self.assertEqual(appr["response_capability"], "live")
        self.assertEqual(appr["meta"]["suggestion_id"], out["id"])

    def test_escape_in_guest_page(self):
        tok, _ = bridge._guest_token_make("hermes:guestesc", 3600)
        self._suggest(tok, "<script>alert(1)</script>")
        html = asyncio.run(bridge.guest_view(tok)).body.decode()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        # 互動面已就位:擬稿框 + 輪詢腳本
        self.assertIn("id='sf'", html)
        self.assertIn("setInterval(poll,5000)", html)
        self.assertIn("等待主人審核", html)

    def test_rate_limit_6_per_minute(self):
        tok, _ = bridge._guest_token_make("hermes:guestrl", 3600)
        for i in range(6):
            self._suggest(tok, f"第 {i} 則")
        with self.assertRaises(bridge.HTTPException) as cm:
            self._suggest(tok, "第 7 則")
        self.assertEqual(cm.exception.status_code, 429)
        # 別的令牌不受牽連(率限按 token 分桶)
        tok2, _ = bridge._guest_token_make("hermes:guestrl", 3600)
        self.assertTrue(self._suggest(tok2, "另一枚令牌")["ok"])

    def test_length_and_empty_guards(self):
        tok, _ = bridge._guest_token_make("hermes:guestlen", 3600)
        with self.assertRaises(bridge.HTTPException) as cm:
            self._suggest(tok, "x" * 2001)
        self.assertEqual(cm.exception.status_code, 413)
        with self.assertRaises(bridge.HTTPException) as cm:
            self._suggest(tok, "   ")
        self.assertEqual(cm.exception.status_code, 400)

    def test_revoked_and_expired_tokens_410(self):
        tok, _ = bridge._guest_token_make("hermes:guestrev", 3600)
        bridge._guest_revoke_all()
        with self.assertRaises(bridge.HTTPException) as cm:
            self._suggest(tok, "呼叫已撤銷的連結")
        self.assertEqual(cm.exception.status_code, 410)
        expired, _ = bridge._guest_token_make("hermes:guestrev", -10)
        with self.assertRaises(bridge.HTTPException) as cm:
            self._suggest(expired, "過期")
        self.assertEqual(cm.exception.status_code, 410)


class TestOwnerDecision(_GuestBase):
    def _create(self, sid, text):
        tok, _ = bridge._guest_token_make(sid, 3600)
        out = self._suggest(tok, text)
        con = self._db()
        aid = con.execute(
            "SELECT approval_id FROM guest_suggestions WHERE id=?",
            (out["id"],)).fetchone()[0]
        con.close()
        return out["id"], aid

    def test_approve_forwards_via_input_path(self):
        captured = {}

        async def fake_input(session_id, request):
            captured["sid"] = session_id
            captured["body"] = await request.json()
            return {"ok": True, "accepted": True}

        bridge.v2_session_input = fake_input
        sugg_id, aid = self._create("codex:tgt-send", "請執行部署")
        res = asyncio.run(bridge._approval_decide_core(aid, {"key": "send"}))
        self.assertEqual(res["status"], "approved")
        self.assertEqual(res["key"], "send")
        # 訊息真的走了既有 input 路徑(統一 v2 路由)
        self.assertEqual(captured["sid"], "codex:tgt-send")
        self.assertEqual(captured["body"]["text"], "請執行部署")
        self.assertTrue(captured["body"]["client_id"].startswith("guest-"))
        con = self._db()
        st = con.execute("SELECT status FROM guest_suggestions WHERE id=?",
                         (sugg_id,)).fetchone()[0]
        con.close()
        self.assertEqual(st, "sent")
        # 第二次按 = 冪等收據,不重送
        captured.clear()
        res2 = asyncio.run(bridge._approval_decide_core(aid, {"key": "send"}))
        self.assertTrue(res2.get("idempotent"))
        self.assertEqual(captured, {})

    def test_approve_compat_sugar(self):
        captured = {}

        async def fake_input(session_id, request):
            captured["sid"] = session_id
            return {"ok": True}

        bridge.v2_session_input = fake_input
        _, aid = self._create("hermes:guestsugar", "sugar")
        res = asyncio.run(bridge._approval_decide_core(aid, {"approve": True}))
        self.assertEqual(res["status"], "approved")
        self.assertEqual(captured["sid"], "hermes:guestsugar")

    def test_ignore_does_not_forward(self):
        called = []

        async def fake_input(session_id, request):
            called.append(session_id)
            return {"ok": True}

        bridge.v2_session_input = fake_input
        sugg_id, aid = self._create("claude_code:tgt-ign", "不要送這句")
        res = asyncio.run(bridge._approval_decide_core(aid, {"key": "ignore"}))
        self.assertEqual(res["status"], "denied")
        self.assertEqual(called, [], "忽略絕不能碰 session input")
        con = self._db()
        st = con.execute("SELECT status FROM guest_suggestions WHERE id=?",
                         (sugg_id,)).fetchone()[0]
        con.close()
        self.assertEqual(st, "ignored")

    def test_forward_failure_marks_failed(self):
        async def fake_input(session_id, request):
            raise bridge.http_err(409, "CONFLICT", "session 忙不過來")

        bridge.v2_session_input = fake_input
        sugg_id, aid = self._create("codex:tgt-fail", "會失敗的")
        with self.assertRaises(bridge.HTTPException) as cm:
            asyncio.run(bridge._approval_decide_core(aid, {"key": "send"}))
        self.assertEqual(cm.exception.status_code, 502)
        con = self._db()
        st = con.execute("SELECT status FROM guest_suggestions WHERE id=?",
                         (sugg_id,)).fetchone()[0]
        con.close()
        self.assertEqual(st, "failed")

    def test_unknown_key_rejected(self):
        _, aid = self._create("hermes:guestkey", "key 檢查")
        with self.assertRaises(bridge.HTTPException) as cm:
            asyncio.run(bridge._approval_decide_core(aid, {"key": "yolo"}))
        self.assertEqual(cm.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
