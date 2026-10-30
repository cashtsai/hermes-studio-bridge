"""QR 登入依賴守門:缺 `qrcode` 不會壞,只會**安靜退化**(2026-10-30 實測)。

發現經過:端到端跑桌面控制時,日誌裡躺著一行被吞掉的
`ModuleNotFoundError: No module named 'qrcode'`,`severity: expected`。
查下去才發現 `requirements.txt` **有**列 qrcode,而 production 的 venv
**沒裝** —— 跟 2026-10-24 的 h2 事故完全同一個病:這個服務住在 Hermes 的
版本化虛擬環境(venvs/hermes-<版本>)裡,**升級換環境,宣告過的相依就消失**。

為什麼比 h2 更難發現:h2 缺了推播**會失敗**(至少有 748 筆失敗紀錄);
qrcode 缺了頁面照出、HTTP 200、沒有任何錯誤 —— 只是圖沒了,使用者得手打
`pocket://console-login?rid=…`。對一個叫「掃碼登入」的功能來說那等於沒做,
而且**不報錯就沒有人會發現**。

釘住的契約:
  1. qrcode 在 → 無問題;缺 → 回一句含「修法」的人話。
  2. 功能沒開(旗標關)→ 完全安靜,連檢查都不做。
  3. 功能開了又缺 → 啟動時出聲一次(事件日誌 + stderr)。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="qrcode-dep-guard-canon-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class QrcodeProblemTest(unittest.TestCase):
    def test_none_when_qrcode_importable(self):
        with patch.dict(sys.modules, {"qrcode": types.ModuleType("qrcode")}):
            self.assertIsNone(bridge._qrcode_problem())

    def test_problem_names_the_fix_when_missing(self):
        # sys.modules[name] = None 會讓 `import qrcode` 丟 ImportError。
        with patch.dict(sys.modules, {"qrcode": None}):
            msg = bridge._qrcode_problem()
        self.assertIsNotNone(msg)
        self.assertIn("qrcode", msg)
        self.assertIn("uv pip install", msg)   # 要講下一步,不是只重述失敗
        self.assertIn(sys.prefix, msg)         # 修到「正在跑的」那個環境
        # 要講清楚後果是「退化」不是「壞掉」—— 否則讀的人會去找不存在的崩潰。
        self.assertIn("手動", msg)


class QrcodeStartupCheckTest(unittest.IsolatedAsyncioTestCase):
    async def test_silent_when_feature_disabled(self):
        """旗標關 = 功能不存在,不該為了它打擾任何人(多數自架者)。"""
        with patch.object(bridge, "POCKET_CONSOLE_ENABLED", False), \
             patch.object(bridge, "POCKET_CONSOLE_QR_LOGIN", False), \
             patch.object(bridge, "_qrcode_problem", return_value="缺 qrcode …"), \
             patch.object(bridge, "_log_event") as log:
            await bridge._check_qrcode_dependency_at_startup()
        log.assert_not_called()

    async def test_silent_when_dependency_present(self):
        with patch.object(bridge, "POCKET_CONSOLE_ENABLED", True), \
             patch.object(bridge, "POCKET_CONSOLE_QR_LOGIN", True), \
             patch.object(bridge, "_qrcode_problem", return_value=None), \
             patch.object(bridge, "_log_event") as log:
            await bridge._check_qrcode_dependency_at_startup()
        log.assert_not_called()

    async def test_speaks_up_when_enabled_but_missing(self):
        """開了功能又缺依賴 —— 這是唯一該出聲的組合。"""
        with patch.object(bridge, "POCKET_CONSOLE_ENABLED", True), \
             patch.object(bridge, "POCKET_CONSOLE_QR_LOGIN", True), \
             patch.object(bridge, "_qrcode_problem", return_value="缺 qrcode 套件:修法 …"), \
             patch.object(bridge, "_log_event") as log:
            await bridge._check_qrcode_dependency_at_startup()
        log.assert_called_once()
        self.assertEqual(log.call_args[0][0], "qrcode_dependency_missing")


if __name__ == "__main__":
    unittest.main()
