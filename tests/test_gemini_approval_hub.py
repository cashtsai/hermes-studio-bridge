"""Gemini 權限請求接上審核中心(`feat/gemini-approval-hub`)。

這刀之前 `session/request_permission` 是**無條件自動拒絕** —— 所以
App Store 文案的「在它卡住時放行」對 Gemini 根本不成立(2026-10-10 上架前
稽核查出的不實宣稱之一)。`_gm_on_request` 的註解自己也寫著「互動審批接
Approval Hub 是下一刀」。

架構照 cc2 seam:**真相在等待者手上**,決定端點只負責叫醒,DB mark 與卡片
收尾由等待者協程統一做 —— 決議與逾時走同一條收尾路,不會出現「卡片已決、
agent 還在那裡等」。

釘住的五件事:
  1. 核准 → ACP `{"outcome":{"outcome":"selected","optionId":<allow 的 id>}}`
  2. 駁回 → selected + reject 的 id
  3. **options 對不到就退回 cancelled**,不可以瞎猜一個 optionId 送出去
  4. 逾時 → cancelled + DB 落 expired(fail-closed,等同這刀之前的行為)
  5. 等待者不在(已決/逾時/重啟過)→ 決定端點 409,且**不准改 DB**
     (否則會變成「app 顯示已核准、agent 其實早被拒」)
"""
import _isolation  # noqa: F401
import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="gm-approval-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["POCKET_GEMINI_APPROVAL_SEC"] = "0.4"   # 逾時測試不要等 180s
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402  (import 觸發 _canon_init → 建表)

ALLOW = {"optionId": "allow-1", "name": "Allow once", "kind": "allow_once"}
REJECT = {"optionId": "reject-1", "name": "Reject", "kind": "reject_once"}


def _status(aid):
    con = sqlite3.connect(os.environ["POCKET_CANON_DB"])
    try:
        row = con.execute("SELECT status FROM approvals WHERE id=?",
                          (aid,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _pending_ids():
    con = sqlite3.connect(os.environ["POCKET_CANON_DB"])
    try:
        return [r[0] for r in con.execute(
            "SELECT id FROM approvals WHERE provider='gemini' "
            "AND status='pending'").fetchall()]
    finally:
        con.close()


class PickOptionTest(unittest.TestCase):
    def test_maps_approve_to_allow(self):
        self.assertEqual(bridge._gm_pick_option([REJECT, ALLOW], True), "allow-1")

    def test_maps_deny_to_reject(self):
        self.assertEqual(bridge._gm_pick_option([ALLOW, REJECT], False), "reject-1")

    def test_no_match_returns_none(self):
        """對不到要回 None,讓呼叫端退回 cancelled —— 不准瞎猜。"""
        self.assertIsNone(bridge._gm_pick_option([], True))
        self.assertIsNone(bridge._gm_pick_option(
            [{"optionId": "x", "kind": "something_else"}], True))
        self.assertIsNone(bridge._gm_pick_option(["not-a-dict"], True))


class RequestFlowTest(unittest.TestCase):
    def setUp(self):
        self._orig_push = bridge._approval_push
        bridge._approval_push = lambda *a, **k: None
        bridge._GM_CARD_DIGESTS.clear()
        bridge._GM_TURNS.clear()
        bridge._GM_PERM_WAITERS.clear()
        bridge._GM_TURNS["acp-1"] = {"cid": "c", "text": "", "key": "gm-key"}

    def tearDown(self):
        bridge._approval_push = self._orig_push

    def _req(self, options):
        return {"method": "session/request_permission",
                "params": {"sessionId": "acp-1",
                           "toolCall": {"title": "WriteFile"},
                           "options": options}}

    def _run(self, options, decide=None):
        """跑一次請求,回 (outcome, aid)。decide 非 None 時叫醒等待者。

        **一律先抓到 aid 再收尾** —— 不要事後用 `ORDER BY created_at` 去猜
        哪一筆是這次的:created_at 是秒級整數,同一秒內多筆會並列,會抓到
        別的測試留下的那筆(我第一版就是這樣紅的)。
        """
        async def main():
            task = asyncio.create_task(bridge._gm_on_request(self._req(options)))
            aid = None
            for _ in range(200):                # 等等待者登記好
                await asyncio.sleep(0.002)
                live = list(bridge._GM_PERM_WAITERS.keys())
                if live:
                    aid = live[0]
                    break
                if task.done():                 # 還沒登記就結束 = 走了退回路徑
                    break
            if decide is not None and aid:
                bridge._gm_approval_decide(aid, {"approve": decide})
            return await task, aid
        return asyncio.run(main())

    def test_approve_returns_selected_allow_option(self):
        out, aid = self._run([ALLOW, REJECT], decide=True)
        self.assertEqual(out, {"outcome": {"outcome": "selected",
                                           "optionId": "allow-1"}})
        self.assertEqual(_status(aid), "approved")

    def test_deny_returns_selected_reject_option(self):
        out, aid = self._run([ALLOW, REJECT], decide=False)
        self.assertEqual(out, {"outcome": {"outcome": "selected",
                                           "optionId": "reject-1"}})
        self.assertEqual(_status(aid), "denied")

    def test_unusable_options_fall_back_to_cancelled(self):
        """Gemini 沒給可用選項 → 退回 cancelled(= 這刀之前的行為)。"""
        out, aid = self._run([{"optionId": "x", "kind": "weird"}], decide=True)
        self.assertEqual(out, {"outcome": {"outcome": "cancelled"}})

    def test_timeout_is_fail_closed_and_marks_expired(self):
        out, aid = self._run([ALLOW, REJECT])    # 不決定 → 等到逾時
        self.assertEqual(out, {"outcome": {"outcome": "cancelled"}})
        self.assertIsNotNone(aid)
        self.assertEqual(_status(aid), "expired")

    def test_waiter_is_removed_after_resolution(self):
        self._run([ALLOW, REJECT], decide=True)
        self.assertEqual(bridge._GM_PERM_WAITERS, {},
                         "等待者要收乾淨,否則第二次決定會打到舊 future")

    def test_unroutable_session_falls_back(self):
        """認不出哪條對話 → 出不了卡也找不到它,退回舊行為。"""
        bridge._GM_TURNS.clear()
        orig = bridge.GEMINI.acp_to_key
        bridge.GEMINI.acp_to_key = lambda _: None
        try:
            out = asyncio.run(bridge._gm_on_request(self._req([ALLOW])))
        finally:
            bridge.GEMINI.acp_to_key = orig
        self.assertEqual(out, {"outcome": {"outcome": "cancelled"}})


class DecideEndpointTest(unittest.TestCase):
    def test_no_waiter_is_409_and_does_not_touch_db(self):
        bridge._GM_PERM_WAITERS.clear()
        before = _status("gm-nonexistent")
        with self.assertRaises(Exception) as ctx:
            bridge._gm_approval_decide("gm-nonexistent", {"approve": True})
        self.assertEqual(getattr(ctx.exception, "status_code", None), 409)
        self.assertEqual(_status("gm-nonexistent"), before)

    def test_other_methods_pass_through(self):
        out = asyncio.run(bridge._gm_on_request({"method": "session/other"}))
        self.assertIsNone(out, "非權限請求要原樣放過(回 None)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
