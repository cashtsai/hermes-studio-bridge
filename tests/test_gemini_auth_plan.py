"""Gemini 認證:訂閱(Google 登入)優先,API key 退路(2026-11-01 機主拍板)。

「我想要讓 gemini 一樣給 cc cx 的訂閱仔一樣,哪有那麼多人有 API key。」

事實基礎(實測自 CLI 0.63.0 的 ACP authMethods,不是猜的):
  oauth-personal(Log in with Google)/ gemini-api-key / vertex-ai / gateway。
Google 登入馬上有免費額度,AI Pro/Ultra 訂閱的額度也掛在這;
API key 走的是 AI Studio 計費,跟訂閱無關 —— 原本 bridge 寫死
`gemini-api-key`,等於訂閱用戶被迫去生一把 key 燒錢。

釘住的契約:
  1. auto(預設):有 Google 登入 → OAuth 在前、key 在後;都沒有 → 空計畫。
  2. 強制模式(oauth / api-key)只走指定那條 —— 而且缺料就誠實回空,
     不偷偷退路(除錯時要的就是「只試這條」)。
  3. configured() 認得「只有 Google 登入」的人 —— 原本只認 key,
     已登入的使用者會看到「未配置」。
  4. 壞掉的 auth 值不寫入(save_config 守門)—— 錯字會讓計畫變空,
     整個 provider 安靜變成未配置。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gemini_provider as gp  # noqa: E402


def _cfg(auth="auto", key=""):
    return {"auth": auth, "api_key": key}


class AuthPlanTest(unittest.TestCase):

    # ── auto:訂閱優先 ──────────────────────────────────────────

    def test_auto_prefers_oauth_over_key(self):
        """兩者都有 → OAuth 在前(訂閱/免費額度),key 是退路。"""
        self.assertEqual(gp.auth_plan(_cfg(key="k"), oauth=True),
                         ["oauth-personal", "gemini-api-key"])

    def test_auto_oauth_only(self):
        """只登入了 Google、沒有 key —— 機主說的大多數人。要能用。"""
        self.assertEqual(gp.auth_plan(_cfg(), oauth=True), ["oauth-personal"])

    def test_auto_key_only_still_works(self):
        """只有 key 的既有使用者照舊 —— 一個都不踢。"""
        self.assertEqual(gp.auth_plan(_cfg(key="k"), oauth=False),
                         ["gemini-api-key"])

    def test_auto_nothing_is_empty(self):
        self.assertEqual(gp.auth_plan(_cfg(), oauth=False), [])

    # ── 強制模式:只走指定那條,缺料誠實回空 ──────────────────

    def test_forced_oauth_without_cache_is_empty_not_fallback(self):
        """強制 oauth 但沒登入 → 空,**不准**偷偷退回 key。
        除錯時要的就是「只試這條」;偷退路會把問題蓋起來。"""
        self.assertEqual(gp.auth_plan(_cfg(auth="oauth", key="k"), oauth=False), [])

    def test_forced_api_key_ignores_oauth(self):
        self.assertEqual(gp.auth_plan(_cfg(auth="api-key", key="k"), oauth=True),
                         ["gemini-api-key"])

    def test_forced_api_key_without_key_is_empty(self):
        self.assertEqual(gp.auth_plan(_cfg(auth="api-key"), oauth=True), [])

    # ── configured():只有 Google 登入也算配置好 ──────────────

    def test_configured_recognizes_oauth_only(self):
        with patch.object(gp, "load_config", return_value=_cfg()), \
             patch.object(gp, "oauth_available", return_value=True):
            self.assertTrue(gp.GEMINI.configured() if hasattr(gp, "GEMINI")
                            else bool(gp.auth_plan(gp.load_config(),
                                                   gp.oauth_available())))

    # ── save_config 的 auth 守門 ──────────────────────────────

    def test_save_config_rejects_bad_auth_value(self):
        d = tempfile.mkdtemp(prefix="gm-auth-")
        with patch.object(gp, "_CONFIG_FILE", os.path.join(d, "gemini.json")):
            gp.save_config(auth="oauht")          # 錯字
            self.assertEqual(gp.load_config()["auth"], "auto",
                             "壞值不得落檔 —— 會讓計畫變空、provider 安靜失聯")
            gp.save_config(auth="oauth")
            self.assertEqual(gp.load_config()["auth"], "oauth")

    def test_default_auth_is_auto(self):
        d = tempfile.mkdtemp(prefix="gm-auth2-")
        with patch.object(gp, "_CONFIG_FILE", os.path.join(d, "gemini.json")):
            self.assertEqual(gp.load_config()["auth"], "auto")


class BackendSwitchTest(unittest.TestCase):
    """ACP 後端切換(gemini→antigravity)—— gemini CLI 個人 OAuth 被 Google 封,
    Antigravity 接手免費/訂閱。結構接縫:預設不變,env 切後端。"""

    def test_default_backend_is_gemini(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("POCKET_GEMINI_BACKEND", None)
            self.assertEqual(gp.acp_backend(), "gemini")

    def test_unknown_backend_falls_back_to_gemini(self):
        """亂填的後端名不得讓 provider 用一個不存在的表 → 退回 gemini。"""
        with patch.dict(os.environ, {"POCKET_GEMINI_BACKEND": "typo"}):
            self.assertEqual(gp.acp_backend(), "gemini")

    def test_switch_to_antigravity(self):
        with patch.dict(os.environ, {"POCKET_GEMINI_BACKEND": "antigravity"}):
            self.assertEqual(gp.acp_backend(), "antigravity")

    def test_auth_plan_uses_backend_specific_ids(self):
        """切後端後,auth_plan 回的 methodId 要是**那個後端的**字串。
        寫死 gemini 的 'oauth-personal' 會讓 antigravity 握手拿錯 id。"""
        with patch.dict(os.environ, {"POCKET_GEMINI_BACKEND": "antigravity"}):
            plan = gp.auth_plan(_cfg(key="k"), oauth=True)
            # 目前是 PLACEHOLDER(待 SG 龍蝦實測填),但一定**不是** gemini 的字串。
            self.assertNotIn("oauth-personal", plan)
            self.assertNotIn("gemini-api-key", plan)
            self.assertEqual(len(plan), 2, "oauth + key 兩條都在,只是 id 換了")

    def test_gemini_backend_ids_unchanged(self):
        """預設後端的 id 一字不變 —— 現狀零回歸。"""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("POCKET_GEMINI_BACKEND", None)
            self.assertEqual(gp.auth_plan(_cfg(key="k"), oauth=True),
                             ["oauth-personal", "gemini-api-key"])


if __name__ == "__main__":
    unittest.main()
