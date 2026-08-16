"""/pair/qr 與 /pair/qr.json 的一次性 boot code 閘測試。

背景:install-ubuntu.sh 把 bridge 藏在 cloudflared tunnel 後面,tunnel 會把
公網流量 proxy 進 127.0.0.1 —— _pair_local_only 的 loopback 檢查因此形同虛設,
任何拿到 tunnel URL 的人都能鑄配對碼接管主機。修法:qr 端點必須帶
?boot=<code>(磁碟上 chmod 600 的一機一碼),loopback 檢查降為第二層。

驗項:缺碼 403 / 錯碼 403 / 正確碼 200+payload / 鑄碼節流 429 /
loopback 第二層仍在 / boot code 檔案 0600。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="pair-boot-")
os.environ["PAIR_BOOT_CODE_FILE"] = os.path.join(_TMP, "pair-boot-code")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
from fastapi import HTTPException  # noqa: E402

# 上面那行 os.environ 只在「本檔是第一個 import bridge 的人」時算數:
# `_PAIR_BOOT_CODE_FILE` 是模組層常數,bridge import 當下就定死了;而且
# bridge import 的最後就會呼叫一次 `_pair_boot_code()` 把碼讀進行程快取。
# 全套 `unittest discover` 一起跑時 bridge 早被別的測試檔 import 過 → 本檔
# 設的 env 變成 no-op,測試驗的是**真的** `~/.pocket/pair-boot-code`
# (這台正在跑 production bridge)。
#
# 正式行為不動,只在測試側把模組常數綁回本檔的 tmp、順便把快取清掉,讓
# 下一次 `_pair_boot_code()` 重鑄到 tmp:順序無關,也不再碰使用者家目錄。
bridge._PAIR_BOOT_CODE_FILE = os.environ["PAIR_BOOT_CODE_FILE"]
with bridge._PAIR_BOOT_CODE_LOCK:
    bridge._PAIR_BOOT_CODE_CACHE["code"] = ""


class _FakeReq:
    def __init__(self, query=None, host="127.0.0.1"):
        self.query_params = dict(query or {})
        self.client = type("C", (), {"host": host})()
        self.headers = {}

    class _URL:
        path = "/pair/qr.json"
    url = _URL()


def _hosts_stub(force=False):
    return (["http://192.168.1.23:8081", "https://demo.trycloudflare.com"], False)


class TestPairQrBootCode(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.boot = bridge._pair_boot_code()
        with bridge._PAIR_LOCK:
            bridge._PAIR_QR_MINTS.clear()
            bridge._PAIR_CODES.clear()
        self.patches = [
            patch.object(bridge, "_pair_host_candidates", side_effect=_hosts_stub),
            patch.object(bridge, "_pair_tunnel_url", return_value=None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    # ── boot code 檔案本身 ──────────────────────────────────────────────
    def test_boot_code_file_created_0600(self):
        path = bridge._PAIR_BOOT_CODE_FILE
        self.assertTrue(os.path.isfile(path))
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), self.boot)
        self.assertGreaterEqual(len(self.boot), 16)   # token_urlsafe(16) ≥ 16 chars

    # ── qr.json:缺碼/錯碼 → 403,不鑄碼 ────────────────────────────────
    async def test_qr_json_missing_boot_403(self):
        with self.assertRaises(HTTPException) as ctx:
            await bridge.pair_qr_json(_FakeReq())
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(len(bridge._PAIR_CODES), 0)

    async def test_qr_json_wrong_boot_403(self):
        with self.assertRaises(HTTPException) as ctx:
            await bridge.pair_qr_json(_FakeReq(query={"boot": "x" * 22}))
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(len(bridge._PAIR_CODES), 0)

    # ── qr.json:正確碼 → 200 + 有效 payload ───────────────────────────
    async def test_qr_json_correct_boot_ok(self):
        d = await bridge.pair_qr_json(_FakeReq(query={"boot": self.boot}))
        self.assertTrue(d["ok"])
        self.assertTrue(d["payload"].startswith("pocket://pair?"))
        self.assertIn("hosts=", d["payload"])
        self.assertEqual(d["hosts"], _hosts_stub()[0])
        self.assertEqual(len(bridge._PAIR_CODES), 1)   # 真的鑄了一枚碼
        code = next(iter(bridge._PAIR_CODES))
        self.assertIn(code, d["payload"])

    # ── /pair/qr 頁面同一道閘 ──────────────────────────────────────────
    async def test_qr_page_requires_boot(self):
        with self.assertRaises(HTTPException) as ctx:
            await bridge.pair_qr_page(_FakeReq())
        self.assertEqual(ctx.exception.status_code, 403)
        resp = await bridge.pair_qr_page(_FakeReq(query={"boot": self.boot}))
        self.assertEqual(resp.status_code, 200)

    # ── defense-in-depth:loopback 第二層仍在 ──────────────────────────
    async def test_local_only_layer_still_enforced(self):
        with self.assertRaises(HTTPException) as ctx:
            await bridge.pair_qr_json(
                _FakeReq(query={"boot": self.boot}, host="203.0.113.9"))
        self.assertEqual(ctx.exception.status_code, 403)

    # ── 鑄碼節流:10/min,第 11 次 429 ─────────────────────────────────
    async def test_mint_rate_limit(self):
        for _ in range(bridge._PAIR_QR_MINT_LIMIT):
            d = await bridge.pair_qr_json(_FakeReq(query={"boot": self.boot}))
            self.assertTrue(d["ok"])
        with self.assertRaises(HTTPException) as ctx:
            await bridge.pair_qr_json(_FakeReq(query={"boot": self.boot}))
        self.assertEqual(ctx.exception.status_code, 429)


if __name__ == "__main__":
    unittest.main()
