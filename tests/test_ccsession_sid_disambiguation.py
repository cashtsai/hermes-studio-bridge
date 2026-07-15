"""CC hook sid disambiguation for same-workdir Claude Code sessions."""

import asyncio
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
