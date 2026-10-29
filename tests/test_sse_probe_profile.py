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
