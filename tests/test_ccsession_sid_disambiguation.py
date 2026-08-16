"""CC hook sid disambiguation for same-workdir Claude Code sessions."""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="cc-sid-canon-")
os.environ["HOME"] = tempfile.mkdtemp(prefix="cc-sid-home-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class FakeClient:
    host = "127.0.0.1"


class FakeHookRequest:
    client = FakeClient()

    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


class TestCCSessionSidDisambiguation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.workdir = "/Users/xcash/apps/studio-os"
        self.project_dir = tempfile.mkdtemp(prefix="cc-sid-project-")
        self.main_old_sid = "11111111-1111-1111-1111-111111111111"
        self.cc_old_sid = "22222222-2222-2222-2222-222222222222"
        self.cc_new_sid = "33333333-3333-3333-3333-333333333333"
        self.cc_name = "cc-51a85f55"
        self.main_old_jsonl = os.path.join(self.project_dir, self.main_old_sid + ".jsonl")
        self.cc_old_jsonl = os.path.join(self.project_dir, self.cc_old_sid + ".jsonl")
        self.cc_new_jsonl = os.path.join(self.project_dir, self.cc_new_sid + ".jsonl")
        for path in (self.main_old_jsonl, self.cc_old_jsonl, self.cc_new_jsonl):
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}\n")
        now = time.monotonic()
        bridge._CC_SID_CACHE.clear()
        bridge._CC_SID_PINS.clear()
        bridge._CC_SID_HISTORY.clear()
        bridge._CC_HOOK_STATE.clear()
        bridge._cc_cache_sid("Main", self.main_old_sid, now=now)
        bridge._cc_cache_sid(self.cc_name, self.cc_old_sid, now=now)

    async def asyncTearDown(self):
        bridge._CC_SID_CACHE.clear()
        bridge._CC_SID_PINS.clear()
        bridge._CC_SID_HISTORY.clear()
        bridge._CC_HOOK_STATE.clear()

    async def test_clear_sid_hook_routes_to_busy_candidate_and_pins_new_jsonl(self):
        async def fake_capture(name):
            if name == self.cc_name:
                return "esc to interrupt"
            return "idle prompt"

        body = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": self.cc_new_sid,
            "transcript_path": self.cc_new_jsonl,
            "cwd": self.workdir,
        }
        rows = [("Main", self.workdir, "1"), (self.cc_name, self.workdir, "1")]
        with (
            patch.object(bridge, "_cc_conf_rows", return_value=rows),
            patch.object(bridge, "_cc_project_dir", return_value=self.project_dir),
            patch.object(bridge, "_cchist_find", return_value=None),
            patch.object(bridge, "_cc_capture_pane_fresh", side_effect=fake_capture),
            patch.object(bridge, "_cc_write_resume_pin", return_value=None),
            patch.object(bridge, "_log_event", return_value=None),
        ):
            result = await bridge.cc_session_hook(FakeHookRequest(body))

            # busy 輪詢改為 hook 回應後的延後任務(claude 在 hook 回應前不會
            # 開跑,同步輪詢等不到 spinner)——先拿到 deferred,再等背景任務收斂。
            self.assertTrue(result["deferred"])
            await asyncio.gather(*list(bridge._CC_HOOK_BG_TASKS))
            self.assertEqual(bridge._CC_HOOK_STATE[self.cc_name]["busy"], True)
            self.assertNotIn("Main", bridge._CC_HOOK_STATE)
            self.assertEqual(bridge._CC_SID_PINS[self.cc_name], self.cc_new_sid)

            # Simulate the 30s TTL boundary: the cache is stale and the pane
            # cmdline still advertises the old --resume sid. The hook pin must
            # remain authoritative so the app follows the new transcript.
            bridge._CC_SID_CACHE[self.cc_name] = (
                time.monotonic() - bridge._CC_SID_TTL - 1,
                self.cc_old_sid,
            )
            jsonl = await bridge._cc_session_jsonl(self.cc_name, self.workdir)

        self.assertEqual(jsonl, self.cc_new_jsonl)

    async def test_ambiguous_same_cwd_hook_does_not_pollute_first_session(self):
        async def fake_capture(_name):
            return "idle prompt"

        body = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": self.cc_new_sid,
            "transcript_path": self.cc_new_jsonl,
            "cwd": self.workdir,
        }
        rows = [("Main", self.workdir, "1"), (self.cc_name, self.workdir, "1")]
        with (
            patch.object(bridge, "_cc_conf_rows", return_value=rows),
            patch.object(bridge, "_cc_project_dir", return_value=self.project_dir),
            patch.object(bridge, "_cc_capture_pane_fresh", side_effect=fake_capture),
            patch.object(bridge, "_cc_write_resume_pin", return_value=None),
            patch.object(bridge, "_log_event", return_value=None),
            patch.object(bridge, "_CC_HOOK_BUSY_POLL_ATTEMPTS", 2),
            patch.object(bridge, "_CC_HOOK_BUSY_POLL_DELAY", 0.01),
        ):
            result = await bridge.cc_session_hook(FakeHookRequest(body))
            self.assertTrue(result["deferred"])
            # 兩個候選 pane 都閒置 → 延後輪詢也不敢認人,誰都不准被污染。
            await asyncio.gather(*list(bridge._CC_HOOK_BG_TASKS))

        self.assertNotIn("Main", bridge._CC_HOOK_STATE)
        self.assertNotIn(self.cc_name, bridge._CC_HOOK_STATE)
        self.assertNotIn(self.cc_name, bridge._CC_SID_PINS)


