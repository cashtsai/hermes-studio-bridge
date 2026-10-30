"""QR 登入的來源綁定(規格 CONSOLE_QR_LOGIN_SPEC §8.2)。

**要守的那句話:看到 QR 不等於能登入。**

QR 會被拍照、被轉傳、被投影在會議室螢幕上。`rid` 就印在上面,所以它不能是
憑證。真正的綁定有兩層,而且**兩層都在輪詢那一支**(批准只標記狀態):

  1. **httpOnly 的 sid cookie** —— 只有發起登入的那個瀏覽器有。
  2. **來源一致性** —— 輪詢的來源必須等於建立 QR 時的來源。
     所以把整份 cookie 連同 rid 一起搬到別的網路也沒用。

為什麼要獨立一檔釘:這是個**組起來才看得出來**的性質。
`_console_login_create` 正確、`approve` 正確、`console_login_status` 正確,
三個元件各自的測試都綠 —— 但只要有人在輪詢那支少比對一次 client key,
整條鏈就退化成「誰看到螢幕誰就能登入」,而且**不會有任何錯誤**。
端到端實測(2026-10-30)時發現原本沒有任何測試覆蓋這條。

另外釘住兩個刻意的設計:
  · 來源不符時**回「過期」而不是專屬錯誤碼** —— 不給人當探測器用。
  · 憑證**一次性**(state → consumed),重放拿不到第二次。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="console-src-bind-")
os.environ["PAIR_BOOT_CODE_FILE"] = os.path.join(_TMP, "pair-boot-code")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["POCKET_CONSOLE_ENABLED"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

bridge._PAIR_BOOT_CODE_FILE = os.environ["PAIR_BOOT_CODE_FILE"]
with bridge._PAIR_BOOT_CODE_LOCK:
    bridge._PAIR_BOOT_CODE_CACHE["code"] = ""

#: 帶 XFF = 經過 proxy/tunnel = 對 `_pair_local_only` 就是遠端來源。
#: 兩個不同的值 = 兩個不同的網路(來源一致性比對看的正是 XFF 最左那段)。
_BROWSER = {"X-Forwarded-For": "203.0.113.9"}      # 真正發起登入的人
_ELSEWHERE = {"X-Forwarded-For": "198.51.100.7"}   # QR 被轉傳到的另一個網路
_OWNER = {"Authorization": "Bearer %s" % os.environ["BRIDGE_TOKEN"]}
_RID_RE = re.compile(r"pocket://console-login\?rid=([A-Za-z0-9._~%-]+)")


class SourceBindingTest(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(bridge, "POCKET_CONSOLE_ENABLED", True),
            patch.object(bridge, "POCKET_CONSOLE_QR_LOGIN", True),
            patch.object(bridge, "_pair_tunnel_url", return_value=None),
        ]
        for p in self.patches:
            p.start()
        with bridge._CONSOLE_LOGIN_LOCK:
            bridge._CONSOLE_LOGIN_REQS.clear()
            bridge._CONSOLE_LOGIN_MINTS.clear()
        self.http = TestClient(bridge.app, client=("127.0.0.1", 50000))

    def tearDown(self):
        self.http.close()
        for p in reversed(self.patches):
            p.stop()
        with bridge._CONSOLE_LOGIN_LOCK:
            bridge._CONSOLE_LOGIN_REQS.clear()
            bridge._CONSOLE_LOGIN_MINTS.clear()

    def _start_login(self, headers):
        """遠端來源開 /console → 拿到 QR 頁。回 (rid, sid cookie 值)。"""
        r = self.http.get("/console", headers=headers)
        m = _RID_RE.search(r.text)
        self.assertIsNotNone(m, f"QR 頁裡找不到 rid:{r.text[:200]}")
        sid = r.cookies.get(bridge._CONSOLE_LOGIN_SID_COOKIE)
        self.assertTrue(sid, "沒有種下 sid cookie —— 之後輪詢認不出是誰")
        return m.group(1), sid

    def _approve(self, rid):
        r = self.http.post(f"/app/v1/console/login/requests/{rid}/approve",
                           headers=_OWNER)
        self.assertEqual(r.status_code, 200, r.text[:200])

    def _poll(self, headers, sid=None, rid=None):
        """⚠️ **每次都先清 TestClient 的 cookie jar。**

        TestClient 會像瀏覽器一樣自動保存並回送 cookie —— `_start_login` 種下的
        sid 會黏在這個 client 上。不清的話「沒有 cookie 的攻擊者」其實揣著受害者
        的 cookie,測出來是**假的紅燈**(2026-10-30 第一版就這樣誤報了一次)。
        cookie 一律由 `sid=` 參數顯式控制,不靠 jar。
        """
        self.http.cookies.clear()
        h = dict(headers)
        if sid:
            h["Cookie"] = f"{bridge._CONSOLE_LOGIN_SID_COOKIE}={sid}"
        params = {"rid": rid} if rid else None
        return self.http.get("/console/login/status", headers=h, params=params)

    # ── 1. 沒有 cookie = 沒有憑據,rid 幫不上忙 ──────────────────────

    def test_rid_alone_is_not_a_credential(self):
        """看到 QR 的人有 rid,但沒有那張 httpOnly cookie。

        這是最直接的攻擊:把螢幕上的 QR 拍下來,自己去輪詢。
        """
        rid, _sid = self._start_login(_BROWSER)
        self._approve(rid)

        # 同一個網路、同樣帶著 rid,就是沒有 cookie。
        r = self._poll(_BROWSER, rid=rid)
        self.assertNotEqual(r.json().get("state"), "approved",
                            "光靠 rid 就領到憑證 —— 看到 QR 就能登入")
        self.assertNotIn("set-cookie", {k.lower() for k in r.headers},
                         "不該發任何 cookie")

    # ── 2. 有 cookie 但換了網路 = QR/cookie 被搬走 ────────────────

    def test_same_cookie_from_another_network_is_rejected(self):
        """整份 cookie 被搬到別的網路也不行(規格 §8.2 的字面要求)。

        這條擋的是「連 cookie 一起外洩」那種更強的攻擊,也擋掉
        「把 QR 轉傳給同事、他在自己家裡開」這種日常誤用。
        """
        rid, sid = self._start_login(_BROWSER)
        self._approve(rid)

        r = self._poll(_ELSEWHERE, sid=sid)
        self.assertNotEqual(r.json().get("state"), "approved",
                            "換了來源仍放行 —— 來源一致性比對沒作用")

    def test_mismatch_looks_like_expiry_not_a_distinct_error(self):
        """來源不符要回「過期」,**不要**回專屬錯誤碼。

        回 "client_mismatch" 之類的等於告訴攻擊者「rid 是對的、只差來源」,
        把這支變成探測器。刻意讓兩種失敗長得一樣。
        """
        rid, sid = self._start_login(_BROWSER)
        self._approve(rid)

        mismatched = self._poll(_ELSEWHERE, sid=sid).json()
        unknown = self._poll(_ELSEWHERE, sid="not-a-real-sid").json()
        self.assertEqual(mismatched.get("state"), unknown.get("state"),
                         f"兩種失敗要長一樣,實際 {mismatched} vs {unknown}")

    # ── 3. 正確的路徑要真的走得通,否則上面全是假的安全 ────────────

    def test_originating_browser_succeeds(self):
        """同 cookie、同來源、已批准 → 必須成功。

        少了這條,上面三條可以靠「永遠拒絕」通過 —— 那不是安全,是壞掉。
        """
        rid, sid = self._start_login(_BROWSER)
        self._approve(rid)

        r = self._poll(_BROWSER, sid=sid)
        self.assertEqual(r.json().get("state"), "approved", r.text[:200])

    def test_credential_is_single_use(self):
        """領過就不能再領(state → consumed)。"""
        rid, sid = self._start_login(_BROWSER)
        self._approve(rid)

        first = self._poll(_BROWSER, sid=sid).json()
        self.assertEqual(first.get("state"), "approved")
        second = self._poll(_BROWSER, sid=sid).json()
        self.assertNotEqual(second.get("state"), "approved",
                            "憑證可以重複領 —— 重放就能再登入一次")

    def test_pending_before_approval(self):
        """還沒批准時是 pending,不是 approved。"""
        rid, sid = self._start_login(_BROWSER)
        self.assertEqual(self._poll(_BROWSER, sid=sid).json().get("state"),
                         "pending")


if __name__ == "__main__":
    unittest.main()
