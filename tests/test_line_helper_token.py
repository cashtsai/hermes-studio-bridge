"""POST /app/v1/line/token —— 幫 LINE 小幫手鑄專屬 device token。

守住:沒憑證不能鑄(401)/ device token 能鑄 / 鑄出來的 token 真的能過
_check_auth(pocket-id 之後就是拿它打 /app/v1/messages)/ 重鑄會撤舊的
(一機一枚,不養殭屍)/ 裝置清單看得見「LINE 小幫手」。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="line-token-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class TestLineHelperToken(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(bridge.app)
        self.dev_token = "pdev-test-line-minter"
        with bridge._PAIR_LOCK:
            # 清掉殘留的 line token(全域 store,case 之間會互滲)
            for t in [t for t, d in bridge._DEVICE_TOKENS.items()
                      if d.get("platform") == "line"]:
                bridge._DEVICE_TOKENS.pop(t, None)
            bridge._DEVICE_TOKENS[self.dev_token] = {
                "name": "iPhone", "platform": "ios",
                "created": 0.0, "last_seen": 0.0,
                "apple_user_id": None, "device_id": None,
            }
        self._save = patch.object(bridge, "_save_device_tokens", lambda d: None)
        self._save.start()

    def tearDown(self):
        self._save.stop()
        with bridge._PAIR_LOCK:
            bridge._DEVICE_TOKENS.pop(self.dev_token, None)
            for t in [t for t, d in bridge._DEVICE_TOKENS.items()
                      if d.get("platform") == "line"]:
                bridge._DEVICE_TOKENS.pop(t, None)

    def _mint(self, auth):
        return self.client.post("/app/v1/line/token",
                                headers={"Authorization": f"Bearer {auth}"})

    def test_no_or_bad_token_is_401(self):
        r = self.client.post("/app/v1/line/token")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self._mint("pdev-nonsense").status_code, 401)

    def test_device_token_mints_usable_token(self):
        r = self._mint(self.dev_token)
        self.assertEqual(r.status_code, 200)
        line_tok = r.json()["token"]
        self.assertTrue(line_tok.startswith("pdev-"))
        # 鑄出來的 token 要真的能過 _check_auth —— 拿它列裝置清單即為證。
        devs = self.client.get("/pair/devices",
                               headers={"Authorization": f"Bearer {line_tok}"})
        self.assertEqual(devs.status_code, 200)
        names = [d["name"] for d in devs.json()["devices"]]
        self.assertIn("LINE 小幫手", names)

    def test_remint_revokes_previous_line_token(self):
        first = self._mint(self.dev_token).json()["token"]
        second = self._mint(self.dev_token).json()["token"]
        self.assertNotEqual(first, second)
        with bridge._PAIR_LOCK:
            self.assertNotIn(first, bridge._DEVICE_TOKENS)   # 舊的撤了
            self.assertIn(second, bridge._DEVICE_TOKENS)
            line_rows = [d for d in bridge._DEVICE_TOKENS.values()
                         if d.get("platform") == "line"]
        self.assertEqual(len(line_rows), 1)                  # 一機一枚


if __name__ == "__main__":
    unittest.main()
