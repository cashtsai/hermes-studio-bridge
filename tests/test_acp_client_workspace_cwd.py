import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import acp_client  # noqa: E402


# 期望值從**同一個來源**算出來,不寫死任何人的使用者名。
# 2026-10-19:這支原本把 `/Users/<maintainer>/…` 寫死在斷言裡 —— 公開 repo 不該
# 帶維護者的家目錄(見 tests/test_no_private_infra_leak.py),而且換一台機器就紅。
# `workspace_cwd_for` 回的是**設定裡的絕對路徑**(不是從 home 參數推的),
# 所以這裡用 `~` 展開來組期望值。
USER_HOME = os.path.expanduser("~")


class TestACPWorkspaceCwd(unittest.TestCase):
    def test_builtin_personas_use_workspace_cwd(self):
        home = f"{USER_HOME}/apps/hermes-agent/home"
        self.assertEqual(
            acp_client.workspace_cwd_for("yuanfang", home),
            f"{USER_HOME}/apps/lobster-tg/workspace",
        )
        self.assertEqual(
            acp_client.workspace_cwd_for("xcash", home),
            f"{USER_HOME}/apps/lobster-tg/xcash-workspace",
        )
        self.assertEqual(
            acp_client.workspace_cwd_for("pantianqing", home),
            f"{USER_HOME}/apps/lobster-tg/fliper-workspace",
        )

    def test_unknown_persona_falls_back_to_home(self):
        home = f"{USER_HOME}/apps/hermes-agent/home"
        self.assertEqual(acp_client.workspace_cwd_for("custom", home), home)


if __name__ == "__main__":
    unittest.main()
