"""Hermes 人格後端模型 key 的面板化(2026-11-22,機主點名)。

契約:
  · GET /app/v1/agents/hermes/backend —— 只回 key_set 布林,絕不回 key
  · PUT /app/v1/agents/hermes/key —— 先向 upstream 驗證,驗不過**不落檔**;
    寫入只改 OPENAI_API_KEY 那一行(.env 其他行原封);console-remote 403;
    旗標關閉時 404(合併不改變現行行為)
"""
import _isolation  # noqa: F401
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

_TMP = tempfile.mkdtemp(prefix="hermes-key-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["POCKET_AGENT_LOGIN_IN_APP"] = "1"   # 開旗標才有端點
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


def _mk_profiles(root: str) -> None:
    for prof in ("xcash", "fliper", "shuijing"):
        d = os.path.join(root, prof)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, ".env"), "w", encoding="utf-8") as f:
            f.write("TELEGRAM_BOT_TOKEN=keepme\nOPENAI_API_KEY=old-key\nSIEGE_API_KEY=also-keep\n")


class TestHermesKeyWrite(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="profiles-")
        _mk_profiles(self.root)
        self._orig = bridge._HERMES_PROFILE_ROOT
        bridge._HERMES_PROFILE_ROOT = self.root

    def tearDown(self):
        bridge._HERMES_PROFILE_ROOT = self._orig

    def test_write_only_touches_key_line(self):
        ok = bridge._hermes_write_key("xcash", "new-key-123")
        self.assertTrue(ok)
        body = open(os.path.join(self.root, "xcash", ".env"), encoding="utf-8").read()
        self.assertIn("OPENAI_API_KEY=new-key-123\n", body)
        self.assertIn("TELEGRAM_BOT_TOKEN=keepme\n", body, ".env 其他行不准動")
        self.assertIn("SIEGE_API_KEY=also-keep\n", body)
        self.assertEqual(body.count("OPENAI_API_KEY="), 1, "不准長出第二行")

    def test_write_perm_0600(self):
        bridge._hermes_write_key("fliper", "k")
        mode = os.stat(os.path.join(self.root, "fliper", ".env")).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_read_key(self):
        self.assertEqual(bridge._hermes_read_key("xcash"), "old-key")
        self.assertEqual(bridge._hermes_read_key("nonexistent"), "")

    def test_missing_env_returns_false(self):
        self.assertFalse(bridge._hermes_write_key("pantianqing", "k"),
                         "沒有 .env 的 profile 要回 False,不是生一個殘缺檔")


class TestValidateGate(unittest.TestCase):
    """驗不過不落檔 —— 寧可退回,不要把壞 key 寫進三個 production gateway。"""

    def test_invalid_key_never_written(self):
        root = tempfile.mkdtemp(prefix="profiles-")
        _mk_profiles(root)
        with patch.object(bridge, "_HERMES_PROFILE_ROOT", root), \
             patch.object(bridge, "_hermes_validate_key",
                          AsyncMock(return_value=False)):
            # 直接呼叫 handler 等級太重;驗核心語意:validate False 時
            # handler 會在寫入前 raise —— 用 handler 的程式路徑驗證
            async def run():
                class FakeReq:
                    headers = {"authorization": "Bearer test-unit-token"}
                    async def json(self):
                        return {"api_key": "bad"}
                try:
                    await bridge.app_hermes_set_key(FakeReq())
                except Exception as e:
                    return str(e)
                return ""
            msg = asyncio.run(run())
        body = open(os.path.join(root, "xcash", ".env"), encoding="utf-8").read()
        self.assertIn("OPENAI_API_KEY=old-key", body, "驗證失敗絕不能動到檔案")
        self.assertTrue(msg, "應該 raise")


if __name__ == "__main__":
    unittest.main()