class TestCCHookDropObservability(unittest.IsolatedAsyncioTestCase):
    """hook 被丟掉就一定要留痕(2026-07-29)。

    四個早退原本全都靜靜 return 200 + ignored,日誌一個字也沒有 —— 實測全 log
    210 筆 hook POST 只有 66 筆被採用、6 筆記成 ambiguous,138 筆憑空消失,
    追查時完全分不出是哪個閘門擋的。這組測試把「每一條丟棄路徑都要留痕」釘住。
    """

    def setUp(self):
        self.events = []

    def _capture(self, name, **fields):
        self.events.append((name, fields))

    async def _drop(self, body):
        with patch.object(bridge, "_log_event", self._capture):
            result = await bridge.cc_session_hook(FakeHookRequest(body))
        return result

    async def test_untracked_event_is_logged(self):
        r = await self._drop({"hook_event_name": "PreToolUse", "cwd": "/Users/xcash"})
        self.assertTrue(r["ignored"])
        drops = [f for n, f in self.events if n == "cc_hook_dropped"]
        self.assertEqual(len(drops), 1)
        self.assertEqual(drops[0]["reason"], "event_not_tracked")
        self.assertEqual(drops[0]["hook_event_name"], "PreToolUse")

    async def test_non_dict_body_is_logged(self):
        r = await self._drop(["not", "a", "dict"])
        self.assertTrue(r["ignored"])
        self.assertEqual([f["reason"] for n, f in self.events if n == "cc_hook_dropped"],
                         ["body_not_dict"])

    async def test_sid_mismatch_is_logged_with_shape_not_content(self):
        body = {"hook_event_name": "UserPromptSubmit",
                "session_id": "11111111-1111-1111-1111-111111111111",
                "transcript_path": "/tmp/proj/22222222-2222-2222-2222-222222222222.jsonl",
                "cwd": "/Users/xcash",
                "prompt": "使用者打的字,絕對不可以進 log"}
        await self._drop(body)
        drops = [f for n, f in self.events if n == "cc_hook_dropped"]
        self.assertEqual(len(drops), 1)
        self.assertEqual(drops[0]["reason"], "sid_transcript_mismatch")
        # 指認得出是哪個 session、哪個閘門
        self.assertEqual(drops[0]["transcript_basename"],
                         "22222222-2222-2222-2222-222222222222.jsonl")
        self.assertIn("prompt", drops[0]["payload_keys"])
        # 但內容一個字都不准出現
        self.assertNotIn("使用者打的字", json.dumps(drops[0], ensure_ascii=False))

    async def test_transcript_cwd_mismatch_is_logged(self):
        sid = "44444444-4444-4444-4444-444444444444"
        body = {"hook_event_name": "Stop", "session_id": sid,
                "transcript_path": f"/somewhere/else/{sid}.jsonl",
                "cwd": "/Users/xcash"}
        with patch.object(bridge, "_cc_transcript_path_matches_cwd", return_value=False):
            await self._drop(body)
        self.assertEqual([f["reason"] for n, f in self.events if n == "cc_hook_dropped"],
                         ["transcript_cwd_mismatch"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
