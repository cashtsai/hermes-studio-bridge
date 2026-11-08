"""Replay fixture 骨架(2026-11-04,t3code #3 後半)。

t3code 的制度:新 provider 必須有錄製的 transcript,重放過真 digest 管線、
斷言投影結果 —— 「fixture 全綠 = 端到端通」,取代人工檢查單。這一檔是
Pocket 版的起點:fixture = [{method, params}] 幀序列,重放進
CodexThreadDigest.handle(),斷言 store 的卡片/狀態/turn 投影。

## 之後怎麼錄真 transcript
bridge 的 app-server 通知入口(CODEX_APP 的 notification dispatch)加一個
POCKET_RECORD_FIXTURE=路徑 環境變數,把 (method, params) 原樣 append 成
jsonl,跑一次真會話就是一份 fixture —— 錄下來放 tests/fixtures/,本檔的
runner 直接吃。合成幀先頂著:形狀照 carddigest.handle 的實際分支寫,
分支改了這裡會紅,就是要的效果。
"""
import _isolation  # noqa: F401
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="replay-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carddigest  # noqa: E402
from live_update_event import LiveUpdateCursor  # noqa: E402
from replay_fixture import (ReplayFixtureError, load_jsonl,
                            validate_manifest)  # noqa: E402


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "replay"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"


def load_manifest() -> dict:
    """Load the reviewed provider/scenario coverage manifest."""
    with MANIFEST_PATH.open(encoding="utf-8") as fh:
        manifest = json.load(fh)
    if manifest.get("schema_version") != 1:
        raise AssertionError("unsupported replay fixture manifest schema")
    validate_manifest(manifest, FIXTURE_DIR)
    return manifest


def load_fixture(name: str) -> list[dict]:
    """Load one reviewed provider transcript fixture from JSONL."""
    try:
        return load_jsonl(FIXTURE_DIR / name)
    except ValueError as exc:
        raise AssertionError(f"invalid replay fixture: {name}") from exc


def replay(frames: list) -> "carddigest.CodexThreadDigest":
    d = carddigest.CodexThreadDigest()
    for f in frames:
        d.handle(f["method"], f.get("params") or {})
    return d


def replay_persona(frames: list, digest):
    """Replay the normalized turn contract used by Persona/Gemini digests."""
    for f in frames:
        method = f["method"]
        params = f.get("params") or {}
        if method == "seed_messages":
            digest.seed_messages(params.get("messages") or [])
        elif method == "turn_begin":
            digest.turn_begin(str(params.get("cid") or ""),
                              str(params.get("label") or ""))
        elif method == "turn_delta":
            digest.turn_delta(str(params.get("cid") or ""),
                              str(params.get("delta") or ""))
        elif method == "turn_status":
            digest.turn_status(str(params.get("label") or ""))
        elif method == "turn_end":
            digest.turn_end(str(params.get("cid") or ""),
                            str(params.get("full_text") or ""),
                            reply_mid=str(params.get("reply_mid") or ""),
                            error=str(params.get("error") or ""))
        else:
            raise AssertionError(f"unsupported persona fixture method: {method}")
    return digest


def replay_openclaw(frames: list) -> "carddigest.OpenClawDigest":
    """Replay gateway event frames without opening a live WebSocket."""
    digest = carddigest.OpenClawDigest()
    for f in frames:
        event = f.get("event")
        if event not in {"chat", "agent"}:
            raise AssertionError(f"unsupported OpenClaw fixture event: {event}")
        digest.handle(event, f.get("payload") or {})
    return digest


def _cards(d):
    return d.store.snapshot()["cards"]


