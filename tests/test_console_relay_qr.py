import _isolation  # noqa: F401
import collections
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge
from fastapi.testclient import TestClient


class RelayQRTest(unittest.TestCase):
    relay = "owner.relay.example"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.marker = root / "console-qr-enabled"
        self.marker.touch()
        self.enabled = root / "enabled"
        self.enabled.touch()
        (root / "grant.json").write_text(json.dumps({"hostname": self.relay}))
        for key, val in dict(POCKET_CONSOLE_ENABLED=True, POCKET_CONSOLE_WEB=False,
                             POCKET_CONSOLE_QR_LOGIN=False, _DEVICE_TOKENS={},
                             _PAIR_CODES={}, _CONSOLE_LOGIN_REQS={}, _CONSOLE_GRANTS={},
                             _CONSOLE_LOGIN_MINTS=collections.deque(), _relay_dir=lambda: str(root),
                             _save_device_tokens=lambda _: None, _pair_qr_check_mint_rate=lambda: None,
                             _account_device_for_token=lambda _: None, _console_boot_hosts=lambda: [],
                             _console_mint_short_code=lambda: ("123456", 9999999999)).items():
            p = patch.object(bridge, key, val)
            p.start()
            self.addCleanup(p.stop)
        self.web = TestClient(bridge.app, base_url=f"https://{self.relay}",
                              client=("127.0.0.1", 1234),
                              headers={"X-Forwarded-For": "198.51.100.5"})
        self.addCleanup(self.web.close)
        self.phone = {"Authorization": "Bearer " + bridge._pair_issue_device_token("Phone", "ios")[0],
                      "Host": "phone.example"}

    def test_phone_discovery_and_no_relay_fallback(self):
        def discover():
            return self.web.get("/app/v1/console/boot", headers=self.phone).json()
        self.assertEqual(discover()["relay_qr_url"], f"https://{self.relay}/console")
        self.assertIsNone(discover()["url"])  # legacy tailnet field
        self.enabled.unlink()
        self.assertIsNone(discover()["relay_qr_url"])
        self.assertEqual(self.web.get("/console").status_code, 403)
        self.enabled.touch()
        self.marker.unlink()
        self.assertIsNone(discover()["relay_qr_url"])
        self.assertEqual(self.web.get("/console").status_code, 403)

    def test_wrong_ingress_cannot_mint_poll_or_use_a_signed_cookie(self):
        with patch.object(bridge, "POCKET_CONSOLE_WEB", True), patch.object(bridge, "POCKET_CONSOLE_QR_LOGIN", True):
            for host in ("other.relay.example", self.relay + ".evil", self.relay + ":8081"):
                headers = {"Host": host, "X-Forwarded-Host": self.relay,
                           "Cookie": bridge._CONSOLE_COOKIE_NAME + "=" + bridge._console_cookie_value()}
                for path in ("/console", "/console/login/status"):
                    self.assertEqual(self.web.get(path, headers=headers).status_code, 403)
            self.assertEqual(self.web.get("http://" + self.relay + "/console").status_code, 403)
            self.assertFalse(bridge._CONSOLE_LOGIN_REQS)
            direct = TestClient(bridge.app, client=("127.0.0.1", 1234))
            self.addCleanup(direct.close)
            self.assertEqual(direct.get("/console", headers={"Host": "other.example"}).status_code, 403)
            self.assertEqual(direct.get("/console", headers={"Host": "localhost:8081"}).status_code, 401)
            r = self.web.get("/console?boot=123456")
            self.assertIn("pocket://console-login", r.text)
            self.assertNotIn('action="/console/auth"', r.text)
            self.assertIn("Secure", r.headers["set-cookie"])
            self.assertEqual(self.web.post("/console/auth", json={"code": "123456"}).status_code, 403)

    def test_approval_claim_scoped_host_and_logout(self):
        page = self.web.get("/console")
        self.assertIn("<svg", page.text)
        rid = re.search(r"pocket://console-login\?rid=([0-9a-f]{32})", page.text)[1]
        approve = "/app/v1/console/login/requests/" + rid + "/approve"
        self.assertEqual(self.web.get("/console/login/status").json()["state"], "pending")
        self.assertEqual(self.web.get("/app/v2/sessions").status_code, 401)
        self.assertEqual(self.web.post(approve).status_code, 401)
        web_token = bridge._pair_issue_device_token("Console", "web", extra={"console_session": True})[0]
        self.assertEqual(self.web.post(approve, headers={"Authorization": "Bearer " + web_token}).status_code, 403)
        self.assertEqual(self.web.post(approve, headers=self.phone).status_code, 200)
        self.assertEqual(self.web.get("/console/login/status", headers={"Host": "other.example"}).status_code, 403)
        self.assertEqual(self.web.get("/console/login/status").json()["state"], "approved")
        code = self.web.cookies.get(bridge._CONSOLE_CLAIM_COOKIE_NAME)
        self.assertEqual(self.web.post("/pair/claim", headers={"Host": "other.example"},
                                       json={"code": code}).status_code, 403)
        claim = self.web.post("/pair/claim", json={"code": code, "scope": "owner"}).json()
        self.assertEqual(claim["scope"], bridge.CONSOLE_REMOTE_SCOPE)
        auth = {"Authorization": "Bearer " + claim["token"]}
        self.assertIn('data-console-access="remote"', self.web.get("/console").text)
        self.assertEqual(self.web.get("/health", headers=auth).status_code, 200)
        self.assertEqual(self.web.get("/app/v2/sessions", headers={**auth, "Host": "other.example"}).status_code, 403)
        self.marker.unlink()
        self.assertEqual(self.web.get("/health", headers=auth).status_code, 403)
        self.assertEqual(self.web.post("/console/logout", headers=auth).status_code, 200)
        self.assertEqual(self.web.get("/app/v2/sessions", headers=auth).status_code, 401)
