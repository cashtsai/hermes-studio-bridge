"""Replay fixture 骨架(2026-11-04,t3code #3 後半)。

t3code 的制度:新 provider 必須有錄製的 transcript,重放過真 digest 管線、
斷言投影結果 —— 「fixture 全綠 = 端到端通」,取代人工檢查單。這一檔是
Pocket 版的起點:fixture = [{method, params}] 幀序列,重放進
CodexThreadDigest.handle(),斷言 store 的卡片/狀態/turn 投影。

## 之後怎麼錄真 transcript
bridge 的 app-server 通知入口(CODEX_APP 的 notification dispatch)加一個
POCKET_RECORD_FIXTURE=路徑 環境變數,把 (method, params) 原樣 append 成
jsonl,跑一次真會話就是一份 fixture —— 錄下來放 tests/fixtures/,本檔的
runner 直接吃。合成幀先頂著:形狀照 carddigest.handle 的實際分支寫,
分支改了這裡會紅,就是要的效果。
"""
import _isolation  # noqa: F401
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="replay-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carddigest  # noqa: E402


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "replay"


def load_fixture(name: str) -> list[dict]:
    """Load one reviewed provider transcript fixture from JSONL."""
    frames = []
    with (FIXTURE_DIR / name).open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                frame = json.loads(line)
            except ValueError as exc:
                raise AssertionError(f"invalid JSON in {name}:{line_no}") from exc
            if not isinstance(frame, dict) or not frame.get("method"):
                raise AssertionError(f"invalid frame in {name}:{line_no}")
            frames.append(frame)
    return frames


def replay(frames: list) -> "carddigest.CodexThreadDigest":
    d = carddigest.CodexThreadDigest()
    for f in frames:
        d.handle(f["method"], f.get("params") or {})
    return d


def _cards(d):
    return d.store.snapshot()["cards"]


class TestCxSimpleTurn(unittest.TestCase):
    def test_delta_accumulates_into_one_card(self):
        d = replay(load_fixture("cx-simple.jsonl"))
        cards = _cards(d)
        msg = [c for c in cards if c["id"] == "card-cx-i-1"]
        self.assertEqual(len(msg), 1, "同 itemId 的 delta 要累積同一張卡")
        self.assertEqual(msg[0]["body"]["text"], "你好,世界")

    def test_turn_lifecycle_and_busy(self):
        d = carddigest.CodexThreadDigest()
        frames = load_fixture("cx-simple.jsonl")
        d.handle(frames[0]["method"], frames[0]["params"])
        self.assertTrue(d.busy)
        self.assertEqual(d.runtime_status, "running")
        d.handle(frames[-1]["method"], frames[-1]["params"])
        self.assertFalse(d.busy)
        self.assertEqual(d.runtime_status, "done")
        kinds = [e["type"] for e in d.store.events]
        self.assertIn("turn", kinds)
        turns = [e["data"]["state"] for e in d.store.events
                 if e["type"] == "turn"]
        self.assertEqual(turns, ["begin", "end"])


class TestCxTurnError(unittest.TestCase):
    def test_error_turn_emits_error_card(self):
        d = replay(load_fixture("cx-error.jsonl"))
        errs = [c for c in _cards(d) if "boom" in
                (c["body"].get("text") or "")]
        self.assertEqual(len(errs), 1)
        self.assertFalse(d.busy)


class TestCxApproval(unittest.TestCase):
    def test_approval_card_appears_and_resolves(self):
        d = carddigest.CodexThreadDigest()
        d.handle("turn/started", {"turn": {"id": "t-3"}})
        record = {"id": "codex-appr-1", "title": "允許執行?",
                  "detail": "rm -rf /tmp/x", "risk": "high",
                  "source": "codex:t-3", "kind": "permission",
                  "options": [{"key": "approve", "label": "允許",
                               "style": "primary"},
                              {"key": "deny", "label": "拒絕",
                               "style": "danger"}]}
        d.handle_approval(record)
        pend = [c for c in _cards(d) if c.get("kind") == "approval"]
        self.assertEqual(len(pend), 1, "審批要出卡")
        d.feed_approval(record, resolved="approved")
        done = [c for c in _cards(d) if c.get("kind") == "approval"]
        self.assertEqual(len(done), 1, "決議是原卡收尾,不是第二張")
        self.assertNotEqual(done[0].get("rev"), pend[0].get("rev"))


if __name__ == "__main__":
    unittest.main()
