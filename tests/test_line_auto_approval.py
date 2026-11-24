"""LINE defaults to single-use permissions, never session-wide or question answers."""
import _isolation  # noqa: F401
import asyncio
import os
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge
import carddigest
from line_sessions import Receipts, ReplyTracker, permission_once, wait_reply


def approval(turn="t", **body):
    return carddigest.make_card("approval", turn, "system", "approval", {
        "approval_id": "aid", "request_turn_id": turn, **body}, final=False)


def pending(**changes):
    return {"kind": "permission", "status": "pending", "session_id": "codex:test",
            "created_at": time.time(), "expires_at": time.time() + 60,
            "options": [{"key": "approve", "label": "Allow", "style": "primary"}],
            **changes}


class PolicyTests(unittest.TestCase):
    def test_default_on_and_explicit_opt_out(self):
        with patch.dict(os.environ):
            os.environ.pop("POCKET_LINE_AUTO_APPROVE", None)
            self.assertTrue(bridge._line_auto_approve_enabled())
            for value in ("0", "false", "off", "", "unknown"):
                os.environ["POCKET_LINE_AUTO_APPROVE"] = value
                self.assertFalse(bridge._line_auto_approve_enabled())

    def test_single_use_options_only(self):
        for provider in ("hermes", "cc2", "codex", "openclaw"):
            opts = [{"key": "approve_for_session", "style": "primary"},
                    {"key": "allow-always", "style": "primary"},
                    {"key": "allow-once", "style": "primary"}]
            self.assertEqual(permission_once(provider, pending(options=opts))["key"], "allow-once")
            self.assertIsNone(permission_once(provider, pending(options=opts[:2])))
        for provider in ("gemini", "grok", "opencode"):
            opts = bridge._gm_normalize_options([
                {"optionId": "always", "kind": "allow_always"},
                {"optionId": "once", "kind": "allow_once"}])
            self.assertEqual(permission_once(provider, pending(options=opts))["key"], "once")
            self.assertIsNone(permission_once(provider, pending(options=opts[:1])))
        self.assertIsNone(permission_once("unknown", pending()))

    def test_cc_requires_unambiguous_once_label(self):
        for label in ("Yes", "Allow once", "允許一次"):
            self.assertEqual(permission_once("claude_code", pending(options=[
                {"key": "2", "label": "Yes, and don't ask again", "style": "primary"},
                {"key": "1", "label": label, "style": "primary"}]))["key"], "1")
        self.assertIsNone(permission_once("claude_code", pending(options=[
            {"key": "2", "label": "Yes, always", "style": "primary"}])))

    def test_never_answers_questions_guest_or_terminal_rows(self):
        for kind in ("question", "notice", "guest_message", None):
            self.assertIsNone(permission_once("codex", pending(kind=kind)))
        for status in ("approved", "denied", "expired", "answered"):
            self.assertIsNone(permission_once("codex", pending(status=status)))
        self.assertIsNone(permission_once("codex", pending(options=[
            None, "bad", {"key": "approve", "style": "danger"}])))

    def test_display_turn_is_not_authoritative(self):
        tracker = ReplyTracker("cid", "t")
        self.assertTrue(tracker.owns_permission(approval()))
        for body in ({"request_turn_id": ""}, {"request_turn_id": "other"},
                     {"kind": "question"}, {"resolved": "approved"}):
            self.assertFalse(tracker.owns_permission(approval(**body)))
        tracker.feed({"type": "turn", "data": {"state": "end", "turn_id": "t"}})
        self.assertFalse(tracker.owns_permission(approval()))

    def test_native_echo_and_intervening_user(self):
        store = carddigest.SessionCardStore()
        tracker = ReplyTracker("cid", native_cc=True)
        self.assertFalse(tracker.owns_permission(approval()))
        for name, client in (("mine", "cid"), ("another-device", "different")):
            store.upsert_card(carddigest.make_card(name, "pane", "user", "text", {
                "native_message_id": name, "client_id": client}))
            tracker.feed(store.since(store.seq - 1)[0])
            self.assertEqual(tracker.owns_permission(approval()), name == "mine")
        # An older repeated event must not roll back the lock-time recheck.
        tracker.feed(store.since(0)[0])
        self.assertFalse(tracker.owns_permission(approval()))


class CallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ctx = ExitStack()
        self.addCleanup(self.ctx.close)
        self.ctx.enter_context(patch.dict(os.environ, {"POCKET_LINE_AUTO_APPROVE": "1"}))
        self.entry = {"platform": "line", "scope": "line-session", "session_id": "codex:test"}
        self.ctx.enter_context(patch.object(bridge, "_token_entry", return_value=self.entry))
        self.ctx.enter_context(patch.object(bridge, "_token_is_valid", return_value=True))
        self.row = pending()
        self.ctx.enter_context(patch.object(bridge, "_approval_get_row", side_effect=lambda _: self.row))
        self.decide = self.ctx.enter_context(patch.object(bridge, "_approval_decide_serial",
            AsyncMock(return_value={"status": "approved"})))
        self.ctx.enter_context(patch.object(bridge, "_log_event"))
        self.store = carddigest.SessionCardStore()
        self.card = approval()
        self.store.upsert_card(self.card)
        self.tracker = ReplyTracker("cid", "t")
        self.tracker.feed(self.store.since(0)[0])

    async def run_auto(self):
        await bridge._line_approve_permission("codex:test", "token", self.store,
            time.time() - 1, self.card, self.tracker, 1)

    async def test_uses_existing_decision_core_with_once_key(self):
        await self.run_auto()
        self.decide.assert_awaited_once_with("aid",
            {"key": "approve", "approve": True, "_line_allow_once": True})

    async def test_revoked_switched_pending_and_non_line_binding(self):
        cases = ({}, {"session_id": "codex:other"}, {"platform": "ios"},
                 {"scope": "owner"}, {"pending": True})
        for changes in cases:
            with self.subTest(changes=changes):
                saved = dict(self.entry)
                self.entry.update(changes)
                if not changes:
                    self.entry.clear()
                await self.run_auto()
                self.entry.clear()
                self.entry.update(saved)
        with patch.object(bridge, "_token_is_valid", return_value=False):
            await self.run_auto()
        with patch.dict(os.environ, {"POCKET_LINE_AUTO_APPROVE": "0"}):
            await self.run_auto()
        self.decide.assert_not_awaited()

    async def test_old_expired_wrong_session_and_question_rows(self):
        for change in ({"created_at": 0}, {"expires_at": 0}, {"session_id": "codex:other"},
                       {"kind": "question"}, {"status": "denied"}):
            self.row = pending(**change)
            await self.run_auto()
        self.decide.assert_not_awaited()

    async def test_late_resolution_and_event_gap(self):
        self.store.upsert_card(approval(resolved="denied"))
        await self.run_auto()
        with patch.object(self.store, "since", return_value=None):
            await self.run_auto()
        self.decide.assert_not_awaited()

    async def test_binding_rechecked_after_waiting_for_manual_decision_lock(self):
        lock = asyncio.Lock()
        bridge._APPROVAL_DECISION_LOCKS["aid"] = lock
        await lock.acquire()
        task = asyncio.create_task(self.run_auto())
        await asyncio.sleep(0)
        self.entry["pending"] = True
        lock.release()
        await task
        self.decide.assert_not_awaited()

    async def test_manual_deny_wins_race(self):
        lock = asyncio.Lock()
        bridge._APPROVAL_DECISION_LOCKS["aid"] = lock
        await lock.acquire()
        task = asyncio.create_task(self.run_auto())
        await asyncio.sleep(0)
        self.row["status"] = "denied"
        lock.release()
        await task
        self.decide.assert_not_awaited()

    async def test_decision_failure_stays_manual_without_escaping(self):
        self.decide.side_effect = RuntimeError("provider disconnected")
        await self.run_auto()
        self.assertEqual(self.row["status"], "pending")


class WaiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_line_endpoint_defaults_to_auto_once(self):
        token, sid = "line-test-token", "codex:test"
        cid = "line-" + "a" * 64
        class Request:
            client = None
            headers = {"authorization": "Bearer " + token}
            scope = {"path": "/app/v1/line/messages", "method": "POST"}
            async def json(self):
                return {"session_id": sid, "client_id": cid, "content": "test"}
        store = carddigest.SessionCardStore()
        async def send(*args):
            store.upsert_card(approval())
            return {"turn_id": "t"}
        async def decide(aid, body):
            self.assertEqual(body["key"], "approve")
            store.upsert_card(carddigest.make_card("answer", "t", "assistant", "text",
                                                   {"text": "done"}))
            store.push_turn("end", "t")
            return {"status": "approved"}
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as ctx:
            receipts = Receipts(os.path.join(tmp, "line.db"))
            ctx.enter_context(patch.object(bridge, "_LINE_RECEIPTS", receipts))
            ctx.enter_context(patch.dict(bridge._DEVICE_TOKENS, {token: {
                "platform": "line", "scope": "line-session", "session_id": sid}}))
            ctx.enter_context(patch.object(bridge, "_token_entry", return_value={
                "platform": "line", "scope": "line-session", "session_id": sid}))
            ctx.enter_context(patch.object(bridge, "_token_is_valid", return_value=True))
            ctx.enter_context(patch.object(bridge, "_line_target", AsyncMock()))
            ctx.enter_context(patch.object(bridge, "_v2_card_store", AsyncMock(return_value=store)))
            input_mock = ctx.enter_context(patch.object(bridge, "v2_session_input", AsyncMock(side_effect=send)))
            ctx.enter_context(patch.object(bridge, "_approval_get_row", side_effect=lambda _: pending()))
            decision = ctx.enter_context(patch.object(bridge, "_approval_decide_serial", AsyncMock(side_effect=decide)))
            ctx.enter_context(patch.dict(os.environ, {"POCKET_LINE_AUTO_APPROVE": "1"}))
            accepted = await bridge.line_session_message(Request())
            self.assertEqual(accepted["approval_mode"], "auto-once")
            task = bridge._LINE_TASKS[cid]
            await asyncio.wait_for(task, 1)
            duplicate = await bridge.line_session_message(Request())
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(receipts.pending_delivery()[0]["result"], "done")
            self.assertEqual(input_mock.await_count, 1)
            self.assertEqual(decision.await_count, 1)
            self.assertEqual(store.subscribers, 0)

    async def test_targets_expose_host_policy_for_readonly_verification(self):
        with patch.object(bridge, "v2_sessions", AsyncMock(return_value={"sessions": []})), \
             patch.object(bridge, "_pocket_id_state", return_value={}):
            for flag, expected in (("1", "auto-once"), ("0", "manual")):
                with patch.dict(os.environ, {"POCKET_LINE_AUTO_APPROVE": flag}):
                    self.assertEqual((await bridge.line_targets(None))["approval_mode"], expected)

    async def test_deduplicates_and_preserves_cleanup(self):
        store = carddigest.SessionCardStore()
        card = approval()
        store.upsert_card(card)
        calls = []
        async def decide(card, tracker, cursor):
            calls.append(card["body"]["approval_id"])
            store.upsert_card(card)
            store.upsert_card(carddigest.make_card("answer", "t", "assistant", "text",
                                                   {"text": "finished"}))
            store.push_turn("end", "t")
        self.assertEqual(await wait_reply(store, "cid", 0, "t",
            timeout=.2, approve_permission=decide), "finished")
        self.assertEqual(calls, ["aid"])
        self.assertEqual(store.subscribers, 0)
        self.assertFalse(store._wakers)

    async def test_plain_observer_does_not_auto_approve(self):
        store = carddigest.SessionCardStore()
        store.upsert_card(approval())
        self.assertIn("尚未取得", await wait_reply(store, "cid", 0, "t", timeout=.01))
        self.assertFalse(store.cards["approval"]["final"])

    async def test_late_native_echo_can_claim_existing_request(self):
        store = carddigest.SessionCardStore()
        store.upsert_card(approval())
        called = asyncio.Event()
        async def decide(*args):
            called.set()
        task = asyncio.create_task(wait_reply(store, "cid", 0, timeout=1,
            native_cc=True, approve_permission=decide))
        await asyncio.sleep(0)
        self.assertFalse(called.is_set())
        store.upsert_card(carddigest.make_card("u", "pane", "user", "text",
            {"native_message_id": "u", "client_id": "cid"}))
        await asyncio.wait_for(called.wait(), .2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(store.subscribers, 0)
        self.assertFalse(store._wakers)


class ProviderFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_acp_once_selection_through_real_hub(self):
        options = [{"optionId": "always", "name": "Always", "kind": "allow_always"},
                   {"optionId": "once", "name": "Once", "kind": "allow_once"},
                   {"optionId": "deny", "name": "No", "kind": "reject_once"}]
        for prefix, provider, digest_cls in (
                ("GM", "gemini", carddigest.GeminiDigest),
                ("GK", "grok", carddigest.GrokDigest),
                ("OCE", "opencode", carddigest.OpenCodeDigest)):
            with self.subTest(provider=provider), ExitStack() as ctx:
                d = digest_cls()
                d.turn_begin("cid")
                ctx.enter_context(patch.object(bridge, "_" + prefix + "_TURNS",
                    {"acp-test": {"key": "test", "cid": "cid", "text": ""}}))
                ctx.enter_context(patch.object(bridge, "_" + prefix + "_CARD_DIGESTS", {"test": d}))
                ctx.enter_context(patch.object(bridge, "_" + prefix + "_PERM_TIMEOUT", 2))
                ctx.enter_context(patch.object(bridge, "_approval_push"))
                ctx.enter_context(patch.object(bridge, "_token_is_valid", return_value=True))
                ctx.enter_context(patch.object(bridge, "_token_entry", return_value={
                    "platform": "line", "scope": "line-session", "session_id": provider + ":test"}))
                ctx.enter_context(patch.dict(os.environ, {"POCKET_LINE_AUTO_APPROVE": "1"}))
                handler = getattr(bridge, "_" + prefix.lower() + "_on_request")
                task = asyncio.create_task(handler({"method": "session/request_permission",
                    "params": {"sessionId": "acp-test", "options": options,
                               "toolCall": {"title": "Read test fixture"}}}))
                try:
                    for _ in range(100):
                        await asyncio.sleep(.001)
                        cards = [c for c in d.store.cards.values() if c["kind"] == "approval"]
                        if cards:
                            break
                    self.assertTrue(cards)
                    card = cards[0]
                    self.assertEqual(card["body"]["request_turn_id"], "turn-cid")
                    tracker = ReplyTracker("cid", "turn-cid")
                    for event in d.store.since(0):
                        tracker.feed(event)
                    await bridge._line_approve_permission(provider + ":test", "token",
                        d.store, time.time() - 1, card, tracker, d.store.seq)
                    self.assertEqual(await task,
                        {"outcome": {"outcome": "selected", "optionId": "once"}})
                    self.assertEqual(bridge._approval_get_row(card["body"]["approval_id"])["status"],
                                     "approved")
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    async def test_codex_request_turn_and_single_use_protocol_decision(self):
        client = bridge.CodexAppServerClient()
        d = carddigest.CodexThreadDigest()
        with patch.object(bridge, "_approval_push"), \
             patch.object(bridge, "_cx_cards_feed_approval", side_effect=lambda r, **kw:
                 d.resolve_approval(r, kw["resolved"]) if kw else d.handle_approval(r)), \
             patch.object(client, "_write_server_result", AsyncMock()) as write, \
             patch.object(bridge, "CODEX_APP", client):
            client._handle_approval_request({"id": 123, "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "test", "turnId": "real-turn",
                           "availableDecisions": ["acceptForSession", "accept", "decline"]}})
            record = next(iter(client.pending_approvals.values()))
            card = next(iter(d.store.cards.values()))
            self.assertEqual(card["body"]["request_turn_id"], "real-turn")
            body = permission_once("codex", bridge._approval_get_row(record["id"]))
            await bridge._approval_decide_core(record["id"], body)
            write.assert_awaited_once_with(123, {"decision": "accept"})
            self.assertEqual(bridge._approval_get_row(record["id"])["status"], "approved")

    async def test_openclaw_never_infers_run_id(self):
        for request in ({}, {"runId": "run-1"}):
            row = bridge._oc_approval_record({"id": "oc-test", "request": request}, "exec")
            self.assertEqual(row["turn_id"], "turn-run-1" if request else "")


if __name__ == "__main__":
    unittest.main()

