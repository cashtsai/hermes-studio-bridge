"""Cancel-only handling for the Claude Code auto-mode onboarding dialog."""
import _isolation  # noqa: F401

import asyncio
from contextlib import ExitStack
import time
import unittest
from unittest.mock import AsyncMock, patch

import bridge
import carddigest


ONBOARDING = """\
▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔
  Teach auto mode about your environment?

  Claude Code reads this project, your recent Claude sessions, and optionally
  your shell history and other repositories. Claude analyzes this data and
  customizes auto mode to make better decisions.

    How you use Claude here     Mixed
  ❯ Also scan shell history     on
    Also scan your other repos  off

    Continue

  ↑/↓ to navigate · Space to toggle · Esc to cancel
"""
COMPOSER = "────────────────────────────────────────\n❯\n? for shortcuts\n"
CANCEL = "cancel_onboarding"


class OnboardingPromptTests(unittest.TestCase):
    def test_known_dialog_has_only_a_cancel_action(self):
        prompt = bridge._cc_prompt(ONBOARDING)
        self.assertIsNotNone(prompt)
        self.assertEqual(prompt["semantic"], "notice")
        self.assertEqual([o["key"] for o in prompt["options"]], [CANCEL])
        self.assertIn("shell history", prompt["detail"])
        self.assertIn("other repos", prompt["detail"])

    def test_old_progress_and_blank_rows_do_not_hide_live_dialog(self):
        pane = "Working (1m 51s · 6.5k tokens)\nesc to interrupt\n" + ONBOARDING
        self.assertIsNotNone(bridge._cc_prompt(pane + "\n" * 30))

    def test_composer_or_running_footer_makes_old_dialog_stale(self):
        for tail in (COMPOSER, COMPOSER.replace("❯", "❯ hello"),
                     "Working (1m 51s · 6.5k tokens)\nesc to interrupt\n"):
            with self.subTest(tail=tail):
                self.assertIsNone(bridge._cc_prompt(ONBOARDING + tail))

    def test_unknown_form_or_transcript_is_not_given_cancel_action(self):
        for pane in (ONBOARDING.replace("Teach auto mode about your environment?", "Choose settings"),
                     ONBOARDING.replace("Esc to cancel", "Esc to close"),
                     ONBOARDING.replace("❯", " "),
                     ONBOARDING.replace("Also scan shell history", "Other setting"),
                     ONBOARDING.replace("▔" * 40, "")):
            with self.subTest(pane=pane):
                self.assertIsNone(bridge._cc_prompt(pane))

    def test_focus_changes_keep_the_same_notice_identity(self):
        first = bridge._cc_prompt(ONBOARDING)
        second = bridge._cc_prompt(ONBOARDING.replace("❯ Also", "  Also").replace(
            "    Continue", "  ❯ Continue"))
        self.assertIsNotNone(second)
        self.assertEqual(bridge._cc_prompt_sig(first), bridge._cc_prompt_sig(second))

    def test_notice_card_and_label_do_not_offer_permission_approval(self):
        prompt = bridge._cc_prompt(ONBOARDING)
        record = bridge._cc_approval_payload("qa-onboarding", prompt)
        self.assertEqual(record["kind"], "notice")
        store = carddigest.SessionCardStore()
        store.handle_approval({"id": "qa-notice", **record})
        card = next(iter(store.cards.values()))
        self.assertEqual(card["body"]["kind"], "notice")
        self.assertEqual([o["key"] for o in card["body"]["options"]], [CANCEL])
        self.assertEqual(carddigest.cc_status_label(True, prompt), "等待操作")


class OnboardingFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.alive = self.mock("_tmux_alive", AsyncMock(return_value=True))
        self.capture = self.mock("_tmux_capture_cached", AsyncMock(return_value=ONBOARDING))
        self.mock("_cc_capture_pane_fresh", AsyncMock(return_value=ONBOARDING))
        self.send = self.mock("_tmux_run", AsyncMock(return_value=(0, "", "")))
        self.mock("_cc_conf_rows", return_value=[("qa-onboarding", "/tmp", "1")])
        self.mock("_cc_session_jsonl", AsyncMock(return_value=None))
        self.mock("_cc_read_pin", return_value=None)
        self.mock("_cc_fresh_hook_state", return_value={"busy": True})
        self.mock("_cc_refine_q_index", AsyncMock())
        self.stack.enter_context(patch.dict(bridge._CC_APPROVAL_ACTIVE, {}, clear=True))

    def mock(self, name, *args, **kwargs):
        return self.stack.enter_context(patch.object(bridge, name, *args, **kwargs))

    async def test_live_dialog_is_waiting_even_with_a_stale_busy_hook(self):
        status = await bridge._cc_status_core("qa-onboarding")
        self.assertFalse(status["busy"])
        self.assertIsNone(status["mode"])
        self.assertEqual(status["prompt"]["semantic"], "notice")
        self.send.assert_not_awaited()

    async def test_stale_transcript_question_does_not_replace_live_notice(self):
        self.mock("_cc_session_jsonl", AsyncMock(return_value="/tmp/qa-transcript.jsonl"))
        self.mock("_cc_pending_ask", return_value={"semantic": "question", "options": []})
        self.mock("_cc_scan_jsonl", return_value=(None, None))
        status = await bridge._cc_status_core("qa-onboarding")
        self.assertEqual(status["prompt"]["semantic"], "notice")

    async def test_paste_is_rejected_without_sending_any_keys(self):
        with self.assertRaises(bridge.BridgeError) as err:
            await bridge._cc_paste_text_locked("qa-onboarding", "continue my work")
        self.assertEqual(err.exception.status_code, 409)
        self.send.assert_not_awaited()

    async def test_cancel_sends_only_escape(self):
        await bridge._cc_key_core("qa-onboarding", CANCEL)
        self.send.assert_awaited_once_with("send-keys", "-t", "qa-onboarding", "Escape")

    async def test_stale_cancel_cannot_interrupt_new_work(self):
        for pane in (COMPOSER, ONBOARDING + "\nesc to interrupt\n"):
            self.capture.return_value = pane
            with self.assertRaises(bridge.BridgeError) as err:
                await bridge._cc_key_core("qa-onboarding", CANCEL)
            self.assertEqual(err.exception.status_code, 409)
        self.send.assert_not_awaited()

    async def test_watcher_deduplicates_without_automatically_cancelling(self):
        ticks = 0

        async def sleep(_):
            nonlocal ticks
            ticks += 1
            if ticks > 3:
                raise asyncio.CancelledError

        self.stack.enter_context(patch.object(bridge.asyncio, "sleep", sleep))
        self.mock("_cc_approval_scope_names", return_value={"qa-onboarding"})
        self.mock("_cc_find_pending_aid", return_value=None)
        create = self.mock("_cc_approval_create", return_value="qa-aid")
        self.mock("_approval_get_row", return_value={"id": "qa-aid"})
        push = self.mock("_approval_push")
        feed = self.mock("_cc_cards_feed_approval")
        with self.assertRaises(asyncio.CancelledError):
            await bridge._cc_approval_watcher()
        create.assert_called_once()
        push.assert_called_once()
        feed.assert_called_once()
        self.send.assert_not_awaited()

    def pending_notice(self):
        prompt = bridge._cc_prompt(ONBOARDING)
        aid = bridge._cc_approval_create("qa-onboarding", prompt)
        bridge._CC_APPROVAL_ACTIVE["qa-onboarding"] = {
            "aid": aid, "sig": bridge._cc_prompt_sig(prompt)}
        self.mock("_cc_cards_feed_approval")
        return aid

    async def test_decision_cancel_is_acknowledged_and_idempotent(self):
        aid = self.pending_notice()
        first = await bridge._approval_decide_core(aid, {"key": CANCEL})
        second = await bridge._approval_decide_core(aid, {"key": CANCEL})
        self.assertEqual(first["status"], "acknowledged")
        self.assertTrue(second["idempotent"])
        self.send.assert_awaited_once_with("send-keys", "-t", "qa-onboarding", "Escape")

    async def test_shipped_boolean_clients_can_only_cancel(self):
        for approve in (True, False):
            aid = self.pending_notice()
            self.send.reset_mock()
            result = await bridge._approval_decide_core(aid, {"approve": approve})
            self.assertEqual(result["status"], "acknowledged")
            self.assertEqual(result["key"], CANCEL)
            self.send.assert_awaited_once_with("send-keys", "-t", "qa-onboarding", "Escape")

    async def test_notice_rejects_unoffered_keys(self):
        aid = self.pending_notice()
        for body in ({"key": "enter"}, {"key": "space"}, {"key": "1"},
                     {"key": "esc"}, {}, {"approve": "true"}):
            with self.subTest(body=body), self.assertRaises(bridge.BridgeError) as err:
                await bridge._approval_decide_core(aid, body)
            self.assertEqual(err.exception.status_code, 400)
        self.assertEqual(bridge._approval_get_row(aid)["status"], "pending")
        self.send.assert_not_awaited()

    async def test_notice_expired_if_another_prompt_replaces_it(self):
        from test_cc_permission_modal import READ_MODAL
        aid = self.pending_notice()
        self.capture.return_value = READ_MODAL
        with self.assertRaises(bridge.HTTPException) as err:
            await bridge._approval_decide_core(aid, {"key": CANCEL})
        self.assertEqual(err.exception.status_code, 409)
        self.send.assert_not_awaited()

    async def test_notice_is_rechecked_after_status_before_escape(self):
        aid = self.pending_notice()
        self.capture.side_effect = [ONBOARDING, COMPOSER]
        with self.assertRaises(bridge.BridgeError) as err:
            await bridge._approval_decide_core(aid, {"key": CANCEL})
        self.assertEqual(err.exception.status_code, 409)
        self.send.assert_not_awaited()

    async def test_failed_escape_never_writes_a_success_receipt(self):
        aid = self.pending_notice()
        self.send.return_value = (1, "", "tmux unavailable")
        with self.assertRaises(bridge.BridgeError) as err:
            await bridge._approval_decide_core(aid, {"key": CANCEL})
        self.assertEqual(err.exception.status_code, 502)
        self.assertEqual(bridge._approval_get_row(aid)["status"], "pending")

    async def test_follower_reports_waiting_not_queued_or_finished(self):
        store = carddigest.SessionCardStore()
        store.subscribers = 1
        store.queued_until = time.time() + 120
        self.mock("_cc_card_store", return_value=store)
        self.mock("_cc_card_seed", AsyncMock())
        self.mock("_cc_token_stream_enabled", return_value=False)
        self.mock("_cc_status_core", AsyncMock(side_effect=[
            {"busy": True, "prompt": None},
            {"busy": False, "prompt": bridge._cc_prompt(ONBOARDING)},
        ]))
        ticks = 0

        async def sleep(_):
            nonlocal ticks
            ticks += 1
            store.queued_until = time.time() + 120
            if ticks >= 2:
                raise asyncio.CancelledError

        self.stack.enter_context(patch.object(bridge.asyncio, "sleep", sleep))
        with self.assertRaises(asyncio.CancelledError):
            await bridge._cc_card_follower("qa-onboarding", "/tmp")
        self.assertEqual(store.status["phase"], "blocked")
        self.assertEqual(store.status["label"], "等待操作")
        self.assertFalse(store.status["busy"])
        self.assertFalse(any(e["type"] == "turn.end" for e in store.events))
        self.assertEqual(store.snapshot()["status"], store.status)
        self.assertEqual([e["data"] for e in store.events if e["type"] == "session.status"][-1],
                         store.status)

    async def test_http_status_cards_and_cancel_contract(self):
        import httpx

        self.mock("BRIDGE_TOKEN", "test-onboarding-token")
        aid = self.pending_notice()
        store = carddigest.SessionCardStore()
        store.handle_approval(bridge._approval_get_row(aid))
        self.mock("_v2_card_store_with_media", AsyncMock(return_value=store))
        self.mock("_cc_read_spawn_config", return_value={})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app),
                                     base_url="http://localhost") as client:
            auth = {"Authorization": "Bearer test-onboarding-token"}
            path = "/app/v2/sessions/claude_code:qa-onboarding"
            unauthorized = await client.get("/ccsessions/qa-onboarding/status")
            self.assertEqual(unauthorized.status_code, 401)
            status = await client.get("/ccsessions/qa-onboarding/status", headers=auth)
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json()["prompt"]["semantic"], "notice")
            seqs = []
            for _ in range(3):
                snap = await client.get(path + "/cards", headers=auth)
                self.assertEqual(snap.status_code, 200)
                seqs.append(snap.json()["latest_seq"])
                self.assertEqual(snap.json()["cards"][0]["body"]["options"][0]["key"], CANCEL)
            self.assertEqual(len(set(seqs)), 1)
            for body in ({"approval_id": aid, "approve": True},
                         {"approval_id": aid, "key": CANCEL}):
                result = await client.post(path + "/approve", json=body, headers=auth)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["status"], "acknowledged")
        self.send.assert_awaited_once_with("send-keys", "-t", "qa-onboarding", "Escape")


if __name__ == "__main__":
    unittest.main()
