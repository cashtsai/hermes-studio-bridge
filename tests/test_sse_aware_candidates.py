"""公網候選要依**實測串流可用性**排序 —— 探活回 200 不代表 SSE 能用。

建立原因(2026-10-26 實機事故):機主的手機被廣告到一條 cloudflared
**quick tunnel**(`*.trycloudflare.com`)上。那條路:

    GET /health          → 200
    GET /codexsessions   → 200,8KB,0.4 秒
    SSE(卡片流/事件流)  → **70 秒 0 byte**,連回放都沒有

同一時刻直連 / tailnet / 具名 tunnel 三條都是精準每 2 秒一個 keepalive。
後果:app 開串流 → 30 秒自己 timeout → 3 秒後重連 → 無限踏步機
(build 186 實測:逾時 178 次、串流關 348 次其中短命 319 次、降級 1054 秒)。
內容還看得到是因為 app 退回每 10 秒輪詢 —— 所以它**看起來像網路不穩,
不像壞掉**,一整天沒人發現。

兩條不變式:
  ① 候選排序的門檻是「SSE 真的通」,不是「HTTP 回 200」。
  ② 公網候選**不是單一值** —— 固定網域掛掉時(當天 gateway.shan.house 回 502)
     要有退路,否則「設了固定網域」反而比「沒設」更容易掉線。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="sse-cand-test-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))

import bridge  # noqa: E402


def _verdict(sse, reachable=True, ts=10_000.0):
    return {"ts": ts, "reachable": reachable, "sse": sse}


class SortPublicBySSETests(unittest.TestCase):
    """實測能串流的在前,未知其次,『通但不送串流』墊底。"""

    def test_streamable_wins_over_degraded(self):
        good, bad = "https://named.example", "https://quick.trycloudflare.com"
        with mock.patch.dict(bridge._SSE_VERDICTS,
                             {good: _verdict(True), bad: _verdict(False)},
                             clear=True):
            # 刻意把壞的放前面:排序必須把它推到後面去。
            self.assertEqual(bridge._sort_public_by_sse([bad, good]), [good, bad])

    def test_unknown_sits_between(self):
        ok, unknown, bad = "https://a", "https://b", "https://c"
        with mock.patch.dict(bridge._SSE_VERDICTS,
                             {ok: _verdict(True), unknown: _verdict(None),
                              bad: _verdict(False)}, clear=True):
            self.assertEqual(bridge._sort_public_by_sse([bad, unknown, ok]),
                             [ok, unknown, bad])

    def test_degraded_is_kept_not_dropped(self):
        """零設定的使用者可能只有這一條公網出口 —— 退回輪詢也比連不上好。
        它只是不准排在能串流的前面。"""
        bad = "https://only.trycloudflare.com"
        with mock.patch.dict(bridge._SSE_VERDICTS, {bad: _verdict(False)}, clear=True):
            self.assertEqual(bridge._sort_public_by_sse([bad]), [bad])

    def test_no_verdict_is_stable(self):
        """還沒探測過(bridge 剛起來)→ 維持原本的設定意圖順序,不亂動。"""
        urls = ["https://a", "https://b", "https://c"]
        with mock.patch.dict(bridge._SSE_VERDICTS, {}, clear=True):
            self.assertEqual(bridge._sort_public_by_sse(urls), urls)


class PublicCandidateListTests(unittest.TestCase):
    """公網候選是清單,不是單一值。"""

    def test_all_three_sources_in_intent_order(self):
        with mock.patch.dict(os.environ, {"POCKET_TUNNEL_URL": "https://env.example"}), \
             mock.patch.object(bridge, "_tunnel_url_from_file", return_value="https://file.example"), \
             mock.patch.object(bridge, "_quick_tunnel_url", return_value="https://quick.example"):
            self.assertEqual(bridge._pair_tunnel_urls(),
                             ["https://env.example", "https://file.example",
                              "https://quick.example"])

    def test_fixed_domain_down_still_leaves_a_fallback(self):
        """**這是回歸測試**:原本「有固定網域就只回它」,那個網域一掛就沒退路。"""
        with mock.patch.dict(os.environ, {"POCKET_TUNNEL_URL": "https://dead.example"}), \
             mock.patch.object(bridge, "_tunnel_url_from_file", return_value=""), \
             mock.patch.object(bridge, "_quick_tunnel_url", return_value="https://alive.example"):
            urls = bridge._pair_tunnel_urls()
            self.assertIn("https://alive.example", urls)
            with mock.patch.dict(bridge._SSE_VERDICTS,
                                 {"https://dead.example": _verdict(None, reachable=False),
                                  "https://alive.example": _verdict(True)}, clear=True):
                # 掛掉的那條被探測判為未知 → 能串流的要排前面。
                self.assertEqual(bridge._sort_public_by_sse(urls)[0], "https://alive.example")

    def test_dedupes_and_strips_trailing_slash(self):
        with mock.patch.dict(os.environ, {"POCKET_TUNNEL_URL": "https://same.example/"}), \
             mock.patch.object(bridge, "_tunnel_url_from_file", return_value="https://same.example"), \
             mock.patch.object(bridge, "_quick_tunnel_url", return_value=""):
            self.assertEqual(bridge._pair_tunnel_urls(), ["https://same.example"])

    def test_singular_helper_keeps_back_compat(self):
        with mock.patch.object(bridge, "_pair_tunnel_urls",
                               return_value=["https://first", "https://second"]):
            self.assertEqual(bridge._pair_tunnel_url(), "https://first")
        with mock.patch.object(bridge, "_pair_tunnel_urls", return_value=[]):
            self.assertIsNone(bridge._pair_tunnel_url())


class ProbeClassificationTests(unittest.TestCase):
    """「不可達」與「可達但不送串流」必須分得開 —— 只有後者該被降級。"""

    def test_bytes_arriving_means_streamable(self):
        resp = mock.MagicMock()
        resp.status = 200
        resp.read.return_value = b":"
        resp.__enter__ = mock.Mock(return_value=resp)
        resp.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(bridge.urllib.request, "urlopen", return_value=resp):
            d = bridge._probe_candidate_sse("https://x", wait=1)
        self.assertTrue(d["reachable"])
        self.assertIs(d["sse"], True)

    def test_silent_but_http_ok_means_degraded(self):
        """逾時 + 一般請求通 = 「通但不送串流」= quick tunnel 的病徵。"""
        with mock.patch.object(bridge.urllib.request, "urlopen", side_effect=TimeoutError()), \
             mock.patch.object(bridge, "_candidate_http_ok", return_value=True):
            d = bridge._probe_candidate_sse("https://x", wait=1)
        self.assertIs(d["sse"], False)
        self.assertTrue(d["reachable"])

    def test_silent_and_http_down_means_unknown(self):
        """整條路不通 → 未知,**不可以**當成「串流壞掉」(通常只是暫時的)。"""
        with mock.patch.object(bridge.urllib.request, "urlopen", side_effect=TimeoutError()), \
             mock.patch.object(bridge, "_candidate_http_ok", return_value=False):
            d = bridge._probe_candidate_sse("https://x", wait=1)
        self.assertIsNone(d["sse"])
        self.assertFalse(d["reachable"])

    def test_probe_never_raises(self):
        """探測是旁路,炸了不能影響候選組裝。"""
        with mock.patch.object(bridge.urllib.request, "urlopen",
                               side_effect=RuntimeError("boom")):
            d = bridge._probe_candidate_sse("https://x", wait=1)
        self.assertIsNone(d["sse"])

    def test_probe_sends_a_user_agent(self):
        """⚠️ Cloudflare 對 `Python-urllib/3.x` 回 403 —— 沒帶 UA 會把好的具名
        tunnel 誤判成不可達,而真正壞掉的 quick tunnel 反而看起來比較好。
        (2026-10-26 實測踩到。)"""
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["ua"] = req.get_header("User-agent")
            raise TimeoutError()

        with mock.patch.object(bridge.urllib.request, "urlopen", fake_urlopen), \
             mock.patch.object(bridge, "_candidate_http_ok", return_value=False):
            bridge._probe_candidate_sse("https://x", wait=1)
        self.assertTrue(seen.get("ua"))
        self.assertNotIn("urllib", (seen.get("ua") or "").lower())

    def test_probe_path_asks_for_no_backlog(self):
        """探針不可以把整段歷史灌過隧道(機主的網路要錢)。"""
        self.assertIn("since_seq=999999999999", bridge._SSE_PROBE_PATH)


class RefreshVerdictsTests(unittest.TestCase):

    def test_caches_within_ttl(self):
        url = "https://x"
        with mock.patch.dict(bridge._SSE_VERDICTS,
                             {url: {"ts": bridge.time.monotonic(),
                                    "reachable": True, "sse": True}}, clear=True), \
             mock.patch.object(bridge, "_probe_candidate_sse") as p:
            bridge.refresh_sse_verdicts([url])
            p.assert_not_called()

    def test_degraded_candidate_is_logged(self):
        """這次事故一整天沒人發現,就是因為沒有任何地方會出聲。"""
        url = "https://quick.trycloudflare.com"
        with mock.patch.dict(bridge._SSE_VERDICTS, {}, clear=True), \
             mock.patch.object(bridge, "_probe_candidate_sse",
                               return_value=_verdict(False, ts=bridge.time.monotonic())), \
             mock.patch.object(bridge, "_log_event") as log:
            bridge.refresh_sse_verdicts([url])
        events = [c.args[0] for c in log.call_args_list if c.args]
        self.assertIn("sse_candidate_degraded", events)


class RedactionTests(unittest.TestCase):
    """日誌要看得出通道**種類**,但不洩漏完整私有網址(公開 repo 的日誌慣例)。"""

    def test_quick_tunnel_and_tailnet_are_generalised(self):
        self.assertEqual(bridge._redact_host("https://abc-def.trycloudflare.com"),
                         "<quick-tunnel>.trycloudflare.com")
        # ⚠️ 這裡的主機名要帶 `example`:公開 repo 的私有識別守門
        # (test_no_private_infra_leak)會攔 `*.<tailnet>.ts.net` 這個形狀,
        # 只放行佔位寫法。期望值裡的 `<tailnet>` 有角括號,守門本來就放行。
        self.assertEqual(bridge._redact_host("https://host.example.ts.net"),
                         "<tailnet>.ts.net")
        self.assertEqual(bridge._redact_host("http://192.168.1.33:8081"), "<lan-ip>")

    def test_unparsable_does_not_raise(self):
        self.assertTrue(bridge._redact_host("::::"))


if __name__ == "__main__":
    unittest.main()
