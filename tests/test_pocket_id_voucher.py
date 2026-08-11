"""Pairing V3(Pocket ID)— voucher claim + enroll/heartbeat client 測試。

藍圖:studio-os/docs/PAIRING_V3_POCKET_ID_20260811.md。
驗項:
- voucher happy path:本地產 Ed25519 金鑰對、模擬已 enroll、簽 voucher →
  /pair/claim-voucher 發 device token(形狀同 /pair/claim)
- 過期 voucher 403 / 錯 host_id 403 / nonce 重放 403 / 竄改 payload 403
- 未 enroll → 404(graceful absence)
- enroll(register)與 heartbeat client 的 payload 形狀(mock 到 API 邊界)
- POCKET_ID_RESET / env 撤除 → 開機清 enrollment
"""
import asyncio
import base64
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="pocket-id-")
os.environ["PAIR_BOOT_CODE_FILE"] = os.path.join(_TMP, "pair-boot-code")
os.environ["POCKET_ID_STATE_FILE"] = os.path.join(_TMP, "pocket-id-enrollment.json")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding, PublicFormat)


def _make_keypair():
    priv = Ed25519PrivateKey.generate()
    raw_pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv, base64.b64encode(raw_pub).decode("ascii")


def _make_voucher(priv, payload: dict) -> str:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = priv.sign(body)
    return bridge._b64u(body) + "." + bridge._b64u(sig)


class _FakeReq:
    def __init__(self, body=None):
        self._body = body or {}
        self.headers = {}
        self.client = type("C", (), {"host": "203.0.113.9"})()

    class _URL:
        path = "/pair/claim-voucher"
    url = _URL()

    async def json(self):
        return dict(self._body)


def _run(coro):
    return asyncio.run(coro)


