import _isolation  # noqa: F401
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge
import media_artifacts
import session_reports
from fastapi.testclient import TestClient


class SessionReportsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.media = media_artifacts.MediaArtifactStore(root / "archive", safe_roots=[str(root)])
        self.path = root / "report with spaces.md"
        self.path.write_text("# Release report\n\n|Check|Result|\n|---|---|\n|API|PASS|\n")
        self.item = self.media.capture_path("codex:a", str(self.path))
        self.other = self.media.capture_path("codex:b", str(self.path))
        self.tool_path = root / "tool-only.md"
        self.tool_path.write_text("never delivered")
        self.tool = self.media.capture_path("codex:a", str(self.tool_path))
        self.cards = {"a": {"role": "agent", "kind": "text", "body": {"text": f"[Report](<{self.path}:12>)"}},
                      "b": {"role": "tool", "kind": "text", "body": {"text": f"[Tool]({self.tool_path})"}}}
        token = "test-report-browser"
        self.token = token
        self.h = {"Authorization": "Bearer " + token}
        values = {
            "_DEVICE_TOKENS": {token: {"scope": bridge.CONSOLE_REMOTE_SCOPE, "expires": time.time()+600,
                                      "created": time.time(), "last_seen": time.time()}},
            "_CONSOLE_GRANTS": {bridge._console_grant_key(token): (time.monotonic(), {
                "codex:a": ({"replay"}, None, {}, False, None)})},
            "_media_store": lambda: self.media,
            "_v2_card_store": AsyncMock(return_value=SimpleNamespace(cards=self.cards)),
            "_console_uses_relay_policy": lambda: False,
            "_save_device_tokens": lambda *_: None,
        }
        for key, value in values.items():
            p = patch.object(bridge, key, value); p.start(); self.addCleanup(p.stop)
        self.web = TestClient(bridge.app)
        self.addCleanup(self.web.close)

    def test_list_and_read_archived_markdown(self):
        url = "/app/v2/sessions/codex:a/reports"
        response = self.web.get(url, headers=self.h)
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        self.assertEqual([i["media_id"] for i in items], [self.item["media_id"]])
        self.assertNotIn("_stored_path", str(items))
        self.path.unlink()  # archived copy remains readable
        read = self.web.get(items[0]["download_url"], headers=self.h)
        self.assertEqual(read.status_code, 200)
        self.assertIn("# Release report", read.text)
        self.assertEqual(read.headers["cache-control"], "no-store")
        self.assertEqual(read.headers["x-content-type-options"], "nosniff")

    def test_guessed_cross_session_and_tool_artifacts_are_denied(self):
        for item in (self.other, self.tool):
            res = self.web.get("/app/v2/sessions/codex:a/reports", params={"media_id": item["media_id"]}, headers=self.h)
            self.assertEqual(res.status_code, 404)
        self.assertEqual(self.web.get("/app/v2/sessions/codex:b/reports", headers=self.h).status_code, 403)
        self.assertEqual(self.web.get("/app/v2/artifacts/" + self.item["media_id"], headers=self.h).status_code, 403)
        self.assertEqual(self.web.get("/app/v2/sessions/codex:a/media", headers=self.h).status_code, 403)

    def test_removed_delivery_link_revokes_read(self):
        self.cards.clear()
        r = self.web.get("/app/v2/sessions/codex:a/reports", params={"media_id": self.item["media_id"]}, headers=self.h)
        self.assertEqual(r.status_code, 404)

    def test_query_path_cannot_capture_or_read_files(self):
        with patch.object(self.media, "capture_path", side_effect=AssertionError("must not capture")):
            r = self.web.get("/app/v2/sessions/codex:a/reports", params={"media_id": str(self.tool_path)}, headers=self.h)
        self.assertEqual(r.status_code, 404)

    def test_expired_grant_and_token_and_no_auth(self):
        url = "/app/v2/sessions/codex:a/reports"
        self.assertEqual(self.web.get(url).status_code, 401)
        bridge._CONSOLE_GRANTS[bridge._console_grant_key(self.token)] = (0, {"codex:a": ({"replay"},)})
        self.assertEqual(self.web.get(url, headers=self.h).status_code, 403)
        bridge._DEVICE_TOKENS[self.token]["expires"] = 0
        self.assertEqual(self.web.get(url, headers=self.h).status_code, 401)

    def test_only_delivered_prose_not_code_user_or_reasoning(self):
        link = f"[R]({self.path})"
        cards = [{"role": r, "kind": k, "body": {"text": link}} for r, k in
                 [("user", "text"), ("tool", "text"), ("agent", "reasoning")]]
        cards.append({"role": "agent", "kind": "text", "body": {"text": f"```md\n{link}\n```\n`{link}`"}})
        self.assertEqual(session_reports.delivered_refs(cards), {})

    def test_explicit_attachment_and_file_url_and_artifact(self):
        ref = "/app/v2/artifacts/" + self.item["media_id"]
        text = f"[[attach:{self.path}]] [R]({self.path.as_uri()}) [R]({ref})"
        refs = session_reports.delivered_refs([{"role": "assistant", "body": {"text": text}}])
        self.assertIn(str(self.path), refs)
        self.assertIn(ref, refs)
        self.assertEqual(len(session_reports.report_manifest(self.media, "codex:a", refs)), 1)

    def test_unavailable_external_and_non_markdown_are_not_exposed(self):
        self.assertIsNone(session_reports.report_ref("https://example.test/report.md"))
        self.assertIsNone(session_reports.report_ref("file://elsewhere/report.md"))
        self.assertIsNone(session_reports.report_ref("/tmp/secret.env"))
        self.assertEqual(session_reports.report_manifest(self.media, "codex:a", {"/not-archived.md": "Missing"}), [])


if __name__ == "__main__":
    unittest.main()
