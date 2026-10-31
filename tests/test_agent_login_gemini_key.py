"""`PUT /app/v1/agents/gemini/key`:從 app 頁籤設 Gemini API key。

AGENT_LOGIN_IN_APP_SPEC 階段 1。龍蝦上三個 agent 原本都要使用者自己 ssh 進去
設定;Gemini 最簡單(API key、非 OAuth),先打通「頁籤內設定 → bridge 落檔 →
gate 解除」這條管線。

這一組釘的是**安全與行為邊界**,不是 happy path:

  · 旗標關 → 404(預設行為不變)
  · **console-remote scope 不准碰**(設 key 等同綁帳號 = 機主級動作,
    不下放給遠端瀏覽器)
  · key 空 → 400,不寫空設定
  · **key 絕不進 log**(只記「設了」這個事實)
"""
import _isolation  # noqa: F401
import asyncio
import os
import sys
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="agent-login-gm-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["GEMINI_CONFIG_FILE"] = os.path.join(_TMP, "gemini.json")
os.environ.pop("GEMINI_API_KEY", None)
os.environ["POCKET_AGENT_LOGIN_IN_APP"] = "1"   # 旗標:這一檔測開啟後的行為
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
import gemini_provider  # noqa: E402

# 旗標是模組常數,import 當下就定了。測試側直接綁回 True(與其他測試檔同招)。
bridge.POCKET_AGENT_LOGIN_IN_APP = True

_SECRET_KEY = "AIzaSy-THIS-IS-A-FAKE-TEST-KEY-do-not-log-0001"


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
        path = "/app/v1/agents/gemini/key"
    url = _URL()


def _call(body=None, token=None):
    # Python 3.14:get_event_loop() 不再自動建 loop,用 asyncio.run()。
    return asyncio.run(bridge.app_gemini_set_key(_Req(body, token)))


class GeminiKeyEndpointTest(unittest.TestCase):
    def setUp(self):
        try:
            os.remove(os.environ["GEMINI_CONFIG_FILE"])
        except FileNotFoundError:
            pass

    def test_sets_key_and_reports_configured(self):
        self.assertFalse(gemini_provider.GeminiClient(log=lambda **k: None).configured(),
                         "前置:一開始不該是 configured")
        r = _call({"api_key": _SECRET_KEY})
        self.assertTrue(r["ok"])
        self.assertTrue(r["configured"], "寫完 key 應該立刻 configured")
        self.assertEqual(gemini_provider.load_config()["api_key"], _SECRET_KEY)

    def test_config_file_is_0600(self):
        _call({"api_key": _SECRET_KEY})
        mode = os.stat(os.environ["GEMINI_CONFIG_FILE"]).st_mode & 0o777
        self.assertEqual(mode, 0o600, "憑證檔必須 0600")

    def test_empty_key_rejected(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            _call({"api_key": "   "})
        self.assertEqual(cm.exception.status_code, 400)
        self.assertFalse(os.path.exists(os.environ["GEMINI_CONFIG_FILE"]),
                         "空 key 不可以寫出任何設定檔")

    def test_console_remote_scope_forbidden(self):
        """遠端控制台不准設帳號憑證 —— 等同機主權限,不下放給遠端瀏覽器。"""
        from fastapi import HTTPException
        tok = "pdev-console-remote-x"
        with bridge._PAIR_LOCK:
            bridge._DEVICE_TOKENS[tok] = {
                "name": "borrowed", "platform": "web",
                "scope": bridge.CONSOLE_REMOTE_SCOPE,
                "expires": time.time() + 600, "created": time.time(),
                "last_seen": time.time()}
        try:
            with self.assertRaises(HTTPException) as cm:
                _call({"api_key": _SECRET_KEY}, token=tok)
            self.assertEqual(cm.exception.status_code, 403)
            self.assertFalse(os.path.exists(os.environ["GEMINI_CONFIG_FILE"]),
                             "被 scope 擋下就不該寫出設定")
        finally:
            with bridge._PAIR_LOCK:
                bridge._DEVICE_TOKENS.pop(tok, None)

    def test_flag_off_is_404(self):
        from fastapi import HTTPException
        bridge.POCKET_AGENT_LOGIN_IN_APP = False
        try:
            with self.assertRaises(HTTPException) as cm:
                _call({"api_key": _SECRET_KEY})
            self.assertEqual(cm.exception.status_code, 404)
        finally:
            bridge.POCKET_AGENT_LOGIN_IN_APP = True

    def test_key_never_logged(self):
        """key 絕不進 log —— 連長度/前綴都不記,只記『設了』這個事實。"""
        seen = []
        orig = bridge._log_event
        bridge._log_event = lambda event=None, **k: seen.append((event, dict(k)))
        try:
            _call({"api_key": _SECRET_KEY})
        finally:
            bridge._log_event = orig
        blob = repr(seen)
        self.assertIn("gemini_key_set", blob)
        self.assertNotIn(_SECRET_KEY, blob, "log 裡出現了 key 本身!")
        self.assertNotIn(_SECRET_KEY[:8], blob, "連 key 前綴都不該記")


if __name__ == "__main__":
    unittest.main()
