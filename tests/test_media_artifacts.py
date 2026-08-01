import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

_TEST_BRIDGE_ROOT = tempfile.mkdtemp(prefix="media-artifact-endpoints-")
os.environ.setdefault(
    "POCKET_CANON_DB", os.path.join(_TEST_BRIDGE_ROOT, "canonical.db")
)
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")

import bridge
import carddigest
from media_artifacts import MediaArtifactStore, media_kind, references_in_text
from starlette.requests import Request


class MediaArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.store = MediaArtifactStore(
            self.base / "store", safe_roots=[str(self.source)], max_bytes=1024 * 1024
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_survives_source_deletion(self):
        path = self.source / "photo one.png"
        path.write_bytes(b"\x89PNG\r\nstable")

        item = self.store.capture_path("codex:abc", str(path))
        os.unlink(path)

        opened = self.store.open_media(item["media_id"])
        self.assertIsNotNone(opened)
        self.assertEqual(Path(opened[0]).read_bytes(), b"\x89PNG\r\nstable")
        self.assertTrue(item["available"])
        self.assertEqual(item["kind"], "image")

    def test_metadata_replay_does_not_invalidate_archived_source(self):
        path = self.source / "temporary output"
        path.write_bytes(b"%PDF-durable")

        first = self.store.capture_path("codex:abc", str(path))
        os.unlink(path)
        replayed = self.store.capture_path(
            "codex:abc",
            str(path),
            filename="Q3 report.pdf",
            mime="application/pdf",
            kind="file",
        )

        self.assertEqual(replayed["media_id"], first["media_id"])
        self.assertTrue(replayed["available"])
        self.assertEqual(replayed["filename"], "Q3 report.pdf")
        self.assertEqual(replayed["kind"], "pdf")
        opened = self.store.open_media(replayed["media_id"])
        self.assertEqual(Path(opened[0]).read_bytes(), b"%PDF-durable")

    def test_content_is_deduplicated_across_sessions(self):
        first = self.source / "first.pdf"
        second = self.source / "second.pdf"
        first.write_bytes(b"%PDF-same")
        second.write_bytes(b"%PDF-same")

        a = self.store.capture_path("codex:a", str(first))
        b = self.store.capture_path("claude_code:b", str(second))

        self.assertNotEqual(a["media_id"], b["media_id"])
        self.assertEqual(a["sha256"], b["sha256"])
        blobs = [p for p in (self.base / "store" / "blobs").rglob("*") if p.is_file()]
        self.assertEqual(len(blobs), 1)
        self.assertEqual((self.base / "store").stat().st_mode & 0o077, 0)
        self.assertEqual(self.store.db_path.stat().st_mode & 0o077, 0)
        self.assertEqual(blobs[0].stat().st_mode & 0o077, 0)

    def test_missing_reference_can_be_recovered_later(self):
        path = self.source / "late file.txt"
        missing = self.store.capture_path("hermes:cash", str(path))
        self.assertFalse(missing["available"])
        self.assertEqual(missing["unavailable_reason"], "not_found")

        path.write_text("ready", encoding="utf-8")
        ready = self.store.capture_path("hermes:cash", str(path))
        self.assertTrue(ready["available"])
        self.assertEqual(ready["media_id"], missing["media_id"])

    def test_payload_extracts_paths_with_spaces_and_links(self):
        path = self.source / "Q3 report final.pdf"
        path.write_bytes(b"%PDF")
        payload = {
            "content": f"Read [{path.name}]({path}) and https://example.com/report",
            "attachments": [{
                "kind": "file",
                "path": str(path),
                "filename": "Quarterly.pdf",
            }],
        }

        items = self.store.capture_payload("hermes:cash", payload)
        refs = {item["source_ref"] for item in items}
        self.assertIn(str(path), refs)
        self.assertIn("https://example.com/report", refs)
        local = next(item for item in items if item["source_kind"] == "path")
        self.assertEqual(local["filename"], "Quarterly.pdf")
        self.assertEqual(local["kind"], "pdf")

    def test_unsafe_path_is_indexed_as_unavailable(self):
        unsafe = self.base / "outside.pdf"
        unsafe.write_bytes(b"%PDF")
        item = self.store.capture_path("codex:a", str(unsafe))
        self.assertFalse(item["available"])
        self.assertEqual(item["unavailable_reason"], "unsafe_path")
        self.assertIsNone(self.store.open_media(item["media_id"]))

    def test_session_listing_is_paged(self):
        for index in range(3):
            path = self.source / f"{index}.txt"
            path.write_text(str(index), encoding="utf-8")
            self.store.capture_path("codex:a", str(path))

        first = self.store.list_session("codex:a", limit=2)
        second = self.store.list_session(
            "codex:a", limit=2, before=first["next_cursor"]
        )
        self.assertEqual(len(first["items"]), 2)
        self.assertEqual(len(second["items"]), 1)
        self.assertIsNone(second["next_cursor"])


class MediaReferenceTests(unittest.TestCase):
    def test_reference_parser_preserves_spaces(self):
        text = (
            "Open `/tmp/照片 1.jpg`, then [PDF](/tmp/Q3 report final.pdf). "
            "Also https://example.com/a.png"
        )
        self.assertEqual(
            references_in_text(text),
            [
                "/tmp/Q3 report final.pdf",
                "/tmp/照片 1.jpg",
                "https://example.com/a.png",
            ],
        )

    def test_media_kind_mapping(self):
        self.assertEqual(media_kind("clip.mov"), "video")
        self.assertEqual(media_kind("voice.m4a"), "audio")
        self.assertEqual(media_kind("sheet.xlsx"), "document")
        self.assertEqual(media_kind("README.md"), "text")


class MediaCardDigestTests(unittest.TestCase):
    def test_persona_attachment_only_message_is_kept_with_opening_fields(self):
        digest = carddigest.PersonaDigest()
        digest.message_card({
            "id": "message-1",
            "role": "user",
            "content": "",
            "attachments": [{
                "kind": "file",
                "filename": "Q3 report.pdf",
                "mime": "application/pdf",
                "path": "/tmp/Q3 report.pdf",
            }],
            "ts": 1,
        })

        card = digest.store.cards["card-hp-message-1"]
        self.assertEqual(card["body"]["attachments"][0]["path"],
                         "/tmp/Q3 report.pdf")
        self.assertIn("Q3 report.pdf", card["body"]["fallback_text"])

    def test_codex_local_image_is_structured_attachment(self):
        cards = carddigest.codex_item_to_cards({
            "id": "item-1",
            "type": "userMessage",
            "content": [{"type": "localImage", "path": "/tmp/照片 1.jpg"}],
        })

        self.assertEqual(cards[0]["body"]["attachments"][0]["kind"], "image")
        self.assertEqual(cards[0]["body"]["attachments"][0]["path"],
                         "/tmp/照片 1.jpg")


class MediaArtifactEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.store = MediaArtifactStore(
            self.base / "store", safe_roots=[str(self.source)]
        )

    async def asyncTearDown(self):
        self.temp.cleanup()

    @staticmethod
    def request() -> Request:
        return Request({
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(
                b"authorization",
                f"Bearer {bridge.BRIDGE_TOKEN}".encode("utf-8"),
            )],
        })

    async def test_media_index_and_download_handlers_use_archived_blob(self):
        path = self.source / "report final.pdf"
        path.write_bytes(b"%PDF-endpoint")
        item = self.store.capture_path("codex:abc", str(path))

        with patch.object(bridge, "_MEDIA_ARTIFACT_STORE", self.store), \
             patch.object(bridge, "_v2_card_store", AsyncMock(return_value=object())):
            page = await bridge.v2_session_media(
                "codex:abc", self.request(), limit=20
            )
            response = await bridge.v2_artifact_download(
                item["media_id"], self.request()
            )

        self.assertEqual(page["items"][0]["media_id"], item["media_id"])
        self.assertEqual(
            page["items"][0]["download_url"],
            f"/app/v2/artifacts/{item['media_id']}",
        )
        self.assertEqual(Path(response.path).read_bytes(), b"%PDF-endpoint")


