"""OpenClaw gateway broadcasts must not become unsolicited Pocket pushes."""
import _isolation  # noqa: F401
import asyncio
from collections import OrderedDict
from contextlib import ExitStack
import os
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge
import openclaw_provider
from openclaw_push import PushOrigins

KEY = "agent:scout:telegram:direct:fake-user"
SID = "openclaw:" + KEY


def final(run_id="run-1", key=KEY, text="synthetic private reply"):
    return {"sessionKey": key, "runId": run_id, "state": "final",
            "message": {"role": "assistant", "content": text}}


class OriginLedgerTests(unittest.TestCase):
    def test_unknown_and_wrong_session_are_denied(self):
        ledger = PushOrigins()
        self.assertIsNone(ledger.get(KEY, "pocket-guessed"))
        ledger.begin(KEY, "run-1")
        self.assertIsNone(ledger.get("agent:other:pocket", "run-1"))

    def test_expiry_and_restart_fail_closed(self):
        now = [1.0]
        ledger = PushOrigins(ttl=10, clock=lambda: now[0])
        ledger.begin(KEY, "run-1")
        self.assertIsNone(PushOrigins().get(KEY, "run-1"))
        now[0] = 11.0
        self.assertIsNone(ledger.get(KEY, "run-1"))

    def test_alias_and_retry_share_delivery_state(self):
        ledger = PushOrigins()
        origin, created = ledger.begin(KEY, "idem")
        self.assertTrue(created)
        ledger.bind(origin, "run-1")
        origin.final_sent = True
        retry, created = ledger.begin(KEY, "idem")
        self.assertFalse(created)
        self.assertIs(retry, ledger.get(KEY, "run-1"))
        self.assertTrue(retry.final_sent)
        ledger.revoke(origin)
        self.assertIsNone(ledger.get(KEY, "idem"))
        self.assertIsNone(ledger.get(KEY, "run-1"))

    def test_capacity_is_bounded_and_eviction_denies(self):
        ledger = PushOrigins(limit=2)
        for rid in ("r1", "r2", "r3"):
            ledger.begin(KEY, rid)
        self.assertEqual(len(ledger._runs), 2)
        self.assertIsNone(ledger.get(KEY, "r1"))

    def test_config_reset_cannot_be_undone_by_old_pending_ack(self):
        ledger = PushOrigins()
        origin, _ = ledger.begin(KEY, "old-input")
        ledger.clear()
        ledger.bind(origin, "late-ack")
        self.assertIsNone(ledger.get(KEY, "old-input"))
        self.assertIsNone(ledger.get(KEY, "late-ack"))


class PushIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.ledger = PushOrigins()
        self.tasks = set()
        self.send = AsyncMock(return_value=(200, ""))
        self.call = AsyncMock(return_value={"runId": "run-1"})
        self.prefs = {}
        replacements = {
            "_OC_PUSH_ORIGINS": self.ledger,
            "_BG_TASKS": self.tasks, "_OC_CARD_DIGESTS": {},
            "_OC_HEARTBEAT_RUNS": OrderedDict(),
            "_oc_resilience_enabled": lambda: False,
            "_oc_msg_notify": Mock(), "_log_event": Mock(),
            "_record_digest_fixture_frame": Mock(),
            "_oc_call_with_retry": self.call,
            "apns_configured": lambda: True,
            "_push_http2_problem": lambda: None,
            "_devices": lambda: ["fake-a", "fake-b"],
            "_push_prefs_load": lambda: self.prefs,
            "_apns_send": self.send,
            "_approval_decision_keys": lambda aid: (None, None),
        }
        for name, value in replacements.items():
            self.stack.enter_context(patch.object(bridge, name, value, create=True))

    async def drain(self):
        if self.tasks:
            await asyncio.gather(*list(self.tasks))

    async def start(self):
        return await bridge._oc_input_core(
            KEY, SID, {"content": "synthetic request", "client_id": "input-1"})

    async def emit(self, **kwargs):
        bridge._oc_events_feed("chat", final(**kwargs))
        await self.drain()

    async def test_unopened_telegram_run_never_pushes(self):
        await self.emit()
        self.send.assert_not_awaited()

    async def test_opening_session_is_not_notification_consent(self):
        bridge._OC_CARD_DIGESTS[KEY] = Mock()
        await self.emit()
        self.send.assert_not_awaited()

    async def test_prefix_is_not_provenance(self):
        await self.emit(run_id="pocket-someone-elses-run")
        self.send.assert_not_awaited()

    async def test_pocket_input_retains_push(self):
        await self.start()
        await self.emit()
        self.assertEqual(self.send.await_count, 2)
        self.assertEqual(self.send.call_args.kwargs["thread_id"], SID)

    async def test_external_turn_after_pocket_input_stays_private(self):
        await self.start()
        await self.emit(run_id="external-run")
        self.send.assert_not_awaited()

    async def test_run_cannot_authorize_another_session(self):
        await self.start()
        await self.emit(key="agent:ultraman:telegram:direct:fake-other")
        self.send.assert_not_awaited()

    async def test_missing_run_id_fails_closed(self):
        await self.start()
        await self.emit(run_id="")
        self.send.assert_not_awaited()

    async def test_preferences_and_hidden_preview_apply(self):
        self.prefs.update({"fake-a": {"personas": [SID], "preview": False},
                           "fake-b": {"personas": ["other"]}})
        await self.start()
        await self.emit()
        self.assertEqual(self.send.await_count, 1)
        self.assertEqual(self.send.call_args.args[0], "fake-a")
        self.assertNotIn("private", self.send.call_args.args[2])

    async def test_final_and_catchup_cannot_duplicate_push(self):
        await self.start()
        await self.emit()
        await self.emit()
        self.assertEqual(self.send.await_count, 2)

    async def test_fast_final_before_ack_and_alias_are_deduplicated(self):
        async def fast_reply(method, params, **kwargs):
            bridge._oc_events_feed("chat", final(run_id=params["idempotencyKey"]))
            return {"runId": "run-1"}
        self.call.side_effect = fast_reply
        await self.start()
        await self.emit()
        self.assertEqual(self.send.await_count, 2)

    async def test_heartbeat_still_suppressed(self):
        await self.start()
        bridge._OC_HEARTBEAT_RUNS["run-1"] = True
        await self.emit()
        self.send.assert_not_awaited()

    async def test_failed_input_revokes_tentative_grant(self):
        self.call.side_effect = openclaw_provider.OpenClawError("synthetic failure")
        with self.assertRaises(bridge.HTTPException):
            await self.start()
        await self.emit(run_id="pocket-input-1")
        self.send.assert_not_awaited()
        self.assertIsNone(self.ledger.get(KEY, "pocket-input-1"))

    async def test_cancelled_input_revokes_tentative_grant(self):
        self.call.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.start()
        self.assertIsNone(self.ledger.get(KEY, "pocket-input-1"))

    async def test_history_catchup_requires_exact_known_run(self):
        history = AsyncMock(return_value={"messages": [
            {"role": "assistant", "content": "synthetic private history",
             "runId": "external-run"}]})
        self.stack.enter_context(patch.object(bridge.OPENCLAW, "call", history))
        await self.start()
        await bridge._oc_catchup_push(KEY)
        await self.drain()
        self.send.assert_not_awaited()
        history.return_value["messages"][0]["runId"] = "run-1"
        await bridge._oc_catchup_push(KEY)
        await self.drain()
        self.assertEqual(self.send.await_count, 2)

    async def test_approval_notifications_require_known_run(self):
        record = {"id": "fake-approval", "session_id": SID,
                  "turn_id": "turn-external-run", "title": "Synthetic approval",
                  "detail": "synthetic private command"}
        bridge._oc_approval_push(record)
        await self.drain()
        self.send.assert_not_awaited()
        await self.start()
        record["turn_id"] = "turn-run-1"
        self.prefs["fake-b"] = {"personas": []}
        bridge._oc_approval_push(record)
        await self.drain()
        self.assertEqual(self.send.await_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
