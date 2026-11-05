"""Contract tests for the T3Code durable primitives."""
from __future__ import annotations

import tempfile
import subprocess
import unittest
from pathlib import Path

import t3code_runtime as t3


class T3CodeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "canonical.db")
        t3.ensure_schema(self.db)
        self.app = t3.create_app_thread(self.db, "xcash", "round2")
        self.provider = t3.link_provider_thread(
            self.db, self.app["id"], "codex", "thread-1",
            {"streaming": "live", "interrupt": True})

    def tearDown(self):
        self.tmp.cleanup()

    def test_app_thread_provider_link_is_stable_and_readable(self):
        again = t3.link_provider_thread(
            self.db, self.app["id"], "codex", "thread-1",
            {"streaming": "live"})
        self.assertEqual(again["id"], self.provider["id"])
        loaded = t3.get_app_thread(self.db, self.app["id"])
        self.assertEqual(len(loaded["provider_threads"]), 1)
        self.assertEqual(loaded["provider_threads"][0]["native_id"], "thread-1")

    def test_context_transfer_is_redacted_and_idempotent(self):
        first = t3.create_context_transfer(
            self.db, self.app["id"], self.provider["id"], "claude_code",
            "continue the reviewed work", [{"key": "decision", "value": "ship"}],
            [{"media_id": "med_1", "sha256": "abc"}], [])
        second = t3.create_context_transfer(
            self.db, self.app["id"], self.provider["id"], "claude_code",
            "continue the reviewed work", [{"key": "decision", "value": "ship"}],
            [{"media_id": "med_1", "sha256": "abc"}], [], first["id"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["envelope_hash"], first["envelope_hash"])
        with self.assertRaises(t3.T3CodeError):
            t3.create_context_transfer(
                self.db, self.app["id"], self.provider["id"], "claude_code",
                "do not leak", [{"api_key": "should-not-pass"}], [], [])

    def test_context_transfer_state_machine_requires_approval(self):
        transfer = t3.create_context_transfer(
            self.db, self.app["id"], self.provider["id"], "claude_code",
            "continue reviewed work")
        with self.assertRaises(t3.T3CodeError):
            t3.transition_context_transfer(self.db, transfer["id"], "approved")
        approved = t3.transition_context_transfer(
            self.db, transfer["id"], "approved", approval_id="approval-1")
        self.assertEqual(approved["status"], "approved")
        target = t3.transition_context_transfer(
            self.db, transfer["id"], "target_created", approval_id="approval-1",
            target_provider_thread_id="provider-target")
        self.assertEqual(target["status"], "target_created")
        applied = t3.transition_context_transfer(
            self.db, transfer["id"], "applied", approval_id="approval-1",
            target_provider_thread_id="provider-target")
        self.assertEqual(applied["status"], "applied")
        with self.assertRaises(t3.T3CodeError):
            t3.transition_context_transfer(self.db, transfer["id"], "dry_run")

    def test_checkpoint_is_read_only_and_inside_worktree(self):
        manifest = t3.build_checkpoint_manifest(self.tmp.name, self.app["id"],
                                                 self.provider["id"])
        self.assertTrue(manifest["read_only"])
        checkpoint = t3.create_checkpoint(self.db, manifest)
        self.assertEqual(checkpoint["status"], "dry_run")
        self.assertEqual(checkpoint["manifest"]["cwd"], str(Path(self.tmp.name).resolve()))
        same = t3.create_checkpoint(self.db, manifest, checkpoint["id"])
        self.assertEqual(same["manifest_hash"], checkpoint["manifest_hash"])
        with self.assertRaises(t3.T3CodeError):
            t3.create_checkpoint(self.db, {**manifest, "dirty": not manifest["dirty"]}, checkpoint["id"])

    def test_checkpoint_ref_requires_approval_and_restores_clean_worktree(self):
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        def run(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                  capture_output=True, text=True)
        run("init", "-q")
        run("config", "user.email", "test@example.invalid")
        run("config", "user.name", "T3Code Test")
        file = repo / "state.txt"
        file.write_text("before\n")
        run("add", "state.txt")
        run("commit", "-qm", "before")
        manifest = t3.build_checkpoint_manifest(str(repo), self.app["id"], self.provider["id"])
        checkpoint = t3.create_checkpoint(self.db, manifest, "checkpoint-1")
        with self.assertRaises(t3.T3CodeError):
            t3.materialize_checkpoint_ref(self.db, checkpoint["id"], "")
        materialized = t3.materialize_checkpoint_ref(
            self.db, checkpoint["id"], "approval-1")
        self.assertEqual(materialized["restore_status"], "available")
        file.write_text("after\n")
        run("add", "state.txt")
        run("commit", "-qm", "after")
        restored = t3.restore_checkpoint(
            self.db, checkpoint["id"], "approval-1", confirm=True)
        self.assertEqual(restored["restore_status"], "restored")
        self.assertEqual(file.read_text(), "before\n")

    def test_legacy_projection_is_deterministic_and_detaches_terminal_rows(self):
        rows = [{"id": "dlg-1", "parent_persona": "xcash",
                 "provider": "codex", "provider_session_id": "thread-old",
                 "title": "Old work", "status": "done"}]
        first = t3.project_legacy_delegations(self.db, rows)
        second = t3.project_legacy_delegations(self.db, rows)
        self.assertEqual(first["projected"], 1)
        self.assertEqual(second["projected"], 1)
        app_id = "app_legacy_" + __import__("hashlib").sha256(
            b"xcash:dlg-1").hexdigest()[:24]
        app_rows = t3.get_app_thread(self.db, app_id)
        self.assertEqual(app_rows["provider_threads"][0]["status"], "detached")

    def test_usage_samples_are_deduped_and_summarised(self):
        usage = {"input_tokens": 10, "output_tokens": 4,
                 "cachedInputTokens": 2, "total_tokens": 14,
                 "cost_usd": 0.01}
        a = t3.record_usage_sample(self.db, "codex:thread-1", "codex", usage,
                                   model="gpt-5-codex", dedupe_key="turn-1")
        b = t3.record_usage_sample(self.db, "codex:thread-1", "codex", usage,
                                   model="gpt-5-codex", dedupe_key="turn-1")
        self.assertEqual(a["id"], b["id"])
        summary = t3.usage_summary(self.db, 0)
        self.assertEqual(summary["codex"]["samples"], 1)
        self.assertEqual(summary["codex"]["total_tokens"], 14)

    def test_live_activity_token_registration_is_replaceable(self):
        first = t3.register_live_activity(
            self.db, "codex:thread-1", "abc", "com.pocketagent.kernel")
        second = t3.register_live_activity(
            self.db, "codex:thread-1", "def", "com.pocketagent.kernel")
        self.assertNotEqual(first["updated_at"], 0)
        self.assertEqual(t3.get_live_activity(self.db, "codex:thread-1")["token"], "def")
        self.assertTrue(t3.remove_live_activity(self.db, "codex:thread-1"))
        self.assertIsNone(t3.get_live_activity(self.db, "codex:thread-1"))


if __name__ == "__main__":
    unittest.main()
