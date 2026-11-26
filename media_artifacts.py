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
from contextlib import closing
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


#: 允許範圍內、但**絕對不可以去 open() 的掛載點**。
#:
#: 2026-11-20 事故背景:bridge 卡死 17.5 小時,20 條檔案 I/O 執行緒全部停在
#: `open()` 這個 syscall 上。`/healthz` 之類不碰檔案的路由照樣 3 毫秒回 404,
#: 所以所有存活指標都說健康(見 docs/incidents/20261120-*)。
#:
#: `default_safe_roots()` 收**整個家目錄**,而家目錄底下有
#: `~/Library/Developer/CoreDevice/DeviceFS` —— 那不是一般目錄,是 fskit 的
#: `devicefs` 掛載點,背後是 `CoreDeviceService`,內容是**實際連著的 iPhone 的
#: 檔案系統**(`du` 爬它會算出 93 GB 的假數字)。使用者空間檔案系統在裝置拔掉
#: 或背後服務卡住時,`open()` 會**無限期阻塞**,而且行程停在可中斷睡眠(`S`)
#: 不是 `U`,所以連「卡在 I/O」都看不出來。
#:
#: 只要逐字稿裡出現一個這底下的路徑,`capture_path` 就會去開它。這條防線
#: 比逾時更前面:**根本不要去碰**。
_VIRTUAL_MOUNT_MARKERS = (
    "/Library/Developer/CoreDevice/DeviceFS",
)


def _is_virtual_mount(path: str) -> bool:
    """`path` 落在已知的虛擬/裝置掛載點底下?(比對用 realpath 過的路徑)"""
    return any(marker in path for marker in _VIRTUAL_MOUNT_MARKERS)


def default_safe_roots() -> tuple[str, ...]:
    roots = [os.path.realpath(os.path.expanduser("~"))]
    for raw in ("/tmp", "/private/tmp", "/var/folders"):
        root = os.path.realpath(raw)
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def temp_roots() -> tuple[str, ...]:
    """Agent 產出物的慣常落腳處 —— 只有**共用** scratch,`/tmp` 這一支。

    刻意**不含** `/var/folders`(macOS 的 per-user TMPDIR),儘管
    `default_safe_roots()` 有收它:那底下住的是整台機器每個 app 的快取與暫存
    (Mail 的下載、瀏覽器的中繼檔…),把它整個列為「可以變成下載連結」太寬。
    `/tmp` 不一樣 —— 它是 agent 自己放產出物的慣例位置。

    代價講明:macOS 上 `mkstemp`/`mktemp` 落在 `/var/folders`,那些檔案不會
    被掛成附件。要放寬請用 `POCKET_MEDIA_ATTACH_ROOTS`(冒號分隔)。
    """
    roots: list[str] = []
    for raw in ("/tmp", "/private/tmp"):
        root = os.path.realpath(raw)
        if root not in roots:
            roots.append(root)
    return tuple(roots)


# 可以掛成「可點附件」的媒體類別(= `media_kind()` 的輸出)。
#
# 刻意**排除** `text` 與 `file`:code agent 的逐字稿裡每一步都在講
# `.py` / `.swift` / `.md` / `.json` / `.log` —— 全掛上去的結果是每張卡
# 底下一排垃圾附件(2026-10-29 已經發生過一次:一個 pptx 旁邊掛了 15 個
# 網址碎片)。使用者要的是「影片/圖/PDF/試算表/壓縮檔點得開」,不是
# 「原始碼變成下載連結」。
ATTACHABLE_KINDS = frozenset({
    "image", "video", "audio", "pdf", "document", "archive",
})


def _too_broad_roots() -> frozenset:
    """不准當附件根目錄的「太大」目錄 —— 收了它等於沒有邊界。

    `~` 在列表裡的理由(2026-11-17 複查抓到):cc2 的 session 沒給 workdir
    時 `CCSdkSession.__init__` 會把 cwd 設成 `os.path.expanduser("~")`,於是
    「允許根目錄 = 工作目錄」一路推導成**整個家目錄可下載** —— agent 只要
    在逐字稿裡提到 `~/Downloads/某份合約.pdf`,那份 PDF 就會被複製進
    artifact 庫並長出一個 Bearer 可取的網址。工作目錄是「專案資料夾」的意思,
    家目錄不是專案資料夾。
    """
    broad = {"/", ""}
    for raw in ("~", "/Users", "/home", "/var", "/etc", "/opt", "/usr"):
        broad.add(os.path.realpath(os.path.expanduser(raw)))
    return frozenset(broad)


