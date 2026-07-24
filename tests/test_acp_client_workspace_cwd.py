import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import acp_client  # noqa: E402


class TestACPWorkspaceCwd(unittest.TestCase):
    def test_builtin_personas_use_workspace_cwd(self):
        home = "/Users/xcash/apps/hermes-agent/home"
        self.assertEqual(
            acp_client.workspace_cwd_for("yuanfang", home),
            "/Users/xcash/apps/lobster-tg/workspace",
        )
        self.assertEqual(
            acp_client.workspace_cwd_for("xcash", home),
            "/Users/xcash/apps/lobster-tg/xcash-workspace",
        )
        self.assertEqual(
            acp_client.workspace_cwd_for("pantianqing", home),
            "/Users/xcash/apps/lobster-tg/fliper-workspace",
        )

    def test_unknown_persona_falls_back_to_home(self):
        home = "/Users/xcash/apps/hermes-agent/home"
        self.assertEqual(acp_client.workspace_cwd_for("custom", home), home)


if __name__ == "__main__":
    unittest.main()
