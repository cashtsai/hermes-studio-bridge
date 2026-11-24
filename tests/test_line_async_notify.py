"""LINE 異步回報(reply token 60 秒魔咒的解)。

守住:沒 LINE 綁定 → 完全不打 pocket-id(零成本);有綁定 → 帶那枚
platform=line 的 token POST /v1/line/notify;文字截 4000。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="line-notify-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class TestLineAsyncNotify(unittest.TestCase):
    def tearDown(self):
        with bridge._PAIR_LOCK:
            for t in [t for t, d in bridge._DEVICE_TOKENS.items()
                      if d.get("platform") == "line"]:
                bridge._DEVICE_TOKENS.pop(t, None)

    def test_no_line_token_means_no_call(self):
        calls = []
        with patch.object(bridge.asyncio, "to_thread",
                          side_effect=lambda f: calls.append(1)):
            asyncio.run(bridge._line_async_notify("xcash", "hi"))
        self.assertEqual(calls, [])

    def test_with_token_posts_to_pocket_id(self):
        with bridge._PAIR_LOCK:
            bridge._DEVICE_TOKENS["pdev-line-test"] = {
                "name": "LINE 小幫手", "platform": "line",
                "created": 0.0, "last_seen": 0.0,
                "apple_user_id": None, "device_id": None}
        seen = {}

        async def fake_to_thread(fn):
            seen["invoked"] = True
            return 200

        with patch.object(bridge, "POCKET_ID_URL", "https://id.example"), \
             patch.object(bridge.asyncio, "to_thread", fake_to_thread):
            asyncio.run(bridge._line_async_notify("xcash", "回報" * 3000))
        self.assertTrue(seen.get("invoked"))
        self.assertEqual(bridge._line_helper_token_value(), "pdev-line-test")


if __name__ == "__main__":
    unittest.main()