def attach_roots(cwds=(), extra=()) -> tuple[str, ...]:
    """卡片附件掃描的允許根目錄:session 工作目錄(含子目錄)+ 暫存區。

    這比 `MediaArtifactStore.safe_roots`(整個家目錄)**窄**,是刻意的:
    逐字稿文字裡的絕對路徑是啟發式偵測,不能讓它把家目錄任何檔案變成
    可下載的 URL。真正的權限檢查仍在 `capture_path` → `_safe_local_path`,
    這裡是第二道、更緊的門。太大的目錄(家目錄、`/Users`…)直接丟掉,
    見 `_too_broad_roots`。
    """
    broad = _too_broad_roots()
    roots: list[str] = []
    for raw in list(cwds) + list(extra) + list(temp_roots()):
        raw = str(raw or "").strip()
        if not raw:
            continue
        root = os.path.realpath(os.path.expanduser(raw))
        if root in broad or root in roots:
            continue
        roots.append(root)
    return tuple(roots)


def local_path_of(ref: str, *, require_absolute: bool = True) -> str:
    """`file://` / `~` / 裸路徑 → 絕對實路徑(未做任何授權判斷)。

    `require_absolute=False` 時相對路徑會照 process cwd 解析 —— 那是
    `MediaArtifactStore._safe_local_path` 的既有語意,不能改(它的 source_ref
    來自 provider 事件,歷史上就收相對路徑)。卡片附件偵測那條要
    `True`:逐字稿裡的相對路徑無從判斷基準目錄,猜了就是亂掛。
    """
    raw = str(ref or "")
    if raw.startswith("file://"):
        raw = urllib.parse.unquote(urllib.parse.urlparse(raw).path)
    if require_absolute and not raw.startswith(("/", "~")):
        return ""
    return os.path.realpath(os.path.expanduser(raw))


def within_roots(path: str, roots: Iterable[str]) -> bool:
    """`path`(**必須是 realpath 過的**)落在某個根目錄之內?

    授權判斷只有這一份(`_safe_local_path` 與 `vet_attachable_path` 共用)——
    兩份複製品是遲早會分岔的安全邏輯。比對用「等於根目錄,或以根目錄 +
    分隔符開頭」,所以 `/tmp-evil` 不會被 `/tmp` 當成自己人。
    """
    if not path:
        return False
    return any(path == root or path.startswith(root + os.sep)
               for root in roots)


def vet_attachable_path(ref: str, roots: Iterable[str], allow_any_kind: bool = False):
    """`ref` 可以變成卡片附件嗎?**三態**回傳:

    `allow_any_kind=True`(= agent 用 `[[attach:…]]` **明確**附的檔)時,跳過
    ATTACHABLE_KINDS 類型檢查 —— 放行 .txt/.md/任何類型。類型限制存在只是為了
    擋「啟發式亂掃原始碼變下載連結」的洗版;明確附檔沒有這個問題。**其餘所有
    邊界照守**:仍必須是本機絕對路徑、仍必須在允許 root 內、仍解 symlink 防逃逸、
    仍必須真的存在。所以明確附檔也絕不能掛 /etc/passwd 或 root 外的檔。


      · `str`(實路徑)= 可以掛
      · `False` = **現在**不行,但可能等一下就行(路徑合法、副檔名合格、在
        允許範圍內,**只是檔案還沒出現**)→ 呼叫端該保留重試機會
      · `None` = 永久不合(不是本機絕對路徑 / 不在允許根目錄 / 副檔名不是
        媒體類)→ 呼叫端可以記下來永遠不必再問

    三態是必要的,不是潔癖:code agent 的卡片**先出現**(`ffmpeg -i a.mov
    /work/out.mp4` 的工具卡)、檔案**後產生**。把這種 miss 當永久失敗記進
    「看過了」集合,那支影片就再也不會被掛上 —— 正是這個功能要治的病。

    四道門照「不用 syscall 的先擋」排序,所以永久性拒絕一次 stat 都不花:
      1. 看得出是本機絕對路徑(`/…` / `~/…` / `file://…`)
      2. realpath 之後落在 `roots` 之內 —— **解完符號連結才比對**,所以
         工作目錄裡指向 `/etc/passwd` 的 symlink 過不了(路徑逃逸)
      3. 副檔名/MIME 屬於 `ATTACHABLE_KINDS`(影音圖、PDF、文件、壓縮檔)
      4. 真的存在、是一般檔案(不 stat 不算偵測到)—— 只有這一關會回 False
    """
    path = local_path_of(_clean_reference(str(ref or "")))
    if not path:
        return None
    roots = tuple(roots or ())
    if not roots or not within_roots(path, roots):
        return None
    if not allow_any_kind:
        guessed = mimetypes.guess_type(path)[0] or ""
        if media_kind(path, guessed) not in ATTACHABLE_KINDS:
            return None
    try:
        if not os.path.isfile(path):
            return False        # 還沒產生 → 保留重試機會
    except OSError:
        return None
    return path


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


