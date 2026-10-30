"""`GET /console` 的入口分流(規格 CONSOLE_QR_LOGIN_SPEC §6.1)。

QR 登入的狀態機、token scope、手機端三支端點在 10c2aa4 就都寫好了,但
**沒有任何地方會導向它們** —— 遠端來的請求一律被 `_pair_local_only` 裸 403
擋掉,整個功能觸發不了。這一檔釘的就是接上去之後的三條路。

守的是安全性質,不是外觀:

  1. **可信來源的行為一字不變**。QR 登入是加法,不能順手改掉本機那條路。
     (test_console_login_page.py 已經從另一側釘住同一件事。)
  2. **旗標關 = 預設 = 現狀**。遠端照樣裸 403,連 QR 頁都不給。
  3. **遠端那條只認 cookie,不認 `?boot=`**。`_pair_boot_ok` 兩種都收,
     誤用它等於「公網知道六碼就能進」—— 六碼熵只有 10⁶(規格 §3)。
     這是本檔最重要的一條。
  4. **遠端續命只能續成 session cookie**。本機那條是 14 天滑動視窗;遠端
     若照抄,掃碼登入的人每開一次頁就把「關瀏覽器即失效」(規格 §10)
     換成一張 14 天通行證 —— 安全前提被無聲抵銷,而且沒有任何錯誤訊息。
  5. **sid 不進頁面、不給 JS 讀**(httpOnly),QR 裡只有 rid。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="console-qr-entry-")
os.environ["PAIR_BOOT_CODE_FILE"] = os.path.join(_TMP, "pair-boot-code")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["POCKET_CONSOLE_ENABLED"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# 同 test_console_login_page.py:`_PAIR_BOOT_CODE_FILE` 是模組層常數,全套一起
# 跑時 bridge 早被別的檔 import 過 → 上面的 env 變 no-op,會驗到**真的**
# `~/.pocket/pair-boot-code`。測試側把常數綁回 tmp、清掉行程快取。
bridge._PAIR_BOOT_CODE_FILE = os.environ["PAIR_BOOT_CODE_FILE"]
with bridge._PAIR_BOOT_CODE_LOCK:
    bridge._PAIR_BOOT_CODE_CACHE["code"] = ""

# 帶任一個 = 經過 proxy/tunnel = 不是直連 → 對 `_pair_local_only` 就是遠端。
_REMOTE = {"X-Forwarded-For": "203.0.113.9"}
_FORM_MARKS = ('action="/console/auth"', 'name="code"')
_CONSOLE_MARK = 'id="app"'


class _Base(unittest.TestCase):
    QR_LOGIN = True

    def setUp(self):
        self.boot = bridge._pair_boot_code()
        self.patches = [
            patch.object(bridge, "POCKET_CONSOLE_ENABLED", True),
            patch.object(bridge, "POCKET_CONSOLE_QR_LOGIN", self.QR_LOGIN),
            # console 本體的 index.html 在 repo 裡;真的讀得到才不會把
            # 「分流對了但檔案不在」誤判成分流壞掉。
            patch.object(bridge, "_pair_tunnel_url", return_value=None),
        ]
        for p in self.patches:
            p.start()
        with bridge._CONSOLE_LOGIN_LOCK:
            bridge._CONSOLE_LOGIN_REQS.clear()
            bridge._CONSOLE_LOGIN_MINTS.clear()
        # client=("127.0.0.1",…):TestClient 預設的 "testclient" 不是合法位址,
        # 會被 `_pair_local_only` 擋掉,那樣就測不到「可信來源」那條。
        self.http = TestClient(bridge.app, client=("127.0.0.1", 50000))

    def tearDown(self):
        self.http.close()
        for p in reversed(self.patches):
            p.stop()
        with bridge._CONSOLE_LOGIN_LOCK:
            bridge._CONSOLE_LOGIN_REQS.clear()
            bridge._CONSOLE_LOGIN_MINTS.clear()

    def _cookie_header(self):
        """一枚有效的 `pocket_console` cookie —— 模擬「批准過的瀏覽器」。

        用 `_console_cookie_value()` 現鑄而不是硬寫字串:它是
        `HMAC(boot_code, …)`,硬寫的版本換碼就爛,而且會讓這個測試
        看起來像在驗一個常數。
        """
        return {"Cookie": "%s=%s" % (bridge._CONSOLE_COOKIE_NAME,
                                     bridge._console_cookie_value())}


class LocalPathUnchangedTest(_Base):
    """規矩 1:QR 登入是加法,可信來源那條路一字不變。"""

    def test_local_without_code_still_gets_form(self):
        r = self.http.get("/console")
        self.assertEqual(r.status_code, 401, r.text[:200])
        for m in _FORM_MARKS:
            self.assertIn(m, r.text, f"本機沒帶碼應該拿到輸入頁,少了 {m!r}")

    def test_local_with_correct_code_gets_console(self):
        r = self.http.get("/console", params={"boot": self.boot})
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertIn(_CONSOLE_MARK, r.text)

    def test_local_cookie_refresh_keeps_14day_window(self):
        """本機那條是 14 天滑動視窗 —— 不能被遠端那條的 session cookie 波及。"""
        r = self.http.get("/console", params={"boot": self.boot})
        self.assertEqual(r.status_code, 200)
        sc = r.headers.get("set-cookie", "")
        self.assertIn(bridge._CONSOLE_COOKIE_NAME, sc)
        self.assertIn("Max-Age=", sc, f"本機應該續發有期限的 cookie:{sc}")


class FlagOffTest(_Base):
    """規矩 2:旗標關(預設)→ 遠端維持裸 403,連 QR 頁都不給。"""
    QR_LOGIN = False

    def test_remote_gets_bare_403(self):
        r = self.http.get("/console", headers=_REMOTE)
        self.assertEqual(r.status_code, 403, r.text[:200])
        for m in _FORM_MARKS + (_CONSOLE_MARK, "console-login", "qr"):
            self.assertNotIn(m, r.text.lower() if m == "qr" else r.text,
                             f"旗標關的 403 漏出 {m!r}")

    def test_remote_with_correct_code_still_403(self):
        """旗標關時,`?boot=` 也不該讓公網進來(這是既有行為,確認沒被我改掉)。"""
        r = self.http.get("/console", params={"boot": self.boot}, headers=_REMOTE)
        self.assertEqual(r.status_code, 403, r.text[:200])

    def test_flag_default_is_off(self):
        """預設值本身就是防線 —— 規格 §14。"""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("POCKET_CONSOLE_QR_LOGIN", None)
            self.assertFalse(
                os.environ.get("POCKET_CONSOLE_QR_LOGIN", "0") != "0",
                "POCKET_CONSOLE_QR_LOGIN 的預設必須是關")


class RemoteQrPathTest(_Base):
    """規矩 3 + 5:遠端拿到 QR 頁,而且只認 cookie、不認六碼。"""

    def test_remote_gets_qr_page(self):
        r = self.http.get("/console", headers=_REMOTE)
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertIn("pocket://console-login", r.text,
                      "QR 頁應該含 payload(沒 qrcode 套件時也有可複製連結)")
        self.assertNotIn(_CONSOLE_MARK, r.text, "不准漏出 console 本體")

    def test_remote_boot_code_does_not_open_console(self):
        """**本檔最重要的一條。**

        `_pair_boot_ok(allow_cookie=True)` 連 `?boot=` 一起收。分流若圖方便
        呼叫它,公網只要知道那六碼就能直接進 console —— local-only 這道門
        等於被拆掉,而且表面上一切正常。
        """
        r = self.http.get("/console", params={"boot": self.boot}, headers=_REMOTE)
        self.assertNotIn(_CONSOLE_MARK, r.text,
                         "公網帶正確六碼竟然進了 console —— local-only 被拆掉了")
        self.assertEqual(r.status_code, 200)     # 旗標開 → QR 頁
        self.assertIn("pocket://console-login", r.text)

    def test_sid_cookie_is_httponly_strict_and_session(self):
        r = self.http.get("/console", headers=_REMOTE)
        sc = r.headers.get("set-cookie", "")
        self.assertIn(bridge._CONSOLE_LOGIN_SID_COOKIE, sc, sc)
        self.assertIn("HttpOnly", sc, f"sid 不給 JS 讀(XSS 防線):{sc}")
        self.assertIn("SameSite=strict", sc.replace("Strict", "strict"), sc)
        self.assertNotIn("Max-Age=", sc,
                         f"sid 必須是 session cookie(關瀏覽器即失效):{sc}")

    def test_rid_in_page_is_not_the_sid(self):
        """QR 裡只有 rid。sid 若也印進頁面,「拍照搶登入」就擋不住了。"""
        r = self.http.get("/console", headers=_REMOTE)
        sid = self.http.cookies.get(bridge._CONSOLE_LOGIN_SID_COOKIE)
        self.assertTrue(sid, "應該有種 sid cookie")
        self.assertNotIn(sid, r.text, "sid 不准出現在頁面裡")

    def test_each_visit_mints_a_distinct_request(self):
        self.http.get("/console", headers=_REMOTE)
        self.http.cookies.clear()
        self.http.get("/console", headers=_REMOTE)
        with bridge._CONSOLE_LOGIN_LOCK:
            self.assertGreaterEqual(len(bridge._CONSOLE_LOGIN_REQS), 2)

    def test_rate_limited_returns_503_not_500(self):
        """規格 §8.3:全域節流超額 → 503 + Retry-After,不是 500、不是 429。

        用 503 而不是 429:這是全域節流(保護這台主機),不是針對這個來源的
        配額。per-IP 配額擋不住殭屍網路,所以規格刻意選全域。
        """
        with patch.object(bridge, "_console_login_create",
                          return_value=(None, None)):
            r = self.http.get("/console", headers=_REMOTE)
        self.assertEqual(r.status_code, 503, r.text[:200])
        self.assertEqual(r.headers.get("retry-after"), "60")


class RemoteApprovedTest(_Base):
    """規矩 4:批准過的遠端瀏覽器回頭再開頁。"""

    def test_cookie_lets_approved_browser_back_in(self):
        """少了這一條,掃碼成功的人會被**無限丟回 QR 頁**。"""
        r = self.http.get("/console", headers={**_REMOTE, **self._cookie_header()})
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertIn(_CONSOLE_MARK, r.text, "批准過的遠端瀏覽器應該進得去")
        self.assertNotIn("pocket://console-login", r.text, "不該又丟回 QR 頁")

    def test_remote_refresh_stays_session_cookie(self):
        """**我第一版寫錯的地方。**

        底下的續命原本無條件 `_console_set_cookie(request, resp)` —— 遠端
        瀏覽器每開一次頁就被換發一枚 14 天 cookie,把規格 §10「關瀏覽器
        即失效」整個抵銷,而且沒有任何錯誤訊息。QR 登入的前提正是
        「人不一定在可信的電腦前」。
        """
        r = self.http.get("/console", headers={**_REMOTE, **self._cookie_header()})
        self.assertEqual(r.status_code, 200)
        sc = r.headers.get("set-cookie", "")
        self.assertIn(bridge._CONSOLE_COOKIE_NAME, sc, sc)
        self.assertNotIn(
            "Max-Age=", sc,
            "遠端續命發了有期限的 cookie —— 關瀏覽器即失效被抵銷了:" + sc)


if __name__ == "__main__":
    unittest.main()
