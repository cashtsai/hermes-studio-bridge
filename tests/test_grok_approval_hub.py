"""Grok(S6)權限請求接上審核中心 —— 審批映射的守門。

架構同 cc2/gemini seam:`session/request_permission` 是 blocking reverse-
request(研究報告 §2:「審批是活的 RPC,不是事件」),真相在等待者手上,
決定端點只叫醒;DB mark / 卡片收尾 / ACP outcome 全由等待者協程統一做。

釘住的六件事:
  1. 核准 → `{"outcome":{"outcome":"selected","optionId":<allow 的 id>}}`
  2. 駁回 → selected + reject 的 id
  3. **放行偏好 allow_once**:agent 把 allow_always 排前面時,不可替使用者
     按「永久允許」(這是 grok 比照 acp_client._NEVER_AUTO 的教訓新加的,
     gemini 的 _gm_pick_option 沒這層)
  4. options 對不到 → cancelled,不瞎猜 optionId
  5. 逾時 → cancelled + DB 落 expired(fail-closed)
  6. 等待者不在 → 決定端點 409 且不改 DB
"""
import _isolation  # noqa: F401
import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="gk-approval-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["POCKET_GROK_APPROVAL_SEC"] = "0.4"   # 逾時測試不要等 180s
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402  (import 觸發 _canon_init → 建表)

ALLOW = {"optionId": "allow-1", "name": "Allow once", "kind": "allow_once"}
ALLOW_ALWAYS = {"optionId": "allow-forever", "name": "Always allow",
                "kind": "allow_always"}
REJECT = {"optionId": "reject-1", "name": "Reject", "kind": "reject_once"}


