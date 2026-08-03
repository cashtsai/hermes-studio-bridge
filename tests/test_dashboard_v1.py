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
    def __init__(self, token=None, query=None):
        tok = token if token is not None else os.environ["BRIDGE_TOKEN"]
        self.headers = {"authorization": f"Bearer {tok}"}
        self.client = type("C", (), {"host": "127.0.0.1"})()
        self.query_params = dict(query or {})

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

    async def no_weather(cities=None):
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
            "hexagrams": {
                "primary": {"number": 47, "name": "困", "theme": "守志",
                            "upper": {"name": "兌", "image": "澤",
                                      "keywords": ["交流", "收口"]},
                            "lower": {"name": "坎", "image": "水",
                                      "keywords": ["風險", "守險"]}},
                "relating": {"number": 40, "name": "解", "theme": "鬆綁"},
            },
            "interpretation": {"summary": "一句話", "attack_or_defend": "宜守",
                               "advice": "建言", "biggest_risk": "風險",
                               "one_thing_to_push": "推進",
                               "personal_context": {"known_factors": ["秘密"]}},
        })
        o = bridge._dashboard_oracle()
        self.assertEqual(o["summary"], "一句話")
        self.assertEqual(o["attack_or_defend"], "宜守")
        self.assertEqual(
            o["hexagram_line"],
            "主卦：47.困（上兌澤 / 下坎水） / 變卦：40.解 / 動爻：九五",
        )
        self.assertIn("水在澤下", o["hexagram_reading"])
        self.assertIn("九五動", o["hexagram_reading"])
        self.assertEqual(o["primary"], {"number": 47, "name": "困", "theme": "守志"})
        self.assertEqual(o["relating"]["name"], "解")
        self.assertEqual(o["changing_lines"], [5])
        self.assertEqual(o["changing_labels"], ["第5爻 老陽"])
        # 個人底盤(personal_context/seed)絕不外流
        self.assertNotIn("秘密", json.dumps(o, ensure_ascii=False))


_BANGKOK = (("13.75,100.50", "曼谷", 13.75, 100.50, "Asia/Bangkok"),)
_TAIPEI = (("25.03,121.56", "台北", 25.03, 121.56, "Asia/Taipei"),)


class TestWeatherCache(unittest.TestCase):
    def setUp(self):
        self._orig_fetch = bridge._weather_fetch_cities
        self._orig_cache = dict(bridge._WEATHER_CACHE)
        bridge._WEATHER_CACHE.clear()
        self.calls = 0
        self.seen = []

        async def fake_fetch(cities=None):
            cities = bridge._DASH_WEATHER_CITIES if cities is None else tuple(cities)
            self.calls += 1
            self.seen.append(cities)
            return [{"id": cid, "name": name, "temp_max": 34.0,
                     "temp_min": 27.0, "precip_prob": 10}
                    for cid, name, _lat, _lon, _tz in cities]
        bridge._weather_fetch_cities = fake_fetch

    def tearDown(self):
        bridge._weather_fetch_cities = self._orig_fetch
        bridge._WEATHER_CACHE.clear()
        bridge._WEATHER_CACHE.update(self._orig_cache)

    def _age(self, cities, secs):
        """把某城市組的快取往前撥,模擬過期。"""
        bridge._WEATHER_CACHE[bridge._weather_cache_key(cities)]["at"] -= secs

    def test_ttl_no_refetch(self):
        w1 = asyncio.run(bridge._dashboard_weather())
        w2 = asyncio.run(bridge._dashboard_weather())
        self.assertEqual(self.calls, 1)          # 30 分鐘內不重打外部 API
        self.assertEqual(w1, w2)
        self.assertEqual(w1["cities"][0]["id"], "taipei")

    def test_expired_refetch(self):
        asyncio.run(bridge._dashboard_weather())
        self._age(bridge._DASH_WEATHER_CITIES, bridge._WEATHER_TTL + 1)
        asyncio.run(bridge._dashboard_weather())
        self.assertEqual(self.calls, 2)

    def test_failure_returns_stale(self):
        asyncio.run(bridge._dashboard_weather())
        key = bridge._weather_cache_key(bridge._DASH_WEATHER_CITIES)
        stale = bridge._WEATHER_CACHE[key]["data"]

        async def boom(cities=None):
            raise RuntimeError("net down")
        bridge._weather_fetch_cities = boom
        self._age(bridge._DASH_WEATHER_CITIES, bridge._WEATHER_TTL + 1)
        w = asyncio.run(bridge._dashboard_weather())
        self.assertEqual(w, stale)               # 失敗回舊資料,不清快取

    def test_never_succeeded_none(self):
        async def boom(cities=None):
            raise RuntimeError("net down")
        bridge._weather_fetch_cities = boom
        self.assertIsNone(asyncio.run(bridge._dashboard_weather()))

    # ── 城市在地化(去機主特化)────────────────────────────────────────

    def test_cache_key_separates_cities(self):
        """兩個不同城市各有自己的快取格 — 曼谷不會拿到台北的溫度。"""
        asyncio.run(bridge._dashboard_weather(_TAIPEI))
        asyncio.run(bridge._dashboard_weather(_BANGKOK))
        self.assertEqual(self.calls, 2)          # 各打一次,沒互相命中
        self.assertEqual(len(bridge._WEATHER_CACHE), 2)
        # 各自再打一次都命中快取
        asyncio.run(bridge._dashboard_weather(_TAIPEI))
        asyncio.run(bridge._dashboard_weather(_BANGKOK))
        self.assertEqual(self.calls, 2)
        self.assertEqual(
            asyncio.run(bridge._dashboard_weather(_BANGKOK))["cities"][0]["name"],
            "曼谷")

    def test_cache_key_ignores_label(self):
        """同座標換顯示名(中/英)不該重打外部 API。"""
        asyncio.run(bridge._dashboard_weather(_BANGKOK))
        renamed = (("13.75,100.50", "Bangkok", 13.75, 100.50, "Asia/Bangkok"),)
        asyncio.run(bridge._dashboard_weather(renamed))
        self.assertEqual(self.calls, 1)

    def test_default_cities_distinct_from_single(self):
        """雙城預設與單城台北是不同的快取鍵。"""
        asyncio.run(bridge._dashboard_weather())
        asyncio.run(bridge._dashboard_weather(_TAIPEI))
        self.assertEqual(self.calls, 2)

    def test_weather_off_no_fetch(self):
        """關閉天氣 → 直接 None,不外呼、不佔快取。"""
        self.assertIsNone(asyncio.run(bridge._dashboard_weather(())))
        self.assertEqual(self.calls, 0)
        self.assertEqual(len(bridge._WEATHER_CACHE), 0)

    def test_cache_evicts_oldest_when_full(self):
        """鍵由 client 控制 — 塞爆時丟最舊的,不無限長大。"""
        for i in range(bridge._WEATHER_CACHE_MAX + 5):
            lat = 1.0 + i * 0.5
            asyncio.run(bridge._dashboard_weather(
                ((f"c{i}", f"City {i}", lat, 100.0, "auto"),)))
        self.assertLessEqual(len(bridge._WEATHER_CACHE), bridge._WEATHER_CACHE_MAX)
        # 最後一筆一定還在
        last = ((f"c{bridge._WEATHER_CACHE_MAX + 4}", "x",
                 1.0 + (bridge._WEATHER_CACHE_MAX + 4) * 0.5, 100.0, "auto"),)
        self.assertIn(bridge._weather_cache_key(last), bridge._WEATHER_CACHE)


