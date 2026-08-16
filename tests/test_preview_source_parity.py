"""列表 preview 必須與卡片流套同一組 TG 過濾(0806 巡檢 S1)。

病症:預覽顯示一則訊息,點進對話卻找不到 —— 因為卡片流會把 TG 側的
雙寫副本壓掉、把活 turn 中的 TG assistant 近訊檢疫,而預覽以前不套這兩層。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)

import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="preview-parity-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class TestPreviewSourceParity(unittest.TestCase):
    """`_persona_preview_merged` 的 TG 分支過濾行為。"""

    def setUp(self):
        bridge._canon_init()
        self._canon = bridge._canon_messages
        self._hist = bridge._persona_history
        self._reports = bridge._report_messages
        self._started = bridge._session_turn_started_at
        bridge._report_messages = lambda *a, **k: []
        bridge._session_turn_started_at = lambda *a, **k: None   # 預設無活回合

    def tearDown(self):
        bridge._canon_messages = self._canon
        bridge._persona_history = self._hist
        bridge._report_messages = self._reports
        bridge._session_turn_started_at = self._started

    def test_tg_double_write_copy_does_not_win_preview(self):
        """canonical 與 state.db 各有一份同句(措辭微漂)→ 預覽用 canonical 那份。

        卡片流壓掉 TG 副本;預覽若不壓,挑到的會是畫面上不存在的字串。
        """
        # ⚠️ 刻意寫長:`_dual_source_dup` 的模糊後備有「短方 ≥24 字」守門
        # (短句不做覆蓋率壓重,免得誤殺真引用)。太短的樣本測不到這條路徑。
        canonical_text = ("上下文已經整理完成,重點有三項:持倉損益、待辦收斂、"
                          "以及明天要送出的版本號。請看附錄的執行步驟。")
        tg_drifted = ("對話上下文已經整理完成,重點有三項:持倉損益、待辦收斂、"
                      "以及明天要送出的版本號。請看附錄的執行步驟。")
        bridge._canon_messages = lambda *a, **k: [
            {"role": "assistant", "content": canonical_text, "ts": 1000.0},
        ]
        bridge._persona_history = lambda *a, **k: [
            {"role": "assistant", "content": tg_drifted, "ts": 1005.0},
        ]

        latest, _ts, sender, inbound, _in_ts = bridge._persona_preview_merged(
            "yuanfang", "/nonexistent-home")
        self.assertEqual(sender, "persona")
        self.assertEqual(latest, canonical_text)
        self.assertEqual(inbound, canonical_text)

    def test_in_flight_turn_quarantines_tg_after_turn_start(self):
        """回合進行中:回合起始**之後**的 TG assistant 進度句不得成為預覽。"""
        import time
        now = time.time()
        bridge._canon_messages = lambda *a, **k: [
            {"role": "user", "content": "幫我查一下", "ts": now - 30},
        ]
        bridge._persona_history = lambda *a, **k: [
            {"role": "assistant", "content": "TG 側搶先落地的半成品", "ts": now - 10},
        ]
        bridge._session_turn_started_at = lambda *a, **k: now - 40   # 回合起於 40s 前

        latest, _ts, sender, inbound, _in_ts = bridge._persona_preview_merged(
            "yuanfang", "/nonexistent-home")
        self.assertEqual(latest, "幫我查一下")
        self.assertEqual(sender, "user")
        self.assertIsNone(inbound, "檢疫中的 TG assistant 不該當 inbound(會誤觸未讀)")

    def test_tg_before_turn_start_is_not_quarantined(self):
        """新窗的關鍵改進:回合起始**之前**的既定 TG 歷史照常放行(舊窗會誤扣)。

        回合 30s 前才開始,一小時前的 TG assistant 是既定歷史,不是本回合副本 ——
        舊的「距今 1h 內全部」會把它一起扣住,新窗必須放行。
        """
        import time
        now = time.time()
        bridge._canon_messages = lambda *a, **k: []
        bridge._persona_history = lambda *a, **k: [
            {"role": "assistant", "content": "半小時前就講完的既定回覆", "ts": now - 1800},
        ]
        bridge._session_turn_started_at = lambda *a, **k: now - 30   # 回合 30s 前才開始

        latest, _ts, sender, inbound, _in_ts = bridge._persona_preview_merged(
            "yuanfang", "/nonexistent-home")
        self.assertEqual(latest, "半小時前就講完的既定回覆")
        self.assertEqual(inbound, "半小時前就講完的既定回覆")

    def test_plain_tg_message_still_shows(self):
        """純 TG 訊息(canonical 無副本、非活 turn)照常成為預覽 —— 別過濾過頭。"""
        bridge._canon_messages = lambda *a, **k: []
        bridge._persona_history = lambda *a, **k: [
            {"role": "assistant", "content": "只在 Telegram 講過的話", "ts": 2000.0},
        ]

        latest, _ts, sender, inbound, _in_ts = bridge._persona_preview_merged(
            "yuanfang", "/nonexistent-home")
        self.assertEqual(latest, "只在 Telegram 講過的話")
        self.assertEqual(sender, "persona")
        self.assertEqual(inbound, "只在 Telegram 講過的話")


if __name__ == "__main__":
    unittest.main()
