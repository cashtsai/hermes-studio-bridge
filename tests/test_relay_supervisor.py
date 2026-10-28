"""中繼隧道(frpc)主機端 supervisor。

伺服器端在 ~/apps/pocket-id(frps + 收費閘);這裡釘的是 bridge 這一側:
開關、授權快取、frpc.toml 內容、以及「frpc 掛掉之後怎麼決定下一步」。

**這組的重點是安全預設,不是 happy path** —— 開著等於把本機 8081 常態
暴露到公網,所以「什麼時候**不會**開」比「開起來會怎樣」更值得釘。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(見 tests/_isolation.py)
import asyncio
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

GRANT = {
    "grant_id": "g1",
    "subdomain": "uabc1234567",
    "hostname": "uabc1234567.pocket.shan.house",
    "frp_token": "tok-secret",
    "frp_server": "203.0.113.9",
    "frp_port": 7000,
}


class RelayTestBase(unittest.TestCase):
    """每個測試自己一個 POCKET_RELAY_DIR。

    路徑在 bridge 裡是**函式求值**(不是模組常數),所以改 env 當場生效 ——
    這正是為了躲開 openclaw.json / pocket-id-enrollment.json 那類
    「import 當下綁死 → 測試寫進真家目錄」的事故。
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="relay-test-")
        self._prev = os.environ.get("POCKET_RELAY_DIR")
        os.environ["POCKET_RELAY_DIR"] = self._tmp

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("POCKET_RELAY_DIR", None)
        else:
            os.environ["POCKET_RELAY_DIR"] = self._prev

    def enable(self):
        open(bridge._relay_setting_path(), "w").close()


class TestPathsFollowEnv(RelayTestBase):
    def test_env_change_takes_effect_immediately(self):
        """模組常數版本會在這裡失敗 —— 那正是要擋的事故形態。"""
        self.assertTrue(bridge._relay_dir().startswith(self._tmp))
        other = tempfile.mkdtemp(prefix="relay-other-")
        os.environ["POCKET_RELAY_DIR"] = other
        self.assertTrue(bridge._relay_grant_path().startswith(other))


