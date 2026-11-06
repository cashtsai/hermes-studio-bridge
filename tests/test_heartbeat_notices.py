"""心跳 notices 下行(backup orphan 推播提醒,2026-11-06)。

契約(docs/APP_BRIDGE_CONTRACT.md 附註 A):
  · pocket-id 心跳回應可帶 notices:[{type:"backup_orphan", product_id}]
  · bridge 收到 → 轉本地推播(push_notify 既有管線)給已配對裝置
  · 同一則 notice 24h 內不重複推(bridge 本地檔案 dedup,重啟不失憶)
  · 沒有 notices 欄位 = 無事(舊版 pocket-id 相容),垃圾形狀不准炸
  · 消費失敗不能看起來像心跳失敗(否則健康的 pocket-id 被指數退避)
"""
import _isolation  # noqa: F401
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

_TMP = tempfile.mkdtemp(prefix="hb-notices-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
# 硬蓋不 setdefault:_isolation 已把它導去自己的 tmp,本檔要讀寫同一份路徑
# 來驗 dedup 的落盤行為,直接佔一個本檔專屬的檔案最不含糊。
os.environ["POCKET_HB_NOTICE_STATE"] = os.path.join(_TMP, "hb-notices.json")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


def _consume(resp: dict) -> AsyncMock:
    pn = AsyncMock(return_value={"sent": 1, "total": 1, "failures": []})
    with patch.object(bridge, "push_notify", pn):
        asyncio.run(bridge._pocket_id_consume_notices(resp))
    return pn


class TestHeartbeatNotices(unittest.TestCase):
    def setUp(self):
        try:
            os.remove(bridge._hb_notice_state_path())
        except FileNotFoundError:
            pass

    def test_backup_orphan_notice_pushes_once(self):
        pn = _consume({"ok": True, "notices": [
            {"type": "backup_orphan", "product_id": "com.pocketagent.backup.m"}]})
        self.assertEqual(pn.await_count, 1)
        args, kwargs = pn.await_args
        title, body, data = args[0], args[1], args[2]
        self.assertIn("備份訂閱", title)
        self.assertIn("取消", body)
        self.assertEqual(data["pocket"]["kind"], "backup_orphan")
        self.assertEqual(data["pocket"]["productId"], "com.pocketagent.backup.m")
        self.assertEqual(kwargs.get("thread_id"), "account-notices")

    def test_same_notice_is_deduped_within_24h(self):
        notice = {"type": "backup_orphan", "product_id": "com.x.backup"}
        pn1 = _consume({"notices": [notice]})
        pn2 = _consume({"notices": [notice]})   # 心跳 60s 後又帶同一則
        self.assertEqual(pn1.await_count, 1)
        self.assertEqual(pn2.await_count, 0, "24h 內同一則 notice 不准重複推")

    def test_dedup_expires_after_24h(self):
        notice = {"type": "backup_orphan", "product_id": "com.x.backup"}
        _consume({"notices": [notice]})
        path = bridge._hb_notice_state_path()
        with open(path, encoding="utf-8") as f:
            seen = json.load(f)
        key = "backup_orphan:com.x.backup"
        self.assertIn(key, seen)
        seen[key] = time.time() - bridge._HB_NOTICE_DEDUP_SECS - 60
        with open(path, "w", encoding="utf-8") as f:
            json.dump(seen, f)
        pn = _consume({"notices": [notice]})
        self.assertEqual(pn.await_count, 1, "窗過了要再提醒 —— 錢還在扣")

    def test_different_product_ids_are_separate_notices(self):
        pn = _consume({"notices": [
            {"type": "backup_orphan", "product_id": "com.x.a"},
            {"type": "backup_orphan", "product_id": "com.x.b"}]})
        self.assertEqual(pn.await_count, 2)

    def test_missing_or_garbage_notices_field_is_a_noop(self):
        for resp in ({"ok": True},                 # 無欄 = 無事(舊 pocket-id)
                     {"notices": None},
                     {"notices": "garbage"},
                     {"notices": []},
                     {"notices": [None, 42, "x"]},
                     {"notices": [{"type": "future_thing"}]},
                     {"notices": [{}]}):
            pn = _consume(resp)
            self.assertEqual(pn.await_count, 0, f"不該推: {resp}")
        self.assertFalse(os.path.exists(bridge._hb_notice_state_path()),
                         "沒推過就不該落 dedup 檔")

    def test_heartbeat_consumes_response_and_swallows_notice_errors(self):
        """_pocket_id_heartbeat 要把回應餵進消費端;消費端炸了不能讓
        心跳看起來失敗(loop 會退避)。"""
        api = AsyncMock(return_value={"ok": True, "notices": [
            {"type": "backup_orphan", "product_id": "com.x.backup"}]})
        pn = AsyncMock(side_effect=RuntimeError("apns boom"))
        with patch.object(bridge, "_pocket_id_api", api), \
                patch.object(bridge, "push_notify", pn), \
                patch.object(bridge, "_pocket_id_state",
                             return_value={"host_id": "h", "host_secret": "s"}), \
                patch.object(bridge, "_pocket_id_candidates", return_value=[]), \
                patch.object(bridge, "_host_capabilities", return_value={}):
            asyncio.run(bridge._pocket_id_heartbeat())   # 不准 raise
        self.assertEqual(pn.await_count, 1)

    def test_push_marked_before_send_so_failures_do_not_spam(self):
        """先記再推(at-most-once):推播炸掉也不會在下一次心跳重轟。"""
        notice = {"type": "backup_orphan", "product_id": "com.x.backup"}
        pn = AsyncMock(side_effect=RuntimeError("apns boom"))
        with patch.object(bridge, "push_notify", pn):
            with self.assertRaises(RuntimeError):
                asyncio.run(bridge._pocket_id_consume_notices({"notices": [notice]}))
        pn2 = _consume({"notices": [notice]})
        self.assertEqual(pn2.await_count, 0)

    def test_corrupt_dedup_file_fails_open(self):
        path = bridge._hb_notice_state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        pn = _consume({"notices": [
            {"type": "backup_orphan", "product_id": "com.x.backup"}]})
        self.assertEqual(pn.await_count, 1, "壞檔 = 空(寧可多推,不要永遠閉嘴)")


class TestSubscriptionExpiringNotice(unittest.TestCase):
    """到期倒數(2026-11-06)。伺服器每次心跳都帶 days_left 給 App 顯示倒數,
    但只有 push=True 的節點(60/30/14/7/3/1)才推播 —— 節奏決策在伺服器。"""

    def setUp(self):
        try:
            os.remove(bridge._hb_notice_state_path())
        except FileNotFoundError:
            pass

    def _notice(self, days_left, push=True, expires_ts=1800000000.0):
        return {"type": "subscription_expiring", "days_left": days_left,
                "expires_ts": expires_ts, "push": push}

    def test_push_day_sends_with_countdown_payload(self):
        pn = _consume({"notices": [self._notice(7)]})
        self.assertEqual(pn.await_count, 1)
        args, kwargs = pn.await_args
        title, body, data = args[0], args[1], args[2]
        self.assertIn("到期", title)
        self.assertIn("7", body)
        self.assertEqual(data["pocket"]["kind"], "subscription_expiring")
        self.assertEqual(data["pocket"]["daysLeft"], 7)
        self.assertEqual(data["pocket"]["expiresTs"], 1800000000.0)
        self.assertEqual(kwargs.get("thread_id"), "account-notices")

    def test_non_push_day_does_not_notify(self):
        """非節奏日照樣每 60s 來一則(App 要倒數),但**不准推播**。"""
        pn = _consume({"notices": [self._notice(45, push=False)]})
        self.assertEqual(pn.await_count, 0)
        self.assertFalse(os.path.exists(bridge._hb_notice_state_path()),
                         "沒推就不該佔 dedup 格 —— 否則真的到節奏日會被吃掉")

    def test_each_cadence_day_pushes_separately(self):
        """**回歸測試**:dedup key 若不含天數,整段倒數只會推出第一則
        (T-60 推了,T-30/T-14/T-7… 全被 24h 窗當成重複吃掉)。"""
        counts = [_consume({"notices": [self._notice(d)]}).await_count
                  for d in (60, 30, 14, 7, 3, 1)]
        self.assertEqual(counts, [1, 1, 1, 1, 1, 1],
                         "每個倒數節點都要推 —— 這是續費不流失的關鍵")

    def test_same_day_repeat_heartbeat_is_deduped(self):
        n = self._notice(7)
        self.assertEqual(_consume({"notices": [n]}).await_count, 1)
        self.assertEqual(_consume({"notices": [n]}).await_count, 0,
                         "同一天內重複心跳只推一次")

    def test_last_day_wording_is_tomorrow_not_1_day(self):
        pn = _consume({"notices": [self._notice(1)]})
        body = pn.await_args[0][1]
        self.assertIn("明天", body)

    def test_garbage_countdown_fields_do_not_push_or_crash(self):
        for n in ({"type": "subscription_expiring", "push": True},          # 缺天數
                  self._notice("abc"),                                      # 非數字
                  self._notice(0),                                          # 已到期
                  self._notice(-3)):
            pn = _consume({"notices": [n]})
            self.assertEqual(pn.await_count, 0, f"不該推: {n}")

    def test_expires_ts_optional(self):
        n = {"type": "subscription_expiring", "days_left": 3, "push": True}
        pn = _consume({"notices": [n]})
        self.assertEqual(pn.await_count, 1)
        self.assertNotIn("expiresTs", pn.await_args[0][2]["pocket"])

    def test_mixed_with_backup_orphan_both_push(self):
        pn = _consume({"notices": [
            {"type": "backup_orphan", "product_id": "com.x.backup"},
            self._notice(3)]})
        self.assertEqual(pn.await_count, 2)
        kinds = {c.args[2]["pocket"]["kind"] for c in pn.await_args_list}
        self.assertEqual(kinds, {"backup_orphan", "subscription_expiring"})


if __name__ == "__main__":
    unittest.main()
