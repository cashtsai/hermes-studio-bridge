"""Durable media index for PocketAgent session artifacts.

Agent transcripts frequently point at files in /tmp.  This module snapshots
those files while they still exist, stores content-addressed blobs, and keeps a
small SQLite index that can be paged by session.  External URLs are indexed but
never fetched by the bridge.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Iterable


DEFAULT_MAX_BYTES = 100 * 1024 * 1024

_KNOWN_EXTENSIONS = (
    "avif", "bmp", "csv", "doc", "docx", "gif", "heic", "heif", "html",
    "jpeg", "jpg", "json", "key", "log", "m4a", "md", "mov", "mp3", "mp4",
    "numbers", "pages", "pdf", "png", "ppt", "pptx", "rtf", "svg", "text",
    "tif", "tiff", "tsv", "txt", "wav", "webm", "webp", "xls", "xlsx", "xml",
    "yaml", "yml", "zip",
)
_EXT_GROUP = "|".join(sorted(_KNOWN_EXTENSIONS, key=len, reverse=True))
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]\n]*\]\(([^)\n]+)\)")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_FILE_URL_RE = re.compile(r"file://[^\s<>'\"]+")
_HTTP_URL_RE = re.compile(r"https?://[^\s<>'\"]+")
_ABSOLUTE_PATH_RE = re.compile(
    rf"(?<![:/])(?P<path>(?:~?/|/)[^\n\r<>\"']*?\.(?:{_EXT_GROUP}))"
    r"(?=$|[\s)\]}>`'\",;:!?])",
    re.IGNORECASE,
)

_PATH_KEYS = {
    "path", "file_path", "filepath", "local_path", "localpath",
    "notebook_path", "output_path",
}
_URL_KEYS = {
    "url", "uri", "image_url", "source_url", "download_url",
}
_FILENAME_KEYS = {"filename", "file_name", "name"}
_MIME_KEYS = {"mime", "mime_type", "content_type"}


def default_safe_roots() -> tuple[str, ...]:
    roots = [os.path.realpath(os.path.expanduser("~"))]
    for raw in ("/tmp", "/private/tmp", "/var/folders"):
        root = os.path.realpath(raw)
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def media_kind(filename: str = "", mime: str = "") -> str:
    mime = (mime or "").lower()
    suffix = Path(filename or "").suffix.lower()
    if mime.startswith("image/") or suffix in {
        ".avif", ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png",
        ".svg", ".tif", ".tiff", ".webp",
    }:
        return "image"
    if mime.startswith("video/") or suffix in {".mov", ".mp4", ".webm"}:
        return "video"
    if mime.startswith("audio/") or suffix in {".m4a", ".mp3", ".wav"}:
        return "audio"
    if suffix == ".pdf" or mime == "application/pdf":
        return "pdf"
    if mime.startswith("text/") or suffix in {
        ".csv", ".html", ".json", ".log", ".md", ".rtf", ".text", ".tsv",
        ".txt", ".xml", ".yaml", ".yml",
    }:
        return "text"
    if suffix in {
        ".doc", ".docx", ".key", ".numbers", ".pages", ".ppt", ".pptx",
        ".xls", ".xlsx",
    }:
        return "document"
    if suffix == ".zip" or mime in {"application/zip", "application/x-zip-compressed"}:
        return "archive"
    return "file"


def _clean_reference(value: str) -> str:
    value = value.strip().strip("`").strip()
    while value and value[-1] in ".,;:!?":
        value = value[:-1]
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    return value


def references_in_text(text: str) -> list[str]:
    """Extract local paths and HTTP links, including local paths with spaces."""
    if not text or len(text) > 2_000_000:
        return []
    refs: list[str] = []

    def add(raw: str) -> None:
        value = _clean_reference(raw)
        if value and value not in refs:
            refs.append(value)

    for match in _MARKDOWN_LINK_RE.finditer(text):
        add(match.group(1))
    for match in _BACKTICK_RE.finditer(text):
        candidate = match.group(1)
        if candidate.startswith(("/", "~/", "file://", "http://", "https://")):
            add(candidate)
    url_spans: list[tuple[int, int]] = []
    for regex in (_FILE_URL_RE, _HTTP_URL_RE):
        for match in regex.finditer(text):
            add(match.group(0))
            url_spans.append(match.span())
    for match in _ABSOLUTE_PATH_RE.finditer(text):
        if any(start < match.end() and match.start() < end for start, end in url_spans):
            continue
        add(match.group("path"))
    return refs


class MediaArtifactStore:
    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        safe_roots: Iterable[str] | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ):
        self.root = Path(root).expanduser()
        self.db_path = self.root / "index.sqlite3"
        self.blob_root = self.root / "blobs"
        self.safe_roots = tuple(
            os.path.realpath(os.path.expanduser(p))
            for p in (safe_roots or default_safe_roots())
        )
        self.max_bytes = max(1, int(max_bytes))
        self._init_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self._ensure_schema()
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.blob_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.root, 0o700)
            os.chmod(self.blob_root, 0o700)
            conn = sqlite3.connect(self.db_path, timeout=10.0)
            try:
                os.chmod(self.db_path, 0o600)
                conn.executescript(
                    """
                    PRAGMA journal_mode = WAL;
                    PRAGMA synchronous = NORMAL;
                    CREATE TABLE IF NOT EXISTS artifacts (
                        media_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        source_ref TEXT NOT NULL,
                        source_kind TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        mime TEXT NOT NULL,
                        media_kind TEXT NOT NULL,
                        byte_size INTEGER,
                        sha256 TEXT,
                        stored_relpath TEXT,
                        available INTEGER NOT NULL DEFAULT 0,
                        unavailable_reason TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE(session_id, source_ref)
                    );
                    CREATE INDEX IF NOT EXISTS artifacts_session_rowid
                    ON artifacts(session_id);
                    CREATE INDEX IF NOT EXISTS artifacts_source_ref
                    ON artifacts(source_ref);
                    """
                )
                conn.commit()
            finally:
                conn.close()
            self._initialized = True

    @staticmethod
    def _media_id(session_id: str, source_ref: str) -> str:
        digest = hashlib.sha256(
            f"{session_id}\0{source_ref}".encode("utf-8", "replace")
        ).hexdigest()
        return f"med_{digest[:32]}"

    def _safe_local_path(self, source_ref: str) -> str | None:
        raw = source_ref
        if raw.startswith("file://"):
            raw = urllib.parse.unquote(urllib.parse.urlparse(raw).path)
        raw = os.path.expanduser(raw)
        path = os.path.realpath(raw)
        if any(path == root or path.startswith(root + os.sep) for root in self.safe_roots):
            return path
        return None

    def _snapshot_file(self, path: str) -> tuple[int, str, str]:
        """Copy and hash one immutable snapshot, avoiding hash/copy races."""
        digest = hashlib.sha256()
        size = 0
        temporary = self.blob_root / (
            f".incoming.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}"
        )
        try:
            with open(path, "rb") as source, open(temporary, "xb") as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise OverflowError("artifact exceeds configured limit")
                    digest.update(chunk)
                    target.write(chunk)
            value = digest.hexdigest()
            stored_relpath = str(Path("blobs") / value[:2] / value)
            destination = self.root / stored_relpath
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(destination.parent, 0o700)
            if destination.exists():
                temporary.unlink()
            else:
                os.replace(temporary, destination)
            os.chmod(destination, 0o600)
            return size, value, stored_relpath
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _existing_available(self, session_id: str, source_ref: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT rowid AS cursor, * FROM artifacts "
                "WHERE session_id = ? AND source_ref = ?",
                (session_id, source_ref),
            ).fetchone()
        if not row:
            return None
        item = self._row_item(row)
        stored = item.get("_stored_path")
        return item if item["available"] and stored and os.path.isfile(stored) else None

    def _preserve_available(
        self,
        item: dict,
        *,
        filename: str = "",
        mime: str = "",
        kind: str = "",
    ) -> dict:
        """Keep a durable blob when its original path has since disappeared."""
        if not any((filename, mime, kind)):
            return self.public_item(item)
        resolved_name = filename or item["filename"]
        resolved_mime = mime or item["mime"]
        resolved_kind = (
            media_kind(resolved_name, resolved_mime)
            if kind in {"", "file", "attachment"}
            else kind
        )
        with self._connect() as conn:
            conn.execute(
                "UPDATE artifacts SET filename = ?, mime = ?, media_kind = ? "
                "WHERE media_id = ?",
                (resolved_name, resolved_mime, resolved_kind, item["media_id"]),
            )
            conn.commit()
        item.update(
            filename=resolved_name,
            mime=resolved_mime,
            kind=resolved_kind,
        )
        return self.public_item(item)

    def capture_path(
        self,
        session_id: str,
        source_ref: str,
        *,
        filename: str = "",
        mime: str = "",
        kind: str = "",
    ) -> dict:
        session_id = str(session_id or "").strip()
        source_ref = _clean_reference(str(source_ref or ""))
        if not session_id or not source_ref:
            raise ValueError("session_id and source_ref are required")

        now = time.time()
        media_id = self._media_id(session_id, source_ref)
        local_path = self._safe_local_path(source_ref)
        previous = self._existing_available(session_id, source_ref)
        if previous and (
            not any((filename, mime, kind))
            or local_path is None
            or not os.path.isfile(local_path)
        ):
            return self._preserve_available(
                previous, filename=filename, mime=mime, kind=kind
            )

        guessed_name = filename or Path(
            urllib.parse.urlparse(source_ref).path
        ).name or "artifact"
        guessed_mime = mime or mimetypes.guess_type(guessed_name)[0] or ""
        item_kind = (
            media_kind(guessed_name, guessed_mime)
            if kind in {"", "file", "attachment"}
            else kind
        )
        size = None
        digest = None
        stored_relpath = None
        available = False
        reason = "not_found"

        if local_path is None:
            reason = "unsafe_path"
        elif not os.path.isfile(local_path):
            reason = "not_found"
        else:
            try:
                size = os.path.getsize(local_path)
                if size > self.max_bytes:
                    reason = "too_large"
                else:
                    size, digest, stored_relpath = self._snapshot_file(local_path)
                    available = True
                    reason = None
            except OverflowError:
                reason = "too_large"
            except OSError:
                reason = "read_failed"

        if not available and previous:
            return self._preserve_available(
                previous, filename=filename, mime=mime, kind=kind
            )

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO artifacts(
                    media_id, session_id, source_ref, source_kind, filename,
                    mime, media_kind, byte_size, sha256, stored_relpath,
                    available, unavailable_reason, created_at, updated_at
                ) VALUES (?, ?, ?, 'path', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, source_ref) DO UPDATE SET
                    filename = excluded.filename,
                    mime = excluded.mime,
                    media_kind = excluded.media_kind,
                    byte_size = excluded.byte_size,
                    sha256 = excluded.sha256,
                    stored_relpath = excluded.stored_relpath,
                    available = excluded.available,
                    unavailable_reason = excluded.unavailable_reason,
                    updated_at = excluded.updated_at
                """,
                (
                    media_id, session_id, source_ref, guessed_name, guessed_mime,
                    item_kind, size, digest, stored_relpath, int(available), reason,
                    now, now,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT rowid AS cursor, * FROM artifacts WHERE media_id = ?",
                (media_id,),
            ).fetchone()
        return self.public_item(self._row_item(row))

    def capture_url(
        self,
        session_id: str,
        source_url: str,
        *,
        filename: str = "",
        mime: str = "",
        kind: str = "",
    ) -> dict:
        session_id = str(session_id or "").strip()
        source_url = _clean_reference(str(source_url or ""))
        parsed = urllib.parse.urlparse(source_url)
        if not session_id or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("valid session_id and HTTP(S) URL are required")
        now = time.time()
        media_id = self._media_id(session_id, source_url)
        guessed_name = filename or Path(parsed.path).name or parsed.netloc
        guessed_mime = mime or mimetypes.guess_type(guessed_name)[0] or ""
        item_kind = (
            ("link" if not Path(parsed.path).suffix else
             media_kind(guessed_name, guessed_mime))
            if kind in {"", "file", "attachment"}
            else kind
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO artifacts(
                    media_id, session_id, source_ref, source_kind, filename,
                    mime, media_kind, available, created_at, updated_at
                ) VALUES (?, ?, ?, 'url', ?, ?, ?, 1, ?, ?)
                ON CONFLICT(session_id, source_ref) DO UPDATE SET
                    filename = excluded.filename,
                    mime = excluded.mime,
                    media_kind = excluded.media_kind,
                    available = 1,
                    unavailable_reason = NULL,
                    updated_at = excluded.updated_at
                """,
                (media_id, session_id, source_url, guessed_name, guessed_mime,
                 item_kind, now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT rowid AS cursor, * FROM artifacts WHERE media_id = ?",
                (media_id,),
            ).fetchone()
        return self.public_item(self._row_item(row))

    def capture_payload(self, session_id: str, payload: Any) -> list[dict]:
        """Index explicit attachment fields plus references embedded in text."""
        captured: list[dict] = []
        seen: set[str] = set()

        def capture(ref: str, metadata: dict | None = None) -> None:
            ref = _clean_reference(ref)
            has_metadata = bool(
                any(meta_value for key, meta_value in (metadata or {}).items()
                    if key in (_FILENAME_KEYS | _MIME_KEYS | {"media_kind", "kind"}))
            )
            if not ref or (ref in seen and not has_metadata) or ref.startswith("data:"):
                return
            seen.add(ref)
            meta = metadata or {}
            filename = next(
                (str(meta[k]) for k in _FILENAME_KEYS if meta.get(k)), ""
            )
            mime = next((str(meta[k]) for k in _MIME_KEYS if meta.get(k)), "")
            kind = str(meta.get("media_kind") or meta.get("kind") or "")
            try:
                if ref.startswith(("http://", "https://")):
                    captured.append(self.capture_url(
                        session_id, ref, filename=filename, mime=mime, kind=kind,
                    ))
                elif ref.startswith(("/", "~/", "file://")):
                    captured.append(self.capture_path(
                        session_id, ref, filename=filename, mime=mime, kind=kind,
                    ))
            except (OSError, sqlite3.Error, ValueError):
                return

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                lower = {str(key).lower(): val for key, val in value.items()}
                for key in _PATH_KEYS | _URL_KEYS:
                    ref = lower.get(key)
                    if isinstance(ref, str):
                        capture(ref, lower)
                for child in value.values():
                    walk(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    walk(child)
            elif isinstance(value, str):
                for ref in references_in_text(value):
                    capture(ref)

        walk(payload)
        latest: dict[str, dict] = {}
        order: list[str] = []
        for item in captured:
            ref = item["source_ref"]
            if ref not in latest:
                order.append(ref)
            latest[ref] = item
        return [latest[ref] for ref in order]

    def _row_item(self, row: sqlite3.Row) -> dict:
        stored_relpath = row["stored_relpath"]
        return {
            "media_id": row["media_id"],
            "session_id": row["session_id"],
            "source_ref": row["source_ref"],
            "source_kind": row["source_kind"],
            "filename": row["filename"],
            "mime": row["mime"],
            "kind": row["media_kind"],
            "byte_size": row["byte_size"],
            "sha256": row["sha256"],
            "available": bool(row["available"]),
            "unavailable_reason": row["unavailable_reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "cursor": int(row["cursor"]),
            "_stored_path": str(self.root / stored_relpath) if stored_relpath else None,
        }

    @staticmethod
    def public_item(item: dict) -> dict:
        return {key: value for key, value in item.items() if not key.startswith("_")}

    def list_session(
        self, session_id: str, *, limit: int = 100, before: int | None = None
    ) -> dict:
        limit = max(1, min(int(limit), 500))
        query = (
            "SELECT rowid AS cursor, * FROM artifacts WHERE session_id = ?"
            + (" AND rowid < ?" if before is not None else "")
            + " ORDER BY rowid DESC LIMIT ?"
        )
        params: tuple[Any, ...] = (
            (session_id, int(before), limit + 1)
            if before is not None
            else (session_id, limit + 1)
        )
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [self.public_item(self._row_item(row)) for row in rows]
        return {
            "items": items,
            "next_cursor": items[-1]["cursor"] if has_more and items else None,
        }

    def open_media(self, media_id: str) -> tuple[str, str, str] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT rowid AS cursor, * FROM artifacts WHERE media_id = ?",
                (media_id,),
            ).fetchone()
        if not row:
            return None
        item = self._row_item(row)
        path = item.get("_stored_path")
        if item["source_kind"] != "path" or not item["available"] or not path:
            return None
        if not os.path.isfile(path):
            return None
        return path, item["mime"], item["filename"]

    def resolve_original(self, source_ref: str) -> tuple[str, str, str] | None:
        source_ref = _clean_reference(source_ref)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT rowid AS cursor, * FROM artifacts "
                "WHERE source_ref = ? AND available = 1 "
                "ORDER BY updated_at DESC",
                (source_ref,),
            ).fetchall()
        for row in rows:
            item = self._row_item(row)
            path = item.get("_stored_path")
            if path and os.path.isfile(path):
                return path, item["mime"], item["filename"]
        return None