def _status(aid):
    con = sqlite3.connect(bridge.CANON_DB)
    try:
        row = con.execute("SELECT status FROM approvals WHERE id=?",
                          (aid,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


class PickOptionTest(unittest.TestCase):
    def test_prefers_allow_once_over_allow_always(self):
        """agent 把 allow_always 排前面 → 放行仍要選 allow_once。
        替使用者按「以後都不要再問」是最不該自動做的事(acp_client 的
        _NEVER_AUTO 同一條紅線)。"""
        self.assertEqual(
            bridge._gk_pick_option([ALLOW_ALWAYS, ALLOW, REJECT], True),
            "allow-1")

    def test_falls_back_to_allow_prefix_when_no_once(self):
        """只有 allow_always 可選時仍要能放行 —— 回應必須對得回 agent 給的
        選項,硬拒等於把「只有永久允許」的工具全鎖死。"""
        self.assertEqual(bridge._gk_pick_option([ALLOW_ALWAYS, REJECT], True),
                         "allow-forever")

    def test_deny_maps_to_reject(self):
        self.assertEqual(bridge._gk_pick_option([ALLOW, REJECT], False),
                         "reject-1")

    def test_no_match_returns_none(self):
        self.assertIsNone(bridge._gk_pick_option([], True))
        self.assertIsNone(bridge._gk_pick_option(
            [{"optionId": "x", "kind": "weird"}], True))
        self.assertIsNone(bridge._gk_pick_option(["not-a-dict"], True))


class RequestFlowTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, bridge, "_GK_PERM_TIMEOUT", bridge._GK_PERM_TIMEOUT)
        bridge._GK_PERM_TIMEOUT = 0.4
        self._orig_push = bridge._approval_push
        bridge._approval_push = lambda *a, **k: None
        bridge._GK_CARD_DIGESTS.clear()
        bridge._GK_TURNS.clear()
        bridge._GK_PERM_WAITERS.clear()
        bridge._GK_TURNS["acp-1"] = {"cid": "c", "text": "", "key": "gk-key"}

    def tearDown(self):
        bridge._approval_push = self._orig_push

    def _req(self, options):
        return {"method": "session/request_permission",
                "params": {"sessionId": "acp-1",
                           "toolCall": {"title": "WriteFile"},
                           "options": options}}

    def _run(self, options, decide=None):
        """跑一次請求,回 (outcome, aid)。先抓 aid 再收尾(同 gemini 測試的
        教訓:created_at 秒級,ORDER BY 會抓到別的測試那筆)。"""
        async def main():
            task = asyncio.create_task(bridge._gk_on_request(self._req(options)))
            aid = None
            for _ in range(200):
                await asyncio.sleep(0.002)
                live = list(bridge._GK_PERM_WAITERS.keys())
                if live:
                    aid = live[0]
                    break
                if task.done():
                    break
            if decide is not None and aid:
                bridge._gk_approval_decide(aid, {"approve": decide})
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
        out, _aid = self._run([{"optionId": "x", "kind": "weird"}], decide=True)
        self.assertEqual(out, {"outcome": {"outcome": "cancelled"}})

    def test_timeout_is_fail_closed_and_marks_expired(self):
        out, aid = self._run([ALLOW, REJECT])    # 不決定 → 等到逾時
        self.assertEqual(out, {"outcome": {"outcome": "cancelled"}})
        self.assertIsNotNone(aid)
        self.assertEqual(_status(aid), "expired")

    def test_waiter_is_removed_after_resolution(self):
        self._run([ALLOW, REJECT], decide=True)
        self.assertEqual(bridge._GK_PERM_WAITERS, {},
                         "等待者要收乾淨,否則第二次決定會打到舊 future")

    def test_record_lands_with_provider_grok_and_contract_options(self):
        """落表要是 provider='grok' + 正規化 {key,label,style} 選項 ——
        ACP 原形塞進去 app 會畫出空白按鈕(S5 的實際事故)。"""
        import json as _json
        _out, aid = self._run([ALLOW, REJECT], decide=True)
        con = sqlite3.connect(bridge.CANON_DB)
        try:
            row = con.execute(
                "SELECT provider, session_id, options FROM approvals "
                "WHERE id=?", (aid,)).fetchone()
        finally:
            con.close()
        self.assertEqual(row[0], "grok")
        self.assertEqual(row[1], "grok:gk-key")
        opts = _json.loads(row[2])
        self.assertEqual(opts[0], {"key": "allow-1", "label": "Allow once",
                                   "style": "primary", "kind": "allow_once"})
        self.assertEqual(opts[1]["style"], "deny")
        self.assertEqual(opts[1]["kind"], "reject_once")

    def test_unroutable_session_falls_back(self):
        bridge._GK_TURNS.clear()
        orig = bridge.GROK.acp_to_key
        bridge.GROK.acp_to_key = lambda _: None
        try:
            out = asyncio.run(bridge._gk_on_request(self._req([ALLOW])))
        finally:
            bridge.GROK.acp_to_key = orig
        self.assertEqual(out, {"outcome": {"outcome": "cancelled"}})


class DecideEndpointTest(unittest.TestCase):
    def test_no_waiter_is_409_and_does_not_touch_db(self):
        bridge._GK_PERM_WAITERS.clear()
        before = _status("gk-nonexistent")
        with self.assertRaises(Exception) as ctx:
            bridge._gk_approval_decide("gk-nonexistent", {"approve": True})
        self.assertEqual(getattr(ctx.exception, "status_code", None), 409)
        self.assertEqual(_status("gk-nonexistent"), before)

    def test_other_methods_pass_through(self):
        out = asyncio.run(bridge._gk_on_request({"method": "session/other"}))
        self.assertIsNone(out, "非權限請求要原樣放過(回 None)")

    def test_disconnect_reask_after_restart_is_409_not_silent(self):
        """斷線重問:bridge 重啟後等待者消失(live-only 的宣告),app 再送
        決定要吃 409 —— 不准安靜吞掉讓使用者以為批過了。ACP 側由逾時路
        fail-closed 收尾(上面的 timeout 測試),兩邊語意一致。"""
        bridge._GK_PERM_WAITERS.clear()   # = 重啟後的世界
        with self.assertRaises(Exception) as ctx:
            bridge._gk_approval_decide("gk-after-restart", {"key": "allow-1"})
        self.assertEqual(getattr(ctx.exception, "status_code", None), 409)


if __name__ == "__main__":
    unittest.main(verbosity=2)
