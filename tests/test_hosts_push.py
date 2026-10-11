"""候選網址變更 → 真靜默推播(`feat/push-host-candidates`)。

免費 quick tunnel **每次重啟就換網址**,而配對 QR 是一次性的 —— 網址一變,
手機就連不回來,使用者只能回桌面重掃。原本的設計靠 CloudKit 同步
`Device.hostCandidates`,但 CloudKit 在出貨的 kernel build 是關掉的,
所以出貨版沒有任何管道知道新網址。

改用已經存在的通道:手機早就註冊了 APNs token,網址變了就推一則靜默推播。

這支釘住四件會寫壞的事:
  1. **真靜默** —— push-type=background、priority=5、aps 只有
     content-available。帶了 alert/sound 的話使用者每次換網址都會收到一則
     莫名的通知,而且 Apple 會因為 push-type 與 payload 不符而拒收。
  2. **第一次觀測不推** —— 剛部署時 last 是空的,照「不同就推」會對所有裝置
     發一輪沒意義的推播。
  3. 候選算不出來(headless/無網)時**不可以**把基準清掉,否則下次一有網就
     誤判成「變了」。
  4. 沒有裝置可推時基準仍要跟上,免得之後補推一筆過期的清單。
"""
import _isolation  # noqa: F401
import asyncio
import json
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="hosts-push-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["POCKET_HOSTS_PUSH_STATE"] = os.path.join(_TMP, "last-pushed-hosts.json")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

STATE = os.environ["POCKET_HOSTS_PUSH_STATE"]


def _read_state():
    with open(STATE, encoding="utf-8") as f:
        return json.load(f)


# 模組常數在 import 時就綁了 env,確認真的指到暫存檔(不要污染機主的 ~/.pocket)。
assert bridge._HOSTS_PUSH_STATE == STATE, bridge._HOSTS_PUSH_STATE


class SilentPayloadTest(unittest.TestCase):
    """直接檢查送出去的 headers / payload —— 這是「真靜默」的唯一判準。"""

    def _capture(self, **kw):
        seen = {}

        class FakeResp:
            status_code = 200
            text = ""

        class FakeClient:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *a):
                return False

            async def post(self_inner, url, headers=None, json=None):
                seen["headers"] = headers
                seen["payload"] = json
                return FakeResp()

        import httpx
        orig_client, orig_jwt = httpx.AsyncClient, bridge._apns_jwt
        httpx.AsyncClient = lambda *a, **k: FakeClient()
        bridge._apns_jwt = lambda: "fake-jwt"
        try:
            asyncio.run(bridge._apns_send("tok", "T", "B", {"kind": "hosts"}, **kw))
        finally:
            httpx.AsyncClient, bridge._apns_jwt = orig_client, orig_jwt
        return seen

    def test_silent_uses_background_type_and_bare_aps(self):
        seen = self._capture(silent=True)
        self.assertEqual(seen["headers"]["apns-push-type"], "background")
        self.assertEqual(seen["headers"]["apns-priority"], "5")
        aps = seen["payload"]["aps"]
        self.assertEqual(aps, {"content-available": 1},
                         "aps 只能帶 content-available —— 有 alert/sound 就不是靜默")
        self.assertEqual(seen["payload"]["kind"], "hosts", "data 要照樣帶上")

    def test_alert_path_unchanged(self):
        """既有的 alert 推播行為不可以被這刀改掉。"""
        seen = self._capture()
        self.assertEqual(seen["headers"]["apns-push-type"], "alert")
        self.assertEqual(seen["headers"]["apns-priority"], "10")
        self.assertIn("alert", seen["payload"]["aps"])
        self.assertEqual(seen["payload"]["aps"]["sound"], "default")

    def test_content_available_alone_stays_alert(self):
        """content_available=True 但 silent=False → 仍是 alert(順手喚醒)。"""
        seen = self._capture(content_available=True)
        self.assertEqual(seen["headers"]["apns-push-type"], "alert")
        self.assertIn("alert", seen["payload"]["aps"])
        self.assertEqual(seen["payload"]["aps"]["content-available"], 1)


class WatchLoopStateTest(unittest.TestCase):
    def setUp(self):
        try:
            os.remove(STATE)
        except FileNotFoundError:
            pass
        self.pushes = []
        self._orig_push = bridge.push_notify
        self._orig_devices = bridge._devices

        async def fake_push(*a, **kw):
            self.pushes.append(kw)
            return {"sent": 1, "total": 1, "failures": []}

        bridge.push_notify = fake_push

    def tearDown(self):
        bridge.push_notify = self._orig_push
        bridge._devices = self._orig_devices

    def _tick(self, hosts, devices=("tok",)):
        """跑迴圈的一輪 —— 把 sleep 短路掉,第二圈丟 CancelledError 收工。"""
        bridge._devices = lambda: list(devices)
        calls = {"n": 0}

        async def fake_sleep(_):
            calls["n"] += 1
            if calls["n"] > 1:
                raise asyncio.CancelledError

        orig_sleep, orig_cands = asyncio.sleep, bridge._pair_host_candidates
        asyncio.sleep = fake_sleep
        bridge._pair_host_candidates = lambda force=False: (list(hosts), False)
        try:
            asyncio.run(bridge._hosts_watch_loop())
        except asyncio.CancelledError:
            pass
        finally:
            asyncio.sleep, bridge._pair_host_candidates = orig_sleep, orig_cands

    def test_first_observation_seeds_without_pushing(self):
        self._tick(["http://10.0.0.2:8081", "https://a.example"])
        self.assertEqual(self.pushes, [], "第一次只建基準,不可以推")
        self.assertEqual(_read_state(),
                         ["http://10.0.0.2:8081", "https://a.example"])

    def test_change_after_seed_pushes_silently(self):
        bridge._hosts_push_save(["https://old.example"])
        self._tick(["https://new.example"])
        self.assertEqual(len(self.pushes), 1)
        kw = self.pushes[0]
        self.assertTrue(kw["silent"], "必須是靜默推播")
        self.assertEqual(kw["data"], {"kind": "hosts",
                                      "hosts": ["https://new.example"]})
        self.assertEqual(_read_state(), ["https://new.example"])

    def test_unchanged_does_not_push(self):
        bridge._hosts_push_save(["https://same.example"])
        self._tick(["https://same.example"])
        self.assertEqual(self.pushes, [])

    def test_empty_candidates_keep_baseline(self):
        """headless/無網 → 不可以把基準清掉,否則下次有網會誤判成「變了」。"""
        bridge._hosts_push_save(["https://keep.example"])
        self._tick([])
        self.assertEqual(self.pushes, [])
        self.assertEqual(_read_state(), ["https://keep.example"])

    def test_no_devices_still_advances_baseline(self):
        """沒裝置可推,但基準要跟上 —— 否則之後會補推一筆過期清單。"""
        bridge._hosts_push_save(["https://old.example"])
        self._tick(["https://new.example"], devices=())
        self.assertEqual(self.pushes, [])
        self.assertEqual(_read_state(), ["https://new.example"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