# base64 連續區段(data URI 本體、貼上的裸 blob)。base64 每 ~64 字元一個
# '/',對 _ABSOLUTE_PATH_RE 是災難輸入:每個 '/' 都是比對起點、惰性掃描到行尾
# → O(n²)。實測 120KB 單行要 9.96s、且 C 層 regex 全程不放 GIL —— 2026-08-01
# production 整個 bridge 因此反覆凍結,被看門狗連環 kickstart(sample 存
# /tmp/bridge-freeze-*.sample.txt)。掃描前先把這些區段挖掉;本 regex 是純
# 字元類 + 長度下限,線性。512 起跳:真路徑/URL 不會有 512 字元的無空白
# base64 連續段,誤挖不了東西。
_BASE64_RUN_RE = re.compile(r"[A-Za-z0-9+/=]{512,}")


def _excise_base64_runs(text: str) -> str:
    return _BASE64_RUN_RE.sub(" ", text)


def references_in_text(text: str) -> list[str]:
    """Extract local paths and HTTP links, including local paths with spaces."""
    if not text or len(text) > 2_000_000:
        return []
    text = _excise_base64_runs(text)
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


# 「明確附檔」標記:agent 在訊息裡寫 `[[attach:/abs/path]]` 表示**刻意**要把這個檔
# 給使用者。明確附檔走寬鬆 vet(放行任何類型,見 vet_attachable_path 的
# allow_any_kind)—— 因為「洗版」只在啟發式亂掃時才是問題;使用者要的 .txt/密碼檔/
# 報告,agent 標明了就該掛得上。路徑邊界(root/逃逸/存在性)仍然照守。
_EXPLICIT_ATTACH_RE = re.compile(r"\[\[attach:\s*(?P<path>[^\]\n]+?)\s*\]\]")


def explicit_attach_refs(text: str) -> list[str]:
    """抽出 `[[attach:/路徑]]` 標記裡的路徑(= agent 明確要附的檔)。"""
    if not text or len(text) > 2_000_000:
        return []
    refs: list[str] = []
    for match in _EXPLICIT_ATTACH_RE.finditer(text):
        value = _clean_reference(match.group("path"))
        if value and value not in refs:
            refs.append(value)
    return refs


