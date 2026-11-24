import _isolation  # noqa: F401
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge
import carddigest
from fastapi.testclient import TestClient
from line_sessions import PROVIDERS, Receipts, ReplyTracker, wait_reply


class TrackerTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_correlated_turn_and_final_prose(self):
        store = carddigest.SessionCardStore()
        store.upsert_card(carddigest.make_card("old", "t0", "assistant", "markdown", {"text": "other device"}))
        store.push_turn("end", "t0")
        store.upsert_card(carddigest.make_card("u", "t1", "user", "text", {"client_id": "cid"}))
        store.upsert_card(carddigest.make_card("thinking", "t1", "assistant", "text", {"text": "💭 private reasoning"}))
        store.upsert_card(carddigest.make_card("tool", "t1", "assistant", "tool_call", {"text": "private command"}))
        store.upsert_card(carddigest.make_card("a", "t1", "assistant", "markdown", {"text": "answer"}))
        store.push_turn("end", "t1")
        self.assertEqual(await wait_reply(store, "cid", 0, timeout=.1), "answer")
        self.assertEqual(store.subscribers, 0)
        self.assertFalse(store._wakers)

    async def test_queue_does_not_return_previous_turn(self):
        t = ReplyTracker("cid")
        t.feed({"type": "card.upsert", "data": {"card": carddigest.make_card(
            "u", "old", "user", "text", {"client_id": "cid", "delivery": "queued"})}})
        t.feed({"type": "turn", "data": {"state": "end", "turn_id": "old"}})
        self.assertIsNone(t.result())

    async def test_gap_and_timeout_are_honest(self):
        store = carddigest.SessionCardStore(ring_max=1)
        store.push_turn("end", "old")
        store.push_turn("end", "old2")
        self.assertIn("追蹤範圍", await wait_reply(store, "cid", 0, timeout=.01))
        self.assertIn("尚未取得", await wait_reply(store, "cid", store.seq, timeout=.01))

    async def test_persona_overlap_keeps_original_turn(self):
        d = carddigest.PersonaDigest()
        d.turn_begin("one")
        d.turn_begin("two")
        d.turn_end("one", "first")
        self.assertTrue(d.busy)
        self.assertEqual(await wait_reply(d.store, "cid", 0, "turn-one", timeout=.1), "first")
        d.turn_end("two", "second")
        self.assertEqual(await wait_reply(d.store, "cid", 0, "turn-two", timeout=.1), "second")

    async def test_cc_native_ancestry_not_pane_turn_ids(self):
        store = carddigest.SessionCardStore()
        def add(cid, role, parent, text, client=""):
            body = {"native_message_id": cid, "native_parent_id": parent, "text": text}
            if client:
                body["client_id"] = client
            store.upsert_card(carddigest.make_card(cid, "unreliable-pane-turn", role,
                "markdown" if role == "assistant" else "text", body))
        add("prior", "assistant", "", "other task")
        add("u", "user", "prior", "my message", "cid")
        add("a", "assistant", "u", "my answer")
        add("u2", "user", "a", "another device message")
        add("a2", "assistant", "u2", "another device answer")
        store.set_status({"busy": False, "phase": "idle"})
        self.assertEqual(await wait_reply(store, "cid", 0, timeout=.1, native_cc=True), "my answer")

    async def test_cc_missing_ancestry_does_not_guess(self):
        store = carddigest.SessionCardStore()
        store.upsert_card(carddigest.make_card("u", "t", "user", "text", {
            "native_message_id": "u", "client_id": "cid"}))
        store.upsert_card(carddigest.make_card("a", "t", "assistant", "markdown", {
            "native_message_id": "a", "native_parent_id": "missing", "text": "not proven"}))
        store.push_turn("end", "t")
        store.set_status({"busy": False, "phase": "idle"})
        self.assertIn("尚未取得", await wait_reply(store, "cid", 0, timeout=.01, native_cc=True))

    async def test_cc_missing_native_echo_ignores_pane_turn(self):
        store = carddigest.SessionCardStore()
        store.upsert_card(carddigest.make_card("u", "t", "user", "text", {"client_id": "cid"}))
        store.upsert_card(carddigest.make_card("a", "t", "assistant", "markdown", {"text": "not proven"}))
        store.push_turn("end", "t")
        self.assertIn("尚未取得", await wait_reply(store, "cid", 0, "t", timeout=.01, native_cc=True))


