"""長思考不得被誤判成「turn stalled」(2026-10-29 機主回報)。

實害:同一條 Cashcamp thread 昨晚連續 6 次出現
`Codex turn stalled (no provider event)`,每次間隔 5-10 分鐘。

根因:看門狗的活著訊號 `last_event_at` 原本只有 `_append` 會更新,而 `_append`
只在 `_codex_format_item` 吐得出文字時才被呼叫 —— reasoning 的 `item/started`
當下 summary 還是空的(回 ""),而 Codex 長思考時真正在流的
`item/reasoning/textDelta` 整個 if 鏈**完全沒處理**。於是 Codex 一直在思考、
事件一直在來,bridge 卻視而不見,滿 300s 就強制中止使用者的回合。
"""
import _isolation  # noqa: F401
import os
import sys
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="cx-stall-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

TID = "01a17a43-c22c-72d3-adf2-042c5823033a"


class CXStallLivenessTest(unittest.TestCase):
    def setUp(self):
        self.app = bridge.CODEX_APP
        self.app.last_event_at.pop(TID, None)

    def _notify(self, method, params=None):
        self.app._handle_notification({"method": method,
                                       "params": params or {"threadId": TID}})

    def test_reasoning_delta_counts_as_alive(self):
        """長思考時真正在流的就是這個 —— 以前完全不更新活著時戳。"""
        self._notify("item/reasoning/textDelta", {"threadId": TID, "delta": "思考中"})
        self.assertIn(TID, self.app.last_event_at,
                      "reasoning delta 必須算活著,否則長思考會被誤判卡住")

    def test_empty_reasoning_started_counts_as_alive(self):
        """summary 還是空的 item/started —— 格式化吐空字串,以前不算活著。"""
        self._notify("item/started",
                     {"threadId": TID, "item": {"type": "reasoning", "summary": []}})
        self.assertIn(TID, self.app.last_event_at)

    def test_unknown_notification_counts_as_alive(self):
        """未知/較新的通知型別也是活著的證據,不能因為看不懂就當它沒發生。"""
        self._notify("item/commandExecution/outputDelta",
                     {"threadId": TID, "chunk": "building…"})
        self.assertIn(TID, self.app.last_event_at)

    def test_liveness_advances(self):
        self._notify("item/reasoning/textDelta", {"threadId": TID, "delta": "a"})
        first = self.app.last_event_at[TID]
        time.sleep(0.02)
        self._notify("item/reasoning/textDelta", {"threadId": TID, "delta": "b"})
        self.assertGreater(self.app.last_event_at[TID], first)

    def test_no_threadid_is_ignored(self):
        self._notify("remoteControl/status/changed", {})
        self.assertNotIn(TID, self.app.last_event_at)


if __name__ == "__main__":
    unittest.main()
