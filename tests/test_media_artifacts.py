import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
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
             patch.object(bridge, "_v2_card_store", AsyncMock(
                 return_value=carddigest.SessionCardStore())):
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


class Base64FreezeRegressionTests(unittest.TestCase):
    """災難性 regex 回溯(2026-08-01 production 連環凍結)。

    base64 每 ~64 字元一個 '/',_ABSOLUTE_PATH_RE 對它是 O(n²):實測 120KB
    單行要 ~10s,且 C 層 regex 全程持 GIL → 整個 bridge 凍結 → 看門狗連環
    kickstart。修法:掃描前線性挖掉 512+ 字元的 base64 連續段。
    """

    def test_base64_line_scans_fast(self):
        import base64 as _b64, os as _os, time as _t
        from media_artifacts import references_in_text
        line = "data:image/png;base64," + _b64.b64encode(_os.urandom(90_000)).decode()
        t0 = _t.monotonic()
        refs = references_in_text(line)
        dt = _t.monotonic() - t0
        self.assertLess(dt, 1.0, f"base64 行掃描花了 {dt:.1f}s —— O(n²) 回歸")
        self.assertEqual(refs, [])

    def test_real_refs_survive_next_to_base64(self):
        import base64 as _b64, os as _os
        from media_artifacts import references_in_text
        blob = _b64.b64encode(_os.urandom(60_000)).decode()
        text = f"圖在 /tmp/pic.jpg 內容 data:image/png;base64,{blob} 完"
        refs = references_in_text(text)
        self.assertIn("/tmp/pic.jpg", refs)

    def test_short_base64ish_tokens_untouched(self):
        # 512 以下不挖 —— commit hash、短 token、正常字全不受影響
        from media_artifacts import references_in_text
        refs = references_in_text("hash abc123def456 檔案 /var/log/x.pdf")
        self.assertIn("/var/log/x.pdf", refs)


class SupersededBlobsLeakTests(unittest.TestCase):
    """`capture_path` 換指新 blob 之後,**舊那顆沒有任何機制回收**。

    2026-11-20 查 production:媒體庫 31.7 GB 裡有 4,651 個孤兒 blob、23 GB
    (86% 的空間)。一開始我以為是 upsert 漏更新 `stored_relpath` 造成「列指著
    舊版、新 blob 變孤兒」—— **錯的**,`capture_path` 本來就有更新(見該處
    ON CONFLICT 子句)。真相相反:列正確地換指新 blob,**被取代的舊 blob
    從此沒人引用,而 media_artifacts 沒有任何 GC**。

    所以這一組釘的不是「指對了沒」,而是**「被取代的舊 blob 會變成孤兒」這件事
    確實存在**,因此 `deploy/media-gc-orphans.py` 這類回收是必需品而不是選配。
    哪天有人在 store 裡加了自動回收,這支測試會紅 —— 那時請改成驗回收行為,
    不要直接刪掉它。
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="media-superseded-"))
        self.store = MediaArtifactStore(self.root)
        self.src = self.root / "src.txt"

    def _blobs(self):
        return {str(p.relative_to(self.root))
                for p in (self.root / "blobs").rglob("*") if p.is_file()}

    def _refs(self):
        import sqlite3
        with sqlite3.connect(self.root / "index.sqlite3") as conn:
            return {r[0] for r in conn.execute(
                "SELECT DISTINCT stored_relpath FROM artifacts"
                " WHERE stored_relpath IS NOT NULL")}

    def _relpath(self, media_id):
        """`public_item()` 刻意不回 `stored_relpath`(不把伺服器路徑交給
        客戶端),所以測試要從索引讀。"""
        import sqlite3
        with sqlite3.connect(self.root / "index.sqlite3") as conn:
            row = conn.execute(
                "SELECT stored_relpath FROM artifacts WHERE media_id = ?",
                (media_id,)).fetchone()
        return row[0] if row else None

    def test_row_follows_the_new_content(self):
        """先確認好的那一半:列**有**換指新內容,使用者不會下載到舊版。"""
        self.src.write_bytes(b"version one")
        first = self.store.capture_path("sess-1", str(self.src))
        self.src.write_bytes(b"version two is longer")
        second = self.store.capture_path("sess-1", str(self.src))

        self.assertEqual(first["media_id"], second["media_id"])
        new_rel = self._relpath(second["media_id"])
        self.assertIsNotNone(new_rel)
        self.assertEqual((self.root / new_rel).read_bytes(),
                         b"version two is longer")

    def test_the_superseded_blob_becomes_an_orphan(self):
        """壞的那一半:舊 blob 留在磁碟上、沒有任何列引用。"""
        self.src.write_bytes(b"version one")
        first = self.store.capture_path("sess-1", str(self.src))
        old_rel = self._relpath(first["media_id"])
        self.src.write_bytes(b"version two")
        self.store.capture_path("sess-1", str(self.src))

        orphans = self._blobs() - self._refs()
        self.assertIn(old_rel, orphans,
                      "舊 blob 竟然不見了?那代表 store 已經會自動回收 —— "
                      "請把這支測試改成驗回收行為,不要直接刪掉")


class VirtualMountIsNeverOpenedTests(unittest.TestCase):
    """`~/Library/Developer/CoreDevice/DeviceFS` 底下的路徑一律不碰。

    2026-11-20:bridge 卡死 17.5 小時,20 條檔案 I/O 執行緒全停在 `open()`,
    而不碰檔案的路由照樣 3 毫秒回應 —— 所有存活指標都說健康。

    `default_safe_roots()` 收整個家目錄,而家目錄底下的 `CoreDevice/DeviceFS`
    是 fskit 的 `devicefs` 掛載點(背後 `CoreDeviceService`,內容是實際連著的
    iPhone)。使用者空間檔案系統在裝置拔掉時 `open()` 會**無限期阻塞**,
    而且行程停在可中斷睡眠,連「卡在 I/O」都看不出來。

    逐字稿裡出現一個這種路徑就足以讓整台 bridge 停擺,所以擋在授權層 ——
    **根本不要去開**,而不是開了再等逾時。
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="media-virtmount-"))
        self.store = MediaArtifactStore(self.root)

    def test_devicefs_path_is_rejected(self):
        ref = os.path.expanduser(
            "~/Library/Developer/CoreDevice/DeviceFS/device-X/report.pdf")
        self.assertIsNone(self.store._safe_local_path(ref))

    def test_capture_marks_it_unavailable_instead_of_hanging(self):
        ref = os.path.expanduser(
            "~/Library/Developer/CoreDevice/DeviceFS/device-X/clip.mp4")
        item = self.store.capture_path("sess-1", ref)
        self.assertFalse(item.get("available"))

    def test_ordinary_home_paths_still_work(self):
        """護欄不可以順手把家目錄整個擋掉 —— 那是正常的工作範圍。"""
        from media_artifacts import _is_virtual_mount
        self.assertFalse(_is_virtual_mount(
            os.path.expanduser("~/apps/project/out.pdf")))
        self.assertTrue(_is_virtual_mount(os.path.expanduser(
            "~/Library/Developer/CoreDevice/DeviceFS/d/a.pdf")))