class ReceiptTests(unittest.TestCase):
    def test_durable_claim_conflict_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "line.db")
            r = Receipts(path)
            self.assertTrue(r.claim("id", "secret", "codex:t", "message")[0])
            self.assertFalse(Receipts(path).claim("id", "secret", "codex:t", "message")[0])
            with self.assertRaises(ValueError):
                r.claim("id", "other secret", "codex:t", "message")
            r.recover()
            row = r.pending_delivery()[0]
            self.assertEqual(row["status"], "interrupted")
            self.assertNotIn("secret", str(row))
            self.assertNotIn("message", str(row))
            r.delivered("id")
            self.assertEqual(r.pending_delivery(), [])

    def test_delivery_backoff_and_retry_horizon(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = Receipts(os.path.join(tmp, "line.db"))
            r.claim("id", "token", "codex:t", "prompt")
            r.finish("id", "done", "answer")
            row = r.pending_delivery()[0]
            r.attempted(row)
            self.assertEqual(r.pending_delivery(), [])
            with r.connect() as db:
                db.execute("UPDATE line_receipts SET created=0,next_attempt=0")
            self.assertEqual(r.pending_delivery(), [])


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.receipts = Receipts(os.path.join(self.tmp.name, "line.db"))
        self.patches = [patch.object(bridge, "_LINE_RECEIPTS", self.receipts),
                        patch.object(bridge, "_save_device_tokens", lambda _: None)]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        tasks = list(bridge._LINE_TASKS.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    async def test_every_provider_reuses_input_and_same_session(self):
        for provider in sorted(PROVIDERS):
            sid = provider + ":known"
            token = "test-line-" + provider
            bridge._DEVICE_TOKENS[token] = {"platform": "line", "scope": "line-session", "session_id": sid}
            cid = "line-" + bridge.line_sessions.digest(provider)
            class Req:
                headers = {"authorization": "Bearer " + token}
                scope = {"path": "/app/v1/line/messages", "method": "POST"}
                async def json(self):
                    return {"session_id": sid, "client_id": cid, "content": "hello"}
            store = carddigest.SessionCardStore()
            async def send(target, request):
                self.assertEqual(target, sid)
                self.assertEqual((await request.json())["client_id"], cid)
                store.upsert_card(carddigest.make_card("u", "t", "user", "text", {
                    "client_id": cid, "native_message_id": "u"}))
                store.upsert_card(carddigest.make_card("a", "t", "assistant", "markdown", {
                    "text": provider + " answer", "native_message_id": "a", "native_parent_id": "u"}))
                store.push_turn("end", "t")
                store.set_status({"busy": False, "phase": "idle"})
                return {"accepted": True}
            with patch.object(bridge, "_line_target", AsyncMock()), \
                 patch.object(bridge, "_v2_card_store", AsyncMock(return_value=store)), \
                 patch.object(bridge, "v2_session_input", AsyncMock(side_effect=send)) as input_mock:
                out = await bridge.line_session_message(Req())
                self.assertTrue(out["accepted"])
                await asyncio.wait_for(bridge._LINE_TASKS[cid], timeout=1)
                duplicate = await bridge.line_session_message(Req())
                self.assertTrue(duplicate["duplicate"])
                self.assertEqual(input_mock.await_count, 1)
                fresh, receipt = self.receipts.claim(cid, token, sid, "hello")
                self.assertFalse(fresh)
                self.assertEqual(receipt["result"], provider + " answer")
            bridge._DEVICE_TOKENS.pop(token)
        self.assertEqual(len(self.receipts.pending_delivery()), len(PROVIDERS))

    async def test_targets_use_roster_and_drop_unknown_readonly_offline(self):
        rows = [{"id": p + ":x", "provider": p, "capabilities": ["input"], "status": "idle"} for p in PROVIDERS]
        rows += [{"id": "terminal:x", "provider": "terminal", "capabilities": ["input"]},
                 {"id": "codex:ro", "provider": "codex", "capabilities": []},
                 {"id": "codex:off", "provider": "codex", "capabilities": ["input"], "status": "offline"}]
        with patch.object(bridge, "v2_sessions", AsyncMock(return_value={"sessions": rows})), \
             patch.object(bridge, "_pocket_id_state", return_value={"host_id": "host-test"}):
            out = await bridge.line_targets(None)
        self.assertEqual(len(out["targets"]), len(PROVIDERS))
        self.assertEqual(out["host_id"], "host-test")


class ScopeTests(unittest.TestCase):
    def test_pending_activation_rotation_scope_and_expiry(self):
        client = TestClient(bridge.app)
        master = {"Authorization": "Bearer " + bridge.BRIDGE_TOKEN}
        with patch.object(bridge, "_line_target", AsyncMock()), \
             patch.object(bridge, "_save_device_tokens", lambda _: None):
            old = client.post("/app/v1/line/token", headers=master).json()["token"]
            tok = client.post("/app/v1/line/token", headers=master,
                              json={"session_id": "codex:t"}).json()["token"]
            auth = {"Authorization": "Bearer " + tok}
            self.assertIn(old, bridge._DEVICE_TOKENS)
            self.assertEqual(client.get("/app/v1/line/binding", headers=auth).json()["session_id"], "codex:t")
            self.assertEqual(client.post("/app/v1/line/messages", headers=auth, json={}).status_code, 403)
            self.assertEqual(client.post("/app/v1/line/activate", headers=auth).status_code, 200)
            self.assertNotIn(old, bridge._DEVICE_TOKENS)
            for method, path in [("GET", "/pair/devices"), ("POST", "/app/v1/line/token"),
                                 ("POST", "/app/v2/sessions/codex:t/approve"),
                                 ("POST", "/app/v2/sessions/codex:other/input")]:
                self.assertEqual(client.request(method, path, headers=auth).status_code, 403)
            self.assertEqual(client.post("/app/v1/line/messages", headers=auth, json={
                "session_id": "codex:other", "client_id": "line-" + "a" * 64, "content": "hi"}).status_code, 422)
            bridge._DEVICE_TOKENS[tok].update(pending=True, expires=1)
            self.assertEqual(client.get("/app/v1/line/binding", headers=auth).status_code, 401)
            bridge._DEVICE_TOKENS.pop(tok, None)


if __name__ == "__main__":
    unittest.main()
