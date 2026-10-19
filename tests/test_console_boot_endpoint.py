"""GET /app/v1/console/boot —— 已配對裝置用自己的 device token 換 console 連結。

背景:要開 /console 原本得人工去終端機翻 `~/.pocket/pair-boot-code` 再手打進
網頁。手機早就配對過了,它手上的 device token 本來就能開終端機 PTY、瀏覽檔案
——比 boot code 更強的憑證——沒道理讓人繞一圈。

權限面查證(寫在 bridge.py 端點上方的註解):全 repo 只有 /pair/qr、
/pair/qr.json、/console 吃 boot code;boot code 能換到的最強東西就是一枚
device token,master BRIDGE_TOKEN 推不出來 → 不是權限擴大。

驗項:沒帶 token → 401 / 亂 token → 401 / device token → 200 且回得出碼 /
撤銷後的 token → 401 / url 用 tailnet 直連(不是 LAN、不是公網 tunnel、
不是 tailscale serve 的 443)/ boot code 不進日誌 / 旗標關 → 404。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="console-boot-")
os.environ["PAIR_BOOT_CODE_FILE"] = os.path.join(_TMP, "pair-boot-code")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["POCKET_CONSOLE_ENABLED"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

bridge._PAIR_BOOT_CODE_FILE = os.environ["PAIR_BOOT_CODE_FILE"]
with bridge._PAIR_BOOT_CODE_LOCK:
    bridge._PAIR_BOOT_CODE_CACHE["code"] = ""

_TS_SELF = {
    "DNSName": "cashcamp.tail905550.ts.net.",
    "TailscaleIPs": ["100.67.0.12", "fd7a:115c:a1e0::1234"],
}


class TestConsoleBootEndpoint(unittest.TestCase):
    def setUp(self):
        self.boot = bridge._pair_boot_code()
        self.dev_token = "pdev-test-console-boot"
        with bridge._PAIR_LOCK:
            bridge._DEVICE_TOKENS[self.dev_token] = {
                "name": "iPhone", "platform": "ios",
                "created": 0.0, "last_seen": 0.0,
                "apple_user_id": None, "device_id": None,
            }
        bridge._CONSOLE_BOOT_URL_CACHE.update({"ts": 0.0, "urls": []})
        self.patches = [
            patch.object(bridge, "POCKET_CONSOLE_ENABLED", True),
            patch.object(bridge, "_pair_tailscale_self", return_value=dict(_TS_SELF)),
            # device token 不落盤(production 的 ~/.pocket/device-tokens.json 不准碰)
            patch.object(bridge, "_save_device_tokens", lambda d: None),
            patch.object(bridge, "_account_device_for_token", return_value=None),
        ]
        for p in self.patches:
            p.start()
        self.client = TestClient(bridge.app, client=("127.0.0.1", 50000))

    def tearDown(self):
        self.client.close()
        for p in reversed(self.patches):
            p.stop()
        with bridge._PAIR_LOCK:
            bridge._DEVICE_TOKENS.pop(self.dev_token, None)
        bridge._CONSOLE_BOOT_URL_CACHE.update({"ts": 0.0, "urls": []})

    def _get(self, token=None):
        h = {"Authorization": "Bearer " + token} if token else {}
        return self.client.get("/app/v1/console/boot", headers=h)

    # ── 認證 ──────────────────────────────────────────────────────────
    def test_no_token_rejected(self):
        self.assertIn(self._get().status_code, (401, 429))

    def test_garbage_token_rejected(self):
        self.assertIn(self._get("pdev-not-a-real-token").status_code, (401, 429))

    def test_device_token_gets_code(self):
        r = self._get(self.dev_token)
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["boot"], self.boot)
        self.assertTrue(d["reachable"])
        self.assertTrue(d["url"])

    def test_master_token_also_allowed(self):
        """master 是嚴格更強的憑證,沒有理由擋(其餘 /app/* 端點也都收)。"""
        r = self._get(os.environ["BRIDGE_TOKEN"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["boot"], self.boot)

    def test_revoked_token_rejected(self):
        self.assertEqual(self._get(self.dev_token).status_code, 200)
        with bridge._PAIR_LOCK:                 # 撤銷 = 從 token store 移除
            bridge._DEVICE_TOKENS.pop(self.dev_token, None)
        self.assertIn(self._get(self.dev_token).status_code, (401, 429))

    # ── url:只能是 tailnet 直連 ───────────────────────────────────────
    def test_url_is_direct_tailnet_with_bridge_port(self):
        d = self._get(self.dev_token).json()
        # MagicDNS 名優先(tailnet IP 會換),且必須帶 bridge 埠 → 直連
        self.assertEqual(
            d["url"],
            "http://cashcamp.tail905550.ts.net:%d/console?boot=%s"
            % (bridge._BRIDGE_PORT, self.boot))
        for u in d["urls"]:
            self.assertTrue(u.startswith("http://"), u)
            self.assertIn(":%d/console?boot=" % bridge._BRIDGE_PORT, u)
        # tailnet IP 保底也在清單裡,v6 要包方括號
        self.assertIn("http://100.67.0.12:%d/console?boot=%s"
                      % (bridge._BRIDGE_PORT, self.boot), d["urls"])
        self.assertIn("http://[fd7a:115c:a1e0::1234]:%d/console?boot=%s"
                      % (bridge._BRIDGE_PORT, self.boot), d["urls"])

    def test_url_never_lan_or_public_tunnel(self):
        """LAN 與公網 tunnel 都開不起 /console(前者非 tailnet,後者帶 XFF)
        → 不准出現在候選裡,否則使用者點了只會吃 403。"""
        with patch.object(bridge, "_pair_host_candidates",
                          side_effect=lambda force=False: (
                              ["http://192.168.1.23:8081",
                               "https://demo.trycloudflare.com"], False)):
            d = self._get(self.dev_token).json()
        joined = " ".join(d["urls"])
        self.assertNotIn("192.168.", joined)
        self.assertNotIn("trycloudflare", joined)

    def test_no_tailnet_still_returns_code_with_hint(self):
        with patch.object(bridge, "_pair_tailscale_self", return_value={}):
            bridge._CONSOLE_BOOT_URL_CACHE.update({"ts": 0.0, "urls": []})
            d = self._get(self.dev_token).json()
        self.assertEqual(d["boot"], self.boot)
        self.assertIsNone(d["url"])
        self.assertEqual(d["urls"], [])
        self.assertFalse(d["reachable"])
        self.assertTrue(d["hint"])

    # ── 拿到的碼真的能開 /console ─────────────────────────────────────
    def test_returned_code_actually_opens_console(self):
        d = self._get(self.dev_token).json()
        r = self.client.get("/console", params={"boot": d["boot"]})
        self.assertEqual(r.status_code, 200)

    # ── boot code 不准進日誌 ─────────────────────────────────────────
    def test_boot_code_never_logged(self):
        seen = []
        with patch.object(bridge, "_log_event",
                          side_effect=lambda ev, **kw: seen.append((ev, kw))):
            self._get(self.dev_token)
        issued = [kw for ev, kw in seen if ev == "console_boot_issued"]
        self.assertEqual(len(issued), 1, "應該留一筆 console_boot_issued")
        blob = json.dumps(seen, default=str)
        self.assertNotIn(self.boot, blob, "boot code 不准出現在任何日誌欄位")

    # ── 旗標關 → 404(對外表現同不存在)────────────────────────────────
    def test_disabled_flag_404(self):
        with patch.object(bridge, "POCKET_CONSOLE_ENABLED", False):
            self.assertEqual(self._get(self.dev_token).status_code, 404)

    # ── 回歸:_pair_tailscale_host 經重構後語意不變 ───────────────────
    def test_tailscale_host_prefers_dnsname(self):
        self.assertEqual(bridge._pair_tailscale_host(),
                         "cashcamp.tail905550.ts.net")
        with patch.object(bridge, "_pair_tailscale_self",
                          return_value={"TailscaleIPs": ["100.1.2.3"]}):
            self.assertEqual(bridge._pair_tailscale_host(), "100.1.2.3")
        with patch.object(bridge, "_pair_tailscale_self", return_value={}):
            self.assertIsNone(bridge._pair_tailscale_host())


if __name__ == "__main__":
    unittest.main()
