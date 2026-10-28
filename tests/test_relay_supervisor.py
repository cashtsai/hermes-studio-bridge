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

    def test_unconfigured_is_distinct_from_off(self):
        """「沒設定」不可以跟「關著」共用狀態值。

        2026-10-28 端到端實測:plist 沒有 POCKET_ID_URL 時 _start_relay 直接
        return,狀態留在初始的 "off",使用者看到「開關=開、狀態=已關閉」——
        矛盾、沒解釋、怎麼按都不會變。兩者的下一步完全不同:一個是去按開關,
        一個是去接 Pocket ID。
        """
        self.enable()
        with patch.object(bridge, "POCKET_ID_URL", ""):
            asyncio.run(bridge._start_relay())
        p = bridge._relay_status_payload()
        self.assertEqual(p["state"], "not_configured")
        self.assertNotEqual(p["state"], "off")
        self.assertIn("Pocket ID", p["detail"])

    def test_cached_grant_runs_without_pocket_id(self):
        """**enrollment 只擋 claim,不擋執行**。

        已經有 grant 的機器(例如從 mac-join-relay.sh 手動加入的),那張 token
        就是 frpc 要的全部憑證。把它擋掉等於「pocket-id 暫時連不上 → 一條本來
        好好的隧道也被弄斷」,跟這個 repo 其他地方的紀律相反。
        """
        bridge._relay_save_grant(GRANT)
        self.enable()
        # _start_relay 會建立 supervisor task。這個測試只關心「它有沒有被擋下」,
        # 所以把 frpc 查找釘成 None —— 絕不讓測試行程有任何機會真的 spawn frpc
        # 去連線上的 frps。(隔離閂把 POCKET_RELAY_ENABLED 硬釘成 0 已經擋了一層,
        # 但安全性不該只靠單一防線。)
        with patch.object(bridge, "POCKET_ID_URL", ""), \
             patch.object(bridge, "_relay_frpc_bin", lambda: None):
            asyncio.run(bridge._start_relay())
        # 有 grant 就不該被判成「沒設定」—— supervisor 要照常啟動。
        self.assertNotEqual(bridge._relay_status_payload()["state"], "not_configured")

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


class TestOffSwitchActuallyStops(RelayTestBase):
    """**關掉必須真的關掉。**

    2026-10-28 端到端實測:按「關」之後旗標寫進去了、API 回 200、UI 顯示已關閉,
    但外網照樣 200 —— supervisor 卡在 `await _relay_run_frpc()` 裡,frpc 自己
    永遠不結束,所以迴圈回不到開關檢查,沒有任何東西去殺它。一個安全開關
    按了沒反應、還跟你說成功了,是這個功能最糟的失敗形態。

    這組用一個「永遠不會自己結束」的假 frpc 來釘:跟真的 frpc 同一個行為。
    """

    def _fake_frpc(self) -> str:
        path = os.path.join(self._tmp, "fake-frpc")
        with open(path, "w") as f:
            f.write("#!/bin/sh\nwhile true; do sleep 0.2; done\n")
        os.chmod(path, 0o755)
        return path

    def test_flag_off_terminates_frpc(self):
        bridge._relay_save_grant(GRANT)
        bridge._relay_write_conf(GRANT)
        binary = self._fake_frpc()

        async def scenario():
            enabled = {"v": True}
            with patch.object(bridge, "_relay_enabled", lambda: enabled["v"]), \
                 patch.object(bridge, "_RELAY_FLAG_POLL_SECS", 0.2):
                task = asyncio.create_task(bridge._relay_run_frpc(binary))
                await asyncio.sleep(0.6)
                self.assertFalse(task.done(), "開關還開著就不該結束")
                enabled["v"] = False       # 使用者按下「關」
                # 給它兩個輪詢週期 —— 真的會停的話這裡就會返回。
                await asyncio.wait_for(task, timeout=5)

        asyncio.run(scenario())

    def test_no_orphan_process_left_behind(self):
        """收屍要確實 —— 留一隻孤兒 frpc 等於開關關了但 Mac 還在公網上。"""
        bridge._relay_save_grant(GRANT)
        bridge._relay_write_conf(GRANT)
        binary = self._fake_frpc()
        seen = {}

        real_exec = asyncio.create_subprocess_exec

        async def spy(*a, **k):
            proc = await real_exec(*a, **k)
            seen["proc"] = proc
            return proc

        async def scenario():
            enabled = {"v": True}
            with patch.object(bridge, "_relay_enabled", lambda: enabled["v"]), \
                 patch.object(bridge, "_RELAY_FLAG_POLL_SECS", 0.2), \
                 patch.object(asyncio, "create_subprocess_exec", spy):
                task = asyncio.create_task(bridge._relay_run_frpc(binary))
                await asyncio.sleep(0.6)
                enabled["v"] = False
                await asyncio.wait_for(task, timeout=5)

        asyncio.run(scenario())
        self.assertIsNotNone(seen.get("proc"))
        self.assertIsNotNone(seen["proc"].returncode,
                             "frpc 行程還活著 —— 隧道沒真的關掉")


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


