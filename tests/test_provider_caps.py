"""Provider capability 宣告(2026-11-04,t3code #3 前半)。

釘兩件事:(1) 每個上了 _V2_SESSION_OPS 對齊矩陣的 provider 都要有能力
宣告 —— 新 provider 上線漏宣告,CI 紅給你看,不是 app 端猜;
(2) payload 的 ops 直接從對齊矩陣推導 —— 單一真相,表改了宣告自動跟。
"""
import _isolation  # noqa: F401
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="caps-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

_REQUIRED_FLAGS = ("interrupt", "queued_messages", "streaming",
                   "terminal_status_quality", "approvals",
                   "approval_callbacks_live_only", "native_id_strength")


class TestProviderCaps(unittest.TestCase):
    def test_every_ops_provider_declares_caps(self):
        table_provs = {p for tbl in bridge._V2_SESSION_OPS.values() for p in tbl}
        missing = table_provs - set(bridge._PROVIDER_CAPS)
        self.assertFalse(missing, f"對齊矩陣有、能力宣告沒有:{missing}")

    def test_all_flags_present_and_typed(self):
        for prov, caps in bridge._PROVIDER_CAPS.items():
            for f in _REQUIRED_FLAGS:
                self.assertIn(f, caps, f"{prov} 缺 {f}")
            self.assertIn(caps["streaming"], ("live", "poll"), prov)
            self.assertIn(caps["terminal_status_quality"],
                          ("strong", "weak"), prov)
            self.assertIn(caps["native_id_strength"], ("strong", "weak"), prov)

    def test_ops_derived_from_matrix(self):
        payload = bridge._provider_caps_payload()
        for prov, entry in payload.items():
            expect = sorted(op for op, tbl in bridge._V2_SESSION_OPS.items()
                            if prov in tbl)
            self.assertEqual(entry["ops"], expect, prov)

    def test_live_only_matches_response_capability_logic(self):
        """openclaw/hermes 宣告 not-live-only —— 跟 approvals 的
        response_capability 判定(openclaw 一律 live)語意一致。"""
        self.assertFalse(
            bridge._PROVIDER_CAPS["openclaw"]["approval_callbacks_live_only"])
        self.assertTrue(
            bridge._PROVIDER_CAPS["codex"]["approval_callbacks_live_only"])


if __name__ == "__main__":
    unittest.main()
