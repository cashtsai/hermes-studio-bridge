"""Guest links(HMAC 零存儲)+ usage summary(2026-11-04,t3code #6/#8)。"""
import _isolation  # noqa: F401
import os
import sys
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="guest-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["HOME"] = _TMP      # guest secret 落 ~/.pocket,測試隔離
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class TestGuestToken(unittest.TestCase):
    def test_roundtrip_and_tamper(self):
        tok, exp = bridge._guest_token_make("codex:abc", 3600)
        self.assertEqual(bridge._guest_token_verify(tok), "codex:abc")
        self.assertGreater(exp, time.time())
        # 竄改 payload → None
        body, sig = tok.split(".", 1)
        bad = body[:-2] + ("AA" if not body.endswith("AA") else "BB") + "." + sig
        self.assertIsNone(bridge._guest_token_verify(bad))
        self.assertIsNone(bridge._guest_token_verify("garbage"))

    def test_expired_token_rejected(self):
        tok, _ = bridge._guest_token_make("codex:abc", -10)
        self.assertIsNone(bridge._guest_token_verify(tok))

    def test_secret_rotation_kills_all(self):
        tok, _ = bridge._guest_token_make("codex:abc", 3600)
        self.assertIsNotNone(bridge._guest_token_verify(tok))
        bridge._GUEST_SECRET_PATH.unlink()        # 撤銷 = 刪密鑰
        self.assertIsNone(bridge._guest_token_verify(tok))

    def test_html_escapes(self):
        html = bridge._guest_html("s", [
            {"role": "assistant", "body": {"text": "<script>alert(1)</script>"}}])
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


class TestUsageSummary(unittest.TestCase):
    def test_counts_by_provider(self):
        bridge._event_append("codex:u1", "turn", {"state": "begin"})
        bridge._event_append("codex:u1", "turn", {"state": "end"})
        bridge._event_append("codex:u1", "card.milestone", {"card": {}})
        bridge._event_append("xcash", "message.upsert", {"message": {}})
        import asyncio

        class _Req:
            headers = {"authorization": "Bearer test-unit-token"}
            query_params = {}

        out = asyncio.run(bridge.usage_summary(_Req(), window="7d"))
        provs = {p["provider"]: p for p in out["providers"]}
        self.assertEqual(provs["codex"]["turns"], 1)      # begin+end = 1 回合
        self.assertEqual(provs["codex"]["messages"], 1)
        self.assertEqual(provs["hermes"]["messages"], 1)
        self.assertIsNone(out["total"]["tokens_in"], "沒有的數字要誠實 null")


if __name__ == "__main__":
    unittest.main()
