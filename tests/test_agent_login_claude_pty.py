"""`POST /app/v1/agents/claude/login/start|finish`:CC OAuth PKCE 直連。

2026-10-31 改寫:原本靠刮 `claude setup-token` 的 TUI 撈 token —— 實機證明那
條路被 Ink 重繪打散、撈到的 token API 一律 401(試了 ANSI strip / pyte / OSC52
全不行)。改成 bridge **自己**做標準 OAuth PKCE:產 verifier/challenge、組授權
URL、拿 code 直接 POST token 端點換 token(乾淨 JSON)。這些測試釘那條路。

httpx 在 finish 裡 lazy import,所以 monkeypatch `httpx.AsyncClient`。
"""
import _isolation  # noqa: F401
import asyncio
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="agent-login-cc-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["POCKET_AGENT_LOGIN_IN_APP"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
bridge.POCKET_AGENT_LOGIN_IN_APP = True
# start 會檢查 CLAUDE_BIN 存在;單元測試用一個一定存在的路徑頂著。
bridge.CLAUDE_BIN = sys.executable


class _Req:
    def __init__(self, body=None, token=None):
        tok = token if token is not None else bridge.BRIDGE_TOKEN
        self.headers = {"authorization": f"Bearer {tok}"}
        self.client = type("C", (), {"host": "127.0.0.1"})()
        self.query_params = {}
        self._body = body or {}
    async def json(self):
        return self._body
    class _URL:
        path = "/app/v1/agents/claude/login/finish"
    url = _URL()


def _run(coro):
    return asyncio.run(coro)


class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
    def json(self):
        return self._payload


class _FakeClient:
    """attr 可設 captured(dict)收下 post 的參數;resp 決定回什麼。"""
    resp = _FakeResp(200, {"access_token": "sk-ant-oat01-REALtokenABCDEF"})
    captured = {}
    def __init__(self, *a, **k):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def post(self, url, headers=None, content=None):
        _FakeClient.captured = {"url": url, "headers": headers, "content": content}
        return _FakeClient.resp


def _patch_httpx():
    import httpx
    httpx.AsyncClient = _FakeClient