def _file_changed(local_path, previous: dict) -> bool:
    """磁碟上的檔案跟上次入庫時相比有沒有變。問不到時一律回 True(重抓)。

    ## 為什麼需要這支(2026-11-20)

    `capture_path` 原本的短路是「已經有可用的列 + 呼叫端沒給 metadata 提示
    → 直接回舊資料」,**連檔案都不看**。那表示同一個路徑的檔案內容改了之後,
    Pocket 永遠拿得到的是**第一次擷取的版本** —— CC/CX 把報告重新產生一次,
    手機上看到的還是舊的那份。(機主 2026-11-20 回報的預覽不一致,CX 也從
    app 端獨立查到同一個現象。)

    用 size + mtime 而不是重算雜湊:短路的全部意義就是省掉讀檔,再去 hash
    等於沒省。size 變了或 mtime 比上次入庫晚,就當作變了 —— 寧可多抓一次,
    不可以給舊內容。
    """
    if local_path is None:
        return False
    try:
        st = os.stat(local_path)
    except OSError:
        return True
    if previous.get("byte_size") not in (None, st.st_size):
        return True
    recorded = previous.get("updated_at") or previous.get("created_at") or 0
    return st.st_mtime > float(recorded)


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
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
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
            conn = sqlite3.connect(self.db_path, timeout=30.0)
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
        # 相對路徑照舊用 process cwd 解析(require_absolute=False)—— 既有語意,
        # source_ref 歷史上就收得到相對路徑。授權判斷走共用的 within_roots。
        path = local_path_of(source_ref, require_absolute=False)
        # 虛擬/裝置掛載點一律不碰 —— 見 `_VIRTUAL_MOUNT_MARKERS` 的事故說明。
        # 擋在這裡而不是靠逾時:逾時只能讓你**晚一點**失敗,這條是根本不去開。
        if _is_virtual_mount(path):
            return None
        return path if within_roots(path, self.safe_roots) else None

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
        with closing(self._connect()) as conn, conn:
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
        with closing(self._connect()) as conn, conn:
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
            (not any((filename, mime, kind)) and not _file_changed(local_path, previous))
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

        with closing(self._connect()) as conn, conn:
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

    def capture_bytes(
        self,
        session_id: str,
        data: bytes,
        *,
        filename: str = "",
        mime: str = "",
        kind: str = "",
    ) -> dict:
        """記憶體位元組直接入庫(MCP 工具直回 base64 圖、無檔案路徑的場景)。

        content-addressed:source_ref = bytes:sha256:<digest> — 同 session 同內容
        冪等(digest 重放不重複落盤),blob 與 capture_path 共用同一個以雜湊
        定址的存放區。"""
        session_id = str(session_id or "").strip()
        if not session_id or not data:
            raise ValueError("session_id and data are required")
        if len(data) > self.max_bytes:
            raise OverflowError("artifact exceeds configured limit")
        now = time.time()
        value = hashlib.sha256(data).hexdigest()
        source_ref = f"bytes:sha256:{value}"
        media_id = self._media_id(session_id, source_ref)
        previous = self._existing_available(session_id, source_ref)
        if previous:
            return self._preserve_available(
                previous, filename=filename, mime=mime, kind=kind
            )
        guessed_name = filename or f"image-{value[:12]}"
        guessed_mime = mime or mimetypes.guess_type(guessed_name)[0] or ""
        item_kind = (
            media_kind(guessed_name, guessed_mime)
            if kind in {"", "file", "attachment"}
            else kind
        )
        stored_relpath = str(Path("blobs") / value[:2] / value)
        destination = self.root / stored_relpath
        self._ensure_schema()
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(destination.parent, 0o700)
        if not destination.exists():
            temporary = self.blob_root / (
                f".incoming.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}"
            )
            with open(temporary, "xb") as target:
                target.write(data)
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO artifacts(
                    media_id, session_id, source_ref, source_kind, filename,
                    mime, media_kind, byte_size, sha256, stored_relpath,
                    available, unavailable_reason, created_at, updated_at
                ) VALUES (?, ?, ?, 'bytes', ?, ?, ?, ?, ?, ?, 1, NULL, ?, ?)
                -- 這支是 content-addressed(`source_ref = bytes:sha256:<digest>`,
                -- 見上方 docstring),所以衝突**只發生在內容完全相同**時 ——
                -- sha256/stored_relpath 本來就一樣,寫不寫都一樣。
                --
                -- 真正要補的是 `available` 復位(2026-11-20):同一份內容若曾被
                -- 歸檔腳本標成 `available=0`,之後又重新入庫時檔案其實回來了,
                -- 不復位的話 `_media_wire_item()` 永遠不給 download_url
                -- (bridge.py:521),卡片就卡在「有附件但點不開」。
                ON CONFLICT(session_id, source_ref) DO UPDATE SET
                    filename = excluded.filename,
                    mime = excluded.mime,
                    available = 1,
                    unavailable_reason = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    media_id, session_id, source_ref, guessed_name, guessed_mime,
                    item_kind, len(data), value, stored_relpath, now, now,
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
        with closing(self._connect()) as conn, conn:
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
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(query, params).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [self.public_item(self._row_item(row)) for row in rows]
        return {
            "items": items,
            "next_cursor": items[-1]["cursor"] if has_more and items else None,
        }

    def find_session_artifact(self, session_id: str, *, media_id: str | None = None,
                              source_ref: str | None = None) -> dict | None:
        if (media_id is None) == (source_ref is None):
            raise ValueError("provide exactly one artifact selector")
        column, value = ("media_id", media_id) if media_id else ("source_ref", source_ref)
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT rowid AS cursor, * FROM artifacts WHERE session_id = ? AND " + column + " = ?",
                (session_id, value),
            ).fetchone()
        return self.public_item(self._row_item(row)) if row else None

    def open_media(self, media_id: str) -> tuple[str, str, str] | None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT rowid AS cursor, * FROM artifacts WHERE media_id = ?",
                (media_id,),
            ).fetchone()
        if not row:
            return None
        item = self._row_item(row)
        path = item.get("_stored_path")
        # bytes 列(capture_bytes:MCP 直回圖)與 path 列同樣有 blob,可下載。
        if item["source_kind"] not in ("path", "bytes") or not item["available"] or not path:
            return None
        if not os.path.isfile(path):
            return None
        return path, item["mime"], item["filename"]

    def resolve_original(self, source_ref: str) -> tuple[str, str, str] | None:
        source_ref = _clean_reference(source_ref)
        with closing(self._connect()) as conn, conn:
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
