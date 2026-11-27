"""Outside-directory read prompts must preserve all four permission choices."""
import _isolation  # noqa: F401

import asyncio
from contextlib import ExitStack
import unittest
from unittest.mock import AsyncMock, patch

import bridge
from test_cc_permission_modal import READ_MODAL


OUTSIDE_READ = """\
  Reading 1 file, running 1 shell command...
────────────────────────────────────────────────────────────────────────────────
 Read outside the working directories
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Read(/Users/you/apps/ops/health_watch.py · lines 290-364)
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Auto mode and the sandbox read outside the working directories without asking.
 Yes or No answers for this read; the rest is for later reads. Block: the file
 tools refuse reads outside the working directories in every project.
 Sandboxed commands lose your home directory (SSH keys, home-installed tools)
 until you re-open paths with sandbox.filesystem.allowRead. To undo, remove
 permissions.blockReadsOutsideWorkingDirectories from your user settings; file
 tools follow next session, sandboxed commands at once.

 Allow this read outside the working directories?
 ❯ 1. Yes, and keep allowing any reads outside the working directories
   2. No, and block reads outside the working directories from now on
   3. No, and ask again next time
   4. Yes, but ask again next time

 Esc to cancel · Tab to amend
────────────────────────────────────────────────────────────────────────── ccs ─
"""


class OutsideReadParseTests(unittest.TestCase):
    def test_four_choices_and_policy_warning_preserved(self):
        prompt = bridge._cc_prompt(OUTSIDE_READ)
        self.assertIsNotNone(prompt)
        self.assertEqual(prompt["semantic"], "permission")
        self.assertTrue(prompt["requires_explicit_choice"])
        self.assertEqual([o["key"] for o in prompt["options"]], ["1", "2", "3", "4"])
        self.assertEqual(prompt["options"][3]["label"], "Yes, but ask again next time")
        payload = bridge._cc_approval_payload("qa-outside-read", prompt)
        self.assertTrue(payload["meta"]["requires_explicit_choice"])
        self.assertIn("health_watch.py", payload["title"])
        self.assertNotIn("SSH keys", payload["title"])
        self.assertIn("SSH keys", payload["detail"])
        self.assertIn("permissions.blockReadsOutsideWorkingDirectories", payload["detail"])
        self.assertEqual([o.get("style") for o in payload["options"]],
                         ["primary", "danger", "danger", "primary"])

    def test_no_branding_and_blank_padding_are_supported(self):
        plain = OUTSIDE_READ.rsplit("\n", 2)[0] + "\n"
        for pane in (plain, OUTSIDE_READ + "\n" * 25):
            self.assertIsNotNone(bridge._cc_prompt(pane))

    def test_legacy_modal_also_allows_ccs_footer(self):
        self.assertEqual(len(bridge._cc_prompt(READ_MODAL + "─" * 30 + " ccs ─\n")["options"]), 3)

    def test_busy_scrollback_does_not_hide_live_modal(self):
        self.assertIsNotNone(bridge._cc_prompt("Working (1m 51s · 6.5k tokens)\nesc to interrupt\n" + OUTSIDE_READ))

    def test_new_composer_or_progress_makes_old_modal_stale(self):
        for tail in ("\n❯\n? for shortcuts\n", "\n❯ next request\n? for shortcuts\n",
                     "\nWorking (1m 51s · 6.5k tokens)\nesc to interrupt\n"):
            self.assertIsNone(bridge._cc_prompt(OUTSIDE_READ + tail))

    def test_arbitrary_trailing_output_is_not_ignored(self):
        for tail in ("tool output", "───────── ccs resumed ─", "❯"):
            self.assertIsNone(bridge._cc_live_permission_prompt(OUTSIDE_READ + tail))

    def test_missing_footer_or_different_question_is_not_guessed(self):
        for pane in (OUTSIDE_READ.replace("Esc to cancel · Tab to amend", ""),
                     OUTSIDE_READ.replace("Allow this read outside the working directories?", "What now?")):
            self.assertIsNone(bridge._cc_prompt(pane))

    def test_same_choices_on_different_paths_have_distinct_signatures(self):
        self.assertNotEqual(bridge._cc_prompt_sig(bridge._cc_prompt(OUTSIDE_READ)),
                            bridge._cc_prompt_sig(bridge._cc_prompt(
                                OUTSIDE_READ.replace("health_watch.py", "other.py"))))

    def test_boolean_choice_cannot_change_future_read_policy(self):
        for approve in (True, False):
            with self.assertRaises(bridge.BridgeError) as err:
                bridge._cc_choice_key(bridge._cc_prompt(OUTSIDE_READ), approve)
            self.assertEqual(err.exception.status_code, 400)


class OutsideReadDecisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.mock("_tmux_alive", AsyncMock(return_value=True))
        self.capture = self.mock("_tmux_capture_cached", AsyncMock(return_value=OUTSIDE_READ))
        self.mock("_cc_conf_rows", return_value=[("qa-outside-read", "/tmp", "1")])
        self.mock("_cc_session_jsonl", AsyncMock(return_value=None))
        self.mock("_cc_fresh_hook_state", return_value={"busy": False})
        self.mock("_cc_refine_q_index", AsyncMock())
        self.mock("_cc_cards_feed_approval")
        self.mock("_cc_read_pin", return_value=None)
        self.send = self.mock("_tmux_run", AsyncMock(return_value=(0, "", "")))
        self.stack.enter_context(patch.dict(bridge._CC_APPROVAL_ACTIVE, {}, clear=True))

    def mock(self, name, *args, **kwargs):
        return self.stack.enter_context(patch.object(bridge, name, *args, **kwargs))

    def pending(self):
        prompt = bridge._cc_prompt(OUTSIDE_READ)
        aid = bridge._cc_approval_create("qa-outside-read", prompt)
        bridge._CC_APPROVAL_ACTIVE["qa-outside-read"] = {"aid": aid, "sig": bridge._cc_prompt_sig(prompt)}
        return aid

    async def test_each_choice_sends_its_own_number_once(self):
        for key in ("1", "2", "3", "4"):
            self.send.reset_mock()
            aid = self.pending()
            result = await bridge._approval_decide_core(aid, {"key": key})
            again = await bridge._approval_decide_core(aid, {"key": key})
            self.assertEqual(result["key"], key)
            self.assertEqual(result["status"], "approved" if key in ("1", "4") else "denied")
            self.assertTrue(again["idempotent"])
            self.send.assert_awaited_once_with("send-keys", "-t", "qa-outside-read", "-l", key)

    async def test_legacy_boolean_or_unoffered_key_never_sends(self):
        aid = self.pending()
        for body in ({"approve": True}, {"approve": False}, {"key": "enter"}, {"key": "9"}):
            with self.assertRaises(bridge.BridgeError) as err:
                await bridge._approval_decide_core(aid, body)
            self.assertEqual(err.exception.status_code, 400)
        self.send.assert_not_awaited()
        self.assertEqual(bridge._approval_get_row(aid)["status"], "pending")

    async def test_explicit_key_wins_over_legacy_boolean_for_receipt(self):
        aid = self.pending()
        result = await bridge._approval_decide_core(aid, {"key": "3", "approve": True})
        self.assertEqual(result["status"], "denied")

    async def test_stale_card_cannot_apply_to_another_file(self):
        aid = self.pending()
        self.capture.return_value = OUTSIDE_READ.replace("health_watch.py", "other.py")
        with self.assertRaises(bridge.HTTPException) as err:
            await bridge._approval_decide_core(aid, {"key": "4"})
        self.assertEqual(err.exception.status_code, 409)
        self.send.assert_not_awaited()

    async def test_prompt_changed_after_status_cannot_receive_the_key(self):
        aid = self.pending()
        self.capture.side_effect = [
            OUTSIDE_READ,
            OUTSIDE_READ.replace("health_watch.py", "other.py"),
        ]
        with self.assertRaises(bridge.HTTPException) as err:
            await bridge._approval_decide_core(aid, {"key": "4"})
        self.assertEqual(err.exception.status_code, 409)
        self.send.assert_not_awaited()

    async def test_v2_card_choice_preserves_allow_once(self):
        aid = self.pending()
        request = AsyncMock()
        request.state.console_remote = False
        request.json.return_value = {"approval_id": aid, "key": "4"}
        self.mock("_check_auth")
        result = await bridge.v2_session_approve("claude_code:qa-outside-read", request)
        self.assertEqual((result["key"], result["status"]), ("4", "approved"))
        self.send.assert_awaited_once_with("send-keys", "-t", "qa-outside-read", "-l", "4")

    async def test_v2_legacy_boolean_is_rejected_without_sending(self):
        aid = self.pending()
        request = AsyncMock()
        request.state.console_remote = False
        request.json.return_value = {"approval_id": aid, "approve": True}
        self.mock("_check_auth")
        with self.assertRaises(bridge.BridgeError) as err:
            await bridge.v2_session_approve("claude_code:qa-outside-read", request)
        self.assertEqual(err.exception.status_code, 400)
        self.send.assert_not_awaited()

    async def test_push_does_not_advertise_ambiguous_yes_no_shortcuts(self):
        self.assertEqual(bridge._approval_decision_keys(self.pending()), (None, None))

    async def test_watcher_only_creates_approval_no_keystroke(self):
        ticks = 0
        async def sleep(_):
            nonlocal ticks
            ticks += 1
            if ticks > 2:
                raise asyncio.CancelledError
        self.stack.enter_context(patch.object(bridge.asyncio, "sleep", sleep))
        self.mock("_cc_approval_scope_names", return_value={"qa-outside-read"})
        self.mock("_cc_find_pending_aid", return_value=None)
        create = self.mock("_cc_approval_create", return_value="qa-read-id")
        self.mock("_approval_get_row", return_value={"id": "qa-read-id"})
        push = self.mock("_approval_push")
        with self.assertRaises(asyncio.CancelledError):
            await bridge._cc_approval_watcher()
        create.assert_called_once()
        push.assert_called_once()
        self.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
