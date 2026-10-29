"""`POST /app/v1/relay/enroll` —— 手機把 enroll token 交給 Mac(2026-10-29)。

## 為什麼需要這條路

中繼放行查的是**主機所屬帳號**有沒有權益。而主機原本只有一條註冊路徑:
桌面端用 Apple 登入。於是**匿名付費的人永遠註冊不了主機** —— 付了錢,
權益掛在交易帳號上,那個帳號名下沒有主機,`/v1/relay/claim` 照樣 402。

補上這條,匿名流程才完整:
  手機付款 → pocket-id session → 換 enroll token → 交給這裡 → 註冊主機。

## 這組守的紅線

**已經註冊過的機器,配對裝置不得改綁。** 否則一支配對過的手機就能把機主的
Mac 搬到別人的帳號底下 —— 那等於把機器送人。重綁一律要 owner token。
"""
import _isolation  # noqa: F401
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="relay-enroll-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["POCKET_ID_URL"] = "https://id.example.invalid"
os.environ["POCKET_ID_STATE_FILE"] = os.path.join(_TMP, "enroll.json")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class RelayEnrollHandoverTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        self.http = TestClient(bridge.app)
        self.owner = {"Authorization": "Bearer " + bridge.BRIDGE_TOKEN}
        self._real_register = bridge._pocket_id_register
        self._real_state = bridge._pocket_id_state
        self.registered = []

        async def fake_register(enroll_token=""):
            self.registered.append(enroll_token)
            self.state = {"host_id": "h-new", "host_secret": "s",
                          "pocket_id_pubkey": "k", "url": bridge.POCKET_ID_URL}

        self.state = {}
        bridge._pocket_id_register = fake_register
        bridge._pocket_id_state = lambda: self.state

    def tearDown(self):
        bridge._pocket_id_register = self._real_register
        bridge._pocket_id_state = self._real_state

    # ── 匿名流程的關鍵:配對裝置能完成第一次綁定 ──

    def test_first_enrollment_allowed_for_paired_device(self):
        """付款在手機上,owner token 在桌面 —— 要求 owner 等於匿名流程走不完。"""
        r = self.http.post("/app/v1/relay/enroll", headers=self.owner,
                           json={"enroll_token": "et-abc"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["enrolled"])
        self.assertFalse(r.json()["rebound"])
        self.assertEqual(self.registered, ["et-abc"])

    def test_enroll_does_not_turn_relay_on(self):
        """註冊 ≠ 開中繼。「把本機端上公網」是 owner 的決定,不下放。"""
        r = self.http.post("/app/v1/relay/enroll", headers=self.owner,
                           json={"enroll_token": "et-abc"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("中繼仍需在桌面端由機主開啟", r.json()["note"])

    # ── 紅線:已註冊不得被配對裝置改綁 ──

    def test_rebind_refused_without_owner_token(self):
        """否則一支配對過的手機可以把機主的 Mac 搬到別人帳號底下。"""
        self.state = {"host_id": "h-existing"}
        r = self.http.post("/app/v1/relay/enroll",
                           headers={"Authorization": "Bearer pdev-some-device"},
                           json={"enroll_token": "et-evil"})
        self.assertIn(r.status_code, (401, 403, 409), r.text)
        self.assertEqual(self.registered, [], "不得真的去註冊")

    def test_rebind_allowed_with_owner_token(self):
        self.state = {"host_id": "h-existing"}
        r = self.http.post("/app/v1/relay/enroll", headers=self.owner,
                           json={"enroll_token": "et-ok"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["rebound"])
        self.assertEqual(self.registered, ["et-ok"])

    # ── 基本防呆 ──

    def test_missing_token_is_400(self):
        r = self.http.post("/app/v1/relay/enroll", headers=self.owner, json={})
        self.assertEqual(r.status_code, 400, r.text)

    def test_requires_auth(self):
        r = self.http.post("/app/v1/relay/enroll", json={"enroll_token": "x"})
        self.assertIn(r.status_code, (401, 403), r.text)

    def test_register_failure_is_502_not_500(self):
        """pocket-id 掛掉是上游問題,要讓 app 看得出來是對方的錯。"""
        async def boom(enroll_token=""):
            raise RuntimeError("pocket-id unreachable")
        bridge._pocket_id_register = boom
        r = self.http.post("/app/v1/relay/enroll", headers=self.owner,
                           json={"enroll_token": "et"})
        self.assertEqual(r.status_code, 502, r.text)


if __name__ == "__main__":
    unittest.main()
