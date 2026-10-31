"""console-remote 這條路**真的能做事** —— 不只是「擋得住」。

為什麼要獨立一檔:`test_console_remote_scope.py` 的 12 條幾乎都在驗「拒絕」,
唯一的正向測試打的是 `/health` —— 那條**任何 token 都過**,所以它證明不了
遠端瀏覽器能不能操作 agent。

少了這一檔,整組 scope 測試可以靠「**全部拒絕**」通過。那不是安全,是功能壞掉
而測試看不出來 —— 而付費賣的就是「從任意電腦開控制台」這件事,壞了等於賣空氣。

釘的是使用者實際會走的那幾步,每一步都必須**不是 403**:
  列 session → 讀卡片流 → 送輸入 → 核准。

同時釘住反面:真正危險的端點仍然擋著。選的三個都有具體後果 ——
  · `/app/v1/relay/*`:能開關公網曝光。借來的電腦不該決定機主的曝光面。
  · `/clientlog`:寫入端點,能灌垃圾進機主的診斷日誌。
  · `/pair/*`:能發新的配對憑證,等於自我提權。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import os
import sys
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="console-remote-usable-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

#: 403 SCOPE_FORBIDDEN 是「這條路沒這個權限」。其他碼(401/404/405/422/200)
#: 都代表**請求抵達了 handler**,那就是這一檔要證明的事 —— 有沒有 session、
#: 參數對不對是另一回事,不該混進權限的判斷。
def _reached_handler(resp) -> bool:
    return not (resp.status_code == 403 and "SCOPE_FORBIDDEN" in resp.text)


class ConsoleRemoteUsableTest(unittest.TestCase):
    TOKEN = "pdev-console-remote-usable"

    def setUp(self):
        self.http = TestClient(bridge.app)
        with bridge._PAIR_LOCK:
            bridge._DEVICE_TOKENS[self.TOKEN] = {
                "name": "borrowed laptop", "platform": "web",
                "scope": bridge.CONSOLE_REMOTE_SCOPE,
                "expires": time.time() + 600,
                "created": time.time(), "last_seen": time.time(),
            }

    def tearDown(self):
        # 同 test_console_remote_scope:不用 `with` —— 踩到鎖重入時這裡會卡死,
        # 整套測試掛掉且看不到任何失敗訊息。
        if bridge._PAIR_LOCK.acquire(timeout=5):
            try:
                bridge._DEVICE_TOKENS.pop(self.TOKEN, None)
            finally:
                bridge._PAIR_LOCK.release()
        else:
            bridge._DEVICE_TOKENS.pop(self.TOKEN, None)
            print("⚠️ tearDown 拿不到 _PAIR_LOCK(5s)")

    def _hdr(self):
        return {"Authorization": "Bearer " + self.TOKEN}

    # ── 正向:使用者實際會走的那幾步 ───────────────────────────────

    def test_can_list_sessions(self):
        """第一步:開了控制台要看得到有哪些 agent 在跑。"""
        r = self.http.get("/app/v2/sessions", headers=self._hdr())
        self.assertTrue(_reached_handler(r), f"列 session 被 scope 擋掉:{r.text[:200]}")

    def test_can_read_a_session_card_stream(self):
        """第二步:點進去要看得到內容。"""
        r = self.http.get("/app/v2/sessions/demo/cards", headers=self._hdr())
        self.assertTrue(_reached_handler(r), f"讀卡片流被擋:{r.text[:200]}")

    def test_can_send_input(self):
        """第三步:**這是整個產品的重點** —— 遠端要能真的指揮 agent。

        被 scope 擋掉的話,控制台就只是個唯讀觀景窗,付費賣的東西不存在。
        """
        r = self.http.post("/app/v2/sessions/demo/input",
                           headers=self._hdr(), json={"text": "hello"})
        self.assertTrue(_reached_handler(r), f"送輸入被擋:{r.text[:200]}")

    def test_can_reach_approvals(self):
        """第四步:agent 要權限時要能在遠端按同意。"""
        r = self.http.get("/app/v1/approvals", headers=self._hdr())
        self.assertTrue(_reached_handler(r), f"核准被擋:{r.text[:200]}")

    def test_can_load_the_console_itself(self):
        r = self.http.get("/console/manifest.json", headers=self._hdr())
        self.assertTrue(_reached_handler(r), f"控制台資源被擋:{r.text[:200]}")

    # ── 反面:危險的仍然擋著(每條都有具體後果)─────────────────

    def test_cannot_toggle_relay_exposure(self):
        """借來的電腦不該能決定機主的公網曝光面。"""
        r = self.http.post("/app/v1/relay/enable", headers=self._hdr(),
                           json={"enabled": True})
        self.assertEqual(r.status_code, 403, r.text[:200])
        self.assertIn("SCOPE_FORBIDDEN", r.text)

    def test_cannot_write_client_log(self):
        """寫入端點 —— 能灌垃圾進機主的診斷日誌。"""
        r = self.http.post("/clientlog", headers=self._hdr(),
                           json={"msg": "junk"})
        self.assertEqual(r.status_code, 403, r.text[:200])

    def test_cannot_mint_pairing_credentials(self):
        """能發新配對憑證 = 自我提權,這條最嚴重。"""
        r = self.http.get("/pair/devices", headers=self._hdr())
        self.assertEqual(r.status_code, 403, r.text[:200])


if __name__ == "__main__":
    unittest.main()
