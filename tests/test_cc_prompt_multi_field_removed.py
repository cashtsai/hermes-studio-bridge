"""CC prompt 契約:`multi` 已移除,多題資訊一律看 `q_total`。

為什麼要有這個測試:`multi`(原 ask 有多題)與 `multiselect`(這一題可複選)只差
兩個字、語意完全不同,而且兩者曾經並存在同一個 payload 裡 —— 這是現成的誤讀
陷阱(2026-08-11 實際有 lane 反映混淆)。移除後用這個測試釘住,免得有人「順手
補回來」。

`q_total` 是唯一的多題來源。注意:**pane 路徑的 q_total 是「從 PR #89 起」
才有的** —— #89 之前只有這條 jsonl 路徑會產出 q_total,所以本 PR 必須排在
#89 **之後**合併。`multi` 只出現在 jsonl 路徑,而該路徑實測從不命中
(CC 是答完才 flush tool_use,掃全部 transcript:288 次 AskUserQuestion、
0 筆懸空)。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import unittest

import bridge


class _FakeJsonl:
    """最小可用的 jsonl 事件序列:一個未被回答的三題 AskUserQuestion。"""

    EVENTS = [
        {"type": "assistant", "message": {"content": [{
            "type": "tool_use", "id": "tu-1", "name": "AskUserQuestion",
            "input": {"questions": [
                {"question": "第一題?", "header": "H1", "multiSelect": True,
                 "options": [{"label": "甲", "description": "說明甲"},
                             {"label": "乙", "description": "說明乙"}]},
                {"question": "第二題?", "header": "H2",
                 "options": [{"label": "丙"}, {"label": "丁"}]},
                {"question": "第三題?", "header": "H3",
                 "options": [{"label": "戊"}, {"label": "己"}]},
            ]},
        }]}},
    ]


class CCPromptContractTests(unittest.TestCase):
    def setUp(self):
        self._orig = bridge._cc_jsonl_tail_events
        bridge._cc_jsonl_tail_events = lambda _path: list(_FakeJsonl.EVENTS)
        self.addCleanup(setattr, bridge, "_cc_jsonl_tail_events", self._orig)

    def test_multi_field_is_gone(self):
        ask = bridge._cc_pending_ask("/nonexistent.jsonl")
        self.assertIsNotNone(ask)
        self.assertNotIn("multi", ask,
                         "`multi` 與 `multiselect` 只差兩個字、語意不同 —— "
                         "多題請一律用 q_total,不要把 multi 加回來")

    def test_q_total_carries_the_multi_question_signal(self):
        ask = bridge._cc_pending_ask("/nonexistent.jsonl")
        self.assertEqual(ask["q_total"], 3, "多題資訊要由 q_total 承載")
        self.assertEqual(ask["q_index"], 0, "q_index 是 0-based(與 pane 路徑一致)")

    def test_multiselect_still_present_and_distinct(self):
        """multiselect 講的是「這一題可以複選」,與題數無關。"""
        ask = bridge._cc_pending_ask("/nonexistent.jsonl")
        self.assertTrue(ask["multiselect"])


if __name__ == "__main__":
    unittest.main()
