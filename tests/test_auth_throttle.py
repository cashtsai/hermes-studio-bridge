"""認證節流:per-client 分桶 + 聚合表有界(2026-08-12 多租戶強化)。

原病灶:
  ① 單一全域 deque —— 一個來源的錯誤嘗試會讓**其他來源**的無效請求一起 429
     (正常有效 token 不受影響,那部分本來就對;但新裝置配對/token 過期要重配
     的正常人會被連坐)。relay 多租戶必修。
  ② `_AUTH_FAIL_AGG` 永不清理,key 含 request path → 變換路徑即可無限撐大。
"""
import os, sys, time, types, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BRIDGE_TOKEN", "test-token-for-auth-throttle")
import bridge  # noqa: E402


def _req(client: str, path: str = "/app/v1/x"):
    """最小 Request 替身 —— 只有 _client_host / url.path 會被讀到。"""
    return types.SimpleNamespace(
        client=types.SimpleNamespace(host=client),
        headers={},
        url=types.SimpleNamespace(path=path),
    )


class PerClientThrottleTests(unittest.TestCase):
    def setUp(self):
        bridge._AUTH_FAILS_BY_CLIENT.clear()
        bridge._AUTH_FAIL_AGG.clear()

    def test_one_client_flood_does_not_throttle_another(self):
        """病灶 ①:攻擊者灌爆自己的桶,不得波及其他來源。"""
        now = time.monotonic()
        with bridge._AUTH_LOCK:
            for _ in range(bridge._AUTH_FAIL_MAX * 5):
                bridge._auth_fail_bump_locked(_req("10.0.0.666"), now)
            attacker_over = bridge._auth_fail_bump_locked(_req("10.0.0.666"), now)
            victim_over = bridge._auth_fail_bump_locked(_req("192.168.1.50"), now)
        self.assertTrue(attacker_over, "攻擊者自己應該被擋")
        self.assertFalse(victim_over, "其他來源不該被連坐(這就是原病灶)")

    def test_client_trips_only_after_own_max(self):
        now = time.monotonic()
        with bridge._AUTH_LOCK:
            for i in range(bridge._AUTH_FAIL_MAX):
                self.assertFalse(bridge._auth_fail_bump_locked(_req("1.2.3.4"), now),
                                 f"第 {i+1} 次不該超額")
            self.assertTrue(bridge._auth_fail_bump_locked(_req("1.2.3.4"), now))

    def test_window_expiry_releases_client(self):
        base = time.monotonic()
        with bridge._AUTH_LOCK:
            for _ in range(bridge._AUTH_FAIL_MAX + 2):
                bridge._auth_fail_bump_locked(_req("5.5.5.5"), base)
            later = base + bridge._AUTH_FAIL_WINDOW + 1
            self.assertFalse(bridge._auth_fail_bump_locked(_req("5.5.5.5"), later),
                             "整窗過去後應該重新放行")

    def test_client_table_bounded(self):
        """掃描者換 IP 也不該把桶表撐爆。"""
        now = time.monotonic()
        with bridge._AUTH_LOCK:
            for i in range(bridge._AUTH_TABLE_MAX + 500):
                bridge._auth_fail_bump_locked(_req(f"10.1.{i // 256}.{i % 256}"),
                                              now + bridge._AUTH_FAIL_WINDOW + 1 + i)
        self.assertLessEqual(len(bridge._AUTH_FAILS_BY_CLIENT),
                             bridge._AUTH_TABLE_MAX + 1)


class AggTableBoundedTests(unittest.TestCase):
    def setUp(self):
        bridge._AUTH_FAILS_BY_CLIENT.clear()
        bridge._AUTH_FAIL_AGG.clear()

    def test_varying_paths_cannot_grow_agg_unbounded(self):
        """病灶 ②:變換 path 無限撐大聚合表 → 記憶體耗盡。"""
        now = time.monotonic()
        with bridge._AUTH_LOCK:
            for i in range(bridge._AUTH_TABLE_MAX * 2):
                bridge._auth_fail_summary_locked(
                    _req("9.9.9.9", path=f"/scan/{i}"), 401, now + i * 0.001)
        self.assertLessEqual(len(bridge._AUTH_FAIL_AGG), bridge._AUTH_TABLE_MAX + 1,
                             "聚合表必須有界")

    def test_stale_entries_pruned_by_ttl(self):
        with bridge._AUTH_LOCK:
            bridge._auth_fail_summary_locked(_req("8.8.8.8", path="/old"), 401, 0.0)
            fresh = bridge._AUTH_AGG_TTL + 10
            for i in range(bridge._AUTH_TABLE_MAX + 5):
                bridge._auth_fail_summary_locked(
                    _req("8.8.8.9", path=f"/new/{i}"), 401, fresh)
        self.assertNotIn(("8.8.8.8", "/old", 401), bridge._AUTH_FAIL_AGG,
                         "過期條目應被 TTL 清掉")

    def test_summary_still_reports_first_hit(self):
        """節流行為不變:第 1 次仍要出 log summary。"""
        now = time.monotonic()
        with bridge._AUTH_LOCK:
            s = bridge._auth_fail_summary_locked(_req("7.7.7.7"), 401, now)
        self.assertIsNotNone(s)
        self.assertEqual(s["count"], 1)
        self.assertEqual(s["status"], 401)


if __name__ == "__main__":
    unittest.main()
