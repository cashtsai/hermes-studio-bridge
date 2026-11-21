"""`POST /app/v1/agents/grok/login/start` + `/status`:Grok device-auth。

AGENT_LOGIN_IN_APP_SPEC 階段 5。龍蝦上 Grok 的遠端登入 —— x.ai device code
flow,不靠 localhost callback(那在遠端機會斷),與 codex 同形。

不能真的 spawn grok,所以用假的 asyncio subprocess 餵「實機實測到的那段
輸出」(grok 1.0.77,`grok login --device-auth`),驗證 bridge:
  · 撈得出 URL + 一次性 code(x.ai 網域、code 夾在 ?user_code= 裡)
  · ANSI 色碼剝得乾淨(未來版本若把 URL 上色也不破)
  · console-remote scope 擋得住(等同機主權限)
  · 旗標關 → 404
  · status 輪詢:pending → done / failed / expired
  · url/code 不進 log
"""
import _isolation  # noqa: F401
import asyncio
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="agent-login-gk-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["POCKET_AGENT_LOGIN_IN_APP"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
bridge.POCKET_AGENT_LOGIN_IN_APP = True

# 實機實測到的 device-auth 輸出(grok 1.0.77,逐位元組照抄,含 ANSI 色碼)。
_REAL_OUTPUT = (
    b"\nTo sign in, open this URL in your browser:\n\n"
    b"  https://accounts.x.ai/oauth2/device?user_code=7SGZ-4NKX\n\n"
    b"Confirm this code in your browser:\n\n"
    b"  7SGZ-4NKX\n\n"
    b"\x1b[90mOnly continue with a code you requested. "
    b"Don't share it with anyone.\x1b[0m\n\n"
    b"Waiting for authorization...")

# 未來版本若把 URL 也上色(防禦性):仍要剝得乾淨、不把 ANSI 收進 URL。
_ANSI_WRAPPED = (
    b"\nTo sign in, open this URL in your browser:\n\n"
    b"  \x1b[94mhttps://accounts.x.ai/oauth2/device?user_code=7SGZ-4NKX\x1b[0m\n\n"
    b"  7SGZ-4NKX\n\nWaiting for authorization...")


class _FakeStdout:
    def __init__(self, data: bytes):
        self._data = data
    async def read(self, n: int) -> bytes:
        if not self._data:
            return b""
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk


class _FakeProc:
    def __init__(self, output: bytes, rc: int = 0):
        self.stdout = _FakeStdout(output)
        self.returncode = None
        self._rc = rc
        self._done = asyncio.Event()
    async def wait(self) -> int:
        await self._done.wait()
        self.returncode = self._rc
        return self._rc
    def finish(self):
        self._done.set()
    def kill(self):
        self.returncode = -9
        self._done.set()


class _Req:
    def __init__(self, token=None):
        tok = token if token is not None else bridge.BRIDGE_TOKEN
        self.headers = {"authorization": f"Bearer {tok}"}
        self.client = type("C", (), {"host": "127.0.0.1"})()
        self.query_params = {}
    async def json(self):
        return {}
    class _URL:
        path = "/app/v1/agents/grok/login/start"
    url = _URL()


def _run(coro):
    return asyncio.run(coro)