class TestCxSimpleTurn(unittest.TestCase):
    def test_delta_accumulates_into_one_card(self):
        d = replay(load_fixture("cx-simple.jsonl"))
        cards = _cards(d)
        msg = [c for c in cards if c["id"] == "card-cx-i-1"]
        self.assertEqual(len(msg), 1, "同 itemId 的 delta 要累積同一張卡")
        self.assertEqual(msg[0]["body"]["text"], "你好,世界")

    def test_turn_lifecycle_and_busy(self):
        d = carddigest.CodexThreadDigest()
        frames = load_fixture("cx-simple.jsonl")
        d.handle(frames[0]["method"], frames[0]["params"])
        self.assertTrue(d.busy)
        self.assertEqual(d.runtime_status, "running")
        d.handle(frames[-1]["method"], frames[-1]["params"])
        self.assertFalse(d.busy)
        self.assertEqual(d.runtime_status, "done")
        kinds = [e["type"] for e in d.store.events]
        self.assertIn("turn", kinds)
        turns = [e["data"]["state"] for e in d.store.events
                 if e["type"] == "turn"]
        self.assertEqual(turns, ["begin", "end"])


class TestReplayManifest(unittest.TestCase):
    def test_manifest_references_existing_fixture_scenarios(self):
        manifest = load_manifest()
        fixture_names = {p.stem for p in FIXTURE_DIR.glob("*.jsonl")}
        for provider, scenarios in manifest["providers"].items():
            for scenario, filename in scenarios.items():
                self.assertIn(
                    Path(filename).stem,
                    fixture_names,
                    f"{provider} fixture 不存在: {scenario} -> {filename}",
                )

    def test_release_gate_has_explicit_six_scenario_contract(self):
        manifest = load_manifest()
        self.assertEqual(manifest["recorder_version"], "t3code-replay-1")
        self.assertEqual(
            manifest["digest_algorithm"],
            "sha256-canonical-jsonl-v1",
        )
        self.assertTrue(manifest["recording_policy"]["sanitize_before_write"])
        self.assertTrue(manifest["recording_policy"]["owner_only_output"])
        self.assertFalse(manifest["recording_policy"]["raw_transcript_committed"])
        self.assertEqual(
            set(manifest["release_scenarios"]),
            {
                "simple_turn",
                "stream_resume",
                "interrupt_or_unsupported",
                "approval_pending_and_resolved",
                "queued_message",
                "error_terminal",
            },
        )

    def test_release_validator_rejects_partial_provider(self):
        manifest = load_manifest()
        with self.assertRaisesRegex(ReplayFixtureError,
                                    "release fixture scenarios missing"):
            validate_manifest(manifest, FIXTURE_DIR, release=True)

    def test_manifest_rejects_fixture_path_escape(self):
        manifest = load_manifest()
        manifest["providers"] = {"codex": {"simple_turn": "../outside.jsonl"}}
        with self.assertRaisesRegex(ReplayFixtureError, "escapes manifest"):
            validate_manifest(manifest, FIXTURE_DIR)

    def test_manifest_rejects_digest_drift(self):
        manifest = load_manifest()
        manifest["fixture_digests"]["cx-simple.jsonl"] = "0" * 64
        with self.assertRaisesRegex(ReplayFixtureError, "digest mismatch"):
            validate_manifest(manifest, FIXTURE_DIR)

    def test_capability_fixtures_are_explicit_and_present(self):
        manifest = load_manifest()
        capabilities = manifest.get("capability_scenarios") or {}
        self.assertEqual(set(capabilities), {"visual_replies", "live_updates"})
        for capability, scenarios in capabilities.items():
            self.assertTrue(scenarios, f"{capability} must have a scenario")
            for scenario, filename in scenarios.items():
                self.assertTrue(
                    (FIXTURE_DIR / filename).is_file(),
                    f"{capability}/{scenario} fixture missing: {filename}",
                )


class TestCxTurnError(unittest.TestCase):
    def test_error_turn_emits_error_card(self):
        d = replay(load_fixture("cx-error.jsonl"))
        errs = [c for c in _cards(d) if "boom" in
                (c["body"].get("text") or "")]
        self.assertEqual(len(errs), 1)
        self.assertFalse(d.busy)
        self.assertEqual(d.runtime_status, "failed")
        self.assertEqual(d.store.snapshot()["status"]["phase"], "error")
        self.assertEqual([e["data"]["state"] for e in d.store.events
                          if e["type"] == "turn"], ["begin", "end"])
        d.handle("turn/started", {"turn": {"id": "next"}})
        self.assertEqual(d.store.snapshot()["status"]["phase"], "run")