class TestWeatherCityParams(unittest.TestCase):
    """`?wx_*` query 參數 → 城市組(app 決定城市,bridge 照做)。"""

    def _parse(self, **q):
        return bridge._dashboard_weather_cities(_FakeReq(query=q))

    def test_no_params_keeps_legacy_two_cities(self):
        """舊 app 不帶參數 → 維持現行雙城,行為零變化。"""
        self.assertEqual(self._parse(), bridge._DASH_WEATHER_CITIES)
        self.assertEqual(bridge._dashboard_weather_cities(_FakeReq()),
                         bridge._DASH_WEATHER_CITIES)

    def test_single_city_from_coords(self):
        cities = self._parse(wx_lat="13.75", wx_lon="100.5",
                             wx_label="曼谷", wx_tz="Asia/Bangkok")
        self.assertEqual(len(cities), 1)
        cid, name, lat, lon, tz = cities[0]
        self.assertEqual((name, lat, lon, tz), ("曼谷", 13.75, 100.5, "Asia/Bangkok"))
        self.assertEqual(cid, "13.75,100.50")

    def test_missing_tz_falls_back_to_auto(self):
        """沒給時區 → open-meteo timezone=auto,依座標算當地日界。"""
        self.assertEqual(self._parse(wx_lat="35.68", wx_lon="139.69")[0][4], "auto")

    def test_bogus_tz_rejected(self):
        """不像 IANA 的字串不直接轉給外部 API。"""
        self.assertEqual(
            self._parse(wx_lat="1", wx_lon="1", wx_tz="; drop table")[0][4], "auto")

    def test_missing_label_falls_back_to_coords(self):
        self.assertEqual(self._parse(wx_lat="13.75", wx_lon="100.5")[0][1],
                         "13.75,100.50")

    def test_label_sanitised_and_truncated(self):
        name = self._parse(wx_lat="1", wx_lon="1",
                           wx_label="曼谷" + "長" * 80)[0][1]
        self.assertNotIn("", name)
        self.assertLessEqual(len(name), bridge._WX_LABEL_MAX)

    def test_off_disables_weather(self):
        for v in ("off", "0", "none", "false", "OFF"):
            self.assertEqual(self._parse(wx=v), (), v)

    def test_bad_coords_fall_back_to_default(self):
        for lat, lon in (("abc", "100"), ("91", "100"), ("10", "181"),
                         ("nan", "100"), ("", "100")):
            self.assertEqual(self._parse(wx_lat=lat, wx_lon=lon),
                             bridge._DASH_WEATHER_CITIES, (lat, lon))

    def test_endpoint_threads_city_through(self):
        """端點真的把 query 參數帶進 _dashboard_weather。"""
        seen = []
        orig = bridge._dashboard_weather
        restore = _stub_light(self)          # 換掉 cc/codex/launchctl 等外部依賴

        async def spy(cities=None):
            seen.append(cities)
            return None
        bridge._dashboard_weather = spy
        try:
            asyncio.run(bridge.app_dashboard(_FakeReq(
                query={"wx_lat": "13.75", "wx_lon": "100.5", "wx_label": "曼谷"})))
            asyncio.run(bridge.app_dashboard(_FakeReq()))
            asyncio.run(bridge.app_dashboard(_FakeReq(query={"wx": "off"})))
        finally:
            bridge._dashboard_weather = orig
            restore()
        self.assertEqual(len(seen[0]), 1)
        self.assertEqual(seen[0][0][1], "曼谷")
        self.assertEqual(seen[1], bridge._DASH_WEATHER_CITIES)
        self.assertEqual(seen[2], ())


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
