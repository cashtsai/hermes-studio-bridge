"""/console 的 PWA cookie 閘測試(2026-10-19)。

背景:/console 存成 PWA 之後,manifest 的 `start_url` 啟動時**不會帶 query
string** → 每次開都是裸 `/console` → 吃 403,使用者被迫手貼完整網址。修法:
只有 `/console` 這一道門額外接受一枚簽過名的 cookie;鑄配對碼的端點
(`/pair/qr`、`/pair/qr.json`)**維持只認 query boot**。

驗項:
  1. 帶正確 ?boot= → 過(行為不變)+ 簽發 cookie
  2. 沒 boot、帶有效 cookie → 只有 /console 過
  3. 同一枚 cookie 打鑄碼端點 → 403
  4. cookie 被竄改一個字元 → 403
  5. cookie 的 ts 超過有效期 → 403
  6. 換掉 boot code → 舊 cookie 立刻失效
  7. cookie 屬性:HttpOnly / SameSite=Strict / Secure 跟 scheme 走 / path
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="console-cookie-")
os.environ["PAIR_BOOT_CODE_FILE"] = os.path.join(_TMP, "pair-boot-code")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["POCKET_CONSOLE_ENABLED"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# 同 test_pair_qr_boot_code.py:`_PAIR_BOOT_CODE_FILE` 是模組層常數,全套
# discover 一起跑時 bridge 早被別的檔 import 過 → 上面的 env 變 no-op,會驗到
# **真的** `~/.pocket/pair-boot-code`。測試側把常數綁回 tmp、清掉行程快取。
bridge._PAIR_BOOT_CODE_FILE = os.environ["PAIR_BOOT_CODE_FILE"]
with bridge._PAIR_BOOT_CODE_LOCK:
    bridge._PAIR_BOOT_CODE_CACHE["code"] = ""


def _hosts_stub(force=False):
    return (["http://192.168.1.23:8081"], False)


class _FakeReq:
    """只給 `_pair_check_boot` 用的最小 request(query + cookies + headers)。"""

    def __init__(self, query=None, cookies=None, host="127.0.0.1", scheme="http"):
        self.query_params = dict(query or {})
        self.cookies = dict(cookies or {})
        self.client = type("C", (), {"host": host})()
        self.headers = {}
        self.url = type("U", (), {"scheme": scheme, "path": "/console"})()


class _ConsoleCookieBase(unittest.TestCase):
    def setUp(self):
        self.boot = bridge._pair_boot_code()
        with bridge._PAIR_LOCK:
            bridge._PAIR_QR_MINTS.clear()
            bridge._PAIR_CODES.clear()
        self.patches = [
            # 旗標是模組層常數(env 在 discover 下可能已經定死)→ 直接蓋。
            patch.object(bridge, "POCKET_CONSOLE_ENABLED", True),
            patch.object(bridge, "_pair_host_candidates", side_effect=_hosts_stub),
            patch.object(bridge, "_pair_tunnel_url", return_value=None),
        ]
        for p in self.patches:
            p.start()
        # client=("127.0.0.1",…):`_pair_local_only` 的第二層仍在,TestClient
        # 預設的 "testclient" 會被擋掉。
        self.client = TestClient(bridge.app, client=("127.0.0.1", 50000))

    def tearDown(self):
        self.client.close()
        for p in reversed(self.patches):
            p.stop()

    def _cookie_header(self, value):
        return {"Cookie": f"{bridge._CONSOLE_COOKIE_NAME}={value}"}

    # 2026-10-19 起,「直連 + 憑證缺/壞」不再是裸 403,而是 401 + 配對碼輸入頁
    # (feat/console-login-page)。安全性質一字未改:**拿不到 console 本體、
    # 不種 cookie**;只有外觀從空白 403 變成一個能打字的框。非直連(帶轉送
    # 標頭)仍是裸 403 —— 那條由 tests/test_console_login_page.py 守。
    def assertLoginPage(self, r, why=""):
        self.assertEqual(r.status_code, 401, why)
        self.assertIn('action="/console/auth"', r.text, why)
        self.assertNotIn('id="app"', r.text, "不准漏出 console 本體")
        self.assertEqual(r.headers.get("set-cookie", ""), "",
                         "沒過關不准種 cookie")


class TestConsoleCookieGate(_ConsoleCookieBase):
    # ── 1. 行為不變:帶正確 ?boot= → 200,並簽發 cookie ──────────────────
    def test_query_boot_still_works_and_issues_cookie(self):
        r = self.client.get("/console", params={"boot": self.boot})
        self.assertEqual(r.status_code, 200)
        raw = r.headers.get("set-cookie", "")
        self.assertIn(bridge._CONSOLE_COOKIE_NAME + "=", raw)
        # cookie 的值**不是** boot code 本身(外洩也換不到 pairing code)
        self.assertNotIn(self.boot, raw)

    def test_bare_console_without_cookie_shows_login_page(self):
        self.assertLoginPage(self.client.get("/console"))

    # ── 2. 沒 boot、帶有效 cookie → /console 過(PWA start_url 的情境)──
    def test_valid_cookie_opens_console_without_query(self):
        value = bridge._console_cookie_value()
        r = self.client.get("/console", headers=self._cookie_header(value))
        self.assertEqual(r.status_code, 200)

    def test_issued_cookie_round_trips(self):
        """實際走一遍:第一次帶 boot,之後裸開 /console(靠 jar 裡的 cookie)。"""
        self.assertEqual(
            self.client.get("/console", params={"boot": self.boot}).status_code, 200)
        self.assertEqual(self.client.get("/console").status_code, 200)

    # ── 3. 同一枚 cookie 打鑄碼端點 → 必須 403 ─────────────────────────
    def test_cookie_rejected_by_mint_endpoints(self):
        hdr = self._cookie_header(bridge._console_cookie_value())
        for path in ("/pair/qr", "/pair/qr.json"):
            r = self.client.get(path, headers=hdr)
            self.assertEqual(r.status_code, 403, f"{path} 不該接受 console cookie")
        self.assertEqual(len(bridge._PAIR_CODES), 0, "不該因 cookie 鑄出任何碼")

    def test_pair_check_boot_defaults_to_query_only(self):
        """預設 allow_cookie=False —— 新放寬只能靠呼叫點明確傳。"""
        req = _FakeReq(cookies={bridge._CONSOLE_COOKIE_NAME:
                                bridge._console_cookie_value()})
        with self.assertRaises(HTTPException) as ctx:
            bridge._pair_check_boot(req)
        self.assertEqual(ctx.exception.status_code, 403)
        bridge._pair_check_boot(req, allow_cookie=True)   # 放寬才過

    # ── 4. 竄改一個字元 → 不得進 console ──────────────────────────────
    def test_tampered_cookie_rejected(self):
        value = bridge._console_cookie_value()
        ts, _, sig = value.partition(".")
        flipped = ("b" if sig[0] == "a" else "a") + sig[1:]
        for bad in (f"{ts}.{flipped}",                 # 簽章改一字
                    f"{int(ts) + 1}.{sig}",            # ts 改一字(簽章就不合)
                    sig,                               # 沒有 ts 段
                    f"{ts}.",                          # 空簽章
                    f"notanumber.{sig}",               # ts 不是數字
                    ""):
            r = self.client.get("/console", headers=self._cookie_header(bad))
            self.assertLoginPage(r, f"壞 cookie 居然過了:{bad!r}")

    # ── 5. ts 超過有效期 → 不得進 console ─────────────────────────────
    def test_expired_cookie_rejected(self):
        fresh = bridge._console_cookie_value()
        self.assertTrue(bridge._console_cookie_ok(
            _FakeReq(cookies={bridge._CONSOLE_COOKIE_NAME: fresh})))

        old_ts = time.time() - bridge._CONSOLE_COOKIE_TTL - 60
        expired = bridge._console_cookie_value(now=old_ts)
        self.assertLoginPage(
            self.client.get("/console", headers=self._cookie_header(expired)))

        # 剛好還在期限內的(TTL 前一分鐘)仍要過 —— 別把邊界算反
        almost = bridge._console_cookie_value(
            now=time.time() - bridge._CONSOLE_COOKIE_TTL + 60)
        self.assertEqual(
            self.client.get("/console",
                            headers=self._cookie_header(almost)).status_code, 200)

        # 時間戳在遙遠未來(超過容許偏移)→ 擋掉
        future = bridge._console_cookie_value(
            now=time.time() + bridge._CONSOLE_COOKIE_CLOCK_SKEW + 600)
        self.assertLoginPage(
            self.client.get("/console", headers=self._cookie_header(future)))

    def test_ttl_is_14_days(self):
        self.assertEqual(bridge._CONSOLE_COOKIE_TTL, 14 * 24 * 3600)

    # ── 6. 換掉 boot code → 舊 cookie 立刻失效 ────────────────────────
    def test_cookie_dies_with_boot_code_rotation(self):
        value = bridge._console_cookie_value()
        self.assertEqual(
            self.client.get("/console",
                            headers=self._cookie_header(value)).status_code, 200)
        try:
            os.remove(bridge._PAIR_BOOT_CODE_FILE)     # 換碼:刪檔 + 清快取
            with bridge._PAIR_BOOT_CODE_LOCK:
                bridge._PAIR_BOOT_CODE_CACHE["code"] = ""
            new_boot = bridge._pair_boot_code()
            self.assertNotEqual(new_boot, self.boot)
            r = self.client.get("/console", headers=self._cookie_header(value))
            self.assertLoginPage(r, "換碼後舊 cookie 必須失效")
            # 新碼照樣能開、並簽出新的 cookie
            self.assertEqual(
                self.client.get("/console",
                                params={"boot": new_boot}).status_code, 200)
        finally:
            with open(bridge._PAIR_BOOT_CODE_FILE, "w", encoding="utf-8") as f:
                f.write(self.boot + "\n")
            with bridge._PAIR_BOOT_CODE_LOCK:
                bridge._PAIR_BOOT_CODE_CACHE["code"] = ""

    # ── 7. cookie 屬性 ───────────────────────────────────────────────
    def test_cookie_attributes(self):
        r = self.client.get("/console", params={"boot": self.boot})
        raw = r.headers["set-cookie"]
        low = raw.lower()
        self.assertIn("httponly", low)
        self.assertIn("samesite=strict", low)
        self.assertIn("path=/console", low)
        self.assertIn(f"max-age={bridge._CONSOLE_COOKIE_TTL}", low)
        self.assertNotIn("secure", low)       # http → 不能加 Secure(會被丟掉)

    def test_secure_follows_request_scheme(self):
        from starlette.responses import Response as _Resp
        for scheme, want_secure in (("http", False), ("https", True)):
            resp = _Resp()
            bridge._console_set_cookie(_FakeReq(scheme=scheme), resp)
            raw = resp.headers["set-cookie"].lower()
            self.assertEqual("secure" in raw, want_secure,
                             f"scheme={scheme} 的 Secure 旗標不對:{raw}")

    # ── defense-in-depth:cookie 不繞過 local-only 與旗標 ─────────────
    def test_cookie_does_not_bypass_local_only(self):
        req = _FakeReq(cookies={bridge._CONSOLE_COOKIE_NAME:
                                bridge._console_cookie_value()},
                       host="203.0.113.9")
        with self.assertRaises(HTTPException) as ctx:
            bridge._console_guard(req)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_cookie_does_not_bypass_disabled_flag(self):
        with patch.object(bridge, "POCKET_CONSOLE_ENABLED", False):
            r = self.client.get(
                "/console",
                headers=self._cookie_header(bridge._console_cookie_value()))
            self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
