"""選路探測端點 `GET /app/v2/sessions/{id}/events?profile=probe`(2026-10-29)。

事故:app 2026-10-28 加了 `probeSSE` 來避開「HTTP 通但 SSE 不送 byte」的
cloudflare quick tunnel,它打的是 `profile=probe` —— 但 bridge 只收 `phone`,
一律回 400;而 app 只把 401/403/404 當「路是通的」,400 落在外面 →
**每條候選都被判定不會串流** → 那個修復整個空轉。

使用者症狀:用了一陣子突然顯示「Bridge 離線」,但 session 列表按重試立刻就通
(一般 HTTP 本來就通,死的只有 SSE)。
日誌實證:45 分鐘內 22 次 `UNSUPPORTED_PROFILE` on `/app/v2/sessions/_/events`。
"""
import _isolation  # noqa: F401
import os
import sys
import tempfile
import unittest

# 測試把持有時間壓短,否則每個打探測端點的 case 都要等 6 秒。
# 契約(必須 > app 的 3 秒)由 ProbeHoldsTheStream 讀常數另測。
os.environ.setdefault("POCKET_SSE_PROBE_HOLD_SECS", "0.1")

_TMP = tempfile.mkdtemp(prefix="sse-probe-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class SSEProbeProfileTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        self.auth = {"Authorization": "Bearer " + bridge.BRIDGE_TOKEN}
        self.http = TestClient(bridge.app)

    def test_probe_streams_a_byte_without_a_real_session(self):
        """探測問的是「這條路送不送得出 SSE」,不該需要真的 session。

        佔位 id `_` 走正規路徑會 404 —— 探測必須在 session 查找**之前**就回。
        """
        r = self.http.get("/app/v2/sessions/_/events",
                          params={"profile": "probe", "since_seq": 999999999999},
                          headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("text/event-stream", r.headers.get("content-type", ""))
        self.assertTrue(r.content, "要真的送出至少一個 byte,否則探測仍判定不會串流")

    def test_probe_works_for_any_placeholder_id(self):
        for sid in ("_", "nonexistent", "codex:nope"):
            r = self.http.get(f"/app/v2/sessions/{sid}/events",
                              params={"profile": "probe"}, headers=self.auth)
            self.assertEqual(r.status_code, 200, f"{sid}: {r.text}")

    def test_probe_still_requires_auth(self):
        """探測端點不是公開的 —— 不帶 token 不准過。"""
        r = self.http.get("/app/v2/sessions/_/events",
                          params={"profile": "probe"})
        self.assertIn(r.status_code, (401, 403), r.text)

    def test_unknown_profile_still_400(self):
        """只放行 phone 與 probe,其餘照舊擋掉(不是變成什麼都收)。"""
        r = self.http.get("/app/v2/sessions/_/events",
                          params={"profile": "desktop"}, headers=self.auth)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("UNSUPPORTED_PROFILE", r.text)

    def test_phone_profile_unchanged_404_for_unknown_session(self):
        """正規路徑行為一字不變 —— 探測是純加法。"""
        r = self.http.get("/app/v2/sessions/definitely-not-a-session/events",
                          params={"profile": "phone"}, headers=self.auth)
        self.assertEqual(r.status_code, 404, r.text)


if __name__ == "__main__":
    unittest.main()


class ProbeHoldsTheStream(unittest.TestCase):
    """探測必須**持有串流**,否則對它要抓的東西零鑑別力。

    要抓的失效模式是「中介層把串流吞住不往下送」(quick tunnel 的指紋)。
    第一版 `yield 一次就 return`,而任何緩衝型代理在 response 結束時一定會
    flush —— 壞掉那條照樣把已完成的短 body 交出去,app 讀到 byte 判「會流」。
    用假陽性取代假陰性,比不探還糟(帳面上「已驗過 SSE」是假的)。

    ⚠️ **跨端耦合,改任一邊都要回來看另一邊**:
    `POCKET_SSE_PROBE_HOLD_SECS` 必須大於 app 端 `StudioConfig.probeSSE` 的
    `req.timeoutInterval`(目前 3 秒)。大於才會變成:
      · 會串流 → 第一個 byte 立刻到 → app 回 true
      · 被緩衝 → 持有期間收不到 → app 逾時 → nil(量不到),排在 true 之後
    小於等於的話,緩衝的那條會在 app 逾時**之前**收到 flush → 又變成 true。
    """

    APP_PROBE_TIMEOUT_SECS = 3.0   # 與 StudioConfig.probeSSE 的 timeoutInterval 對齊

    def test_hold_exceeds_app_probe_timeout(self):
        # 讀**預設常數**而不是 env —— env 被測試壓成 0.1 以免整套變慢,
        # 要守的契約是正式部署那個值。
        import bridge as _b
        self.assertGreater(
            _b.SSE_PROBE_HOLD_DEFAULT, self.APP_PROBE_TIMEOUT_SECS,
            "持有時間沒超過 app 的 probe timeout → 緩衝型代理會在逾時前拿到 "
            "flush,探測又變成恆真。這是跨端契約,改之前先看 StudioConfig.probeSSE。")

    def test_probe_source_actually_sleeps(self):
        """原始碼層級確認持有邏輯還在 —— 這條在有人「簡化」掉 sleep 時會紅。"""
        import inspect
        import bridge as _b
        src = inspect.getsource(_b.v2_session_events)
        self.assertIn("POCKET_SSE_PROBE_HOLD_SECS", src)
        self.assertIn("asyncio.sleep(probe_hold)", src)
