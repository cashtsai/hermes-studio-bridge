"""Browser-based Sign in with Apple flow for Developer ID builds."""

import os
import sys
import tempfile
import unittest
import urllib.parse
from unittest.mock import AsyncMock, Mock, patch

_TMP = tempfile.mkdtemp(prefix="apple-web-auth-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class FakeRequest:
    def __init__(self, *, body=b"", json_body=None, content_type="application/json"):
        self._body = body
        self._json_body = json_body
        self.headers = {"content-type": content_type}
        self.client = None
        self.url = Mock(path="/test")

    async def body(self):
        return self._body

    async def json(self):
        if self._json_body is None:
            raise ValueError("no json")
        return self._json_body


class TestAppleWebAuth(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bridge._APPLE_WEB_FLOWS.clear()
        bridge._APPLE_WEB_STARTS.clear()
        self.key_path = os.path.join(_TMP, "AuthKey_TEST.p8")
        with open(self.key_path, "w", encoding="utf-8") as f:
            f.write("test-key")
        self.config_patches = [
            patch.object(bridge, "APPLE_WEB_CLIENT_ID", "com.pocketagent.web"),
            patch.object(
                bridge,
                "APPLE_WEB_REDIRECT_URI",
                "https://pocket.example/app/v1/auth/apple/web/callback",
            ),
            patch.object(bridge, "APPLE_WEB_TEAM_ID", "TEAM123"),
            patch.object(bridge, "APPLE_WEB_KEY_ID", "KEY123"),
            patch.object(bridge, "APPLE_WEB_PRIVATE_KEY_PATH", self.key_path),
        ]
        for config_patch in self.config_patches:
            config_patch.start()

    def tearDown(self):
        for config_patch in reversed(self.config_patches):
            config_patch.stop()
        bridge._APPLE_WEB_FLOWS.clear()
        bridge._APPLE_WEB_STARTS.clear()

    async def test_start_builds_stateful_apple_authorization_url(self):
        request = Mock()
        request.headers = {"cf-connecting-ip": "203.0.113.10"}
        request.client = None
        result = await bridge.app_auth_apple_web_start(request)

        parsed = urllib.parse.urlparse(result["authorization_url"])
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "appleid.apple.com")
        self.assertEqual(query["client_id"], ["com.pocketagent.web"])
        self.assertEqual(query["response_type"], ["code id_token"])
        self.assertEqual(query["response_mode"], ["form_post"])
        self.assertEqual(query["scope"], ["name email"])
        self.assertTrue(query["state"][0])
        self.assertTrue(query["nonce"][0])
        self.assertNotIn(result["poll_secret"], result["authorization_url"])

    def test_local_bridge_accepts_public_web_identity_audience(self):
        self.assertIn("com.pocketagent.web", bridge.APPLE_ID_AUDIENCES)

    def test_client_secret_is_short_lived_es256_jwt(self):
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        private_key = ec.generate_private_key(ec.SECP256R1())
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        with open(self.key_path, "wb") as f:
            f.write(pem)

        token = bridge._apple_web_client_secret()
        header = jwt.get_unverified_header(token)
        claims = jwt.decode(token, options={"verify_signature": False})
        self.assertEqual(header["alg"], "ES256")
        self.assertEqual(header["kid"], "KEY123")
        self.assertEqual(claims["iss"], "TEAM123")
        self.assertEqual(claims["sub"], "com.pocketagent.web")
        self.assertEqual(claims["aud"], "https://appleid.apple.com")
        self.assertLessEqual(claims["exp"] - claims["iat"], 310)

    async def test_code_exchange_binds_client_and_redirect_uri(self):
        captured = {}
        response = Mock(status_code=200)
        response.json.return_value = {"id_token": "verified-id-token"}

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, url, *, data, headers):
                captured.update({"url": url, "data": data, "headers": headers})
                return response

        with (
            patch.object(bridge, "_apple_web_client_secret", return_value="client-secret"),
            patch("httpx.AsyncClient", return_value=FakeClient()),
        ):
            payload = await bridge._apple_web_exchange_code("one-time-code")

        self.assertEqual(payload["id_token"], "verified-id-token")
        self.assertEqual(captured["url"], "https://appleid.apple.com/auth/token")
        self.assertEqual(captured["data"]["client_id"], "com.pocketagent.web")
        self.assertEqual(captured["data"]["client_secret"], "client-secret")
        self.assertEqual(captured["data"]["code"], "one-time-code")
        self.assertEqual(captured["data"]["grant_type"], "authorization_code")
        self.assertEqual(
            captured["data"]["redirect_uri"],
            "https://pocket.example/app/v1/auth/apple/web/callback",
        )

    async def test_callback_exchanges_code_and_status_returns_identity_once(self):
        flow = bridge._apple_web_new_flow()
        form = urllib.parse.urlencode({
            "state": flow["state"],
            "code": "one-time-code",
            "id_token": "front-token",
            "user": '{"email":"user@example.com","name":{"firstName":"Pocket","lastName":"User"}}',
        }).encode()
        claims = {
            "sub": "apple-user-1",
            "aud": "com.pocketagent.web",
            "nonce": flow["nonce"],
            "email": "user@example.com",
        }
        exchanged = dict(claims)
        with (
            patch.object(
                bridge,
                "_apple_verify_identity_token",
                side_effect=[claims, exchanged],
            ) as verify,
            patch.object(
                bridge,
                "_apple_web_exchange_code",
                AsyncMock(return_value={"id_token": "exchanged-token"}),
            ) as exchange,
            patch.object(bridge, "_account_upsert_user") as account_upsert,
        ):
            page = await bridge.app_auth_apple_web_callback(
                FakeRequest(
                    body=form,
                    content_type="application/x-www-form-urlencoded",
                )
            )

        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b"exchanged-token", page.body)
        self.assertEqual(verify.call_count, 2)
        exchange.assert_awaited_once_with("one-time-code")
        account_upsert.assert_not_called()

        status_request = FakeRequest(json_body={
            "flow_id": flow["flow_id"],
            "poll_secret": flow["poll_secret"],
        })
        status = await bridge.app_auth_apple_web_status(status_request)
        self.assertEqual(status["status"], "complete")
        self.assertEqual(status["identity"]["apple_user_id"], "apple-user-1")
        self.assertEqual(status["identity"]["identity_token"], "exchanged-token")
        self.assertEqual(status["identity"]["display_name"], "Pocket User")

        with self.assertRaises(Exception) as raised:
            await bridge.app_auth_apple_web_status(status_request)
        self.assertEqual(getattr(raised.exception, "status_code", None), 404)

    async def test_callback_rejects_wrong_nonce_and_cannot_be_replayed(self):
        flow = bridge._apple_web_new_flow()
        form = urllib.parse.urlencode({
            "state": flow["state"],
            "code": "one-time-code",
            "id_token": "front-token",
        }).encode()
        exchange = AsyncMock(return_value={"id_token": "unused"})
        with (
            patch.object(
                bridge,
                "_apple_verify_identity_token",
                return_value={
                    "sub": "apple-user-1",
                    "aud": "com.pocketagent.web",
                    "nonce": "wrong-nonce",
                },
            ),
            patch.object(bridge, "_apple_web_exchange_code", exchange),
        ):
            first_page = await bridge.app_auth_apple_web_callback(
                FakeRequest(
                    body=form,
                    content_type="application/x-www-form-urlencoded",
                )
            )
            second_page = await bridge.app_auth_apple_web_callback(
                FakeRequest(
                    body=form,
                    content_type="application/x-www-form-urlencoded",
                )
            )

        self.assertIn("登入未完成".encode(), first_page.body)
        self.assertIn("登入未完成".encode(), second_page.body)
        exchange.assert_not_awaited()
        self.assertEqual(bridge._APPLE_WEB_FLOWS[flow["flow_id"]]["status"], "failed")

    async def test_cancelled_authorization_is_reported_without_token_exchange(self):
        flow = bridge._apple_web_new_flow()
        form = urllib.parse.urlencode({
            "state": flow["state"],
            "error": "user_cancelled_authorize",
        }).encode()
        exchange = AsyncMock()
        with patch.object(bridge, "_apple_web_exchange_code", exchange):
            page = await bridge.app_auth_apple_web_callback(
                FakeRequest(
                    body=form,
                    content_type="application/x-www-form-urlencoded",
                )
            )
        self.assertIn("已取消登入".encode(), page.body)
        exchange.assert_not_awaited()

        status = await bridge.app_auth_apple_web_status(FakeRequest(json_body={
            "flow_id": flow["flow_id"],
            "poll_secret": flow["poll_secret"],
        }))
        self.assertEqual(status["status"], "cancelled")
        self.assertEqual(status["error"], "cancelled")

    async def test_status_rejects_wrong_poll_secret(self):
        flow = bridge._apple_web_new_flow()
        with self.assertRaises(Exception) as raised:
            await bridge.app_auth_apple_web_status(FakeRequest(json_body={
                "flow_id": flow["flow_id"],
                "poll_secret": "wrong",
            }))
        self.assertEqual(getattr(raised.exception, "status_code", None), 404)

    async def test_start_is_rate_limited_per_cloudflare_client(self):
        request = Mock()
        request.headers = {"cf-connecting-ip": "203.0.113.99"}
        request.client = None
        for _ in range(bridge.APPLE_WEB_START_RATE_LIMIT):
            result = await bridge.app_auth_apple_web_start(request)
            self.assertTrue(result["ok"])
        with self.assertRaises(Exception) as raised:
            await bridge.app_auth_apple_web_start(request)
        self.assertEqual(getattr(raised.exception, "status_code", None), 429)


if __name__ == "__main__":
    unittest.main()
