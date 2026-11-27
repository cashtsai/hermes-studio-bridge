"""_line_seed_pocket_home —— LINE 綁定自動開 pocket 人格時種家。

守住:空殼 home 會種齊 SOUL/bin/handoff/auth.json/config.yaml(缺 auth
→ ACP 暖路徑起不來,退冷啟撞 "No session found matching 'owui-pocket'";
缺 config → hermes 回 "not connected to any AI provider",機主 2026-11-24
LINE 實測兩坑)/ 冪等:已存在的檔案一律不覆蓋(人格可能已換綁帳號或被
手動瘦身)/ run_hermes 冷啟 fallback 帶 --create-if-missing(新人格第一
回合也要能開 session)。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="line-seed-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class TestLineSeedPocketHome(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="seed-root-")
        self.home = os.path.join(self.root, "profiles", "pocket")
        os.makedirs(self.home, exist_ok=True)
        # 假 HOME_ROOT:有 auth/config/handoff-cli 可供複製
        self.home_root = os.path.join(self.root, "main-home")
        os.makedirs(os.path.join(self.home_root, "bin"), exist_ok=True)
        for name, body in (("auth.json", '{"version": 1}'),
                           ("config.yaml", "model:\n  default: test-model\n")):
            with open(os.path.join(self.home_root, name), "w") as f:
                f.write(body)
        with open(os.path.join(self.home_root, "bin", "handoff-cli"), "w") as f:
            f.write("#!/bin/sh\n")
        self._hr = patch.object(bridge, "HOME_ROOT", self.home_root)
        self._hr.start()

    def tearDown(self):
        self._hr.stop()

    def test_seeds_everything_into_empty_home(self):
        bridge._line_seed_pocket_home(self.home)
        for rel in ("SOUL.md", "bin/handoff", "auth.json", "config.yaml"):
            self.assertTrue(os.path.exists(os.path.join(self.home, rel)),
                            f"missing seeded file: {rel}")
        # 憑證檔必須收斂權限
        mode = os.stat(os.path.join(self.home, "auth.json")).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_idempotent_never_overwrites(self):
        marker = "# user-customized, do not clobber\n"
        for rel in ("SOUL.md", "auth.json", "config.yaml"):
            with open(os.path.join(self.home, rel), "w") as f:
                f.write(marker)
        bridge._line_seed_pocket_home(self.home)
        for rel in ("SOUL.md", "auth.json", "config.yaml"):
            with open(os.path.join(self.home, rel)) as f:
                self.assertEqual(f.read(), marker, f"{rel} was overwritten")

    def test_missing_sources_do_not_crash(self):
        # HOME_ROOT 沒 auth/config(例如全新安裝)→ 種子跳過,不炸
        for name in ("auth.json", "config.yaml"):
            os.remove(os.path.join(self.home_root, name))
        bridge._line_seed_pocket_home(self.home)
        self.assertTrue(os.path.exists(os.path.join(self.home, "SOUL.md")))
        self.assertFalse(os.path.exists(os.path.join(self.home, "auth.json")))

    def test_cold_fallback_passes_create_if_missing(self):
        # 沒有 canonical TG session 的全新人格 → --continue 必帶
        # --create-if-missing,否則第一回合就是機主踩過的那個錯。
        captured = {}

        async def fake_exec(*argv, **kw):
            captured["argv"] = argv
            raise RuntimeError("stop-here")

        with patch.object(bridge, "canonical_telegram_session",
                          lambda home: None), \
             patch.object(bridge.asyncio, "create_subprocess_exec", fake_exec):
            import asyncio
            try:
                asyncio.run(bridge.run_hermes("pocket", "hi"))
            except RuntimeError:
                pass
        argv = list(captured.get("argv") or ())
        self.assertIn("--continue", argv)
        self.assertIn("--create-if-missing", argv)


if __name__ == "__main__":
    unittest.main()
