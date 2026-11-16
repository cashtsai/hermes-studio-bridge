"""OpenCode(S7)權限請求接上審核中心 —— 審批映射的守門。

架構同 cc2/gemini/grok seam:`session/request_permission` 是 blocking
reverse-request(審批是活的 RPC,不是事件),真相在等待者手上,決定端點
只叫醒;DB mark / 卡片收尾 / ACP outcome 全由等待者協程統一做。

這一檔用的 options 就是 **2026-11-16 真機錄到的那三顆**
(once / always / reject),所以「放行偏好 allow_once」不是假設而是實況。

釘住的七件事:
  1. 核准 → `{"outcome":{"outcome":"selected","optionId":"once"}}`
  2. 駁回 → selected + reject 的 id
  3. **放行偏好 allow_once**:不替使用者按「永久允許」
  4. options 對不到 → cancelled,不瞎猜 optionId
  5. 逾時 → cancelled + DB 落 expired(fail-closed)
  6. 等待者不在 → 決定端點 409 且不改 DB
  7. 落表 provider='opencode'(不是 openclaw —— 前綴只差三個字母)
"""
import _isolation  # noqa: F401
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="oce-approval-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["POCKET_OPENCODE_APPROVAL_SEC"] = "0.4"   # 逾時測試不要等 180s
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402  (import 觸發 _canon_init → 建表)

_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "opencode", "recorded-stdio-20261116.txt")


def _recorded_permission() -> dict:
    with open(_FIXTURE, encoding="utf-8") as f:
        for line in f:
            if "session/request_permission" in line:
                return json.loads(line[2:].strip())
    raise AssertionError("fixture 裡沒有 request_permission")


#: 真機的三顆選項(once / always / reject)。
REAL_OPTIONS = _recorded_permission()["params"]["options"]
ALLOW_ONCE = next(o for o in REAL_OPTIONS if o["kind"] == "allow_once")
ALLOW_ALWAYS = next(o for o in REAL_OPTIONS if o["kind"] == "allow_always")
REJECT = next(o for o in REAL_OPTIONS if o["kind"] == "reject_once")


