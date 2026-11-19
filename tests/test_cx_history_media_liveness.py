"""Optional file I/O must not hold CX text history or its SSE connection."""
import _isolation  # noqa: F401
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge
import carddigest


THREAD = "cx-media-liveness-fixture"
PATH = "/tmp/pocket-fixture/report.pdf"
TURNS = [{"id": "turn-1", "items": [{
    "id": "answer-1", "type": "agentMessage", "text": f"Done: {PATH}",
}]}]
CAPTURED = [{"media_id": "fixture-pdf", "available": True,
             "source_ref": PATH, "source_kind": "path", "filename": "report.pdf"}]


class CXHistoryMediaLivenessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.digest = carddigest.CodexThreadDigest()
        self.gate = asyncio.Event()
        self.patches = [
            patch.object(bridge.CODEX_APP, "call", AsyncMock(return_value={"data": TURNS})),
            patch.object(bridge.CODEX_APP, "pending_approval_for_thread", return_value=None),
            patch.object(bridge, "_cx_preview_cache_note"),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        self.gate.set()
        tasks = [getattr(self.digest, "seed_media_task", None),
                 getattr(self.digest.store, "snapshot_media_task", None)]
        for task in tasks:
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        for p in reversed(self.patches):
            p.stop()

    async def blocked_capture(self, fn, *args):
        self.assertIs(fn, bridge._media_capture_sync)
        await self.gate.wait()
        return CAPTURED

    async def test_text_seed_finishes_while_capture_is_blocked(self):
        with patch.object(bridge.asyncio, "to_thread", side_effect=self.blocked_capture):
            await asyncio.wait_for(
                bridge._cx_seed_card_digest(THREAD, self.digest, required=True), .2)
            self.assertTrue(self.digest.seeded)
            self.assertFalse(self.digest.seeding)
            self.assertIn("card-cx-answer-1", self.digest.store.cards)
            self.assertFalse(self.digest.seed_media_task.done())

    async def test_reseed_coalesces_capture_and_does_not_replay_text(self):
        with patch.object(bridge.asyncio, "to_thread", side_effect=self.blocked_capture) as capture:
            await asyncio.wait_for(
                bridge._cx_seed_card_digest(THREAD, self.digest, required=True), .2)
            await asyncio.sleep(0)
            task = self.digest.seed_media_task
            seq = self.digest.store.seq
            for _ in range(3):
                self.digest.last_seed_at = 0
                await bridge._cx_seed_card_digest(THREAD, self.digest)
                self.assertIs(self.digest.seed_media_task, task)
            self.assertEqual(capture.call_count, 1)
            self.assertEqual(self.digest.store.seq, seq)
            self.gate.set()
            await task

    async def test_capture_enriches_existing_card_without_changing_order(self):
        with patch.object(bridge.asyncio, "to_thread", return_value=CAPTURED):
            await bridge._cx_seed_card_digest(THREAD, self.digest, required=True)
            before = dict(self.digest.store.cards["card-cx-answer-1"])
            await self.digest.seed_media_task
            card = self.digest.store.cards["card-cx-answer-1"]
            self.assertEqual(card["ts"], before["ts"])
            self.assertGreater(card["rev"], before["rev"])
            self.assertEqual(card["body"]["attachments"][0]["download_url"],
                             "/app/v2/artifacts/fixture-pdf")
            seq = self.digest.store.seq
            self.digest.last_seed_at = 0
            await bridge._cx_seed_card_digest(THREAD, self.digest)
            await self.digest.seed_media_task
            self.assertEqual(self.digest.store.seq, seq)

    async def test_capture_failure_does_not_degrade_text_seed(self):
        with patch.object(bridge.asyncio, "to_thread", side_effect=OSError("fixture offline")):
            await bridge._cx_seed_card_digest(THREAD, self.digest, required=True)
            await self.digest.seed_media_task
            self.assertTrue(self.digest.seeded)
            self.assertIn("card-cx-answer-1", self.digest.store.cards)
            self.assertFalse(self.digest.seed_degraded_carded)

    async def test_snapshot_has_bounded_wait_and_reuses_pending_scan(self):
        self.digest.seed_turns(TURNS)
        store = self.digest.store
        async def attach(*args):
            await self.gate.wait()
            return 0
        with patch.object(bridge, "_v2_card_store", AsyncMock(return_value=store)), \
                patch.object(bridge, "_media_attach_cards", side_effect=attach) as scan:
            for _ in range(3):
                result = await asyncio.wait_for(
                    bridge._v2_card_store_with_media("codex:" + THREAD), .5)
                self.assertIs(result, store)
            self.assertEqual(scan.call_count, 1)
            self.assertFalse(store.snapshot_media_task.done())
            self.gate.set()
            await store.snapshot_media_task

    async def test_fast_capture_is_present_in_first_snapshot(self):
        self.digest.seed_turns(TURNS)
        store = self.digest.store
        async def attach(*args):
            return carddigest.attach_captured_media(
                store, [bridge._media_wire_item(item) for item in CAPTURED])
        with patch.object(bridge, "_v2_card_store", AsyncMock(return_value=store)), \
                patch.object(bridge, "_media_attach_cards", side_effect=attach):
            result = await bridge._v2_card_store_with_media("codex:" + THREAD)
            self.assertTrue(result.cards["card-cx-answer-1"]["body"]["attachments"])

    async def test_reader_disconnect_does_not_cancel_shared_attachment_scan(self):
        store = self.digest.store
        started = asyncio.Event()
        async def attach(*args):
            started.set()
            await self.gate.wait()
            return 0
        with patch.object(bridge, "_v2_card_store", AsyncMock(return_value=store)), \
                patch.object(bridge, "_media_attach_cards", side_effect=attach):
            reader = asyncio.create_task(bridge._v2_card_store_with_media("codex:" + THREAD))
            await started.wait()
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            self.assertFalse(store.snapshot_media_task.cancelled())
            self.gate.set()
            await store.snapshot_media_task


if __name__ == "__main__":
    unittest.main()