class TestEnabledGate(RelayTestBase):
    """預設關。善彰 2026-10-28 拍板:只認明確開關,有訂閱也不自動開。"""

    def test_default_is_off(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("POCKET_RELAY_ENABLED", None)
            self.assertFalse(bridge._relay_enabled())

    def test_flag_file_turns_it_on(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("POCKET_RELAY_ENABLED", None)
            self.enable()
            self.assertTrue(bridge._relay_enabled())

    def test_env_off_beats_flag_file(self):
        """ops 要能一鍵壓掉,不必去猜旗標檔在哪。"""
        self.enable()
        with patch.dict(os.environ, {"POCKET_RELAY_ENABLED": "0"}):
            self.assertFalse(bridge._relay_enabled())

    def test_env_on_without_flag_file(self):
        with patch.dict(os.environ, {"POCKET_RELAY_ENABLED": "1"}):
            self.assertTrue(bridge._relay_enabled())


class TestGrantStorage(RelayTestBase):
    def test_grant_file_is_0600(self):
        """frp_token = 進隧道的鑰匙,而且明文只發一次。"""
        bridge._relay_save_grant(GRANT)
        mode = stat.S_IMODE(os.stat(bridge._relay_grant_path()).st_mode)
        self.assertEqual(mode, 0o600)

    def test_roundtrip(self):
        bridge._relay_save_grant(GRANT)
        self.assertEqual(bridge._relay_load_grant()["subdomain"], GRANT["subdomain"])

    def test_corrupt_grant_reads_as_absent(self):
        """半寫入的檔不可以讓 supervisor 拿空 token 去連 —— 當作沒有,重新 claim。"""
        os.makedirs(bridge._relay_dir(), exist_ok=True)
        with open(bridge._relay_grant_path(), "w") as f:
            f.write("{not json")
        self.assertEqual(bridge._relay_load_grant(), {})

    def test_grant_without_token_reads_as_absent(self):
        os.makedirs(bridge._relay_dir(), exist_ok=True)
        with open(bridge._relay_grant_path(), "w") as f:
            json.dump({"subdomain": "x"}, f)
        self.assertEqual(bridge._relay_load_grant(), {})


class TestConfWriting(RelayTestBase):
    def test_conf_shape_and_mode(self):
        bridge._relay_write_conf(GRANT)
        text = open(bridge._relay_conf_path(), encoding="utf-8").read()
        self.assertIn('serverAddr = "203.0.113.9"', text)
        self.assertIn("serverPort = 7000", text)
        self.assertIn('token = "tok-secret"', text)
        self.assertIn("localIP = \"127.0.0.1\"", text)
        mode = stat.S_IMODE(os.stat(bridge._relay_conf_path()).st_mode)
        self.assertEqual(mode, 0o600)

    def test_tunnel_points_at_loopback_only(self):
        """frpc 只能轉送到 127.0.0.1 —— 寫成 0.0.0.0 等於把整個網段也端出去。"""
        bridge._relay_write_conf(GRANT)
        text = open(bridge._relay_conf_path(), encoding="utf-8").read()
        self.assertNotIn("0.0.0.0", text)

    def test_local_port_follows_env(self):
        with patch.dict(os.environ, {"POCKET_RELAY_LOCAL_PORT": "9099"}):
            bridge._relay_write_conf(GRANT)
        self.assertIn("localPort = 9099",
                      open(bridge._relay_conf_path(), encoding="utf-8").read())


class TestClaim(RelayTestBase):
    def test_402_becomes_unentitled(self):
        """沒訂閱不是例外狀況,是「這條路現在不開放」—— 要能被上層分開處理。"""
        class _Resp:
            status_code = 402

            def raise_for_status(self):
                raise AssertionError("402 不該走到 raise_for_status")

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                return _Resp()

        with patch.object(bridge, "_pocket_id_state", lambda: {"host_id": "h",
                                                               "host_secret": "s"}), \
             patch("httpx.AsyncClient", lambda *a, **k: _Client()):
            with self.assertRaises(bridge._RelayUnentitled):
                asyncio.run(bridge._relay_claim())


class TestFrpcDiscovery(RelayTestBase):
    def test_missing_binary_reports_none(self):
        """找不到就回 None,讓上層講人話 —— 不要靜默什麼都不做。"""
        with patch.dict(os.environ, {"POCKET_FRPC_BIN": "/nonexistent/frpc"}), \
             patch("shutil.which", lambda _n: None), \
             patch("os.path.isfile", lambda _p: False):
            self.assertIsNone(bridge._relay_frpc_bin())

    def test_explicit_env_wins(self):
        fake = os.path.join(self._tmp, "frpc")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(fake, 0o755)
        with patch.dict(os.environ, {"POCKET_FRPC_BIN": fake}):
            self.assertEqual(bridge._relay_frpc_bin(), os.path.abspath(fake))


class TestStatusTruthfulness(RelayTestBase):
    """燈號必須反映**隧道**,不是開關的位置。

    這兩件事會不一致(按了開但沒訂閱 / token 被撤 / frpc 不在),而那個落差
    正是使用者唯一需要看到的東西 —— 用開關的位置假裝成狀態就是騙人。
    """

    def setUp(self):
        super().setUp()
        self._saved = dict(bridge._RELAY_STATUS)

    def tearDown(self):
        bridge._RELAY_STATUS.clear()
        bridge._RELAY_STATUS.update(self._saved)
        super().tearDown()

    def test_enabled_and_state_are_separate_fields(self):
        self.enable()
        bridge._relay_set_status("unentitled", detail="沒訂閱")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("POCKET_RELAY_ENABLED", None)
            p = bridge._relay_status_payload()
        self.assertTrue(p["enabled"])          # 使用者的意圖
        self.assertEqual(p["state"], "unentitled")   # 現實
        self.assertIn("訂閱", p["detail"])

    def test_proxy_rejection_is_not_connected(self):
        """frpc 行程活著 ≠ 隧道通 —— NewProxy 被拒時行程照樣在重試。"""
        bridge._relay_set_status("connected")
        bridge._relay_set_status("rejected", detail="start error: not entitled")
        self.assertEqual(bridge._RELAY_STATUS["state"], "rejected")

    def test_since_only_moves_on_real_change(self):
        """同一個狀態重複回報不該刷新時間戳,否則「已連線多久」永遠是 0 秒。"""
        bridge._relay_set_status("connected")
        first = bridge._RELAY_STATUS["since"]
        bridge._relay_set_status("connected")
        self.assertEqual(bridge._RELAY_STATUS["since"], first)


class TestRelayEndpoints(RelayTestBase):
    """端點是 owner 專屬 —— 把本機服務端上公網,配對過的手機不該能代為決定。"""

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        self.client = TestClient(bridge.app)
        self.owner = {"Authorization": f"Bearer {bridge.BRIDGE_TOKEN}"}

    def test_status_requires_owner_token(self):
        r = self.client.get("/app/v1/relay/status",
                            headers={"Authorization": "Bearer wrong-token"})
        self.assertIn(r.status_code, (401, 403, 429))

    def test_enable_requires_owner_token(self):
        r = self.client.post("/app/v1/relay/enable", json={"enabled": True},
                             headers={"Authorization": "Bearer wrong-token"})
        self.assertIn(r.status_code, (401, 403, 429))
        self.assertFalse(os.path.exists(bridge._relay_setting_path()))

    def test_toggle_on_then_off(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("POCKET_RELAY_ENABLED", None)
            r = self.client.post("/app/v1/relay/enable", json={"enabled": True},
                                 headers=self.owner)
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["enabled"])
            self.assertTrue(os.path.exists(bridge._relay_setting_path()))

            r = self.client.post("/app/v1/relay/enable", json={"enabled": False},
                                 headers=self.owner)
            self.assertFalse(r.json()["enabled"])
            self.assertFalse(os.path.exists(bridge._relay_setting_path()))

    def test_off_twice_is_not_an_error(self):
        """冪等 —— 重複關不該 500(檔案本來就不在)。"""
        r = self.client.post("/app/v1/relay/enable", json={"enabled": False},
                             headers=self.owner)
        self.assertEqual(r.status_code, 200)

    def test_missing_field_is_400_not_silently_off(self):
        """漏欄位不可以被當成 false —— 那會讓一個打錯的請求安靜地關掉隧道。"""
        self.enable()
        r = self.client.post("/app/v1/relay/enable", json={}, headers=self.owner)
        self.assertEqual(r.status_code, 400)
        self.assertTrue(os.path.exists(bridge._relay_setting_path()))


class TestRejectionHandling(unittest.TestCase):
    """frps 的兩種拒絕理由,處理方式相反 —— 分錯就是無限重試或永遠不重試。

    pocket-id 2026-10-28 才把這兩種理由拆開(原本都叫 not entitled),
    這組測試釘的就是「拆開之後 bridge 真的有分開處理」。
    """

    def test_stale_token_hint_triggers_reclaim(self):
        self.assertTrue(any(h in "error: unknown or revoked relay token"
                            for h in bridge._RELAY_STALE_TOKEN_HINTS))

    def test_inactive_subscription_is_not_a_reclaim(self):
        msg = "new proxy [bridge] error: subscription inactive"
        self.assertFalse(any(h in msg for h in bridge._RELAY_STALE_TOKEN_HINTS))
        self.assertTrue(any(h in msg for h in bridge._RELAY_UNENTITLED_HINTS))


if __name__ == "__main__":
    unittest.main()
