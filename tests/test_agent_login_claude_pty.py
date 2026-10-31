"""`POST /app/v1/agents/claude/login/start|finish`:CC setup-token via PTY。

AGENT_LOGIN_IN_APP_SPEC 階段 3,三個裡最麻煩的。`claude setup-token` 是
full-screen TUI:需要偽 TTY、URL 包在 OSC 8 超連結裡、授權完要把 code 貼回。

測試不能 monkeypatch(重點就是真的 PTY 路徑)。所以用一個**假 claude**:
一個會印出「實機實測到的那段 OSC 8 OAuth URL」、開 bracketed-paste、等 code、
收到就 exit 0 的 shell 腳本。CLAUDE_BIN 指向它,走完整的 pty.fork → 撈 URL →
寫 code → 等退出。

釘:
  · OSC 8 邊界撈得出乾淨 URL(不含折行殘留)
  · console-remote scope 擋得住
  · 旗標關 → 404
  · finish 把 code 寫進 PTY、行程 exit 0 → logged_in
  · url 不進 log
"""
import _isolation  # noqa: F401
import asyncio
import os
import stat
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="agent-login-cc-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["POCKET_AGENT_LOGIN_IN_APP"] = "1"

# 假 claude:印 OSC 8 包的 OAuth URL(照實機格式),開 bracketed paste,
# 等一行輸入;收到就 exit 0。URL 的 state 故意放長,驗證 OSC 8 邊界切得準
# (純文字 regex 會被 state 後的字元黏住,OSC 8 不會)。
_FAKE_URL = ("https://claude.com/cai/oauth/authorize?code=true"
             "&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e&response_type=code"
             "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
             "&scope=user%3Ainference&code_challenge=XnsRvcO9RRE9t17_IBjdkWFXq8cQuGlG56DMJjBs2_0"
             "&code_challenge_method=S256&state=IoQr0yG5kfNBOn4QIA6kj8T97ZdQkgCJaBg42mhZcYw")
_FAKE_CLAUDE = os.path.join(_TMP, "fake-claude")
with open(_FAKE_CLAUDE, "w") as f:
    f.write(
        "#!/usr/bin/env bash\n"
        "printf 'Claude subscription required.\\n'\n"
        "printf '\\033[?2004h'\n"                       # 開 bracketed paste
        # OSC 8: ESC ] 8 ; id=x ; URI ESC \  TEXT ESC ] 8 ; ; ESC \
        "printf '\\033]8;id=t;%s\\033\\\\click\\033]8;;\\033\\\\\\n' '" + _FAKE_URL + "'\n"
        "read -r line\n"                                # 等 code 貼回
        "exit 0\n")
os.chmod(_FAKE_CLAUDE, os.stat(_FAKE_CLAUDE).st_mode | stat.S_IEXEC)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
bridge.POCKET_AGENT_LOGIN_IN_APP = True
bridge.CLAUDE_BIN = _FAKE_CLAUDE


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
        path = "/app/v1/agents/claude/login/start"
    url = _URL()


def _run(coro):
    return asyncio.run(coro)


class ClaudeLoginPtyTest(unittest.TestCase):
    def setUp(self):
        bridge._CC_LOGIN.clear()

    def test_start_extracts_clean_url_via_osc8(self):
        r = _run(bridge.app_claude_login_start(_Req()))
        self.assertTrue(r["ok"])
        self.assertEqual(r["url"], _FAKE_URL, "OSC 8 邊界沒撈到乾淨 URL")
        self.assertNotIn("\x1b", r["url"])
        self.assertTrue(r["login_id"])
        # 收掉背景行程
        e = bridge._CC_LOGIN.get(r["login_id"])
        if e:
            bridge._cc_login_kill(e)

    def test_finish_verifies_real_login_not_exit_code(self):
        """finish **不信 exit code**,實際探 claude 登入狀態才算數。

        2026-10-31 實機:行程 exit 0、端點回 logged_in:true,但 `claude -p`
        仍 Not logged in —— 假成功。所以 finish 現在會呼 _agent_claude_logged_in()
        實際驗證。這裡 monkeypatch 它,測兩個方向。
        """
        import bridge as _b
        orig = _b._agent_claude_logged_in

        async def _yes(): return True
        _b._agent_claude_logged_in = _yes
        try:
            r = _run(bridge.app_claude_login_start(_Req()))
            fin = _run(bridge.app_claude_login_finish(
                _Req({"login_id": r["login_id"], "code": "test-code"})))
            self.assertTrue(fin["logged_in"])
        finally:
            _b._agent_claude_logged_in = orig

    def test_finish_raises_when_not_really_logged_in(self):
        """行程 exit 0 但實際沒登入 → 502,**不假成功**。"""
        from fastapi import HTTPException
        import bridge as _b
        orig = _b._agent_claude_logged_in

        async def _no(): return False
        _b._agent_claude_logged_in = _no
        try:
            r = _run(bridge.app_claude_login_start(_Req()))
            with self.assertRaises(HTTPException) as cm:
                _run(bridge.app_claude_login_finish(
                    _Req({"login_id": r["login_id"], "code": "test-code"})))
            self.assertEqual(cm.exception.status_code, 502)
        finally:
            _b._agent_claude_logged_in = orig

    def test_finish_strips_state_fragment_from_code(self):
        """貼成 `code#state` 時要切掉 `#` 後面 —— OAuth code 不含 fragment。"""
        import bridge as _b
        captured = {}
        orig_login = _b._agent_claude_logged_in
        async def _yes(): return True
        _b._agent_claude_logged_in = _yes
        # 攔截寫進 PTY 的 code
        orig_write = os.write
        def _spy_write(fd, data):
            if b"\x1b[200~" in data:
                captured["code"] = data
            return orig_write(fd, data)
        try:
            r = _run(bridge.app_claude_login_start(_Req()))
            os.write = _spy_write
            _run(bridge.app_claude_login_finish(
                _Req({"login_id": r["login_id"], "code": "REALCODE#statejunk"})))
            self.assertIn(b"REALCODE", captured.get("code", b""))
            self.assertNotIn(b"statejunk", captured.get("code", b""),
                             "# 後面的 state 碎片沒切掉")
        finally:
            os.write = orig_write
            _b._agent_claude_logged_in = orig_login

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

    def test_finish_unknown_login_id_is_410(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            _run(bridge.app_claude_login_finish(
                _Req({"login_id": "nope", "code": "x"})))
        self.assertEqual(cm.exception.status_code, 410)

    def test_url_not_logged(self):
        seen = []
        orig = bridge._log_event
        bridge._log_event = lambda event=None, **k: seen.append((event, dict(k)))
        try:
            r = _run(bridge.app_claude_login_start(_Req()))
        finally:
            bridge._log_event = orig
        blob = repr(seen)
        self.assertIn("claude_login_started", blob)
        self.assertNotIn("oauth/authorize", blob, "URL 不該進 log")
        e = bridge._CC_LOGIN.get(r["login_id"])
        if e:
            bridge._cc_login_kill(e)


if __name__ == "__main__":
    unittest.main()
