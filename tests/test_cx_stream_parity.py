"""CX 的即時串流要追平 CC / 人格(2026-10-29 機主指示)。

CX 原本只接 `item/agentMessage/delta`,所以:
  ‧ 長思考階段完全沒有畫面 —— 一段死寂後整塊出現
  ‧ 指令輸出(build/安裝這類長工具)也只在完成時才看得到
而 CC(cc_sdk.text_delta)與人格(turn_delta)早就是「累積進同一張卡、rev 遞增、
final=False」的模式。這裡把 CX 對齊同一套。

另補兩個 codex 新版型別:enteredReviewMode / contextCompaction —— 原本落到
`return []`,畫面一片空白。
"""
import _isolation  # noqa: F401
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="cx-stream-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carddigest  # noqa: E402


class CXStreamParityTest(unittest.TestCase):
    def setUp(self):
        self.d = carddigest.CodexThreadDigest()
        self.d.store.turn_id = "t1"

    def _texts(self):
        return [str((c.get("body") or {}).get("text") or "")
                for c in self.d.store.cards.values()]

    def test_reasoning_delta_streams_into_one_card(self):
        self.d.handle("item/reasoning/textDelta", {"itemId": "r1", "delta": "正在"})
        self.d.handle("item/reasoning/textDelta", {"itemId": "r1", "delta": "思考"})
        txt = [t for t in self._texts() if t.startswith("💭")]
        self.assertEqual(len(txt), 1, "同一個 item 必須累積進同一張卡,不是每則一張")
        self.assertIn("正在思考", txt[0])

    def test_reasoning_card_is_not_final_while_streaming(self):
        self.d.handle("item/reasoning/textDelta", {"itemId": "r1", "delta": "x"})
        card = self.d.store.cards["card-cx-r1"]
        self.assertFalse(card["final"], "串流中的卡不能是 final")

    def test_command_output_delta_streams(self):
        self.d.handle("item/commandExecution/outputDelta", {"itemId": "e1", "chunk": "line1\n"})
        self.d.handle("item/commandExecution/outputDelta", {"itemId": "e1", "chunk": "line2\n"})
        card = self.d.store.cards["card-cx-e1-r"]
        self.assertIn("line1", card["body"]["text"])
        self.assertIn("line2", card["body"]["text"])
        self.assertEqual(card["kind"], "tool_result")

    def test_empty_delta_ignored(self):
        before = len(self.d.store.cards)
        self.d.handle("item/reasoning/textDelta", {"itemId": "r1", "delta": ""})
        self.assertEqual(len(self.d.store.cards), before)

    def test_entered_review_mode_is_visible(self):
        cards = carddigest.codex_item_to_cards(
            {"id": "i1", "type": "enteredReviewMode", "prompt": "檢查這段"}, turn_id="t1")
        self.assertEqual(len(cards), 1, "原本落到 return [] → 畫面空白")
        self.assertIn("審查模式", cards[0]["body"]["text"])

    def test_context_compaction_is_visible(self):
        cards = carddigest.codex_item_to_cards(
            {"id": "i2", "type": "contextCompaction"}, turn_id="t1")
        self.assertEqual(len(cards), 1)
        self.assertIn("壓縮", cards[0]["body"]["text"])


if __name__ == "__main__":
    unittest.main()
