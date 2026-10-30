"""console-remote scope —— QR 登入發出的 token 能做什麼、不能做什麼。

機主 2026-10-30 拍板(規格 §15 第 1 題):**唯讀 + 限定目錄**,不是規格原本
的「完全擋掉檔案面」。理由是「手機看家中電腦任何檔案」是產品賣點,全擋掉
等於賣一個看不了檔案的遠端控制台。

這一組釘的是**繞過路徑**,不是 happy path —— 會出事的是繞過:

  · `/file` 有 `?token=` 旁路(`<img>` 不能帶 header)。只擋 header 那條的話,
    `<img src="...&token=">` 就是現成的洞。
  · `/filediff` 和 `/file` 共用同一段安全根目錄判定,**兩處都要擋**;
    只擋一個等於留後門,因為 filediff 也會吐檔案內容。
  · 白名單而不是黑名單:新增端點時黑名單會**安靜地**放行。
"""
import _isolation  # noqa: F401
import os
import sys
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="console-scope-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
_ALLOWED = os.path.join(_TMP, "shared")
os.makedirs(_ALLOWED, exist_ok=True)
os.environ["POCKET_CONSOLE_REMOTE_FILE_ROOTS"] = _ALLOWED
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class ConsoleRemoteScopeTest(unittest.TestCase):
    TOKEN = "pdev-console-remote-test"

    def setUp(self):
        from fastapi.testclient import TestClient
        self.http = TestClient(bridge.app)
        self.ok = os.path.join(_ALLOWED, "ok.txt")
        with open(self.ok, "w") as f:
            f.write("visible")
        self.secret = os.path.join(_TMP, "secret.txt")
        with open(self.secret, "w") as f:
            f.write("must not leak")
        with bridge._PAIR_LOCK:
            bridge._DEVICE_TOKENS[self.TOKEN] = {
                "name": "borrowed laptop", "platform": "web",
                "scope": bridge.CONSOLE_REMOTE_SCOPE,
                "expires": time.time() + 600,
                "created": time.time(), "last_seen": time.time(),
            }

    def tearDown(self):
        with bridge._PAIR_LOCK:
            bridge._DEVICE_TOKENS.pop(self.TOKEN, None)

    def _hdr(self):
        return {"Authorization": "Bearer " + self.TOKEN}

    # ── 白名單 ──

    def test_non_whitelisted_path_denied(self):
        """沒列進白名單的敏感端點一律 403 —— 新增端點不會被安靜放行。"""
        r = self.http.get("/pair/devices", headers=self._hdr())
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("SCOPE_FORBIDDEN", r.text)

    def test_whitelisted_path_reaches_handler(self):
        """白名單內的要真的打得到(不是全擋)。"""
        r = self.http.get("/health", headers=self._hdr())
        self.assertEqual(r.status_code, 200, r.text)

    # ── 有效期(一般 device token 沒有,這是新行為)──

    def test_expired_token_is_401(self):
        with bridge._PAIR_LOCK:
            bridge._DEVICE_TOKENS[self.TOKEN]["expires"] = time.time() - 1
        r = self.http.get("/health", headers=self._hdr())
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("TOKEN_EXPIRED", r.text)

    def test_missing_expires_is_treated_as_expired(self):
        """缺欄位代表發的時候就寫錯了 —— 寧可當場失效,不要無限期有效。"""
        with bridge._PAIR_LOCK:
            bridge._DEVICE_TOKENS[self.TOKEN].pop("expires", None)
        r = self.http.get("/health", headers=self._hdr())
        self.assertEqual(r.status_code, 401, r.text)

    # ── 檔案:唯讀 + 限定目錄 ──

    def test_allowed_root_is_readable(self):
        """機主開放的目錄要看得到 —— 這就是 B 方案的重點。"""
        r = self.http.get("/file", params={"path": self.ok}, headers=self._hdr())
        self.assertEqual(r.status_code, 200, r.text)

    def test_outside_allowed_root_is_404_not_403(self):
        """回 404 不是 403:別讓遠端瀏覽器用錯誤碼**探測檔案存不存在**。"""
        r = self.http.get("/file", params={"path": self.secret}, headers=self._hdr())
        self.assertEqual(r.status_code, 404, r.text)

    def test_query_token_bypass_is_closed(self):
        """`<img src="...&token=">` 這條旁路不得繞過 scope。

        `/file` 刻意支援 `?token=`(瀏覽器的 <img>/<video> 不能帶 header)。
        只擋 header 那條的話,這就是現成的洞。
        """
        r = self.http.get("/file", params={"path": self.secret,
                                           "token": self.TOKEN})
        self.assertEqual(r.status_code, 404, r.text)

    def test_filediff_also_gated(self):
        """filediff 也會吐檔案內容 —— 兩處共用同一段安全根目錄判定,
        只擋 /file 等於留後門。"""
        r = self.http.get("/filediff", params={"path": self.secret},
                          headers=self._hdr())
        self.assertEqual(r.status_code, 404, r.text)

    # ── 不得影響一般 token ──

    def test_normal_device_token_unaffected(self):
        """一般 device token 沒有 scope、沒有有效期、不受白名單限制。"""
        plain = "pdev-plain-test"
        with bridge._PAIR_LOCK:
            bridge._DEVICE_TOKENS[plain] = {"name": "phone", "platform": "ios",
                                            "created": time.time(),
                                            "last_seen": time.time()}
        try:
            r = self.http.get("/pair/devices",
                              headers={"Authorization": "Bearer " + plain})
            self.assertEqual(r.status_code, 200, r.text)
            r2 = self.http.get("/file", params={"path": self.secret},
                               headers={"Authorization": "Bearer " + plain})
            self.assertNotEqual(r2.status_code, 404,
                                "一般 token 讀家目錄底下的檔不該被新閘擋掉")
        finally:
            with bridge._PAIR_LOCK:
                bridge._DEVICE_TOKENS.pop(plain, None)

    def test_master_token_unaffected(self):
        r = self.http.get("/pair/devices",
                          headers={"Authorization": "Bearer " + bridge.BRIDGE_TOKEN})
        self.assertEqual(r.status_code, 200, r.text)

    # ── 可撤銷 ──

    def test_remote_console_is_visible_in_device_list(self):
        """機主拍板「可撤銷比可觀察重要」—— 清單要分得出這不是一台裝置。"""
        r = self.http.get("/pair/devices",
                          headers={"Authorization": "Bearer " + bridge.BRIDGE_TOKEN})
        rows = r.json()["devices"]
        mine = [d for d in rows if d.get("is_remote_console")]
        self.assertTrue(mine, "遠端控制台的 token 必須在清單上看得到")
        self.assertEqual(mine[0]["scope"], bridge.CONSOLE_REMOTE_SCOPE)
        self.assertIsNotNone(mine[0]["expires"])

    # ── 不得死鎖 ──

    def test_authenticated_request_does_not_deadlock(self):
        """`_check_auth` 持 `_PAIR_LOCK` 時不得再去拿同一把鎖。

        2026-10-30 我第一版就是這樣寫的:`_check_auth` 在
        `with _PAIR_LOCK:` 裡呼叫 `_console_remote_expired()`,而那支內部
        又拿一次同一把鎖 —— `threading.Lock` 不可重入,結果是**自己鎖自己**。

        這個 bug 最惡劣的地方不是它會壞,是它**掛住**:沒有例外、沒有錯誤碼、
        沒有逾時,請求就停在那裡。而且單跑最早那幾條測試是綠的(middleware
        先回 403/401,根本走不到 `_check_auth`),要到第一條打 `/file` 的
        才踩到 —— 「單跑綠、合跑死」。

        所以這條用 worker thread + join 逾時把它**轉成一個會失敗的測試**,
        而不是讓整套測試(或 production 的那個請求)無聲掛死。
        """
        import threading
        done, err = [], []

        def go():
            try:
                done.append(self.http.get(
                    "/file", params={"path": self.ok},
                    headers=self._hdr()).status_code)
            except Exception as e:          # noqa: BLE001 —— 要連進去一起報
                err.append(e)

        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(20)
        self.assertFalse(t.is_alive(),
                         "帶 console-remote token 的請求掛住了 —— "
                         "幾乎一定是 _PAIR_LOCK 被重入(見本測試 docstring)")
        self.assertFalse(err, f"請求丟例外:{err}")
        self.assertEqual(done, [200])


if __name__ == "__main__":
    unittest.main()
