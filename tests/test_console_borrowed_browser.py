import _isolation  # noqa: F401
import asyncio
import os
import sys
import time
import unittest
from unittest.mock import patch
from contextlib import ExitStack

os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge
from fastapi.testclient import TestClient


class BorrowedBrowserTest(unittest.TestCase):
    def setUp(self):
        self.web = TestClient(bridge.app, base_url="https://relay.test",
                              client=("203.0.113.5", 50000))
        self.addCleanup(self.web.close)
        for key, value in {
            "POCKET_CONSOLE_ENABLED": True, "POCKET_CONSOLE_QR_LOGIN": True,
            "_DEVICE_TOKENS": {}, "_PAIR_CODES": {}, "_CONSOLE_LOGIN_REQS": {},
            "_CONSOLE_LOGIN_MINTS": [], "_save_device_tokens": lambda _: None,
            "_console_web_ok": lambda _: False, "_pair_qr_check_mint_rate": lambda: None,
            "_account_device_for_token": lambda _: None,
        }.items():
            p = patch.object(bridge, key, value)
            p.start()
            self.addCleanup(p.stop)

    def login(self):
        self.assertIn("pocket://console-login", self.web.get("/console").text)
        rid = next(reversed(bridge._CONSOLE_LOGIN_REQS))
        owner = {"Authorization": "Bearer " + bridge.BRIDGE_TOKEN}
        self.assertEqual(self.web.post(f"/app/v1/console/login/requests/{rid}/approve",
                                       headers=owner).status_code, 200)
        self.assertEqual(self.web.get("/console/login/status").json()["state"], "approved")
        code = self.web.cookies.get(bridge._CONSOLE_CLAIM_COOKIE_NAME)
        r = self.web.post("/pair/claim", json={"code": code, "platform": "ios", "scope": "owner"})
        self.assertEqual(r.status_code, 200)
        return r.json()["token"]

    def test_real_claim_is_scoped_and_cannot_open_a_terminal(self):
        token = self.login()
        h = {"Authorization": "Bearer " + token}
        self.assertEqual(self.web.get("/pair/devices", headers=h).status_code, 403)
        self.assertGreater(bridge._DEVICE_TOKENS[token]["expires"], time.time())
        self.assertFalse(bridge._ws_token_authorized(token))
        self.assertIsNone(bridge._terminal_device_id_for_token(token))
        self.assertEqual(self.web.get("/health", headers=h).status_code, 200)

    def test_logout_revokes_only_presented_browser_and_clears_cookies(self):
        token = self.login()
        other = bridge._pair_issue_device_token("Phone", "ios")[0]
        h = {"Authorization": "Bearer " + token}
        r = self.web.post("/console/logout", headers=h, json={"id": bridge._short_hash(other)})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(token, bridge._DEVICE_TOKENS)
        self.assertIn(other, bridge._DEVICE_TOKENS)
        self.assertFalse(list(self.web.cookies))
        self.assertEqual(self.web.get("/pair/devices", headers=h).status_code, 401)
        self.assertIn("pocket://console-login", self.web.get("/console").text)

    def test_no_cookie_query_or_master_logout(self):
        token = self.login()
        self.assertEqual(self.web.post("/console/logout", params={"token": token}).status_code, 401)
        self.assertEqual(self.web.post("/console/logout", headers={
            "Authorization": "Bearer " + bridge.BRIDGE_TOKEN}).status_code, 403)
        self.assertIn(token, bridge._DEVICE_TOKENS)

    def test_revoke_stops_an_open_remote_stream_before_its_next_chunk(self):
        token = self.login()

        async def check():
            async def events():
                yield b"public-to-browser"
                yield b"after-revocation"
            stream = bridge._console_guard_stream(token, events())
            self.assertEqual(await anext(stream), b"public-to-browser")
            del bridge._DEVICE_TOKENS[token]
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)
        asyncio.run(check())

    def test_idle_browser_cannot_resume_but_can_finish_logout(self):
        token = self.login()
        bridge._DEVICE_TOKENS[token]["last_seen"] = time.time() - bridge.CONSOLE_REMOTE_IDLE - 1
        h = {"Authorization": "Bearer " + token}
        self.assertEqual(self.web.get("/health", headers=h).status_code, 401)
        self.assertEqual(self.web.post("/console/logout", headers=h).status_code, 200)

    def test_independent_device_contract_smoke(self):
        owner = {"Authorization": "Bearer " + bridge.BRIDGE_TOKEN}
        self.assertEqual(self.web.get("/capabilities", headers=owner).status_code, 200)
        with patch.dict(bridge.PERSONAS, {"qa-work": {}}):
            r = self.web.post("/app/v1/messages", headers=owner,
                              json={"session": "qa-work", "content": "test", "dry_run": True})
        self.assertEqual(r.status_code, 200)
        self.assertIn("data: [DONE]", r.text)

    def test_home_recency_is_per_cc_lane_not_per_directory(self):
        h = {"Authorization": "Bearer " + self.login()}
        with ExitStack() as stack:
            def stub(key, value):
                stack.enter_context(patch.object(bridge, key, return_value=value))
            stub("_cc_conf_rows", [(n, "/demo", "1") for n in ("edited", "unseen")])
            stub("_v2_cc_state", ("idle", None))
            stub("_delegation_v2_sessions", [])
            stub("_delegated_codex_thread_ids", set())
            stub("_hermes_pending_by_session", {})
            stub("_codex_v2_visible_threads", [])
            stack.enter_context(patch.object(bridge, "_CC_HOOK_STATE", {"edited": {"updated_at": 1000}}))
            stack.enter_context(patch.object(bridge, "PERSONAS", {}))
            for mod, flag in [(bridge.GEMINI, "configured"), (bridge.OPENCLAW, "configured"), (bridge.cc_sdk, "enabled")]:
                stack.enter_context(patch.object(mod, flag, return_value=False))
            stack.enter_context(patch.dict(os.environ, {"POCKET_HOST_NAME": "QA Studio"}))
            res = self.web.get("/app/v2/sessions", headers=h)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["host_name"], "QA Studio")
        self.assertEqual({s["id"]: s["last_event_at"] for s in data["sessions"]},
                         {"claude_code:edited": "1970-01-01T00:16:40Z", "claude_code:unseen": None})

    def test_qr_enabled_relay_does_not_offer_short_code_bypass(self):
        with patch.object(bridge, "_console_web_ok", return_value=True):
            self.assertIn("pocket://console-login", self.web.get("/console").text)
            self.assertEqual(self.web.post("/console/auth", json={"code": "123456"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
