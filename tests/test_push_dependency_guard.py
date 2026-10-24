"""推播依賴守門:APNs 只走 HTTP/2,缺 h2 就必須「大聲」失敗(2026-10-24 事故)。

事故:bridge 的虛擬環境缺 `h2`,從 2026-08-22 起每一則推播都在 httpx 建 client 時
失敗(累計 748 筆 push_notify_failed),鎖屏核准整段不能用,而且沒有任何人看見 ——
失敗有進日誌,但沒有人會去翻那份日誌。

釘住的契約:
  1. h2 在 → 無問題;h2 缺 → 回傳一句含「修法」的人話。
  2. 推播已配置 + 缺 h2 → push_notify 一次講清楚(code=dependency),
     **不**走進逐台裝置迴圈、**不**打 APNs。
  3. 推播沒配置(多數自架者)→ 維持安靜的 disabled,**連檢查都不做** ——
     不能為了這個守門去打擾沒有推播的人。
  4. 啟動檢查:配置了又缺才出聲;齊全或未配置都完全安靜。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(見 tests/_isolation.py)
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="push-dep-guard-canon-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class Http2ProblemTest(unittest.TestCase):
    def test_none_when_h2_importable(self):
        with patch.dict(sys.modules, {"h2": types.ModuleType("h2")}):
            self.assertIsNone(bridge._push_http2_problem())

    def test_problem_names_the_fix_when_h2_missing(self):
        # sys.modules[name] = None 會讓 `import h2` 丟 ImportError —— 與真的沒裝等價。
        with patch.dict(sys.modules, {"h2": None}):
            msg = bridge._push_http2_problem()
        self.assertIsNotNone(msg)
        self.assertIn("h2", msg)
        self.assertIn("uv pip install", msg)   # 要講「下一步」,不是只重述失敗
        self.assertIn(sys.prefix, msg)         # 修到「正在跑的」那個環境


class PushNotifyGuardTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_dependency_fails_once_and_never_reaches_apns(self):
        with patch.object(bridge, "apns_configured", return_value=True), \
             patch.object(bridge, "_push_http2_problem", return_value="缺 h2 套件:修法 …"), \
             patch.object(bridge, "_devices", return_value=["tok-a", "tok-b"]), \
             patch.object(bridge, "_apns_send",
                          side_effect=AssertionError("缺依賴時不該去打 APNs")) as send:
            r = await bridge.push_notify("標題", "內文")
        send.assert_not_called()
        self.assertEqual(r["sent"], 0)
        self.assertEqual(r["total"], 2)
        self.assertTrue(r["dependency_missing"])
        self.assertEqual(len(r["failures"]), 1)           # 一筆,不是每台裝置各一筆
        self.assertEqual(r["failures"][0]["code"], "dependency")

    async def test_unconfigured_apns_stays_quiet_and_skips_the_check(self):
        with patch.object(bridge, "apns_configured", return_value=False), \
             patch.object(bridge, "_push_http2_problem",
                          side_effect=AssertionError("沒配金鑰不該做 h2 檢查")):
            r = await bridge.push_notify("標題", "內文")
        self.assertTrue(r.get("disabled"))
        self.assertEqual(r["sent"], 0)


class StartupCheckTest(unittest.IsolatedAsyncioTestCase):
    async def _run(self, configured, problem):
        events = []
        with patch.object(bridge, "apns_configured", return_value=configured), \
             patch.object(bridge, "_push_http2_problem", return_value=problem), \
             patch.object(bridge, "_log_event", lambda e, **f: events.append((e, f))):
            await bridge._check_push_dependency_at_startup()
        return events

    async def test_speaks_up_when_configured_and_missing(self):
        events = await self._run(True, "缺 h2 套件:修法 …")
        names = [e for e, _ in events]
        self.assertIn("push_dependency_missing", names)
        self.assertEqual(dict(events)["push_dependency_missing"]["phase"], "startup")

    async def test_silent_when_dependency_present(self):
        self.assertEqual(await self._run(True, None), [])

    async def test_silent_when_apns_not_configured(self):
        self.assertEqual(await self._run(False, "缺 h2 套件:修法 …"), [])


if __name__ == "__main__":
    unittest.main()
