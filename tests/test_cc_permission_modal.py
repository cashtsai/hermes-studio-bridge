"""Permission modal regression coverage for Claude Code 2.1.332."""
import _isolation  # noqa: F401

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import bridge


# Sanitized from an isolated 80x24 Read permission prompt, 2026-11-27.
READ_MODAL = """\
Reading 1 file...
────────────────────────────────────────────────────────────────────────────────
 Read file
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Read(/Users/you/workspace/external/scripts/story-material-render.js)
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, allow reading from
      /Users/you/workspace/external/scripts during this
      session
   3. No

 Esc to cancel · Tab to amend

"""


class PermissionModalTests(unittest.TestCase):
    def test_read_permission_remains_permission(self):
        p = bridge._cc_prompt(READ_MODAL)
        self.assertEqual(p["semantic"], "permission")
        self.assertEqual([o["key"] for o in p["options"]], ["1", "2", "3"])

    def test_wrapped_scope_is_visible_on_the_approval_card(self):
        p = bridge._cc_prompt(READ_MODAL)
        row = bridge._cc_approval_payload("test-permission", p)
        self.assertIn("/Users/you/workspace/external/scripts during this session",
                      row["options"][1].get("description", ""))
        self.assertEqual(row["options"][2]["style"], "danger")

    def test_permission_subject_is_visible(self):
        row = bridge._cc_approval_payload("test-permission", bridge._cc_prompt(READ_MODAL))
        self.assertIn("story-material-render.js", row["detail"])

    def test_old_spinner_above_live_modal_cannot_hide_it(self):
        p = bridge._cc_prompt("Working... (1m 51s · 6.5k tokens)\n" + READ_MODAL)
        self.assertIsNotNone(p)

    def test_old_interrupt_hint_above_live_modal_cannot_hide_it(self):
        self.assertIsNotNone(bridge._cc_prompt("Previous status: esc to interrupt\n" + READ_MODAL))

    def test_blank_rows_do_not_hide_a_live_modal(self):
        self.assertIsNotNone(bridge._cc_prompt(READ_MODAL + "\n" * 24))

    def test_new_running_footer_means_the_old_modal_is_stale(self):
        self.assertIsNone(bridge._cc_prompt(READ_MODAL + "\nWorking... (1m 51s · 6.5k tokens)\nesc to interrupt\n"))

    def test_new_idle_composer_means_the_old_modal_is_stale(self):
        self.assertIsNone(bridge._cc_prompt(READ_MODAL + "\n────────────────────────\n❯\n? for shortcuts\n"))

    def test_new_composer_draft_does_not_resurrect_old_permission(self):
        self.assertIsNone(bridge._cc_prompt(READ_MODAL + "\n────────────────────────\n❯ continue\n? for shortcuts\n"))

    def test_same_choices_for_different_files_have_different_identities(self):
        first = bridge._cc_prompt(READ_MODAL)
        second = bridge._cc_prompt(READ_MODAL.replace("story-material-render.js", "another-file.js"))
        self.assertNotEqual(bridge._cc_prompt_sig(first), bridge._cc_prompt_sig(second))

    def test_command_output_without_live_footer_is_not_a_new_modal(self):
        p = READ_MODAL.replace("Esc to cancel · Tab to amend", "")
        self.assertIsNone(bridge._cc_live_permission_prompt(p))

    def test_question_menu_is_not_reclassified_as_permission(self):
        from test_cc_prompt_menu_parse import REAL_PANE
        self.assertEqual(bridge._cc_prompt(REAL_PANE)["semantic"], "question")

    def test_old_numbered_prose_cannot_become_an_extra_option(self):
        p = bridge._cc_prompt("1. Old task\n2. Old task\n" + READ_MODAL)
        self.assertEqual([o["key"] for o in p["options"]], ["1", "2", "3"])

    def test_watcher_creates_card_without_approving(self):
        async def check():
            async def sleep_once(_):
                nonlocal ticks
                ticks += 1
                if ticks > 1:
                    raise asyncio.CancelledError

            ticks = 0
            pane = "Working... (1m 51s · 6.5k tokens)\n" + READ_MODAL
            with patch.object(bridge.asyncio, "sleep", sleep_once), \
                    patch.object(bridge, "_cc_approval_scope_names", return_value={"test-permission"}), \
                    patch.object(bridge, "_cc_conf_rows", return_value=[("test-permission", "/tmp", "1")]), \
                    patch.object(bridge, "_cc_session_jsonl", AsyncMock(return_value=None)), \
                    patch.object(bridge, "_tmux_alive", AsyncMock(return_value=True)), \
                    patch.object(bridge, "_tmux_capture_cached", AsyncMock(return_value=pane)), \
                    patch.object(bridge, "_cc_fresh_hook_state", return_value={"busy": True}), \
                    patch.object(bridge, "_cc_refine_q_index", AsyncMock()), \
                    patch.object(bridge, "_cc_find_pending_aid", return_value=None), \
                    patch.object(bridge, "_cc_approval_create", return_value="test-aid") as create, \
                    patch.object(bridge, "_approval_get_row", return_value={"id": "test-aid"}), \
                    patch.object(bridge, "_approval_push") as push, \
                    patch.object(bridge, "_cc_cards_feed_approval") as feed, \
                    patch.object(bridge, "_cc_key_core", AsyncMock()) as key, \
                    patch.dict(bridge._CC_APPROVAL_ACTIVE, {}, clear=True):
                with self.assertRaises(asyncio.CancelledError):
                    await bridge._cc_approval_watcher()
                create.assert_called_once()
                push.assert_called_once()
                feed.assert_called_once()
                key.assert_not_awaited()

        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
