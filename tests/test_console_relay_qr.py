import _isolation  # noqa: F401
import collections
import hashlib
import json
import os
import plistlib
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
        self.env = patch.dict(os.environ, {"POCKET_CONSOLE_ENTRY_MODE": "relay",
                                            "POCKET_RELAY_ENABLED": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.enabled = root / "enabled"
        self.enabled.touch()
        self.grant = root / "grant.json"
        self.grant.write_text(json.dumps({"hostname": self.relay, "frp_token": "fixture"}))
        for key, val in dict(POCKET_CONSOLE_ENABLED=True, POCKET_CONSOLE_WEB=False,
                             POCKET_CONSOLE_QR_LOGIN=False, _DEVICE_TOKENS={},
                             _PAIR_CODES={}, _CONSOLE_LOGIN_REQS={}, _CONSOLE_GRANTS={},
                             _RELAY_STATUS={"state": "connected", "hostname": self.relay},
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
        self.grant.unlink()
        self.assertIsNone(discover()["relay_qr_url"])
        self.assertEqual(self.web.get("/console").status_code, 403)

    def test_stale_or_unentitled_routes_are_not_advertised(self):
        for state in ("not_enrolled", "claiming", "connecting", "unentitled", "rejected", "off"):
            with self.subTest(state=state), patch.dict(bridge._RELAY_STATUS, state=state):
                data = self.web.get("/app/v1/console/boot", headers=self.phone).json()
                self.assertIsNone(data["relay_qr_url"])
                self.assertEqual(self.web.get("/console").status_code, 403)
        with patch.dict(bridge._RELAY_STATUS, hostname="stale.relay.example"):
            self.assertIsNone(self.web.get("/app/v1/console/boot", headers=self.phone).json()["relay_qr_url"])

    def test_unconfigured_upgrade_recognizes_existing_grant(self):
        with patch.dict(os.environ, {"POCKET_CONSOLE_ENTRY_MODE": ""}):
            self.assertIn("pocket://console-login", self.web.get("/console").text)
            self.assertEqual(self.web.get("/console", headers={"Host": "another.example"}).status_code, 403)

    def test_no_relay_still_allows_private_login_not_public_login(self):
        self.enabled.unlink()
        self.grant.unlink()
        with patch.object(bridge, "POCKET_CONSOLE_QR_LOGIN", True):
            private = TestClient(bridge.app, client=("127.0.0.1", 8081))
            self.addCleanup(private.close)
            self.assertEqual(private.get("/console", headers={"Host": "localhost:8081"}).status_code, 401)
            self.assertEqual(self.web.get("/console").status_code, 403)
            self.assertEqual(self.web.get("/console/login/status").status_code, 404)

    def test_customer_installers_enable_same_mode_and_bundle_qr_support(self):
        root = Path(__file__).resolve().parents[1]
        mac = (root / "deploy/install-local-bridge.sh").read_text()
        plist = mac.split('cat > "$LAUNCH_AGENT" <<PLIST\n', 1)[1].split("\nPLIST", 1)[0]
        env = plistlib.loads(plist.encode())["EnvironmentVariables"]
        self.assertEqual(env["POCKET_CONSOLE_ENABLED"], "${POCKET_CONSOLE_ENABLED:-1}")
        self.assertEqual(env["POCKET_CONSOLE_ENTRY_MODE"], "${POCKET_CONSOLE_ENTRY_MODE:-relay}")
        self.assertIn("qrcode", mac.split('"$BRIDGE_VENV/bin/python" -m pip install \\\n', 1)[1].split("\n", 1)[0].split())
        linux = (root / "deploy/install-ubuntu.sh").read_text()
        self.assertIn('CONSOLE_ENTRY_MODE="${POCKET_CONSOLE_ENTRY_MODE:-relay}"', linux)
        self.assertIn('CONSOLE_QR_LOGIN="${POCKET_CONSOLE_QR_LOGIN:-0}"', linux)
        self.assertIn('pip install -q -r "$INSTALL_ROOT/requirements.txt"', linux)
        self.assertIn("qrcode", (root / "requirements.txt").read_text().splitlines())

    def test_second_customer_gets_their_own_bookmark_and_cannot_approve_the_first(self):
        rid = re.search(r"pocket://console-login\?rid=([0-9a-f]{32})", self.web.get("/console").text)[1]
        second = "other.customer.example"
        with tempfile.TemporaryDirectory() as home, patch.object(bridge, "_relay_dir", return_value=home), \
                patch.object(bridge, "_CONSOLE_LOGIN_REQS", {}), patch.object(bridge, "_DEVICE_TOKENS", {}), \
                patch.dict(bridge._RELAY_STATUS, hostname=second):
            Path(home, "enabled").touch()
            bridge._relay_save_grant({"hostname": second, "frp_token": "fixture-b"})
            phone_b = {"Authorization": "Bearer " + bridge._pair_issue_device_token("Phone B", "ios")[0]}
            self.assertEqual(self.web.get("/app/v1/console/boot", headers=phone_b).json()["relay_qr_url"],
                             "https://" + second + "/console")
            self.assertEqual(self.web.get("/console").status_code, 403)
            self.assertEqual(self.web.post(f"/app/v1/console/login/requests/{rid}/approve", headers=phone_b).status_code, 404)
            self.assertIn("pocket://console-login", self.web.get("https://" + second + "/console").text)
        self.assertEqual(self.web.post(f"/app/v1/console/login/requests/{rid}/approve", headers=phone_b).status_code, 401)
        self.assertEqual(self.web.get("/console/login/status").json()["state"], "pending")

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
        with patch.object(bridge, "POCKET_CONSOLE_ENABLED", False):
            self.assertEqual(self.web.get("/app/v2/sessions", headers=auth).status_code, 403)
        self.assertEqual(self.web.get("/app/v2/sessions", headers={**auth, "Host": "other.example"}).status_code, 403)
        self.enabled.unlink()
        self.assertEqual(self.web.get("/health", headers=auth).status_code, 403)
        self.assertEqual(self.web.post("/console/logout", headers=auth).status_code, 200)
        self.assertEqual(self.web.get("/app/v2/sessions", headers=auth).status_code, 401)

    def test_shared_entry_one_scan_requires_original_browser_and_paired_approval(self):
        rid, verifier = "c"*32, "d"*64
        proof = {"rid":rid, "challenge":hashlib.sha256(verifier.encode()).hexdigest()}
        page = "/console/entry/start"
        form = {"rid":rid, "verifier":verifier}
        origin = {"Origin":"https://console.pocketone.app"}
        lookup = "/app/v1/console/entry/lookup"
        self.assertEqual(self.web.post(page, data=form).status_code,403)
        self.assertEqual(self.web.post(lookup,json=proof).status_code,401)
        self.assertEqual(self.web.post(lookup,json=proof,headers=self.phone).status_code,202)
        r = self.web.post(page,data=form,headers=origin)
        self.assertEqual(r.status_code,200)
        self.assertNotIn("pocket://console-login", r.text)
        self.assertNotIn(verifier, r.text)
        self.assertEqual(self.web.post(page,data=form,headers=origin).status_code,409)
        self.assertEqual(self.web.post(lookup,json={**proof,"challenge":"0"*64},headers=self.phone).status_code,202)
        local = self.web.post(lookup,json=proof,headers=self.phone).json()["rid"]
        self.assertEqual(self.web.get("/console/login/status").json()["state"],"pending")
        self.assertEqual(self.web.post("/app/v1/console/login/requests/"+local+"/approve",headers=self.phone).status_code,200)
        stranger=TestClient(bridge.app, base_url=f"https://{self.relay}", client=("127.0.0.1", 2345))
        self.addCleanup(stranger.close)
        self.assertEqual(stranger.get("/console/login/status").json()["state"],"expired")
        self.assertEqual(self.web.get("/console/login/status").json()["state"],"approved")
        code=self.web.cookies.get(bridge._CONSOLE_CLAIM_COOKIE_NAME)
        self.assertEqual(self.web.post("/pair/claim",json={"code":code}).json()["scope"],bridge.CONSOLE_REMOTE_SCOPE)
        self.assertEqual(self.web.get("/console/login/status").json()["state"],"expired")
