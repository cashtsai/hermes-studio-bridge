"""GET /app/v1/dashboard 聚合端點測試(儀表板切片)。

跑法同其他切片:POCKET_CANON_DB 指到 tmp 庫再 import bridge。驗項:
401(壞 token)/ oracle 缺席與過期 → null / 天氣 30 分鐘快取(不重複外呼、
失敗回舊資料)/ approvals pending 數與前 5 筆 / payload 形狀。
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime

_TMP = tempfile.mkdtemp(prefix="dash-canon-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _FakeReq:
    def __init__(self, token=None):
        tok = token if token is not None else os.environ["BRIDGE_TOKEN"]
        self.headers = {"authorization": f"Bearer {tok}"}
        self.client = type("C", (), {"host": "127.0.0.1"})()

    class _URL:
        path = "/app/v1/dashboard"
    url = _URL()


def _insert_pending(aid, source="tg-post", created=None):
    con = sqlite3.connect(bridge.CANON_DB)
    now = created if created is not None else time.time()
    con.execute(
        "INSERT OR REPLACE INTO approvals"
        "(id,title,source,risk,detail,created_at,expires_at,status)"
        " VALUES(?,?,?,?,?,?,?,'pending')",
        (aid, f"t-{aid}", source, "low", "d", now, now + 600))
    con.commit()
    con.close()


def _stub_light(monkey_self):
    """把外部依賴(cc tmux / codex / launchctl / 天氣)換成便宜假件,
    只留 approvals/oracle 真路徑。回傳 restore closure。"""
    orig = (bridge._cc_sessions, bridge._dashboard_weather,
            bridge._dashboard_gateways, bridge.CODEX_APP.call)

    async def no_cc():
        return []

    async def no_weather():
        return None

    async def no_gw():
        return [{"label": l, "persona": p, "alive": None}
                for l, p in bridge._DASH_GATEWAYS]

    async def no_codex(*a, **k):
        return {"data": []}

    bridge._cc_sessions = no_cc
    bridge._dashboard_weather = no_weather
    bridge._dashboard_gateways = no_gw
    bridge.CODEX_APP.call = no_codex

    def restore():
        (bridge._cc_sessions, bridge._dashboard_weather,
         bridge._dashboard_gateways, bridge.CODEX_APP.call) = orig
    return restore


class TestAuth(unittest.TestCase):
    def test_bad_token_401(self):
        restore = _stub_light(self)
        try:
            with self.assertRaises(HTTPException) as cm:
                asyncio.run(bridge.app_dashboard(_FakeReq(token="wrong-token")))
            self.assertEqual(cm.exception.status_code, 401)
        finally:
            restore()

    def test_good_token_ok(self):
        restore = _stub_light(self)
        try:
            out = asyncio.run(bridge.app_dashboard(_FakeReq()))
        finally:
            restore()
        for key in ("generated_at", "weather", "oracle", "approvals",
                    "sessions", "health"):
            self.assertIn(key, out)
        self.assertEqual(
            set(out["sessions"]) - {"degraded"}, {"cc", "cx", "persona"})
        for kind in ("cc", "cx", "persona"):
            self.assertEqual(set(out["sessions"][kind]), {"working", "idle"})
        self.assertEqual(len(out["health"]["gateways"]), 4)
        self.assertIn("apns_configured", out["health"])
        self.assertIn("devices", out["health"])


class TestOracle(unittest.TestCase):
    def setUp(self):
        self._orig = bridge.ORACLE_STATE_FILE

    def tearDown(self):
        bridge.ORACLE_STATE_FILE = self._orig

    def _write(self, payload):
        path = os.path.join(_TMP, "daily-latest.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        bridge.ORACLE_STATE_FILE = path

    def test_missing_file_none(self):
        bridge.ORACLE_STATE_FILE = os.path.join(_TMP, "nope.json")
        self.assertIsNone(bridge._dashboard_oracle())

    def test_stale_date_none(self):
        self._write({"status": "ok", "date": "2000-01-01",
                     "timezone": "Asia/Taipei"})
        self.assertIsNone(bridge._dashboard_oracle())

    def test_bad_status_none(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self._write({"status": "error", "date": today})
        self.assertIsNone(bridge._dashboard_oracle())

    def test_ok_today_compact(self):
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")
        self._write({
            "status": "ok", "date": today, "timezone": "Asia/Taipei",
            "changing_lines": [5],
            "lines": [{"position": 5, "value": 9, "yin_yang": "yang",
                       "changing": True, "label": "老陽"}],
            "hexagrams": {"primary": {"number": 47, "name": "困", "theme": "守志"},
                          "relating": {"number": 40, "name": "解", "theme": "鬆綁"}},
            "interpretation": {"summary": "一句話", "attack_or_defend": "宜守",
                               "advice": "建言", "biggest_risk": "風險",
                               "one_thing_to_push": "推進",
                               "personal_context": {"known_factors": ["秘密"]}},
        })
        o = bridge._dashboard_oracle()
        self.assertEqual(o["summary"], "一句話")
        self.assertEqual(o["attack_or_defend"], "宜守")
        self.assertEqual(o["primary"], {"number": 47, "name": "困", "theme": "守志"})
        self.assertEqual(o["relating"]["name"], "解")
        self.assertEqual(o["changing_lines"], [5])
        self.assertEqual(o["changing_labels"], ["第5爻 老陽"])
        # 個人底盤(personal_context/seed)絕不外流
        self.assertNotIn("秘密", json.dumps(o, ensure_ascii=False))


class TestWeatherCache(unittest.TestCase):
    def setUp(self):
        self._orig_fetch = bridge._weather_fetch_cities
        self._orig_cache = dict(bridge._WEATHER_CACHE)
        bridge._WEATHER_CACHE["at"] = 0.0
        bridge._WEATHER_CACHE["data"] = None
        self.calls = 0

        async def fake_fetch():
            self.calls += 1
            return [{"id": "taipei", "name": "台北", "temp_max": 34.0,
                     "temp_min": 27.0, "precip_prob": 10}]
        bridge._weather_fetch_cities = fake_fetch

    def tearDown(self):
        bridge._weather_fetch_cities = self._orig_fetch
        bridge._WEATHER_CACHE.update(self._orig_cache)

    def test_ttl_no_refetch(self):
        w1 = asyncio.run(bridge._dashboard_weather())
        w2 = asyncio.run(bridge._dashboard_weather())
        self.assertEqual(self.calls, 1)          # 30 分鐘內不重打外部 API
        self.assertEqual(w1, w2)
        self.assertEqual(w1["cities"][0]["id"], "taipei")

    def test_expired_refetch(self):
        asyncio.run(bridge._dashboard_weather())
        bridge._WEATHER_CACHE["at"] -= bridge._WEATHER_TTL + 1
        asyncio.run(bridge._dashboard_weather())
        self.assertEqual(self.calls, 2)

    def test_failure_returns_stale(self):
        asyncio.run(bridge._dashboard_weather())
        stale = bridge._WEATHER_CACHE["data"]

        async def boom():
            raise RuntimeError("net down")
        bridge._weather_fetch_cities = boom
        bridge._WEATHER_CACHE["at"] -= bridge._WEATHER_TTL + 1
        w = asyncio.run(bridge._dashboard_weather())
        self.assertEqual(w, stale)               # 失敗回舊資料,不清快取

    def test_never_succeeded_none(self):
        async def boom():
            raise RuntimeError("net down")
        bridge._weather_fetch_cities = boom
        self.assertIsNone(asyncio.run(bridge._dashboard_weather()))


class TestApprovals(unittest.TestCase):
    def setUp(self):
        con = sqlite3.connect(bridge.CANON_DB)
        con.execute("DELETE FROM approvals")
        con.commit()
        con.close()

    def test_pending_count_and_top5(self):
        base = time.time()
        for i in range(7):
            _insert_pending(f"dash-a{i}", created=base + i)
        d = bridge._dashboard_approvals()
        self.assertEqual(d["pending"], 7)
        self.assertEqual(len(d["items"]), 5)
        # 最新在前(created_at DESC)
        self.assertEqual(d["items"][0]["id"], "dash-a6")
        # 統一物件形狀(_approval_row)含 options 預設鍵
        keys = {o["key"] for o in d["items"][0]["options"]}
        self.assertEqual(keys, {"approve", "deny"})

    def test_empty(self):
        d = bridge._dashboard_approvals()
        self.assertEqual(d["pending"], 0)
        self.assertEqual(d["items"], [])


if __name__ == "__main__":
    unittest.main()