def _status(aid):
    con = sqlite3.connect(bridge.CANON_DB)
    try:
        row = con.execute("SELECT status FROM approvals WHERE id=?",
                          (aid,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


class PickOptionTest(unittest.TestCase):
    """挑選邏輯與 grok 共用(_oce_pick_option is _gk_pick_option)——
    這裡用**真機 options** 再驗一次,確認共用對這家也成立。"""

    def test_prefers_allow_once_over_allow_always(self):
        self.assertEqual(
            bridge._oce_pick_option([ALLOW_ALWAYS, ALLOW_ONCE, REJECT], True),
            ALLOW_ONCE["optionId"])

    def test_real_wire_order_still_picks_once(self):
        """實測的順序就是 once/always/reject;換了順序也不准選 always。"""
        self.assertEqual(bridge._oce_pick_option(REAL_OPTIONS, True), "once")

    def test_falls_back_to_allow_prefix_when_no_once(self):
        self.assertEqual(
            bridge._oce_pick_option([ALLOW_ALWAYS, REJECT], True),
            ALLOW_ALWAYS["optionId"])

    def test_deny_maps_to_reject(self):
        self.assertEqual(bridge._oce_pick_option(REAL_OPTIONS, False),
                         "reject")

    def test_no_match_returns_none(self):
        self.assertIsNone(bridge._oce_pick_option([], True))
        self.assertIsNone(bridge._oce_pick_option(
            [{"optionId": "x", "kind": "weird"}], True))
        self.assertIsNone(bridge._oce_pick_option(["not-a-dict"], True))


class RequestFlowTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, bridge, "_OCE_PERM_TIMEOUT",
                        bridge._OCE_PERM_TIMEOUT)
        bridge._OCE_PERM_TIMEOUT = 0.4
        self._orig_push = bridge._approval_push
        bridge._approval_push = lambda *a, **k: None
        bridge._OCE_CARD_DIGESTS.clear()
        bridge._OCE_TURNS.clear()
        bridge._OCE_PERM_WAITERS.clear()
        bridge._OCE_TURNS["ses_1"] = {"cid": "c", "text": "", "key": "oce-key"}

    def tearDown(self):
        bridge._approval_push = self._orig_push

    def _req(self, options):
        return {"method": "session/request_permission",
                "params": {"sessionId": "ses_1",
                           "toolCall": {"title": "echo bridge-e2e-ok",
                                        "kind": "execute",
                                        "rawInput": {"command": "echo hi"}},
                           "options": options}}

    def _run(self, options, decide=None):
        """跑一次請求,回 (outcome, aid)。先抓 aid 再收尾(created_at 秒級,
        ORDER BY 會抓到別的測試那筆 —— gemini/grok 測試的教訓)。"""
        async def main():
            task = asyncio.create_task(bridge._oce_on_request(self._req(options)))
            aid = None
            for _ in range(200):
                await asyncio.sleep(0.002)
                live = list(bridge._OCE_PERM_WAITERS.keys())
                if live:
                    aid = live[0]
                    break
                if task.done():
                    break
            if decide is not None and aid:
                bridge._oce_approval_decide(aid, {"approve": decide})
            return await task, aid
        return asyncio.run(main())

    def test_approve_returns_selected_allow_option(self):
        out, aid = self._run(REAL_OPTIONS, decide=True)
        self.assertEqual(out, {"outcome": {"outcome": "selected",
                                           "optionId": "once"}})
        self.assertEqual(_status(aid), "approved")

    def test_deny_returns_selected_reject_option(self):
        out, aid = self._run(REAL_OPTIONS, decide=False)
        self.assertEqual(out, {"outcome": {"outcome": "selected",
                                           "optionId": "reject"}})
        self.assertEqual(_status(aid), "denied")

    def test_unusable_options_fall_back_to_cancelled(self):
        out, _aid = self._run([{"optionId": "x", "kind": "weird"}],
                              decide=True)
        self.assertEqual(out, {"outcome": {"outcome": "cancelled"}})

    def test_timeout_is_fail_closed_and_marks_expired(self):
        out, aid = self._run(REAL_OPTIONS)    # 不決定 → 等到逾時
        self.assertEqual(out, {"outcome": {"outcome": "cancelled"}})
        self.assertIsNotNone(aid)
        self.assertEqual(_status(aid), "expired")

    def test_waiter_is_removed_after_resolution(self):
        self._run(REAL_OPTIONS, decide=True)
        self.assertEqual(bridge._OCE_PERM_WAITERS, {},
                         "等待者要收乾淨,否則第二次決定會打到舊 future")

    def test_record_lands_with_provider_opencode_and_contract_options(self):
        """落表要是 provider='opencode'(**不是 openclaw**)+ 正規化
        {key,label,style} 選項 —— ACP 原形塞進去 app 會畫出空白按鈕。"""
        _out, aid = self._run(REAL_OPTIONS, decide=True)
        con = sqlite3.connect(bridge.CANON_DB)
        try:
            row = con.execute(
                "SELECT provider, session_id, options FROM approvals "
                "WHERE id=?", (aid,)).fetchone()
        finally:
            con.close()
        self.assertEqual(row[0], "opencode")
        self.assertEqual(row[1], "opencode:oce-key")
        opts = json.loads(row[2])
        self.assertEqual(opts[0], {"key": "once", "label": "Allow once",
                                   "style": "primary"})
        self.assertEqual(opts[-1]["style"], "deny")

    def test_approval_row_reports_provider_opencode(self):
        """讀側也要對:_approval_row 的 provider 推導不能把它歸成 openclaw。"""
        _out, aid = self._run(REAL_OPTIONS, decide=True)
        row = bridge._approval_get_row(aid)
        self.assertEqual(row["provider"], "opencode")

    def test_unroutable_session_falls_back(self):
        bridge._OCE_TURNS.clear()
        orig = bridge.OPENCODE.acp_to_key
        bridge.OPENCODE.acp_to_key = lambda _: None
        try:
            out = asyncio.run(bridge._oce_on_request(self._req(REAL_OPTIONS)))
        finally:
            bridge.OPENCODE.acp_to_key = orig
        self.assertEqual(out, {"outcome": {"outcome": "cancelled"}})

    def test_pending_scan_groups_by_session(self):
        """列表要看得出「它在等你」(180 秒窗口不然就逾時被拒)。"""
        async def main():
            task = asyncio.create_task(
                bridge._oce_on_request(self._req(REAL_OPTIONS)))
            for _ in range(200):
                await asyncio.sleep(0.002)
                if bridge._OCE_PERM_WAITERS:
                    break
            pend = bridge._opencode_pending_by_session()
            aid = list(bridge._OCE_PERM_WAITERS)[0]
            bridge._oce_approval_decide(aid, {"approve": False})
            await task
            return pend
        pend = asyncio.run(main())
        self.assertIn("opencode:oce-key", pend)
        self.assertEqual(pend["opencode:oce-key"]["provider"], "opencode")


class DecideEndpointTest(unittest.TestCase):
    def test_no_waiter_is_409_and_does_not_touch_db(self):
        bridge._OCE_PERM_WAITERS.clear()
        before = _status("oce-nonexistent")
        with self.assertRaises(Exception) as ctx:
            bridge._oce_approval_decide("oce-nonexistent", {"approve": True})
        self.assertEqual(getattr(ctx.exception, "status_code", None), 409)
        self.assertEqual(_status("oce-nonexistent"), before)

    def test_other_methods_pass_through(self):
        out = asyncio.run(bridge._oce_on_request({"method": "session/other"}))
        self.assertIsNone(out, "非權限請求要原樣放過(回 None)")

    def test_disconnect_reask_after_restart_is_409_not_silent(self):
        """斷線重問:bridge 重啟後等待者消失(live-only 的宣告),app 再送
        決定要吃 409 —— 不准安靜吞掉讓使用者以為批過了。"""
        bridge._OCE_PERM_WAITERS.clear()
        with self.assertRaises(Exception) as ctx:
            bridge._oce_approval_decide("oce-after-restart", {"key": "once"})
        self.assertEqual(getattr(ctx.exception, "status_code", None), 409)


if __name__ == "__main__":
    unittest.main(verbosity=2)
