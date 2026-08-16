"""蒸餾提案只送主人格 —— 2026-08-13 XCash 回報的廣播 bug 回歸。

實際炸法:`_persona_harness_reports(persona)` 收了 persona 參數卻**完全沒用它**,
於是 `_sync_persona_reports` 每個人格跑一次,四個人格在同一秒(06:58:46)各收到
一份**內容完全相同**的 3533 bytes 報告。提案本身是全域的(scope 寫在提案自己
身上),審核也只有一個人要做 —— 其餘三個人格收到只是被打擾。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import unittest
from unittest import mock

import bridge


class _FakeStore:
    def __init__(self, pending=None, last=None):
        self._pending = pending if pending is not None else [{
            "store": "prompt", "key": "hermes:xcash", "scope": "node:hermes:xcash",
            "version": 1, "rationale": "測試用", "evidence": ["traj-1"],
            "preview": "- 舊\n+ 新",
        }]
        self._last = last if last is not None else {
            "started_ts": 1786570335.0, "trajectories": 5, "proposals": 11}

    def list(self, **_kw):
        return list(self._pending)

    def last_run(self):
        return dict(self._last)


class HarnessReportPersonaTests(unittest.TestCase):
    def setUp(self):
        self.store = _FakeStore()
        patches = [
            mock.patch.object(bridge, "_harness_enabled", lambda: True),
            mock.patch.object(bridge, "_harness_store", lambda: self.store),
            mock.patch.object(bridge, "_HARNESS_REPORT_PERSONA", "xcash"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_primary_persona_gets_the_report(self):
        out = bridge._persona_harness_reports("xcash")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["label"], "蒸餾提案")
        self.assertIn("蒸餾提案待審", out[0]["content"])

    def test_other_personas_get_nothing(self):
        """這就是回報的 bug:四個人格各收一份相同報告。"""
        for p in ("yuanfang", "pantianqing", "shuijing"):
            with self.subTest(persona=p):
                self.assertEqual(
                    bridge._persona_harness_reports(p), [],
                    f"{p} 不該收到蒸餾提案 —— 提案是全域的,審核只有一個人要做")

    def test_empty_setting_disables_the_section_entirely(self):
        with mock.patch.object(bridge, "_HARNESS_REPORT_PERSONA", ""):
            for p in ("xcash", "yuanfang"):
                self.assertEqual(bridge._persona_harness_reports(p), [])

    def test_setting_is_configurable(self):
        with mock.patch.object(bridge, "_HARNESS_REPORT_PERSONA", "yuanfang"):
            self.assertEqual(bridge._persona_harness_reports("xcash"), [])
            self.assertEqual(len(bridge._persona_harness_reports("yuanfang")), 1)

    def test_flag_off_beats_everything(self):
        with mock.patch.object(bridge, "_harness_enabled", lambda: False):
            self.assertEqual(bridge._persona_harness_reports("xcash"), [])

    def test_no_pending_and_no_run_is_silent(self):
        """沒東西可報就不要打擾(連主人格也不發)。"""
        self.store._pending, self.store._last = [], None
        self.assertEqual(bridge._persona_harness_reports("xcash"), [])

    def test_external_id_is_stable_per_day(self):
        """一天一則、夜批更新內容不洗版 —— 這是既有保證,別改壞。"""
        a = bridge._persona_harness_reports("xcash")[0]
        b = bridge._persona_harness_reports("xcash")[0]
        self.assertEqual(a["external_id"], b["external_id"])
        self.assertTrue(a["external_id"].startswith("harness:xcash:"))


if __name__ == "__main__":
    unittest.main()
