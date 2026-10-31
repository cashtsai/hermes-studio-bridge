"""`POST /app/v1/agents/codex/login/start` + `/status`:Codex device-auth。

AGENT_LOGIN_IN_APP_SPEC 階段 2。龍蝦上 Codex 的遠端登入 —— device code flow,
不靠 localhost callback(那在遠端機會斷)。

不能真的 spawn codex,所以用假的 asyncio subprocess 餵「實機實測到的那段
輸出」,驗證 bridge:
  · 撈得出 URL + 一次性 code(ANSI 色碼要剝)
  · console-remote scope 擋得住(等同機主權限)
  · 旗標關 → 404
  · status 輪詢:pending → done
  · url/code 不進 log
"""
import _isolation  # noqa: F401
import asyncio
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="agent-login-cx-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["POCKET_AGENT_LOGIN_IN_APP"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
bridge.POCKET_AGENT_LOGIN_IN_APP = True

# 實機實測到的 device-auth 輸出(含 ANSI 色碼,刻意保留以測剝除)。
_REAL_OUTPUT = (
    b"\nWelcome to Codex [\x1b[90mv0.166.0\x1b[0m]\n"
    b"Follow these steps to sign in with ChatGPT using device code authorization:\n"
    b"1. Open this link in your browser and sign in to your account\n"
    b"   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n"
    b"2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m\n"
    b"   \x1b[94mRIUQ-KPYAN\x1b[0m\n")


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
        path = "/app/v1/agents/codex/login/start"
    url = _URL()


def _run(coro):
    return asyncio.run(coro)


class CodexDeviceLoginTest(unittest.TestCase):
    def setUp(self):
        self._fake = _FakeProc(_REAL_OUTPUT)
        self._orig_spawn = bridge.asyncio.create_subprocess_exec
        self._orig_resolve = bridge._resolve_codex_bin
        bridge._resolve_codex_bin = lambda: "/usr/bin/true"  # os.path.exists=True

        async def fake_spawn(*a, **k):
            return self._fake
        bridge.asyncio.create_subprocess_exec = fake_spawn
        bridge._CODEX_LOGIN.clear()

    def tearDown(self):
        bridge.asyncio.create_subprocess_exec = self._orig_spawn
        bridge._resolve_codex_bin = self._orig_resolve
        self._fake.finish()

    def test_start_extracts_url_and_code_stripping_ansi(self):
        async def go():
            r = await bridge.app_codex_login_start(_Req())
            self.assertTrue(r["ok"])
            self.assertEqual(r["url"], "https://auth.openai.com/codex/device")
            self.assertEqual(r["user_code"], "RIUQ-KPYAN")
            self.assertNotIn("\x1b", r["url"], "ANSI 色碼沒剝乾淨")
            self.assertTrue(r["login_id"])
            return r
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
                await bridge.app_codex_login_start(_Req(token=tok))
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
                await bridge.app_codex_login_start(_Req())
            with self.assertRaises(HTTPException) as cm:
                _run(go())
            self.assertEqual(cm.exception.status_code, 404)
        finally:
            bridge.POCKET_AGENT_LOGIN_IN_APP = True

    def test_status_pending_then_done(self):
        async def go():
            r = await bridge.app_codex_login_start(_Req())
            lid = r["login_id"]
            # 還在等授權 → pending
            s1 = await bridge.app_codex_login_status(_Req(), login_id=lid)
            self.assertEqual(s1["state"], "pending")
            # 授權完成(行程退出 rc=0)
            self._fake.finish()
            await asyncio.sleep(0.05)   # 讓背景 _wait() 跑完
            s2 = await bridge.app_codex_login_status(_Req(), login_id=lid)
            self.assertEqual(s2["state"], "done")
        _run(go())

    def test_status_unknown_login_id_is_expired(self):
        async def go():
            s = await bridge.app_codex_login_status(_Req(), login_id="nope")
            self.assertEqual(s["state"], "expired")
        _run(go())

    def test_no_url_in_log(self):
        seen = []
        orig = bridge._log_event
        bridge._log_event = lambda event=None, **k: seen.append((event, dict(k)))
        try:
            _run(bridge.app_codex_login_start(_Req()))
        finally:
            bridge._log_event = orig
        blob = repr(seen)
        self.assertIn("codex_login_started", blob)
        self.assertNotIn("auth.openai.com", blob, "URL 不該進 log")
        self.assertNotIn("RIUQ-KPYAN", blob, "device code 不該進 log")


if __name__ == "__main__":
    unittest.main()