if __name__ == "__main__":
    unittest.main()


class ConnectionLeakTests(unittest.TestCase):
    """sqlite 連線洩漏(2026-08-01 production 事故)。

    `with sqlite3.connect(...) as conn:` **不會關閉連線** —— 那個 context manager
    只管交易 commit/rollback。7 個呼叫點每查一次就漏 1 個 fd,production 跑 19
    小時漏了 72 個,撞上 launchd 預設 256 上限 → socketpair 開不出來 → 子行程
    全失敗 → 桌面/手機看到 bridge「離線」。

    這組測試直接數:操作 N 次之後,開著的 sqlite 連線必須是 0。
    """

    def test_operations_do_not_leak_connections(self):
        import sqlite3 as _sq

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        (base / "src").mkdir()
        store = MediaArtifactStore(base / "store", safe_roots=[str(base / "src")],
                                   max_bytes=1024 * 1024)

        live = []
        real_connect = _sq.connect

        class TrackedConn:
            # sqlite3.Connection 的 close 是 C 型別唯讀屬性,不能 monkeypatch,
            # 用 proxy 追蹤 close 有沒有被呼叫。
            def __init__(self, conn):
                object.__setattr__(self, "_conn", conn)
                live.append(self)

            def close(self):
                if self in live:
                    live.remove(self)
                self._conn.close()

            def __getattr__(self, k):
                return getattr(object.__getattribute__(self, "_conn"), k)

            def __setattr__(self, k, v):   # row_factory 賦值要穿透
                setattr(object.__getattribute__(self, "_conn"), k, v)

            def __enter__(self):
                self._conn.__enter__()
                return self

            def __exit__(self, *a):
                return self._conn.__exit__(*a)

        def counting_connect(*a, **k):
            return TrackedConn(real_connect(*a, **k))

        src = base / "src" / "pic.png"
        src.write_bytes(b"\x89PNG fake")
        with patch("media_artifacts.sqlite3.connect", counting_connect):
            for i in range(5):
                item = store.capture_path(f"codex:s{i}", str(src))
                store.open_media(item["media_id"])
                store.list_session(f"codex:s{i}")
        self.assertEqual(
            len(live), 0,
            f"洩漏了 {len(live)} 條 sqlite 連線 —— `with sqlite3.connect()` 不會關,"
            "必須 contextlib.closing")