class ClaudeOAuthLoginTest(unittest.TestCase):
    def setUp(self):
        bridge._CC_LOGIN.clear()
        _patch_httpx()
        _FakeClient.resp = _FakeResp(
            200, {"access_token": "sk-ant-oat01-REALtokenABCDEF"})
        _FakeClient.captured = {}
        self._cfgdir = tempfile.mkdtemp(prefix="cc-cfg-")
        os.environ["CLAUDE_CONFIG_DIR"] = self._cfgdir
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        self._orig_logged = bridge._agent_claude_logged_in
        async def _yes(): return True
        bridge._agent_claude_logged_in = _yes

    def tearDown(self):
        bridge._agent_claude_logged_in = self._orig_logged
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

    def test_start_returns_pkce_authorize_url(self):
        r = _run(bridge.app_claude_login_start(_Req()))
        self.assertTrue(r["ok"])
        self.assertTrue(r["login_id"])
        self.assertIn("claude.com/cai/oauth/authorize", r["url"])
        self.assertIn("code_challenge=", r["url"])
        self.assertIn("code_challenge_method=S256", r["url"])
        self.assertIn("state=", r["url"])
        self.assertIn(bridge._CC_OAUTH_CLIENT_ID, r["url"])
        # verifier/state 留在記憶體,url 不帶 verifier
        self.assertNotIn("code_verifier", r["url"])
        e = bridge._CC_LOGIN[r["login_id"]]
        self.assertTrue(e["verifier"] and e["state"])

    def test_finish_exchanges_code_and_persists_token(self):
        import json as _json
        r = _run(bridge.app_claude_login_start(_Req()))
        st = bridge._CC_LOGIN[r["login_id"]]["state"]
        fin = _run(bridge.app_claude_login_finish(
            _Req({"login_id": r["login_id"], "code": f"THECODE#{st}"})))
        self.assertTrue(fin["logged_in"])
        # 交換參數對:code 去掉 #state、帶 verifier、form-urlencoded
        body = _FakeClient.captured["content"]
        self.assertIn("grant_type=authorization_code", body)
        self.assertIn("code=THECODE", body)
        self.assertNotIn("THECODE%23", body)   # # 後面沒被當成 code 一部分
        self.assertIn("code_verifier=", body)
        self.assertIn("application/x-www-form-urlencoded",
                      _FakeClient.captured["headers"]["Content-Type"])
        # token 寫進 settings.json 的 env
        with open(os.path.join(self._cfgdir, "settings.json"), encoding="utf-8") as f:
            saved = _json.load(f)
        self.assertEqual(saved["env"]["CLAUDE_CODE_OAUTH_TOKEN"],
                         "sk-ant-oat01-REALtokenABCDEF")
        self.assertEqual(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
                         "sk-ant-oat01-REALtokenABCDEF")
        # login_id 單次:換過就沒了
        self.assertNotIn(r["login_id"], bridge._CC_LOGIN)

    def test_finish_state_mismatch_is_400(self):
        from fastapi import HTTPException
        r = _run(bridge.app_claude_login_start(_Req()))
        with self.assertRaises(HTTPException) as cm:
            _run(bridge.app_claude_login_finish(
                _Req({"login_id": r["login_id"], "code": "THECODE#WRONGSTATE"})))
        self.assertEqual(cm.exception.status_code, 400)

    def test_finish_exchange_non_200_is_502(self):
        from fastapi import HTTPException
        _FakeClient.resp = _FakeResp(400, {"error": "invalid_grant"})
        r = _run(bridge.app_claude_login_start(_Req()))
        st = bridge._CC_LOGIN[r["login_id"]]["state"]
        with self.assertRaises(HTTPException) as cm:
            _run(bridge.app_claude_login_finish(
                _Req({"login_id": r["login_id"], "code": f"THECODE#{st}"})))
        self.assertEqual(cm.exception.status_code, 502)

    def test_finish_no_access_token_is_502(self):
        from fastapi import HTTPException
        _FakeClient.resp = _FakeResp(200, {"token_type": "Bearer"})  # 沒 access_token
        r = _run(bridge.app_claude_login_start(_Req()))
        st = bridge._CC_LOGIN[r["login_id"]]["state"]
        with self.assertRaises(HTTPException) as cm:
            _run(bridge.app_claude_login_finish(
                _Req({"login_id": r["login_id"], "code": f"THECODE#{st}"})))
        self.assertEqual(cm.exception.status_code, 502)

    def test_finish_unknown_login_id_is_410(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            _run(bridge.app_claude_login_finish(
                _Req({"login_id": "nope", "code": "x#y"})))
        self.assertEqual(cm.exception.status_code, 410)

    def test_console_remote_scope_forbidden(self):
        from fastapi import HTTPException
        import time as _t
        tok = "pdev-cr-cc"
        with bridge._PAIR_LOCK:
            bridge._DEVICE_TOKENS[tok] = {
                "name": "x", "platform": "web",
                "scope": bridge.CONSOLE_REMOTE_SCOPE,
                "expires": _t.time() + 600, "created": _t.time(),
                "last_seen": _t.time()}
        try:
            with self.assertRaises(HTTPException) as cm:
                _run(bridge.app_claude_login_start(_Req(token=tok)))
            self.assertEqual(cm.exception.status_code, 403)
        finally:
            with bridge._PAIR_LOCK:
                bridge._DEVICE_TOKENS.pop(tok, None)

    def test_flag_off_is_404(self):
        from fastapi import HTTPException
        bridge.POCKET_AGENT_LOGIN_IN_APP = False
        try:
            with self.assertRaises(HTTPException) as cm:
                _run(bridge.app_claude_login_start(_Req()))
            self.assertEqual(cm.exception.status_code, 404)
        finally:
            bridge.POCKET_AGENT_LOGIN_IN_APP = True

    def test_token_and_state_not_logged(self):
        seen = []
        orig = bridge._log_event
        bridge._log_event = lambda event=None, **k: seen.append((event, dict(k)))
        try:
            r = _run(bridge.app_claude_login_start(_Req()))
            st = bridge._CC_LOGIN[r["login_id"]]["state"]
            _run(bridge.app_claude_login_finish(
                _Req({"login_id": r["login_id"], "code": f"THECODE#{st}"})))
        finally:
            bridge._log_event = orig
        blob = repr(seen)
        self.assertIn("claude_login_started", blob)
        self.assertIn("claude_login_finished", blob)
        self.assertNotIn("sk-ant-oat01", blob, "token 不該進 log")
        self.assertNotIn("THECODE", blob, "code 不該進 log")


if __name__ == "__main__":
    unittest.main()
