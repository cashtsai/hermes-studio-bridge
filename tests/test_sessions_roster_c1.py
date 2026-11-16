"""C1 roster 契約(grok-build steal #3;APP_BRIDGE_CONTRACT.md §17)。

釘四件事:
  1. activity 六態導出規則 —— needs_input 優先序最高(含 meta.approval
     疊加在 running 上)、degraded→dead、idle/dormant 以活動時間分。
  2. last_turn_summary 規則式 —— 最後一筆持久里程碑截斷 ≤12 字。
  3. GET /app/v2/sessions/roster 形狀與相容(/app/v2/sessions 不動)。
  4. sessions.changed 差分 —— 首包全量、變更只推差分、消失進 removed;
     型別登記為 transient(B1)。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import asyncio
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="c1-roster-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge      # noqa: E402
import carddigest  # noqa: E402


def _row(rid="hermes:boss", provider="hermes", status="idle", meta=None,
         last_event_at=None, title=None):
    return {"id": rid, "provider": provider, "title": title or rid,
            "subtitle": None, "status": status,
            "last_event_at": last_event_at, "capabilities": [],
            "meta": meta or {}}


class TestActivityMapping(unittest.TestCase):
    def test_needs_input_beats_everything(self):
        self.assertEqual(bridge._roster_activity(
            _row(status="waiting_approval"), time.time()), "needs_input")
        # running + pending approval → 還是 needs_input(在等你的事最搶眼)
        self.assertEqual(bridge._roster_activity(
            _row(status="running", meta={"approval": {"id": "x"}}),
            time.time()), "needs_input")

    def test_core_states(self):
        now = time.time()
        self.assertEqual(bridge._roster_activity(_row(status="running"), now),
                         "working")
        self.assertEqual(bridge._roster_activity(_row(status="stalled"), now),
                         "working")
        self.assertEqual(bridge._roster_activity(_row(status="failed"), now),
                         "dead")
        self.assertEqual(bridge._roster_activity(_row(status="done"), now),
                         "completed")

    def test_degraded_runner_is_dead_not_invisible(self):
        self.assertEqual(bridge._roster_activity(
            _row(status="idle", meta={"degraded": True}), time.time()), "dead")

    def test_idle_vs_dormant_by_activity_age(self):
        now = time.time()
        self.assertEqual(bridge._roster_activity(_row(status="idle"), now),
                         "idle")
        old = now - bridge._ROSTER_DORMANT_SECS - 10
        self.assertEqual(bridge._roster_activity(_row(status="idle"), old),
                         "dormant")
        # 從無活動(ts=0, last_event_at 也空)→ dormant
        self.assertEqual(bridge._roster_activity(_row(status="idle"), 0.0),
                         "dormant")

    def test_unknown_status_degrades_gracefully(self):
        self.assertEqual(bridge._roster_activity(
            _row(status="weird_new_thing"), time.time()), "idle")


class TestSummary(unittest.TestCase):
    def test_rule_based_truncation(self):
        self.assertEqual(bridge._roster_summary_text("  部署\n腳本   改好了,下一步跑測試 "),
                         "部署 腳本 改好了,下一步"[:12] + "…")
        self.assertEqual(bridge._roster_summary_text("ok"), "ok")
        self.assertEqual(bridge._roster_summary_text(""), "")

    def test_enrich_reads_last_milestone(self):
        sid = "cc2:c1sum"
        bridge._event_append(sid, "card.milestone", {
            "card": {"id": "c1", "role": "assistant", "final": True,
                     "body": {"text": "第一則"}}})
        seq2 = bridge._event_append(sid, "card.milestone", {
            "card": {"id": "c2", "role": "assistant", "final": True,
                     "body": {"text": "最後一則里程碑文字超過十二個字要截斷"}}})
        self.assertGreater(seq2, 0)
        ts, summary = bridge._roster_enrich([sid])[sid]
        self.assertGreater(ts, 0)
        self.assertTrue(summary.startswith("最後一則里程碑文字"),
                        f"要取最後一筆:{summary!r}")
        self.assertLessEqual(len(summary), bridge._ROSTER_SUMMARY_CHARS + 1)

    def test_enrich_persona_uses_bare_mid(self):
        bridge._event_append("bossmid", "message.upsert", {
            "message": {"id": "m1", "role": "assistant", "content": "早安報告好了"}})
        ts, summary = bridge._roster_enrich(["hermes:bossmid"])["hermes:bossmid"]
        self.assertEqual(summary, "早安報告好了")
        self.assertGreater(ts, 0)


class TestRosterEndpoint(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        self.client = TestClient(bridge.app)
        self.auth = {"Authorization": f"Bearer {bridge.BRIDGE_TOKEN}"}

    def test_requires_auth(self):
        r = self.client.get("/app/v2/sessions/roster",
                            headers={"Authorization": "Bearer wrong"})
        self.assertIn(r.status_code, (401, 403, 429))

    def test_shape_and_derivation(self):
        async def fake_sessions(request, provider="", status="", archived=False):
            return {"sessions": [
                _row("claude_code:Ops", "claude_code", "running"),
                _row("codex:t1", "codex", "waiting_approval"),
            ], "degraded_providers": ["gemini"]}

        with patch.object(bridge, "v2_sessions", fake_sessions):
            r = self.client.get("/app/v2/sessions/roster", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["degraded_providers"], ["gemini"])
        rows = {s["id"]: s for s in body["sessions"]}
        self.assertEqual(set(rows), {"claude_code:Ops", "codex:t1"})
        for s in rows.values():
            for k in ("id", "provider", "title", "activity",
                      "last_turn_summary", "updated_ts"):
                self.assertIn(k, s)
        self.assertEqual(rows["claude_code:Ops"]["activity"], "working")
        self.assertEqual(rows["codex:t1"]["activity"], "needs_input")


class TestSessionsChangedDiff(unittest.TestCase):
    def test_registered_transient(self):
        self.assertIn("sessions.changed", carddigest.RING_TRANSIENT)
        self.assertNotIn("sessions.changed", bridge.EVENT_PERSISTED)

    def test_first_push_is_full_snapshot(self):
        cur = {"a": {"id": "a"}, "b": {"id": "b"}}
        up, rm = bridge._roster_diff({}, cur, first=True)
        self.assertEqual({r["id"] for r in up}, {"a", "b"})
        self.assertEqual(rm, [])

    def test_only_changed_rows_are_pushed(self):
        prev = {"a": {"id": "a", "activity": "idle"},
                "b": {"id": "b", "activity": "working"}}
        cur = {"a": {"id": "a", "activity": "needs_input"},
               "b": {"id": "b", "activity": "working"}}
        up, rm = bridge._roster_diff(prev, cur, first=False)
        self.assertEqual([r["id"] for r in up], ["a"])
        self.assertEqual(rm, [])

    def test_removed_sessions_are_announced(self):
        prev = {"a": {"id": "a"}, "b": {"id": "b"}}
        cur = {"a": {"id": "a"}}
        up, rm = bridge._roster_diff(prev, cur, first=False)
        self.assertEqual(up, [])
        self.assertEqual(rm, ["b"])

    def test_quiet_poll_pushes_nothing(self):
        prev = {"a": {"id": "a", "activity": "idle"}}
        up, rm = bridge._roster_diff(prev, dict(prev), first=False)
        self.assertEqual((up, rm), ([], []))


if __name__ == "__main__":
    unittest.main()