class TestAutoEnroll(RelayTestBase):
    """機主用 Apple 登入桌面端時順手 enroll 這台 Mac。

    **為什麼做在 bridge**:桌面端早就把 Apple identityToken 送過來了,
    同一張 JWT 就能跟 pocket-id 換 session —— 不必在桌面端新增 pocket-id
    client、不必多存一份 session,更不必把 enroll token 經過 plist
    (那是秘密,而且 plist 每次重裝都會被整份覆寫)。

    這組釘的是**失敗時的行為**:自動 enroll 是附帶動作,pocket-id 掛了、
    aud 沒設、限流擋了,使用者該做的事(登入桌面端)都必須照常完成。
    """

    def _client(self, auth_status=200, enroll_status=200, auth_json=None,
                enroll_json=None, raise_on=None):
        calls = []

        class _Resp:
            def __init__(self, status, payload):
                self.status_code = status
                self._p = payload

            def json(self):
                return self._p

        class _C:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *a):
                return False

            async def post(self_inner, url, **kw):
                calls.append(url)
                if raise_on and raise_on in url:
                    raise RuntimeError("boom")
                if url.endswith("/v1/auth/apple"):
                    return _Resp(auth_status,
                                 auth_json if auth_json is not None
                                 else {"session_token": "sess-1"})
                return _Resp(enroll_status,
                             enroll_json if enroll_json is not None
                             else {"enroll_token": "enr-1"})

        return _C, calls

    def _run(self, **kw):
        C, calls = self._client(**kw)
        registered = []

        async def fake_register(token=""):
            registered.append(token)

        with patch.object(bridge, "POCKET_ID_URL", "https://id.example.com"), \
             patch("httpx.AsyncClient", lambda *a, **k: C()), \
             patch.object(bridge, "_pocket_id_register", fake_register):
            asyncio.run(bridge._pocket_id_autoenroll("fake-identity-token"))
        return calls, registered

    def test_happy_path_registers_with_fresh_token(self):
        calls, registered = self._run()
        self.assertTrue(calls[0].endswith("/v1/auth/apple"))
        self.assertTrue(calls[1].endswith("/v1/enroll/new"))
        self.assertEqual(registered, ["enr-1"])

    def test_auth_rejected_does_not_register(self):
        """aud 沒設好就是 401 —— 不可以硬著頭皮往下走。"""
        _, registered = self._run(auth_status=401)
        self.assertEqual(registered, [])

    def test_rate_limited_enroll_is_not_fatal(self):
        """/v1/enroll/new 有每小時限流,429 只是「下次登入再說」。"""
        _, registered = self._run(enroll_status=429)
        self.assertEqual(registered, [])

    def test_missing_session_token_does_not_register(self):
        _, registered = self._run(auth_json={})
        self.assertEqual(registered, [])

    def test_network_error_is_swallowed(self):
        """pocket-id 連不上**絕不能讓桌面端登入失敗** —— 這是附帶動作。"""
        _, registered = self._run(raise_on="/v1/auth/apple")
        self.assertEqual(registered, [])
