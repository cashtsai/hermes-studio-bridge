"""內部報告隱藏(feat/hide-internal-reports)測試。

鎖住三道閘門,防止文案/欄位一改就靜默失效:
1. `_is_hidden_report`:source/name/label 三種命中都要擋,一般報告不受影響。
2. `_report_upsert`:隱藏報告不落 report_events、回空 id。
3. `_report_events` / `_event_since`:已在庫裡的隱藏資料(歷史殘留)讀取時過濾。
4. `TOOL_ERROR_REPORTS_ENABLED` 預設關(環境變數沒開就不混入工具錯誤報告)。

⚠️ `_is_hidden_report_message` 靠「📰 **錯誤報告**」等字串前綴比對——
`TOOL_ERROR_REPORT_LABEL` 或報告文案改字,這裡的測試就該同步紅掉。
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="hidden-reports-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class HiddenReportsBase(unittest.TestCase):
    """每類測試用獨立 tmp 庫,不跟同行程其他測試檔共用 CANON_DB。"""

    @classmethod
    def setUpClass(cls):
        cls._orig_db = bridge.CANON_DB
        cls._db = os.path.join(tempfile.mkdtemp(prefix="hidden-rep-db-"),
                               "canonical.db")
        bridge.CANON_DB = cls._db
        bridge._canon_init()

    @classmethod
    def tearDownClass(cls):
        bridge.CANON_DB = cls._orig_db


class IsHiddenReportTest(unittest.TestCase):
    def test_hits_by_source_name_label(self):
        self.assertTrue(bridge._is_hidden_report(
            {"external_source": "hermes-tool-error"}))
        self.assertTrue(bridge._is_hidden_report(
            {"external_source": "bridge-health"}))
        self.assertTrue(bridge._is_hidden_report({"name": "agent-tool-error"}))
        self.assertTrue(bridge._is_hidden_report({"name": "bridge-health"}))
        for label in ("錯誤報告", "Bridge 健康警報", "Bridge 復原", "Bridge 警告"):
            self.assertTrue(bridge._is_hidden_report({"label": label}), label)

    def test_normal_report_passes(self):
        self.assertFalse(bridge._is_hidden_report(
            {"external_source": "hermes-cron", "name": "morning-brief",
             "label": "晨報"}))
        self.assertFalse(bridge._is_hidden_report({}))

    def test_constants_still_covered(self):
        """bridge 的工具錯誤常數改名時,隱藏集合必須跟著改——這裡釘住。"""
        self.assertIn(bridge.TOOL_ERROR_REPORT_SOURCE,
                      bridge.HIDDEN_REPORT_SOURCES)
        self.assertIn(bridge.TOOL_ERROR_REPORT_NAME, bridge.HIDDEN_REPORT_NAMES)
        self.assertIn(bridge.TOOL_ERROR_REPORT_LABEL,
                      bridge.HIDDEN_REPORT_LABELS)


class IsHiddenReportMessageTest(unittest.TestCase):
    def test_hidden_prefixes(self):
        for text in ("📰 **錯誤報告**\n內容", "📰 **Bridge 健康警報**\nx",
                     "📰 **Bridge 復原**\nx", "📰 **Bridge 警告**\nx",
                     "某工具錯誤摘要"):
            self.assertTrue(bridge._is_hidden_report_message(
                {"id": "rep-abc", "content": text}), text)

    def test_non_report_id_never_hidden(self):
        # 一般聊天訊息就算撞到關鍵字也不能被吞(只過濾 rep- 開頭的報告訊息)
        self.assertFalse(bridge._is_hidden_report_message(
            {"id": "msg-1", "content": "📰 **錯誤報告**"}))

    def test_normal_report_message_passes(self):
        self.assertFalse(bridge._is_hidden_report_message(
            {"id": "rep-abc", "content": "📰 **晨報**\n今日重點"}))

    def test_event_wrapper(self):
        self.assertTrue(bridge._is_hidden_message_event(
            {"message": {"id": "rep-x", "content": "📰 **錯誤報告**"}}))
        self.assertFalse(bridge._is_hidden_message_event({"message": None}))
        self.assertFalse(bridge._is_hidden_message_event("not-a-dict"))


class ReportUpsertGateTest(HiddenReportsBase):
    def _count(self):
        con = sqlite3.connect(bridge.CANON_DB)
        try:
            return con.execute("SELECT COUNT(*) FROM report_events").fetchone()[0]
        finally:
            con.close()

    def test_hidden_report_not_persisted(self):
        before = self._count()
        rid = bridge._report_upsert("yuanfang", {
            "label": bridge.TOOL_ERROR_REPORT_LABEL,
            "name": bridge.TOOL_ERROR_REPORT_NAME,
            "external_source": bridge.TOOL_ERROR_REPORT_SOURCE,
            "external_id": "te-1", "content": "工具錯誤內容", "ts": time.time(),
        })
        self.assertEqual(rid, "")
        self.assertEqual(self._count(), before)

    def test_normal_report_persisted(self):
        rid = bridge._report_upsert("yuanfang", {
            "label": "晨報", "name": "morning-brief",
            "external_source": "hermes-cron", "external_id": "mb-1",
            "content": "今日重點", "ts": time.time(),
        })
        self.assertTrue(rid)
        rows = bridge._report_events("yuanfang", limit=10)
        self.assertIn("mb-1", [r["external_id"] for r in rows])


class ReadPathFilterTest(HiddenReportsBase):
    """歷史殘留的隱藏資料(閘門上線前寫入的)讀取時要濾掉。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        con = sqlite3.connect(bridge.CANON_DB)
        now = time.time()
        con.execute(
            "INSERT INTO report_events(id,session,label,name,content,ts,"
            "external_source,external_id,ingested_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("rep-hid", "yuanfang", "錯誤報告", "agent-tool-error", "殘留",
             now, "hermes-tool-error", "old-1", now))
        con.execute(
            "INSERT INTO report_events(id,session,label,name,content,ts,"
            "external_source,external_id,ingested_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("rep-ok", "yuanfang", "晨報", "morning-brief", "正常",
             now, "hermes-cron", "ok-1", now))
        con.commit()
        con.close()

    def test_report_events_filters_hidden(self):
        rows = bridge._report_events("yuanfang", limit=10)
        ids = [r["id"] for r in rows]
        self.assertIn("rep-ok", ids)
        self.assertNotIn("rep-hid", ids)

    def test_event_since_filters_hidden_message(self):
        bridge._event_append("yuanfang", "message", {"message": {
            "id": "rep-hid-ev", "content": "📰 **錯誤報告**\n殘留事件"}})
        bridge._event_append("yuanfang", "message", {"message": {
            "id": "rep-ok-ev", "content": "📰 **晨報**\n正常事件"}})
        events = bridge._event_since("yuanfang", 0, 500)
        mids = [((e.get("data") or {}).get("message") or {}).get("id")
                for e in events]
        self.assertIn("rep-ok-ev", mids)
        self.assertNotIn("rep-hid-ev", mids)
        all_events = bridge._event_since_all(0, 500)
        mids_all = [((e.get("data") or {}).get("message") or {}).get("id")
                    for e in all_events]
        self.assertNotIn("rep-hid-ev", mids_all)