class GrokDeviceLoginTest(unittest.TestCase):
    def setUp(self):
        self._fake = _FakeProc(_REAL_OUTPUT)
        self._orig_spawn = bridge.asyncio.create_subprocess_exec
        self._orig_bin = bridge.grok_sdk.grok_bin
        bridge.grok_sdk.grok_bin = lambda: "/usr/bin/true"

        async def fake_spawn(*a, **k):
            return self._fake
        bridge.asyncio.create_subprocess_exec = fake_spawn
        bridge._GROK_LOGIN.clear()

    def tearDown(self):
        bridge.asyncio.create_subprocess_exec = self._orig_spawn
        bridge.grok_sdk.grok_bin = self._orig_bin
        self._fake.finish()

    def test_start_extracts_url_and_code(self):
        async def go():
            r = await bridge.app_grok_login_start(_Req())
            self.assertTrue(r["ok"])
            self.assertEqual(r["provider"], "grok")
            self.assertEqual(r["mode"], "device_code")
            self.assertEqual(
                r["url"],
                "https://accounts.x.ai/oauth2/device?user_code=7SGZ-4NKX")
            self.assertEqual(r["user_code"], "7SGZ-4NKX")
            self.assertTrue(r["login_id"])
        _run(go())

    def test_start_strips_ansi_from_url(self):
        self._fake = _FakeProc(_ANSI_WRAPPED)
        async def go():
            r = await bridge.app_grok_login_start(_Req())
            self.assertEqual(
                r["url"],
                "https://accounts.x.ai/oauth2/device?user_code=7SGZ-4NKX")
            self.assertNotIn("\x1b", r["url"], "ANSI 色碼沒剝乾淨")
            self.assertEqual(r["user_code"], "7SGZ-4NKX")
        _run(go())

    def test_console_remote_scope_forbidden(self):
        from fastapi import HTTPException
        import time as _t
        tok = "pdev-cr"
        with bridge._PAIR_LOCK:
            bridge._DEVICE_TOKENS[tok] = {
                "name": "x", "platform": "web",
                "scope": bridge.CONSOLE_REMOTE_SCOPE,
                "expires": _t.time() + 600, "created": _t.time(),
                "last_seen": _t.time()}
        try:
            async def go():
                await bridge.app_grok_login_start(_Req(token=tok))
            with self.assertRaises(HTTPException) as cm:
                _run(go())
            self.assertEqual(cm.exception.status_code, 403)
        finally:
            with bridge._PAIR_LOCK:
                bridge._DEVICE_TOKENS.pop(tok, None)

    def test_flag_off_is_404(self):
        from fastapi import HTTPException
        bridge.POCKET_AGENT_LOGIN_IN_APP = False
        try:
            async def go():
                await bridge.app_grok_login_start(_Req())
            with self.assertRaises(HTTPException) as cm:
                _run(go())
            self.assertEqual(cm.exception.status_code, 404)
        finally:
            bridge.POCKET_AGENT_LOGIN_IN_APP = True

    def test_missing_binary_is_503(self):
        from fastapi import HTTPException
        bridge.grok_sdk.grok_bin = lambda: ""
        try:
            async def go():
                await bridge.app_grok_login_start(_Req())
            with self.assertRaises(HTTPException) as cm:
                _run(go())
            self.assertEqual(cm.exception.status_code, 503)
        finally:
            bridge.grok_sdk.grok_bin = lambda: "/usr/bin/true"

    def test_status_pending_then_done(self):
        async def go():
            r = await bridge.app_grok_login_start(_Req())
            lid = r["login_id"]
            s1 = await bridge.app_grok_login_status(_Req(), login_id=lid)
            self.assertEqual(s1["state"], "pending")
            self._fake.finish()
            await asyncio.sleep(0.05)   # 讓背景 _wait() 跑完
            s2 = await bridge.app_grok_login_status(_Req(), login_id=lid)
            self.assertEqual(s2["state"], "done")
        _run(go())

    def test_status_failed_on_nonzero_exit(self):
        self._fake = _FakeProc(_REAL_OUTPUT, rc=1)
        async def go():
            r = await bridge.app_grok_login_start(_Req())
            lid = r["login_id"]
            self._fake.finish()
            await asyncio.sleep(0.05)
            s = await bridge.app_grok_login_status(_Req(), login_id=lid)
            self.assertEqual(s["state"], "failed")
        _run(go())

    def test_status_unknown_login_id_is_expired(self):
        async def go():
            s = await bridge.app_grok_login_status(_Req(), login_id="nope")
            self.assertEqual(s["state"], "expired")
        _run(go())

    def test_no_url_or_code_in_log(self):
        seen = []
        orig = bridge._log_event
        bridge._log_event = lambda event=None, **k: seen.append((event, dict(k)))
        try:
            _run(bridge.app_grok_login_start(_Req()))
        finally:
            bridge._log_event = orig
        blob = repr(seen)
        self.assertIn("grok_login_started", blob)
        self.assertNotIn("accounts.x.ai", blob, "URL 不該進 log")
        self.assertNotIn("7SGZ-4NKX", blob, "device code 不該進 log")


if __name__ == "__main__":
    unittest.main()
