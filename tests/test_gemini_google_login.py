"""Gemini「用 Google 登入」(Antigravity/agy,PTY 代理)。

前提:gemini CLI 個人 OAuth 已死;SG 快照那版自組 PKCE 寫 oauth_creds.json
的實作前提作廢。本版=bridge 當 agy 的 TTY 代理人(同 CC setup-token 模式)。

釘住的契約:
  1. URL regex 吃得下 agy 1.4.12 的**真實輸出**(2026-11-02 本機錄的,
     不是手寫樣本)—— URL 是乾淨單行,regex 不能貪到後面的字。
  2. 旗標關 → 404(預設不改變現行行為);finish 沒 start → 410。
  3. 已登入的機器 start 回 mode="already"(app 顯示成功,不逼人重走)。
  4. finish 的誠實判定:**實探**才回 ok(exit 0 ≠ 登入,CC 10-31 教訓)。
"""
import _isolation  # noqa: F401
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("BRIDGE_TOKEN", "test-token")
import bridge  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# 不能寫死 token 值:整包測試同進程共用一個 bridge module,誰先 import 誰的
# setdefault 生效(2026-11-03 與 test_agent_login_claude_pty 互踩實錄)。
AUTH = {"Authorization": f"Bearer {bridge.BRIDGE_TOKEN}"}

# agy 1.4.12 真實輸出(2026-11-02 Mac 實錄,僅截 URL 行前後)。
REAL_AGY_OUTPUT = (
    "Authentication required. Please visit the URL to log in:\n"
    "  https://accounts.google.com/o/oauth2/auth?access_type=offline&"
    "client_id=1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps."
    "googleusercontent.com&code_challenge=twcymCzpZjmI6OtrBa5PFP5a7J_fJSz3"
    "UGvamzQ_0jY&code_challenge_method=S256&prompt=consent&redirect_uri="
    "https%3A%2F%2Fantigravity.google%2Foauth-callback&response_type=code&"
    "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcloud-platform+openid&"
    "state=fTkz3tB6Lf4I4gKb74oAhw\n"
    "\n"
    "Waiting for authentication (timeout 60s)...\n"
    "Or, paste the authorization code here and press Enter:\n")


class UrlRegexTest(unittest.TestCase):

    def test_extracts_clean_url_from_real_output(self):
        m = bridge._AGY_AUTH_URL_RE.search(REAL_AGY_OUTPUT)
        self.assertIsNotNone(m)
        url = m.group(0)
        self.assertTrue(url.startswith("https://accounts.google.com/o/oauth2/auth?"))
        self.assertTrue(url.endswith("state=fTkz3tB6Lf4I4gKb74oAhw"),
                        f"URL 尾巴貪到別的字了:{url[-60:]}")
        self.assertNotIn("\n", url)
        self.assertNotIn("Waiting", url)

    def test_ansi_colored_output_still_matches(self):
        colored = REAL_AGY_OUTPUT.replace("https://",
                                          "\x1b[36mhttps://").replace(
            "AhwN", "Ahw\x1b[0m")
        text = __import__("re").sub(r"\x1b\[[0-9;]*m", "", colored)
        self.assertIsNotNone(bridge._AGY_AUTH_URL_RE.search(text))


class EndpointGuardTest(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(bridge.app)
        self._flag = bridge.POCKET_AGENT_LOGIN_IN_APP

    def tearDown(self):
        bridge.POCKET_AGENT_LOGIN_IN_APP = self._flag

    def test_flag_off_is_404(self):
        bridge.POCKET_AGENT_LOGIN_IN_APP = False
        r = self.client.post("/app/v1/agents/gemini/login/start", headers=AUTH)
        self.assertEqual(r.status_code, 404)

    def test_finish_without_start_is_410(self):
        bridge.POCKET_AGENT_LOGIN_IN_APP = True
        r = self.client.post("/app/v1/agents/gemini/login/finish",
                             headers=AUTH,
                             json={"login_id": "nope", "code": "4/0xyz"})
        self.assertEqual(r.status_code, 410)

    def test_finish_without_code_is_400(self):
        bridge.POCKET_AGENT_LOGIN_IN_APP = True
        r = self.client.post("/app/v1/agents/gemini/login/finish",
                             headers=AUTH, json={"login_id": "x", "code": ""})
        self.assertEqual(r.status_code, 400)

    def test_start_without_agy_is_503(self):
        bridge.POCKET_AGENT_LOGIN_IN_APP = True
        orig = bridge.gemini_provider.agy_bin
        bridge.gemini_provider.agy_bin = lambda: ""
        try:
            r = self.client.post("/app/v1/agents/gemini/login/start",
                                 headers=AUTH)
        finally:
            bridge.gemini_provider.agy_bin = orig
        self.assertEqual(r.status_code, 503)


class LoginHomeOverrideTest(unittest.TestCase):
    """測試鉤:POCKET_AGY_LOGIN_HOME 指到 scratch HOME → 端到端驗登入流程
    不碰機器上真的登入狀態。"""

    def test_override_changes_home_only_when_set(self):
        base = bridge._agy_login_env()
        self.assertEqual(base.get("HOME"), os.environ.get("HOME"))
        os.environ["POCKET_AGY_LOGIN_HOME"] = "/tmp/agy-scratch"
        try:
            self.assertEqual(bridge._agy_login_env()["HOME"],
                             "/tmp/agy-scratch")
        finally:
            os.environ.pop("POCKET_AGY_LOGIN_HOME", None)


if __name__ == "__main__":
    unittest.main()