class ToolErrorFlagTest(unittest.TestCase):
    def test_disabled_by_default(self):
        # 環境沒開 POCKET_ENABLE_TOOL_ERROR_REPORTS,同步線不混入工具錯誤報告
        if os.environ.get("POCKET_ENABLE_TOOL_ERROR_REPORTS"):
            self.skipTest("環境已顯式開啟,略過預設值檢查")
        self.assertFalse(bridge.TOOL_ERROR_REPORTS_ENABLED)

    def test_escape_hatch_unhides_tool_errors_only(self):
        """flag 開 → 工具錯誤家族放行,bridge-health 家族仍然隱藏。"""
        orig = bridge.TOOL_ERROR_REPORTS_ENABLED
        bridge.TOOL_ERROR_REPORTS_ENABLED = True
        try:
            self.assertFalse(bridge._is_hidden_report(
                {"external_source": "hermes-tool-error",
                 "name": "agent-tool-error", "label": "錯誤報告"}))
            self.assertTrue(bridge._is_hidden_report(
                {"external_source": "bridge-health"}))
            self.assertTrue(bridge._is_hidden_report(
                {"label": "Bridge 健康警報"}))
            self.assertFalse(bridge._is_hidden_report_message(
                {"id": "rep-x", "content": "📰 **錯誤報告**\n內容"}))
            self.assertTrue(bridge._is_hidden_report_message(
                {"id": "rep-x", "content": "📰 **Bridge 健康警報**\n內容"}))
        finally:
            bridge.TOOL_ERROR_REPORTS_ENABLED = orig


if __name__ == "__main__":
    unittest.main()