class TestCxApproval(unittest.TestCase):
    def test_approval_card_appears_and_resolves(self):
        d = carddigest.CodexThreadDigest()
        d.handle("turn/started", {"turn": {"id": "t-3"}})
        record = {"id": "codex-appr-1", "title": "允許執行?",
                  "detail": "rm -rf /tmp/x", "risk": "high",
                  "source": "codex:t-3", "kind": "permission",
                  "options": [{"key": "approve", "label": "允許",
                               "style": "primary"},
                              {"key": "deny", "label": "拒絕",
                               "style": "danger"}]}
        d.handle_approval(record)
        pend = [c for c in _cards(d) if c.get("kind") == "approval"]
        self.assertEqual(len(pend), 1, "審批要出卡")
        d.feed_approval(record, resolved="approved")
        done = [c for c in _cards(d) if c.get("kind") == "approval"]
        self.assertEqual(len(done), 1, "決議是原卡收尾,不是第二張")
        self.assertNotEqual(done[0].get("rev"), pend[0].get("rev"))


class TestGeminiSimpleTurn(unittest.TestCase):
    def test_seed_stream_and_terminal_card(self):
        d = replay_persona(load_fixture("gemini-simple.jsonl"),
                           carddigest.GeminiDigest())
        cards = _cards(d)
        self.assertEqual(len([c for c in cards if c["id"] == "card-gm-gm-user"]), 1)
        reply = [c for c in cards if c["id"] == "card-gm-turn-g-1"]
        self.assertEqual(len(reply), 1)
        self.assertEqual(reply[0]["body"]["text"], "GEMINI_OK")
        self.assertTrue(reply[0].get("final"))
        self.assertFalse(d.busy)


class TestOpenClawSimpleTurn(unittest.TestCase):
    def test_gateway_lifecycle_and_chat_projection(self):
        d = replay_openclaw(load_fixture("openclaw-simple.jsonl"))
        cards = _cards(d)
        reply = [c for c in cards if c["id"] == "card-oc-run-1"]
        self.assertEqual(len(reply), 1)
        self.assertEqual(reply[0]["body"]["text"], "OPENCLAW_OK")
        self.assertTrue(reply[0].get("final"))
        self.assertFalse(d.busy)
        turns = [e["data"]["state"] for e in d.store.events
                 if e["type"] == "turn"]
        self.assertEqual(turns, ["begin", "end"])


class TestCapabilityReplay(unittest.TestCase):
    def test_visual_fixture_projects_typed_blocks_and_fallback(self):
        digest = replay_persona(load_fixture("visual-reply.jsonl"),
                                carddigest.PersonaDigest())
        visual_cards = [c for c in _cards(digest)
                        if c.get("body", {}).get("visual")]
        self.assertEqual(len(visual_cards), 1)
        visual = visual_cards[0]["body"]["visual"]
        self.assertEqual(visual["fallback_text"], "看圖與原文")
        self.assertEqual([b["type"] for b in visual["blocks"]],
                         ["image", "link"])

    def test_live_update_fixture_rejects_stale_and_post_terminal_events(self):
        cursor = LiveUpdateCursor("codex:fixture-1")
        applied = []
        for frame in load_fixture("live-update.jsonl"):
            self.assertEqual(frame["event"], "live_update")
            applied.append(cursor.accept(frame["payload"]))
        self.assertEqual(applied, [True, True, False, True, False])
        self.assertEqual(cursor.snapshot(), {
            "session_id": "codex:fixture-1",
            "seq": 13,
            "state": "completed",
            "phase": "完成",
            "terminal": True,
        })


if __name__ == "__main__":
    unittest.main()
