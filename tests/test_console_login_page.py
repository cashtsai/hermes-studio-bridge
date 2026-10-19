"""/console 配對碼輸入頁(2026-10-19,feat/console-login-page)。

缺口:機主在電腦前開 `http://<tailnet-host>:8081/console`(沒帶 `?boot=`)只會
吃一個**裸 403** —— 畫面上沒有任何可以輸入的地方,他只能自己把碼拼進網址列。
實際情境是:人在電腦前 → 從 iPhone 的 Pocket(設定 → 桌面控制台)讀出碼 →
手打進網頁。所以沒帶碼時改回一頁極簡輸入頁,`POST /console/auth` 驗過就種
cookie(沿用 `_console_cookie_value()`,不另發憑證)並 303 回 `/console`。

這是**認證門面**,所以本檔守的重點全是安全性質,不是外觀:
  1. 輸入頁**只在 `_pair_local_only` 通過時才給**。帶任何轉送標頭
     (XFF / Forwarded / CF-Connecting-IP / X-Real-IP)= 經過 proxy/tunnel →
     維持裸 403,**連表單都不給**。否則等於把一個可暴力破解的登入框掛到網路上。
  2. 嘗試節流:沿用 token 閘那張 per-client 有界表,門檻更嚴(6/分鐘);
     超額時**連比都不比**(連正確的碼也吃 429)。
  3. 錯誤訊息永遠同一句 —— 不透露碼長度/前綴對不對。
  4. 碼不進任何日誌(走 POST body 而非 query,uvicorn access log 記的是 query)。
  5. 成功是 303 到 `/console`(不帶 query)—— 網址列與瀏覽歷史不留碼。
  6. 鑄配對碼的端點(`/pair/qr`、`/pair/qr.json`)維持只認 query,一字未改。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="console-login-")
os.environ["PAIR_BOOT_CODE_FILE"] = os.path.join(_TMP, "pair-boot-code")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["POCKET_CONSOLE_ENABLED"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# 同 test_console_cookie_gate.py:`_PAIR_BOOT_CODE_FILE` 是模組層常數,全套
# discover 一起跑時 bridge 早被別的檔 import 過 → 上面的 env 變 no-op,會驗到
# **真的** `~/.pocket/pair-boot-code`。測試側把常數綁回 tmp、清掉行程快取。
bridge._PAIR_BOOT_CODE_FILE = os.environ["PAIR_BOOT_CODE_FILE"]
with bridge._PAIR_BOOT_CODE_LOCK:
    bridge._PAIR_BOOT_CODE_CACHE["code"] = ""

# 經過 proxy/tunnel 的指紋:任一個出現就不是直連,`_pair_local_only` 必須擋。
_FORWARD_HEADERS = (
    {"X-Forwarded-For": "203.0.113.9"},
    {"X-Forwarded-For": "100.64.0.1"},          # 冒充 tailnet 也不行
    {"Forwarded": "for=203.0.113.9"},
    {"CF-Connecting-IP": "203.0.113.9"},
    {"X-Real-IP": "203.0.113.9"},
)

# 輸入頁的指紋 / console 本體的指紋(用來證明「只拿到頁面殼」或「只拿到表單」)
_FORM_MARKS = ('action="/console/auth"', '<input', 'name="code"', '配對碼')
_CONSOLE_MARK = 'id="app"'


def _hosts_stub(force=False):
    return (["http://192.168.1.23:8081"], False)


class _LoginBase(unittest.TestCase):
    def setUp(self):
        self.boot = bridge._pair_boot_code()
        with bridge._PAIR_LOCK:
            bridge._PAIR_QR_MINTS.clear()
            bridge._PAIR_CODES.clear()
        with bridge._AUTH_LOCK:                 # 節流桶是全域的,每個 case 歸零
            bridge._AUTH_FAILS_BY_CLIENT.clear()
            bridge._AUTH_FAIL_AGG.clear()
        self.patches = [
            patch.object(bridge, "POCKET_CONSOLE_ENABLED", True),
            patch.object(bridge, "_pair_host_candidates", side_effect=_hosts_stub),
            patch.object(bridge, "_pair_tunnel_url", return_value=None),
        ]
        for p in self.patches:
            p.start()
        # client=("127.0.0.1",…):TestClient 預設的 "testclient" 會被
        # `_pair_local_only` 擋掉。
        self.client = TestClient(bridge.app, client=("127.0.0.1", 50000))

    def tearDown(self):
        self.client.close()
        for p in reversed(self.patches):
            p.stop()
        with bridge._AUTH_LOCK:
            bridge._AUTH_FAILS_BY_CLIENT.clear()
            bridge._AUTH_FAIL_AGG.clear()

    # ── 斷言輔助 ──────────────────────────────────────────────────────
    def assertLoginPage(self, r, why=""):
        """401 + 輸入頁,而且**沒有**漏出 console 本體、沒有種 cookie。"""
        self.assertEqual(r.status_code, 401, why or r.text[:200])
        self.assertTrue(r.headers.get("content-type", "").startswith("text/html"),
                        r.headers.get("content-type"))
        for mark in _FORM_MARKS:
            self.assertIn(mark, r.text, f"{why} 輸入頁少了 {mark!r}")
        self.assertNotIn(_CONSOLE_MARK, r.text, "不准漏出 console 本體")
        self.assertEqual(r.headers.get("set-cookie", ""), "",
                         "沒過關不准種 cookie")
        self.assertNotIn(self.boot, r.text, "頁面不准印出 boot code")

    def assertBareForbidden(self, r, why=""):
        """裸 403:body 裡不准有輸入框的任何痕跡。"""
        self.assertEqual(r.status_code, 403, why or r.text[:200])
        for mark in _FORM_MARKS + (_CONSOLE_MARK, "/console/auth", "pairing"):
            self.assertNotIn(mark, r.text, f"{why} 403 的 body 漏出 {mark!r}")
        self.assertNotIn(self.boot, r.text)
        self.assertEqual(r.headers.get("set-cookie", ""), "")

    def cookie_raw(self, r, name):
        """回應裡名為 name 的那條 Set-Cookie 原文(沒有就回 "")。

        一定要用 `get_list` —— 成功的 POST 會種**兩枚** cookie,
        `headers.get("set-cookie")` 只拿得到第一枚,會驗錯對象。
        """
        for raw in r.headers.get_list("set-cookie"):
            if raw.split("=", 1)[0].strip() == name:
                return raw
        return ""

    def cookie_value(self, r, name):
        raw = self.cookie_raw(r, name)
        return raw.split("=", 1)[1].split(";")[0] if raw else ""

    def post(self, code=None, *, headers=None, json_body=False, follow=False):
        kw = {"headers": headers or {}, "follow_redirects": follow}
        if json_body:
            kw["json"] = {} if code is None else {"code": code}
        else:
            kw["data"] = {} if code is None else {"code": code}
        return self.client.post("/console/auth", **kw)


# ═══ 1. local-only 通過 + 無碼 → 輸入頁 ═══════════════════════════════
class TestLoginPageServed(_LoginBase):
    def test_bare_console_returns_login_page(self):
        self.assertLoginPage(self.client.get("/console"))

    def test_wrong_query_boot_also_returns_login_page(self):
        """舊書籤帶著過期/亂打的 ?boot= → 也給輸入頁(而不是死路 403)。"""
        self.assertLoginPage(self.client.get("/console", params={"boot": "x" * 22}))

    def test_login_page_content(self):
        html = self.client.get("/console").text
        self.assertIn("<title>Pocket 桌面控制台</title>", html)
        # 去哪裡拿碼:繁中 + 英文兩句都要在
        self.assertIn("設定 → 桌面控制台", html)
        self.assertIn("Settings → Desktop Console", html)
        self.assertIn("Enter your pairing code", html)
        # 輸入框屬性
        self.assertIn('autocomplete="off"', html)
        self.assertIn('autocapitalize="off"', html)
        self.assertIn('spellcheck="false"', html)
        # 自包含:不准引外部資源(連自家 /console/static 都不引 —— 還沒認證)
        self.assertNotIn("<link", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        # 沒出錯時不該有錯誤列
        self.assertNotIn("配對碼不正確", html)

    def test_login_page_security_headers(self):
        r = self.client.get("/console")
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        csp = r.headers.get("content-security-policy", "")
        # form-action 不會 fallback 到 default-src,必須明寫才管得到 <form>
        self.assertIn("form-action 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(r.headers.get("x-frame-options"), "DENY")
        self.assertEqual(r.headers.get("referrer-policy"), "no-referrer")

    # 第 7 項驗收:輸入頁的 HTML 不可包含 boot code 本身
    def test_login_html_never_contains_boot_code(self):
        for html in (bridge._console_login_html(),
                     bridge._console_login_html(error="x")):
            self.assertNotIn(self.boot, html)
        self.assertNotIn(self.boot, self.client.get("/console").text)
        self.assertNotIn(self.boot, self.post("wrong-code").text)


# ═══ 2. 帶轉送標頭 → 仍是裸 403(連表單都不給)═════════════════════════
class TestForwardedStaysForbidden(_LoginBase):
    def test_forwarded_headers_get_bare_403_on_get(self):
        for hdr in _FORWARD_HEADERS:
            r = self.client.get("/console", headers=hdr)
            self.assertBareForbidden(r, f"GET {hdr}")

    def test_forwarded_headers_get_bare_403_on_post(self):
        """POST 也走同一條線 —— 不然等於留一個可從 tunnel 打的猜碼端點。"""
        for hdr in _FORWARD_HEADERS:
            self.assertBareForbidden(self.post("whatever", headers=hdr),
                                     f"POST {hdr}")

    def test_forwarded_with_correct_code_still_403(self):
        """就算碼是對的,經 proxy 來的也不給 cookie(維持 local-only 語意)。"""
        r = self.post(self.boot, headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertBareForbidden(r)

    def test_non_local_client_gets_bare_403(self):
        """來源不是 loopback/tailnet(直連也一樣)→ 裸 403。"""
        with TestClient(bridge.app, client=("203.0.113.9", 4444)) as c:
            self.assertBareForbidden(c.get("/console"))
            self.assertBareForbidden(c.post("/console/auth",
                                            data={"code": self.boot}))


# ═══ 3. POST 正確碼 → 303 + Set-Cookie + 轉址 /console ════════════════
class TestAuthSuccess(_LoginBase):
    def test_correct_code_redirects_303_with_cookie(self):
        r = self.post(self.boot)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/console")  # 不帶任何 query
        self.assertNotIn("boot", r.headers["location"])
        raw = self.cookie_raw(r, bridge._CONSOLE_COOKIE_NAME)
        self.assertTrue(raw, "沒種 pocket_console")
        # 沿用既有憑證:值是 HMAC 衍生,**不是** boot code 本身
        self.assertNotIn(self.boot, raw)
        low = raw.lower()
        self.assertIn("httponly", low)
        self.assertIn("samesite=strict", low)
        self.assertIn("path=/console", low)

    def test_round_trip_cookie_opens_console(self):
        """走完整一遍:POST 碼 → 跟著 303 → 真的進得去 console,之後裸開也行。"""
        r = self.post(self.boot, follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn(_CONSOLE_MARK, r.text)
        bare = self.client.get("/console")      # 靠 jar 裡的 cookie
        self.assertEqual(bare.status_code, 200)
        self.assertIn(_CONSOLE_MARK, bare.text)

    def test_json_body_accepted(self):
        r = self.post(self.boot, json_body=True)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(self.cookie_raw(r, bridge._CONSOLE_COOKIE_NAME))

    def test_whitespace_is_trimmed(self):
        """手打/貼上難免帶前後空白,不該因此失敗。"""
        self.assertEqual(self.post("  " + self.boot + "\n").status_code, 303)

    def test_success_mints_exactly_one_unbound_code(self):
        """驗過碼的當下鑄**一枚**一次性碼交棒給前端(不是零、也不是一串)。"""
        self.post(self.boot)
        self.assertEqual(len(bridge._PAIR_CODES), 1)
        meta = bridge._pair_code_meta(list(bridge._PAIR_CODES.values())[0])
        self.assertIsNone(meta.get("apple_user_id"), "不該綁到任何帳號")


# ═══ 4. POST 錯誤碼 → 不種 cookie、不洩漏細節 ═════════════════════════
class TestAuthFailure(_LoginBase):
    def test_wrong_code_returns_login_page_without_cookie(self):
        self.assertLoginPage(self.post("definitely-not-the-code"))

    def test_error_message_is_identical_for_every_wrong_shape(self):
        """長度/前綴對不對都不能從回應推出來 —— 一律同一頁、同一句。"""
        bodies = set()
        statuses = set()
        for bad in ("",                            # 空的
                    "x",                           # 太短
                    "x" * len(self.boot),          # 長度剛好對
                    "x" * 400,                     # 超長(會被夾到 256)
                    self.boot[:-1],                # 只差最後一碼
                    self.boot[:-1] + "Z",          # 前綴全對
                    self.boot + "x",               # 正確碼 + 一個字
                    "中文碼"):                      # 非 ASCII(不可以變 500)
            r = self.post(bad)
            statuses.add(r.status_code)
            bodies.add(r.text)
            self.assertEqual(r.headers.get("set-cookie", ""), "",
                             f"錯碼 {bad!r} 居然種了 cookie")
            with bridge._AUTH_LOCK:               # 每次都歸零,避免撞到節流
                bridge._AUTH_FAILS_BY_CLIENT.clear()
        self.assertEqual(statuses, {401}, f"狀態碼不一致:{statuses}")
        self.assertEqual(len(bodies), 1, "錯誤回應的 body 必須字字相同")
        self.assertIn("配對碼不正確", bodies.pop())

    def test_missing_field_is_a_plain_failure(self):
        self.assertLoginPage(self.post(None))
        self.assertLoginPage(self.post(None, json_body=True))

    def test_malformed_json_does_not_500(self):
        r = self.client.post("/console/auth", content=b"{not json",
                             headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 401)

    def test_code_in_query_string_is_not_accepted(self):
        """碼只收 POST body:query 會被 uvicorn access log 記下來。"""
        r = self.client.post("/console/auth?code=%s&boot=%s" % (self.boot, self.boot),
                             data={}, follow_redirects=False)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers.get("set-cookie", ""), "")


# ═══ 5. 連續錯誤 → 節流生效 ═══════════════════════════════════════════
class TestThrottle(_LoginBase):
    def test_repeated_failures_trip_429(self):
        for i in range(bridge._CONSOLE_AUTH_FAIL_MAX):
            self.assertEqual(self.post("wrong-%d" % i).status_code, 401,
                             f"第 {i+1} 次不該被擋")
        r = self.post("wrong-again")
        self.assertEqual(r.status_code, 429)
        self.assertTrue(int(r.headers.get("retry-after", "0")) > 0,
                        "429 要給 Retry-After 讓前端知道退避多久")

    def test_throttled_request_is_not_even_compared(self):
        """超額時連正確的碼也吃 429 —— 只回 429 但照樣驗碼的話,
        暴力破解者根本不在乎狀態碼,那不叫節流。"""
        for i in range(bridge._CONSOLE_AUTH_FAIL_MAX):
            self.post("wrong-%d" % i)
        r = self.post(self.boot)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.headers.get("set-cookie", ""), "")

    def test_throttle_uses_shared_bounded_table(self):
        """沿用 token 閘那張 per-client 有界表,不是另開一張會漏的 dict。"""
        self.post("nope")
        self.assertEqual(len(bridge._AUTH_FAILS_BY_CLIENT.get("127.0.0.1", [])), 1)
        self.assertLessEqual(bridge._CONSOLE_AUTH_FAIL_MAX, bridge._AUTH_FAIL_MAX,
                             "人手打的碼門檻不該比機器帶 token 的還寬")

    def test_window_expiry_releases(self):
        """整窗過去 → 放行(不是永久鎖死)。"""
        for i in range(bridge._CONSOLE_AUTH_FAIL_MAX):
            self.post("wrong-%d" % i)
        self.assertEqual(self.post(self.boot).status_code, 429)
        old = time.monotonic() - bridge._AUTH_FAIL_WINDOW - 1
        with bridge._AUTH_LOCK:                   # 把那些失敗挪到窗外
            dq = bridge._AUTH_FAILS_BY_CLIENT["127.0.0.1"]
            for i in range(len(dq)):
                dq[i] = old
        self.assertEqual(self.post(self.boot).status_code, 303)

    def test_successful_login_does_not_accrue(self):
        """對的碼不該進失敗桶(不然自己把自己鎖出去)。"""
        for _ in range(bridge._CONSOLE_AUTH_FAIL_MAX + 3):
            self.assertEqual(self.post(self.boot).status_code, 303)
            with bridge._PAIR_LOCK:      # 這條只驗失敗桶,別撞到鑄碼桶
                bridge._PAIR_QR_MINTS.clear()

    def test_throttle_does_not_block_the_login_page_itself(self):
        """被節流時 GET /console 仍要看得到頁面(不然使用者只剩白畫面)。"""
        for i in range(bridge._CONSOLE_AUTH_FAIL_MAX + 2):
            self.post("wrong-%d" % i)
        self.assertLoginPage(self.client.get("/console"))


# ═══ 6. 已有有效 cookie → 直接進,不再看到輸入頁 ══════════════════════
class TestCookieSkipsLoginPage(_LoginBase):
    def test_valid_cookie_goes_straight_in(self):
        hdr = {"Cookie": "%s=%s" % (bridge._CONSOLE_COOKIE_NAME,
                                    bridge._console_cookie_value())}
        r = self.client.get("/console", headers=hdr)
        self.assertEqual(r.status_code, 200)
        self.assertIn(_CONSOLE_MARK, r.text)
        self.assertNotIn('action="/console/auth"', r.text)

    def test_query_boot_goes_straight_in(self):
        r = self.client.get("/console", params={"boot": self.boot})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('action="/console/auth"', r.text)


# ═══ 一次性 claim cookie:把「剛驗過碼」交棒給前端 ═══════════════════════
# 背景:輸入頁讓人進得了 /console,但**全新的瀏覽器**進去後 console 仍是
# 「未配對」—— app.js 的 bootstrap 要嘛從 localStorage 拿舊 token、要嘛從
# `?boot=` 換 token,而 303 之後網址上沒有 `?boot=`。修法:驗過碼的當下伺服器
# 就地鑄一枚一次性 pairing code,放進一枚 60 秒、JS 讀得到的獨立 cookie。
class TestClaimCookie(_LoginBase):
    def setUp(self):
        super().setUp()
        # device token 不落盤(production 的 ~/.pocket/device-tokens.json 不准碰)
        self.extra = [patch.object(bridge, "_save_device_tokens", lambda d: None),
                      patch.object(bridge, "_account_device_for_token",
                                   return_value=None)]
        for p in self.extra:
            p.start()
        self.issued = []

    def tearDown(self):
        for p in reversed(self.extra):
            p.stop()
        with bridge._PAIR_LOCK:          # 測試發出的 device token 不留在行程裡
            for t in self.issued:
                bridge._DEVICE_TOKENS.pop(t, None)
        super().tearDown()

    def claim(self, code):
        r = self.client.post("/pair/claim",
                             json={"code": code, "device_name": "Console",
                                   "platform": "web"})
        if r.status_code == 200 and r.json().get("token"):
            self.issued.append(r.json()["token"])
        return r

    # ── 1. 成功 → 兩枚 cookie 同時到,屬性各自正確 ────────────────────
    def test_success_sets_both_cookies(self):
        r = self.post(self.boot)
        long_raw = self.cookie_raw(r, bridge._CONSOLE_COOKIE_NAME)
        claim_raw = self.cookie_raw(r, bridge._CONSOLE_CLAIM_COOKIE_NAME)
        self.assertTrue(long_raw, "沒種長命的 pocket_console")
        self.assertTrue(claim_raw, "沒種短命的 claim cookie")
        # 長命的:HttpOnly(JS 讀不到)
        self.assertIn("httponly", long_raw.lower())
        # 短命的:**不是** HttpOnly(前端要用 document.cookie 讀),且 ≤60 秒
        low = claim_raw.lower()
        self.assertNotIn("httponly", low, "claim cookie 要讓前端讀得到")
        self.assertIn("samesite=strict", low)
        self.assertIn("path=/console", low)
        self.assertNotIn("secure", low)          # http → 加了會被瀏覽器丟掉
        age = int(low.split("max-age=")[1].split(";")[0])
        self.assertLessEqual(age, 60, "claim cookie 必須極短命")
        self.assertEqual(age, bridge._CONSOLE_CLAIM_COOKIE_TTL)

    def test_claim_cookie_is_not_the_boot_code(self):
        """裝的是一次性 pairing code,不是 boot code —— 外洩換不到長期權限。"""
        value = self.cookie_value(self.post(self.boot),
                                  bridge._CONSOLE_CLAIM_COOKIE_NAME)
        self.assertTrue(value)
        self.assertNotEqual(value, self.boot)
        self.assertIn(value, bridge._PAIR_CODES, "值必須是真的鑄出來的那枚碼")

    def test_claim_cookie_secure_follows_scheme(self):
        from starlette.responses import Response as _Resp

        class _Req:
            def __init__(self, scheme):
                self.url = type("U", (), {"scheme": scheme})()
        for scheme, want in (("http", False), ("https", True)):
            resp = _Resp()
            bridge._console_set_claim_cookie(_Req(scheme), resp, "abc123")
            self.assertEqual("secure" in resp.headers["set-cookie"].lower(), want)

    # ── 2. 失敗 → 兩種 cookie 都不種 ─────────────────────────────────
    def test_failure_sets_neither_cookie(self):
        for r in (self.post("wrong-code"), self.post(None),
                  self.post("wrong", json_body=True)):
            self.assertEqual(r.headers.get("set-cookie", ""), "")
            self.assertEqual(self.cookie_raw(r, bridge._CONSOLE_CLAIM_COOKIE_NAME), "")
        self.assertEqual(len(bridge._PAIR_CODES), 0, "失敗不准鑄碼")

    def test_throttled_sets_neither_cookie(self):
        for i in range(bridge._CONSOLE_AUTH_FAIL_MAX):
            self.post("wrong-%d" % i)
        r = self.post(self.boot)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.headers.get("set-cookie", ""), "")
        self.assertEqual(len(bridge._PAIR_CODES), 0)

    def test_forwarded_post_sets_neither_cookie(self):
        r = self.post(self.boot, headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.headers.get("set-cookie", ""), "")
        self.assertEqual(len(bridge._PAIR_CODES), 0)

    # ── 3. 【最重要】長命 cookie 換不到 claim cookie ──────────────────
    def test_long_lived_cookie_never_yields_a_claim_cookie(self):
        """守著 cookie 閘原本的設計前提:「`pocket_console` 外洩頂多能看頁面殼,
        **換不到可以接管主機的 pairing code**」。鑄碼只發生在驗過碼的那一次
        POST,`GET /console` 不論憑證是 cookie 還是 ?boot= 都不准鑄。"""
        hdr = {"Cookie": "%s=%s" % (bridge._CONSOLE_COOKIE_NAME,
                                    bridge._console_cookie_value())}
        r = self.client.get("/console", headers=hdr)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.cookie_raw(r, bridge._CONSOLE_CLAIM_COOKIE_NAME), "",
                         "長命 cookie 居然換到了 claim cookie —— 安全邊界破了")
        self.assertEqual(len(bridge._PAIR_CODES), 0, "GET /console 不准鑄碼")

    def test_query_boot_get_also_mints_nothing(self):
        """連帶 ?boot= 的 GET 也不鑄 —— 那條路前端本來就自己打 /pair/qr.json。"""
        r = self.client.get("/console", params={"boot": self.boot})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.cookie_raw(r, bridge._CONSOLE_CLAIM_COOKIE_NAME), "")
        self.assertEqual(len(bridge._PAIR_CODES), 0)

    def test_following_the_303_does_not_re_mint(self):
        """走完整一遍(含跟著 303 落地)總共只鑄一枚,不會每開一次頁就一枚。"""
        self.post(self.boot, follow=True)
        self.assertEqual(len(bridge._PAIR_CODES), 1)
        self.client.get("/console")
        self.client.get("/console")
        self.assertEqual(len(bridge._PAIR_CODES), 1)

    # ── 4. 單次性:用掉之後再用 → 既有機制擋下 ───────────────────────
    def test_code_is_single_use(self):
        code = self.cookie_value(self.post(self.boot),
                                 bridge._CONSOLE_CLAIM_COOKIE_NAME)
        first = self.claim(code)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["token"].startswith("pdev-"))
        self.assertNotIn(code, bridge._PAIR_CODES, "用掉就該從表裡消失")
        again = self.claim(code)
        self.assertNotEqual(again.status_code, 200, "一次性碼居然能用第二次")

    def test_claimed_token_actually_works(self):
        """端到端:claim cookie → /pair/claim → 這枚 token 真的打得動 /app/*。"""
        code = self.cookie_value(self.post(self.boot),
                                 bridge._CONSOLE_CLAIM_COOKIE_NAME)
        token = self.claim(code).json()["token"]
        r = self.client.get("/app/v1/console/boot",
                            headers={"Authorization": "Bearer " + token})
        self.assertEqual(r.status_code, 200)

    def test_code_ttl_matches_cookie_ttl(self):
        """碼的壽命跟 cookie 綁在一起 —— 不放寬既有鑄碼機制。"""
        self.post(self.boot)
        meta = bridge._pair_code_meta(list(bridge._PAIR_CODES.values())[0])
        left = meta["expiry"] - time.monotonic()
        self.assertLessEqual(left, bridge._CONSOLE_CLAIM_COOKIE_TTL + 1)
        self.assertGreater(left, 0)
        self.assertLess(bridge._CONSOLE_CLAIM_COOKIE_TTL, bridge._PAIR_CODE_TTL,
                        "不該比既有預設 TTL 更長")

    # ── 鑄碼節流與 /pair/qr.json 共用一桶,且不讓登入失敗 ──────────────
    def test_mint_throttle_is_shared_and_degrades_gracefully(self):
        with bridge._PAIR_LOCK:                 # 把鑄碼桶灌滿
            for _ in range(bridge._PAIR_QR_MINT_LIMIT):
                bridge._PAIR_QR_MINTS.append(time.monotonic())
        r = self.post(self.boot)
        self.assertEqual(r.status_code, 303, "鑄碼被節流不該讓登入失敗")
        self.assertTrue(self.cookie_raw(r, bridge._CONSOLE_COOKIE_NAME),
                        "console cookie 照種")
        self.assertEqual(self.cookie_raw(r, bridge._CONSOLE_CLAIM_COOKIE_NAME), "")
        self.assertEqual(len(bridge._PAIR_CODES), 0)


# ═══ 碼不進日誌 ═══════════════════════════════════════════════════════
class TestNoCodeInLogs(_LoginBase):
    def test_code_never_logged(self):
        seen = []
        with patch.object(bridge, "_log_event",
                          side_effect=lambda ev, **kw: seen.append((ev, kw))):
            self.post(self.boot)                      # 成功
            self.post("wrong-code")                   # 失敗
            for i in range(bridge._CONSOLE_AUTH_FAIL_MAX + 1):
                self.post("wrong-%d" % i)             # 失敗到 429
            self.client.get("/console")               # 輸入頁
            self.client.get("/console", params={"boot": self.boot})
        blob = json.dumps(seen, default=str, ensure_ascii=False)
        self.assertNotIn(self.boot, blob, "boot code 不准出現在任何日誌欄位")
        events = {ev for ev, _ in seen}
        self.assertIn("console_auth_failed", events, "失敗要留痕(但不留碼)")
        self.assertIn("console_auth_ok", events)
        # 事件只准帶來源與一個 bool,不准帶任何碼的衍生物
        for ev, kw in seen:
            if ev.startswith("console_auth"):
                self.assertLessEqual(set(kw), {"client", "claim"},
                                     f"{ev} 的欄位太多:{kw}")
                self.assertIsInstance(kw.get("claim", False), bool)

    def test_claim_code_never_logged(self):
        """交棒給前端的那枚一次性碼也不准進日誌(延伸上一條)。"""
        seen = []
        with patch.object(bridge, "_log_event",
                          side_effect=lambda ev, **kw: seen.append((ev, kw))):
            r = self.post(self.boot)
        code = self.cookie_value(r, bridge._CONSOLE_CLAIM_COOKIE_NAME)
        self.assertTrue(code)
        blob = json.dumps(seen, default=str, ensure_ascii=False)
        self.assertNotIn(code, blob, "一次性 pairing code 不准出現在日誌")
        self.assertNotIn(self.boot, blob)


# ═══ 跨站表單 POST ════════════════════════════════════════════════════
class TestCrossSiteBlocked(_LoginBase):
    def test_cross_site_form_post_rejected(self):
        """`_pair_local_only` 只證明「socket 是本機直連」,不證明「是本機的人按的」:
        機主瀏覽器開到的惡意網頁能對 127.0.0.1 送 no-cors 表單 POST。
        `Sec-Fetch-Site` 是瀏覽器貼的、網頁改不掉。"""
        for site in ("cross-site", "same-site"):
            r = self.post(self.boot, headers={"Sec-Fetch-Site": site})
            self.assertEqual(r.status_code, 403, site)
            self.assertEqual(r.headers.get("set-cookie", ""), "", site)

    def test_same_origin_and_missing_header_allowed(self):
        for hdr in ({"Sec-Fetch-Site": "same-origin"},
                    {"Sec-Fetch-Site": "none"},     # 直接在網址列打的
                    {}):
            self.assertEqual(self.post(self.boot, headers=hdr).status_code, 303,
                             hdr)


# ═══ 旗標關 → 404(對外表現同不存在)══════════════════════════════════
class TestDisabledFlag(_LoginBase):
    def test_disabled_flag_404_everywhere(self):
        with patch.object(bridge, "POCKET_CONSOLE_ENABLED", False):
            for r in (self.client.get("/console"),
                      self.post(self.boot),
                      self.post("wrong")):
                self.assertEqual(r.status_code, 404)
                self.assertNotIn("/console/auth", r.text)


# ═══ 6. 鑄配對碼的端點一字未改 ════════════════════════════════════════
class TestMintEndpointsUntouched(_LoginBase):
    def test_mint_endpoints_still_query_only_and_formless(self):
        for path in ("/pair/qr", "/pair/qr.json"):
            r = self.client.get(path)                  # 無碼
            self.assertEqual(r.status_code, 403, path)
            self.assertNotIn("/console/auth", r.text, path)
            # console cookie 換不到鑄碼權
            r2 = self.client.get(path, headers={
                "Cookie": "%s=%s" % (bridge._CONSOLE_COOKIE_NAME,
                                     bridge._console_cookie_value())})
            self.assertEqual(r2.status_code, 403, path)
        self.assertEqual(len(bridge._PAIR_CODES), 0)

    def test_pair_qr_page_still_opens_with_query_boot(self):
        self.assertEqual(
            self.client.get("/pair/qr", params={"boot": self.boot}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