class _Base(unittest.TestCase):
    HOST_ID = "host-abc123"

    def setUp(self):
        self.priv, self.pub_b64 = _make_keypair()
        bridge._pocket_id_save_state({
            "host_id": self.HOST_ID,
            "host_secret": "sekret",
            "pocket_id_pubkey": self.pub_b64,
            "url": "https://id.example.test",
        })
        with bridge._POCKET_ID_LOCK:
            bridge._POCKET_ID_NONCES.clear()
        # token store 導到 tmp,絕不碰真的 ~/.pocket/device-tokens.json
        self._tokens = {}
        self.patches = [
            patch.object(bridge, "_DEVICE_TOKENS_PATH",
                         os.path.join(_TMP, "device-tokens.json")),
            patch.object(bridge, "_DEVICE_TOKENS", self._tokens),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def _voucher(self, **over):
        payload = {"account_id": "acct-1", "host_id": self.HOST_ID,
                   "exp": time.time() + 90, "nonce": os.urandom(8).hex()}
        payload.update(over)
        return _make_voucher(self.priv, payload)

    def _claim(self, body):
        return _run(bridge.pair_claim_voucher(_FakeReq(body)))

    def _claim_403(self, body):
        with self.assertRaises(HTTPException) as ctx:
            self._claim(body)
        self.assertEqual(ctx.exception.status_code, 403)
        return ctx.exception


class TestVoucherClaim(_Base):
    def test_happy_path_mints_device_token(self):
        out = self._claim({"voucher": self._voucher(), "device_name": "小方手機"})
        self.assertTrue(out["token"].startswith("pdev-"))
        self.assertFalse(out["account_bound"])
        self.assertIn("device_id", out)          # /pair/claim 同形
        dev = self._tokens[out["token"]]
        self.assertEqual(dev["name"], "小方手機")
        self.assertEqual(dev["platform"], "ios")
        self.assertIsNone(dev["apple_user_id"])  # 未綁 Apple 帳號 → _check_auth 放行
        self.assertEqual(dev["pocket_account_id"], "acct-1")
        # token store 有落地
        with open(os.path.join(_TMP, "device-tokens.json"), encoding="utf-8") as f:
            self.assertIn(out["token"], json.load(f))

    def test_expired_voucher_403(self):
        self._claim_403({"voucher": self._voucher(exp=time.time() - 300)})

    def test_exp_within_skew_ok(self):
        out = self._claim({"voucher": self._voucher(exp=time.time() - 60)})
        self.assertTrue(out["token"].startswith("pdev-"))

    def test_wrong_host_id_403(self):
        self._claim_403({"voucher": self._voucher(host_id="host-OTHER")})

    def test_replayed_nonce_403(self):
        v = self._voucher()
        self._claim({"voucher": v})
        self._claim_403({"voucher": v})
        # 同 nonce 換一張新簽的也擋
        v2 = self._voucher(nonce=json.loads(
            bridge._b64u_decode(v.split(".")[0]))["nonce"])
        self._claim_403({"voucher": v2})

    def test_tampered_payload_403(self):
        v = self._voucher()
        body = json.loads(bridge._b64u_decode(v.split(".")[0]))
        body["account_id"] = "acct-EVIL"
        forged = bridge._b64u(json.dumps(body, separators=(",", ":")).encode()) \
            + "." + v.split(".")[1]
        self._claim_403({"voucher": forged})

    def test_wrong_key_signature_403(self):
        other_priv, _ = _make_keypair()
        self._claim_403({"voucher": _make_voucher(other_priv, {
            "account_id": "a", "host_id": self.HOST_ID,
            "exp": time.time() + 90, "nonce": "n1"})})

    def test_missing_and_malformed_voucher_403(self):
        self._claim_403({})
        self._claim_403({"voucher": "not-a-voucher"})
        self._claim_403({"voucher": "!!!.###"})

    def test_not_enrolled_404(self):
        bridge._pocket_id_clear_state("test")
        with self.assertRaises(HTTPException) as ctx:
            self._claim({"voucher": self._voucher()})
        self.assertEqual(ctx.exception.status_code, 404)

    def test_state_file_is_0600(self):
        mode = stat.S_IMODE(os.stat(os.environ["POCKET_ID_STATE_FILE"]).st_mode)
        self.assertEqual(mode, 0o600)


class TestEnrollHeartbeatClient(_Base):
    """enroll / heartbeat client 的 payload 形狀 —— mock 在 _pocket_id_api
    (HTTP 邊界),等同 mock server 收到的 body。"""

    def _cands(self):
        return (["http://192.168.1.23:8081", "https://lobster.tail1234.ts.net"], True)

    def test_register_payload_shape(self):
        bridge._pocket_id_clear_state("test")
        seen = {}

        async def fake_api(path, payload):
            seen["path"], seen["payload"] = path, payload
            return {"host_id": "h-9", "host_secret": "s-9",
                    "pocket_id_pubkey": self.pub_b64}

        with patch.object(bridge, "_pocket_id_api", side_effect=fake_api), \
                patch.object(bridge, "POCKET_ID_URL", "https://id.example.test"), \
                patch.object(bridge, "POCKET_ID_ENROLL_TOKEN", "enroll-once"), \
                patch.object(bridge, "_pair_host_candidates",
                             side_effect=lambda force=False: self._cands()):
            _run(bridge._pocket_id_register())
        self.assertEqual(seen["path"], "/v1/hosts/register")
        p = seen["payload"]
        self.assertEqual(p["enroll_token"], "enroll-once")
        self.assertTrue(p["name"])
        self.assertIn(p["platform"], ("macos", "linux", "windows"))
        self.assertEqual(p["candidates"],
                         [{"scheme": "http", "host": "192.168.1.23:8081"},
                          {"scheme": "https", "host": "lobster.tail1234.ts.net"}])
        self.assertIsInstance(p["capabilities"], dict)
        # register 成功 → enrollment 落地
        st = bridge._pocket_id_state()
        self.assertEqual(st["host_id"], "h-9")
        self.assertEqual(st["pocket_id_pubkey"], self.pub_b64)

    def test_heartbeat_payload_shape(self):
        seen = {}

        async def fake_api(path, payload):
            seen["path"], seen["payload"] = path, payload
            return {"ok": True}

        with patch.object(bridge, "_pocket_id_api", side_effect=fake_api), \
                patch.object(bridge, "_pair_host_candidates",
                             side_effect=lambda force=False: self._cands()):
            _run(bridge._pocket_id_heartbeat())
        self.assertEqual(seen["path"], "/v1/hosts/heartbeat")
        p = seen["payload"]
        self.assertEqual(p["host_id"], self.HOST_ID)
        self.assertEqual(p["host_secret"], "sekret")
        self.assertEqual(len(p["candidates"]), 2)
        self.assertIsInstance(p["capabilities"], dict)


class TestBootGovernance(_Base):
    def test_reset_env_clears_enrollment(self):
        with patch.dict(os.environ, {"POCKET_ID_RESET": "1"}), \
                patch.object(bridge, "POCKET_ID_URL", "https://id.example.test"):
            bridge._pocket_id_boot()
        self.assertEqual(bridge._pocket_id_state(), {})
        self.assertFalse(os.path.exists(os.environ["POCKET_ID_STATE_FILE"]))

    def test_url_removed_clears_enrollment(self):
        with patch.object(bridge, "POCKET_ID_URL", ""):
            bridge._pocket_id_boot()
        self.assertEqual(bridge._pocket_id_state(), {})

    def test_url_changed_clears_enrollment(self):
        with patch.object(bridge, "POCKET_ID_URL", "https://other.example.test"):
            bridge._pocket_id_boot()
        self.assertEqual(bridge._pocket_id_state(), {})

    def test_matching_url_keeps_enrollment(self):
        with patch.object(bridge, "POCKET_ID_URL", "https://id.example.test"):
            bridge._pocket_id_boot()
        self.assertEqual(bridge._pocket_id_state()["host_id"], self.HOST_ID)


if __name__ == "__main__":
    unittest.main()
