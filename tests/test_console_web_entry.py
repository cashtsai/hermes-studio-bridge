"""/console 的 web-first 開門(2026-11-04,feat/console-webfirst)。

讓桌面控制台能從**本機自己的中繼 vhost**(付費 relay 的 https 網域)用任意
瀏覽器打開。放行的只有三層門禁中「來源」那一層,而且窄到不能再窄 ——
旗標 `POCKET_CONSOLE_WEB`(預設 0)+ Host 必須等於 relay grant 的 hostname。
其餘兩層(`POCKET_CONSOLE_ENABLED`、boot 碼/cookie 真閘)一毫米不動。

本檔守的安全性質:

  1. **旗標關 = 預設 = 現狀**:中繼 vhost 來的請求照樣裸 403。
  2. **旗標開也只認本機中繼 vhost**:別的 Host、relay 沒開 → 照樣 403。
  3. **放行之後等著的是 401 輸入頁**,不是頁面本體 —— 真閘還在。
  4. **失敗率限照咬**(6 次/60 秒,超額連比都不比),而且公網面的
     `GET /console?boot=` 猜測也進同一張退避表(handoff 坑 #6:每個
     `_pair_boot_ok` 呼叫點都要有退避,否則公網多一條無限猜的 oracle)。
  5. **一次性碼語意不鬆動**:短碼用過即廢,replay 吃錯誤頁。
  6. **公網面的 cookie 全是 session cookie**(POST 簽發與 GET 續命都是),
     且走 https 中繼(X-Forwarded-Proto)時必帶 `Secure`;本機 http 不帶
     (硬加會讓瀏覽器丟棄 cookie,PWA 直接壞)。
  7. **本機直連路徑一字不變**(14 天滑動視窗、輸入頁文案照舊)。
  8. **`/pair/qr*` 的 local-only 不受旗標影響** —— web-first 不碰
     `_pair_local_only` 本身。
  9. **續命失敗只影響該請求的續命**,頁面照送(t3code 憑證續期不斷線原則)。
 10. 公網面的 auth 失敗記進 `_log_event`,帶得到訪客 IP(XFF 最左)。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線)
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="console-web-entry-")
os.environ["PAIR_BOOT_CODE_FILE"] = os.path.join(_TMP, "pair-boot-code")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["POCKET_CONSOLE_ENABLED"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# 同 test_console_login_page.py:`_PAIR_BOOT_CODE_FILE` 是模組層常數,全套一起
# 跑時 bridge 早被別的檔 import 過 → 上面的 env 變 no-op。綁回 tmp、清快取。
bridge._PAIR_BOOT_CODE_FILE = os.environ["PAIR_BOOT_CODE_FILE"]
with bridge._PAIR_BOOT_CODE_LOCK:
    bridge._PAIR_BOOT_CODE_CACHE["code"] = ""

# ── 中繼 vhost 夾具:enabled 旗標檔 + grant.json(hostname)────────────────
_VHOST = "u9test.sg.relay.pocketone.app"
_RELAY_DIR = os.path.join(_TMP, "relay")
os.makedirs(_RELAY_DIR, exist_ok=True)
open(os.path.join(_RELAY_DIR, "enabled"), "w").close()
with open(os.path.join(_RELAY_DIR, "grant.json"), "w", encoding="utf-8") as f:
    json.dump({"hostname": _VHOST}, f)
_EMPTY_RELAY_DIR = os.path.join(_TMP, "relay-off")   # 沒 enabled、沒 grant
os.makedirs(_EMPTY_RELAY_DIR, exist_ok=True)

# 模擬「經過中繼」:Host 是本機的 relay vhost,且帶轉送標頭(= 非直連)。
_WEB = {"Host": _VHOST, "X-Forwarded-For": "203.0.113.9"}
_FORM_MARKS = ('action="/console/auth"', 'name="code"')
_CONSOLE_MARK = 'id="app"'


class _Base(unittest.TestCase):
    WEB = True

    def setUp(self):
        self.boot = bridge._pair_boot_code()
        self.env = patch.dict(os.environ, {"POCKET_RELAY_DIR": _RELAY_DIR})
        self.env.start()
        self.patches = [
            patch.object(bridge, "POCKET_CONSOLE_ENABLED", True),
            patch.object(bridge, "POCKET_CONSOLE_WEB", self.WEB),
            # QR 登入旗標與本旗標獨立;這裡關掉,確保看到的 401/403 都來自
            # web-first 這條分流,不是 QR 頁。
            patch.object(bridge, "POCKET_CONSOLE_QR_LOGIN", False),
        ]
        for p in self.patches:
            p.start()
        self._reset_buckets()
        # client=("127.0.0.1",…):TestClient 預設的 "testclient" 不是合法位址。
        self.http = TestClient(bridge.app, client=("127.0.0.1", 50000))

    def tearDown(self):
        self.http.close()
        for p in reversed(self.patches):
            p.stop()
        self.env.stop()
        self._reset_buckets()

    @staticmethod
    def _reset_buckets():
        with bridge._AUTH_LOCK:
            bridge._AUTH_FAILS_BY_CLIENT.clear()
        with bridge._CONSOLE_CODE_LOCK:
            bridge._CONSOLE_CODES.clear()
        with bridge._PAIR_LOCK:
            bridge._PAIR_QR_MINTS.clear()

    @staticmethod
    def _set_cookies(resp, name):
        return [c for c in resp.headers.get_list("set-cookie")
                if c.startswith(name + "=")]


class WebFlagOff(_Base):
    """旗標關(預設)—— 中繼 vhost 來的一切照舊 403,行為與今天一字不差。"""
    WEB = False

    def test_console_still_403(self):
        r = self.http.get("/console", headers=_WEB)
        self.assertEqual(r.status_code, 403)
        for mark in _FORM_MARKS:
            self.assertNotIn(mark, r.text)      # 連表單都不給

    def test_console_403_even_with_correct_boot(self):
        r = self.http.get(f"/console?boot={self.boot}", headers=_WEB)
        self.assertEqual(r.status_code, 403)

    def test_auth_post_still_403(self):
        r = self.http.post("/console/auth", data={"code": "123456"},
                           headers=_WEB)
        self.assertEqual(r.status_code, 403)


class WebFlagOn(_Base):
    """旗標開 —— 只有本機中繼 vhost 進得到 401 輸入頁;真閘與率限照站。"""

    # ── 來源分流 ────────────────────────────────────────────────────────
    def test_relay_vhost_reaches_login_page(self):
        r = self.http.get("/console", headers=_WEB)
        self.assertEqual(r.status_code, 401)
        for mark in _FORM_MARKS:
            self.assertIn(mark, r.text)
        # 公網面發的是 session cookie,頁尾承諾必須對齊(不能再寫 14 天)。
        self.assertNotIn("14 天", r.text)
        self.assertIn("關閉瀏覽器", r.text)

    def test_other_host_still_403(self):
        r = self.http.get("/console", headers={"Host": "evil.example.com",
                                               "X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(r.status_code, 403)

    def test_relay_disabled_still_403(self):
        with patch.dict(os.environ, {"POCKET_RELAY_DIR": _EMPTY_RELAY_DIR}):
            r = self.http.get("/console", headers=_WEB)
        self.assertEqual(r.status_code, 403)

    def test_local_path_unchanged(self):
        # 本機直連(無轉送標頭):輸入頁照舊,文案仍是 14 天那句。
        r = self.http.get("/console")
        self.assertEqual(r.status_code, 401)
        self.assertIn("14 天", r.text)
        # 本機 POST 正確碼:14 天滑動視窗照舊,http 下不帶 Secure。
        p = self.http.post("/console/auth", data={"code": self.boot},
                           follow_redirects=False)
        self.assertEqual(p.status_code, 303)
        (cookie,) = self._set_cookies(p, bridge._CONSOLE_COOKIE_NAME)
        self.assertIn(f"Max-Age={bridge._CONSOLE_COOKIE_TTL}", cookie)
        self.assertNotIn("Secure", cookie)

    def test_pair_qr_still_local_only(self):
        # web-first 不碰 `_pair_local_only` 本身:/pair/qr.json 連帶正確 boot
        # 也進不來(鑄碼口維持直連限定)。
        r = self.http.get(f"/pair/qr.json?boot={self.boot}", headers=_WEB)
        self.assertEqual(r.status_code, 403)
        self.assertIn("local only", r.text)

    # ── 真閘:短碼 + 率限 + 一次性 ──────────────────────────────────────
    def test_wrong_code_rate_limit_still_bites(self):
        for _ in range(bridge._CONSOLE_AUTH_FAIL_MAX):
            r = self.http.post("/console/auth", data={"code": "000000"},
                               headers=_WEB)
            self.assertEqual(r.status_code, 401)
        r = self.http.post("/console/auth", data={"code": "000000"},
                           headers=_WEB)
        self.assertEqual(r.status_code, 429)
        self.assertTrue(r.headers.get("Retry-After"))
        # 超額時連**正確的**碼都不比(真退避)。
        code, _exp = bridge._console_mint_short_code()
        r = self.http.post("/console/auth", data={"code": code}, headers=_WEB)
        self.assertEqual(r.status_code, 429)

    def test_boot_query_guesses_hit_same_bucket(self):
        # 公網面的 GET ?boot= 錯碼也要進同一張退避表 ——
        for _ in range(bridge._CONSOLE_AUTH_FAIL_MAX):
            r = self.http.get("/console?boot=WRONG", headers=_WEB)
            self.assertEqual(r.status_code, 401)
        r = self.http.get("/console?boot=WRONG", headers=_WEB)
        self.assertEqual(r.status_code, 429)
        # —— 但「裸開輸入頁」不是嘗試:被節流的來源仍看得到輸入頁
        # (否則 PWA 每次啟動都把自己鎖出去)。
        r = self.http.get("/console", headers=_WEB)
        self.assertEqual(r.status_code, 401)
        for mark in _FORM_MARKS:
            self.assertIn(mark, r.text)

    def test_auth_failure_logged_with_client_ip(self):
        with patch.object(bridge, "_log_event") as le:
            self.http.post("/console/auth", data={"code": "999999"},
                           headers=_WEB)
        fails = [c for c in le.call_args_list
                 if c.args[:1] == ("console_auth_failed",)]
        self.assertEqual(len(fails), 1)
        self.assertEqual(fails[0].kwargs.get("client"), "127.0.0.1")
        self.assertEqual(fails[0].kwargs.get("forwarded_for"), "203.0.113.9")

    def test_correct_code_double_cookie_303_and_one_time(self):
        code, _exp = bridge._console_mint_short_code()
        r = self.http.post("/console/auth", data={"code": code},
                           headers={**_WEB, "X-Forwarded-Proto": "https"},
                           follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers.get("location"), "/console")
        (console,) = self._set_cookies(r, bridge._CONSOLE_COOKIE_NAME)
        (claim,) = self._set_cookies(r, bridge._CONSOLE_CLAIM_COOKIE_NAME)
        # 公網面:session cookie(無 Max-Age),https 中繼必帶 Secure。
        self.assertNotIn("Max-Age", console)
        self.assertIn("Secure", console)
        self.assertIn(f"Max-Age={bridge._CONSOLE_CLAIM_COOKIE_TTL}", claim)
        self.assertIn("Secure", claim)
        # 一次性:同一枚短碼 replay 直接吃錯誤頁。
        r2 = self.http.post("/console/auth", data={"code": code}, headers=_WEB)
        self.assertEqual(r2.status_code, 401)

    # ── cookie 開頁 + 續命 ─────────────────────────────────────────────
    def test_cookie_opens_shell_renews_session_only(self):
        r = self.http.get("/console", headers=_WEB,
                          cookies={bridge._CONSOLE_COOKIE_NAME:
                                   bridge._console_cookie_value()})
        self.assertEqual(r.status_code, 200)
        self.assertIn(_CONSOLE_MARK, r.text)
        (renewed,) = self._set_cookies(r, bridge._CONSOLE_COOKIE_NAME)
        self.assertNotIn("Max-Age", renewed)   # 公網面只續 session cookie

    def test_renew_failure_only_affects_renewal(self):
        # t3code 原則:續期失敗只影響該請求的續期,頁面照送、舊憑證照活。
        with patch.object(bridge, "_console_set_cookie",
                          side_effect=RuntimeError("renew boom")):
            r = self.http.get("/console", headers=_WEB,
                              cookies={bridge._CONSOLE_COOKIE_NAME:
                                       bridge._console_cookie_value()})
        self.assertEqual(r.status_code, 200)
        self.assertIn(_CONSOLE_MARK, r.text)
        self.assertEqual(self._set_cookies(r, bridge._CONSOLE_COOKIE_NAME), [])


if __name__ == "__main__":
    unittest.main()
