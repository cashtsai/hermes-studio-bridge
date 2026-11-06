"""第二期 ActivityKit push(/app/v1/live-activity/register + milestone 掛鉤)。

不碰 Apple:httpx 以假客戶端頂替。釘住的契約:

1. register 落 activity_tokens 表;同 activity_id 重報冪等(token 覆蓋、
   單列);缺欄位/壞 device_env → 400;無 auth → 401;DELETE 清列。
2. milestone 掛鉤(_milestone_sink → _live_activity_on_milestone):
   turn begin / session.status busy → liveactivity update(payload 形狀、
   dev → sandbox host);節流(同 activity 3s 內不重推、內容沒變不推)。
3. turn end → event:end + dismissal-date(+120)+ 清表列;重複終態不重送。
4. APNs 410 → 清列、不 raise;無金鑰 → fanout 靜默短路,零網路呼叫。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import

import asyncio
import os
import sys
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ["APNS_LIVE_ACTIVITY_ACTIVITY_MIN_SECS"] = "3"

import bridge  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

AUTH = {"Authorization": "Bearer " + os.environ["BRIDGE_TOKEN"]}
SID = "codex:la-thread"


class _Response:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    responses: list = []
    calls: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.responses.pop(0) if self.responses else _Response(200)


async def _drive_milestone(session_id, etype, payload):
    """同步丟 milestone、等它排出的背景推播任務跑完。"""
    before = set(bridge._BG_TASKS)
    bridge._live_activity_on_milestone(session_id, etype, payload)
    new = [t for t in bridge._BG_TASKS if t not in before]
    if new:
        await asyncio.gather(*new)
    return new


class LiveActivityRegisterV1Test(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(bridge.app)
        bridge._ACTIVITY_PUSH_LAST.clear()
        bridge._ACTIVITY_ENDED.clear()
        bridge._activity_cache_invalidate()
        _FakeClient.calls.clear()
        _FakeClient.responses.clear()
        self._old = {
            "configured": bridge.apns_configured,
            "problem": bridge._push_http2_problem,
            "jwt": bridge._apns_jwt,
            "httpx": sys.modules.get("httpx"),
        }
        bridge.apns_configured = lambda: True
        bridge._push_http2_problem = lambda: None
        bridge._apns_jwt = lambda: "test-jwt"
        sys.modules["httpx"] = types.SimpleNamespace(AsyncClient=_FakeClient)

    def tearDown(self):
        bridge.apns_configured = self._old["configured"]
        bridge._push_http2_problem = self._old["problem"]
        bridge._apns_jwt = self._old["jwt"]
        if self._old["httpx"] is None:
            sys.modules.pop("httpx", None)
        else:
            sys.modules["httpx"] = self._old["httpx"]
        bridge.t3code_runtime.remove_session_activity_tokens(
            bridge.CANON_DB, SID)
        bridge._ACTIVITY_PUSH_LAST.clear()
        bridge._ACTIVITY_ENDED.clear()
        bridge._activity_cache_invalidate()

    # ── 小工具 ───────────────────────────────────────────────────────────
    def _register(self, activity_id="act-1", token="tok-1", env="prod",
                  session_id=SID):
        return self.client.post(
            "/app/v1/live-activity/register", headers=AUTH,
            json={"activity_id": activity_id, "push_token": token,
                  "session_id": session_id, "device_env": env})

    def _rows(self):
        return bridge.t3code_runtime.session_activity_tokens(
            bridge.CANON_DB, SID)

    # ── 1. register / 冪等 / delete ─────────────────────────────────────
    def test_register_idempotent_and_delete(self):
        self.assertEqual(self.client.post(
            "/app/v1/live-activity/register",
            json={"activity_id": "a", "push_token": "t",
                  "session_id": SID}).status_code, 401)
        bad = self.client.post(
            "/app/v1/live-activity/register", headers=AUTH,
            json={"activity_id": "a", "session_id": SID})
        self.assertEqual(bad.status_code, 400)   # 缺 push_token
        bad_env = self._register(env="staging")
        self.assertEqual(bad_env.status_code, 400)

        r = self._register(token="tok-old", env="dev")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["activity"]["device_env"], "dev")
        self.assertEqual(body["activity"]["bundle_id"], bridge.APNS_BUNDLE_ID)
        self.assertIn("apns_configured", body)

        # 同 activity 重報 → 冪等覆蓋,單列、token 更新
        self.assertEqual(self._register(token="tok-new", env="dev")
                         .status_code, 200)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["push_token"], "tok-new")

        # DELETE(帶 activity_id)清列;再 DELETE session 清光
        d = self.client.request(
            "DELETE", "/app/v1/live-activity/register", headers=AUTH,
            json={"activity_id": "act-1"})
        self.assertEqual(d.status_code, 200)
        self.assertEqual(d.json()["removed"], 1)
        self.assertEqual(self._rows(), [])
        d2 = self.client.request(
            "DELETE", "/app/v1/live-activity/register", headers=AUTH,
            json={})
        self.assertEqual(d2.status_code, 400)

    # ── 2. milestone → 推送(payload 形狀 + sandbox 分流)────────────────
    def test_turn_begin_pushes_liveactivity_update(self):
        self._register(env="dev")
        _FakeClient.responses.append(_Response(200))
        asyncio.run(_drive_milestone(
            SID, "turn", {"state": "begin", "turn_id": "turn-x"}))

        self.assertEqual(len(_FakeClient.calls), 1)
        call = _FakeClient.calls[0]
        self.assertTrue(call["url"].startswith(
            bridge.APNS_SANDBOX_HOST + "/3/device/tok-1"))
        self.assertEqual(call["headers"]["apns-push-type"], "liveactivity")
        self.assertEqual(call["headers"]["apns-topic"],
                         f"{bridge.APNS_BUNDLE_ID}.push-type.liveactivity")
        self.assertEqual(call["headers"]["apns-priority"], "5")
        aps = call["json"]["aps"]
        self.assertEqual(aps["event"], "update")
        self.assertNotIn("dismissal-date", aps)
        cs = aps["content-state"]
        self.assertTrue(cs["busy"])
        self.assertEqual(cs["phaseLabel"], "工作中…")
        self.assertIsNone(cs["endedLabel"])
        self.assertEqual(cs["sessionID"], SID)
        self.assertEqual(cs["state"], "running")
        self.assertFalse(cs["terminal"])

    def test_status_updates_are_throttled_and_deduped(self):
        self._register()
        _FakeClient.responses.extend([_Response(200)] * 4)

        async def run():
            await _drive_milestone(SID, "session.status",
                                   {"busy": True, "label": "思考中",
                                    "busy_since": 1234.5})
            # 內容沒變 → 不推
            await _drive_milestone(SID, "session.status",
                                   {"busy": True, "label": "思考中"})
            # 內容變了但 3s 內 → 節流
            await _drive_milestone(SID, "session.status",
                                   {"busy": True, "label": "執行工具:Bash"})

        asyncio.run(run())
        self.assertEqual(len(_FakeClient.calls), 1)
        cs = _FakeClient.calls[0]["json"]["aps"]["content-state"]
        self.assertEqual(cs["phaseLabel"], "思考中")
        self.assertEqual(cs["busySince"], 1234.5)

        # 退回節流窗之外 → 變化就推
        aid = self._rows()[0]["activity_id"]
        ts, sig = bridge._ACTIVITY_PUSH_LAST[aid]
        bridge._ACTIVITY_PUSH_LAST[aid] = (ts - 10, sig)
        asyncio.run(_drive_milestone(SID, "session.status",
                                     {"busy": True, "label": "執行工具:Bash"}))
        self.assertEqual(len(_FakeClient.calls), 2)

    # ── 3. turn end → 終態 + 清列;重複終態不重送 ───────────────────────
    def test_turn_end_sends_final_state_and_clears_row(self):
        self._register()
        _FakeClient.responses.extend([_Response(200), _Response(200)])

        async def run():
            await _drive_milestone(SID, "turn",
                                   {"state": "begin", "turn_id": "t1"})
            await _drive_milestone(SID, "turn",
                                   {"state": "end", "turn_id": "t1"})
            # 終態後的 status idle(同 tick 保底路徑)→ 列已清,不再推
            await _drive_milestone(SID, "session.status", {"busy": False})

        before = int(time.time())
        asyncio.run(run())
        self.assertEqual(len(_FakeClient.calls), 2)
        end_call = _FakeClient.calls[1]
        aps = end_call["json"]["aps"]
        self.assertEqual(aps["event"], "end")
        self.assertEqual(end_call["headers"]["apns-priority"], "10")
        self.assertAlmostEqual(aps["dismissal-date"], before + 120, delta=5)
        cs = aps["content-state"]
        self.assertFalse(cs["busy"])
        self.assertEqual(cs["endedLabel"], "已完成")
        self.assertEqual(cs["state"], "completed")
        self.assertTrue(cs["terminal"])
        self.assertEqual(self._rows(), [])   # 終態送達即清列

    def test_interrupted_turn_maps_to_failed_terminal(self):
        self._register()
        _FakeClient.responses.append(_Response(200))
        asyncio.run(_drive_milestone(
            SID, "turn", {"state": "interrupted", "turn_id": "t1"}))
        cs = _FakeClient.calls[0]["json"]["aps"]["content-state"]
        self.assertEqual(cs["endedLabel"], "已中斷")
        self.assertEqual(cs["state"], "failed")
        self.assertTrue(cs["terminal"])

    # ── 4. 410 清列、無金鑰靜默 ─────────────────────────────────────────
    def test_410_clears_row_without_raising(self):
        self._register()
        _FakeClient.responses.append(_Response(410, "BadDeviceToken"))
        results = asyncio.run(bridge._live_activity_fanout(
            SID, True, phase_label="工作中"))
        self.assertEqual(results[0]["status"], 410)
        self.assertFalse(results[0]["sent"])
        self.assertEqual(self._rows(), [])
        self.assertNotIn(SID, bridge._activity_sessions_cached())

    def test_missing_key_disables_feature_silently(self):
        self._register()
        bridge.apns_configured = lambda: False
        results = asyncio.run(bridge._live_activity_fanout(
            SID, True, phase_label="工作中"))
        self.assertEqual(results, [])
        self.assertEqual(_FakeClient.calls, [])
        # milestone 入口同樣不爆炸、零呼叫
        asyncio.run(_drive_milestone(SID, "turn", {"state": "begin"}))
        self.assertEqual(_FakeClient.calls, [])

    def test_unregistered_session_is_cheaply_skipped(self):
        # 沒註冊的 session:同步閘直接擋下,不排背景任務
        tasks = asyncio.run(_drive_milestone(
            "codex:nobody", "turn", {"state": "begin"}))
        self.assertEqual(tasks, [])
        self.assertEqual(_FakeClient.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
