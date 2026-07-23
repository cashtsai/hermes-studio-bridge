#!/usr/bin/env python3
"""OpenAI-compatible bridge → Hermes agent (per-persona, shared memory).

Open WebUI (or any OpenAI-compatible client) points its API base at this
server. Each Hermes persona is exposed as a "model"; a chat completion runs
`hermes -z <last user msg> --continue <persona-session>` with the persona's
HERMES_HOME, so the reply comes from that persona WITH its shared long-term
memory. PocketAgent/ACP is the primary app surface; Telegram gateways are
legacy ingress/fallback surfaces.

Run:  uvicorn bridge:app --host 0.0.0.0 --port 8081
"""
import asyncio
import base64
import carddigest
import collections
import fcntl
import glob
import hashlib
import hmac
import json
import mimetypes
import os
import pty
import re
import secrets
import shlex
import signal
import struct
import subprocess
import termios
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse, HTMLResponse
from starlette.websockets import WebSocketState

from acp_client import ACPPool, canonical_telegram_session

# Persistent warm ACP process per persona — removes the ~5s `hermes -z`
# cold start per message and streams output live. Cold `hermes -z` stays as a
# fallback if ACP ever fails.
POOL = ACPPool()

# M2/M3 — registry of dispatched CC/Codex sub-sessions, surfaced in GET /sessions
# and continuable like a persona. Keyed by an opaque session id.
SUBSESSIONS: dict = {}

# Strong refs to detached turn tasks so they finish (and record the reply) even
# if the client's network drops mid-stream. Without this they could be GC'd.
_BG_TASKS: set = set()
# A3-3:主事件圈把手 —— 讓 to_thread 裡的同步碼(報告同步的 notice 建立)
# 能 call_soon_threadsafe 把卡片 feed 排回單圈(SessionCardStore 不上鎖)。
# startup 時由 _start_log_rotation 填入。
_MAIN_LOOP = None

# One SSE keepalive cadence for every streaming endpoint (issue #8: it was
# 2s / 4s / 10s across chat, ccsessions and codexsessions for no reason).
SSE_KEEPALIVE_SECS = 2.0
# A persona provider may stay connected but emit no ACP events. The timeout is
# deliberately configurable so the cleanup path can be exercised quickly in
# regression tests while production keeps the five-minute ceiling.
PERSONA_STALL_LIMIT_SECS = float(os.environ.get("PERSONA_STALL_LIMIT_SECS", "300"))

# 工具步驟 cmd/路徑的截斷上限(#38 diff 卡缺口):140 會把深路徑攔腰砍斷,
# app 的 diff chip 拿殘缺路徑去打 /filediff 就 404。所有 transcript/步驟
# 格式化共用這一個值(carddigest._CMD_MAX 同步)。
TOOL_CMD_MAX = 500

# In-flight app-turn dedup (issue #9): (session, client_id) -> {ts, task, state}.
# A duplicate POST with the same client_id while the first run is STILL RUNNING
# attaches to it instead of re-running the turn (side effects must not replay).
# Entries expire after 600s; cleanup happens on each access so the dict can't leak.
_APP_TURN_INFLIGHT: dict = {}
_APP_TURN_INFLIGHT_TTL = 600.0
_APP_TURN_INFLIGHT_LOCK = asyncio.Lock()

# Bearer token gate. The bridge fronts a tool-executing agent, so it must not
# be an open control surface even on the tailnet. Open WebUI sends this as its
# OpenAI API key. Override via the BRIDGE_TOKEN env var.
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "CHANGE-ME")  # real value injected via LaunchAgent env

# --- Per-device tokens + one-time pairing codes -------------------------------
# Hardened pairing: the QR carries a short-lived ONE-TIME CODE, never the master
# BRIDGE_TOKEN. The desktop (which holds the master token) mints a code via
# /pair/new; the phone exchanges it at /pair/claim for its OWN device token,
# which is stored server-side and can be revoked per device. The master token
# keeps working (desktop + any already-connected client), so this is additive.
_POCKET_DIR = os.path.expanduser("~/.pocket")
_DEVICE_TOKENS_PATH = os.path.join(_POCKET_DIR, "device-tokens.json")
_PAIR_LOCK = threading.Lock()
_PAIR_CODES: dict = {}          # code -> {expiry, apple_user_id} or legacy expiry
_PAIR_CODE_TTL = 600.0          # a pairing code is valid for 10 minutes
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
APPLE_ID_ISSUER = "https://appleid.apple.com"
APPLE_WEB_PUBLIC_AUDIENCE = os.environ.get(
    "APPLE_WEB_PUBLIC_AUDIENCE", "com.pocketagent.web"
).strip()
APPLE_ID_AUDIENCES = tuple(dict.fromkeys([
    *(
        a.strip()
        for a in os.environ.get("APPLE_ID_AUDIENCES", "com.pocketagent.ios").split(",")
        if a.strip()
    ),
    APPLE_WEB_PUBLIC_AUDIENCE,
]))
APPLE_WEB_AUTHORIZE_URL = "https://appleid.apple.com/auth/authorize"
APPLE_WEB_TOKEN_URL = "https://appleid.apple.com/auth/token"
APPLE_WEB_CLIENT_ID = os.environ.get(
    "APPLE_WEB_CLIENT_ID", APPLE_WEB_PUBLIC_AUDIENCE
).strip()
APPLE_WEB_REDIRECT_URI = os.environ.get("APPLE_WEB_REDIRECT_URI", "").strip()
APPLE_WEB_TEAM_ID = os.environ.get("APPLE_WEB_TEAM_ID", "").strip()
APPLE_WEB_KEY_ID = os.environ.get("APPLE_WEB_KEY_ID", "").strip()
APPLE_WEB_PRIVATE_KEY_PATH = os.path.expanduser(
    os.environ.get("APPLE_WEB_PRIVATE_KEY_PATH", "").strip()
)
APPLE_WEB_FLOW_TTL = 600
APPLE_WEB_FLOW_LIMIT = 256
APPLE_WEB_START_RATE_LIMIT = 10
APPLE_WEB_START_RATE_WINDOW = 60.0
ACCOUNT_SESSION_PREFIX = "paacct."
ACCOUNT_SESSION_TTL = 60 * 60 * 24 * 90
_APPLE_JWK_CLIENT = None
_APPLE_WEB_FLOWS: dict = {}
_APPLE_WEB_STARTS: dict[str, collections.deque] = {}
_APPLE_WEB_FLOW_LOCK = threading.Lock()


def _load_device_tokens() -> dict:
    try:
        with open(_DEVICE_TOKENS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}          # first run — nothing paired yet, not an error
    except Exception as e:  # noqa: BLE001
        # A corrupt tokens file silently logs every paired device out; that
        # must be visible in the log, not swallowed (issue #7).
        _log_event("device_tokens_load_failed", path=_DEVICE_TOKENS_PATH,
                   error=type(e).__name__, error_message=str(e)[:160])
        return {}


def _save_device_tokens(d: dict) -> None:
    os.makedirs(_POCKET_DIR, exist_ok=True)
    tmp = _DEVICE_TOKENS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _DEVICE_TOKENS_PATH)


# Brute-force guard for the token gate. Once the bridge is reachable from the
# public internet (Tailscale Funnel), the only thing between an attacker and a
# tool-executing agent is this token, so failed attempts are rate-limited. A
# VALID token is never throttled — only wrong guesses accrue, so a flood of bad
# tokens can't lock out the real client (no self-inflicted DoS). With a long
# random token, brute force is already infeasible; this mainly stops scanning
# and log spam, and signals abuse via 429.
_AUTH_FAILS: collections.deque = collections.deque()
_AUTH_FAIL_WINDOW = 60.0  # seconds
_AUTH_FAIL_MAX = 12       # wrong guesses per window before 429
_AUTH_LOCK = threading.Lock()
_AUTH_FAIL_AGG: dict = {}


def _log_event(event: str, **fields) -> None:
    payload = {
        "event": event,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **fields,
    }
    print("[bridge-event] " + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


# Loaded after _log_event exists so a corrupt tokens file gets logged.
_DEVICE_TOKENS: dict = _load_device_tokens()


def _client_host(request: Request) -> str:
    return request.client.host if request.client else ""


def _short_hash(value: str | None) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]


def _attachment_stats(attachments: list) -> dict:
    kinds = collections.Counter((a or {}).get("kind") or "unknown" for a in (attachments or []))
    return {
        "attachment_count": len(attachments or []),
        "image_count": kinds.get("image", 0),
        "audio_count": kinds.get("audio", 0),
        "file_count": sum(v for k, v in kinds.items() if k not in ("image", "audio")),
    }


def _auth_fail_summary_locked(request: Request, status: int, now: float) -> dict | None:
    key = (_client_host(request), request.url.path, status)
    item = _AUTH_FAIL_AGG.setdefault(key, {"count": 0, "last_log": 0.0})
    item["count"] += 1
    count = item["count"]
    should_log = count in (1, 10, 50, 100) or now - item["last_log"] >= 60.0
    if not should_log:
        return None
    item["last_log"] = now
    return {
        "client": key[0],
        "path": key[1],
        "status": status,
        "count": count,
        "window_seconds": int(_AUTH_FAIL_WINDOW),
    }


def _check_auth(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if hmac.compare_digest(token, BRIDGE_TOKEN):
        return  # constant-time match; master token always allowed
    if token:
        # Per-device token (issued via /pair/claim). Membership check + refresh
        # last_seen in memory only (no disk write per request).
        with _PAIR_LOCK:
            dev = _DEVICE_TOKENS.get(token)
            if dev is not None:
                if not dev.get("apple_user_id") or _account_device_for_token(token) is not None:
                    dev["last_seen"] = time.time()
                    return
        if _account_device_for_token(token) is not None:
            return
    now = time.monotonic()
    with _AUTH_LOCK:
        while _AUTH_FAILS and now - _AUTH_FAILS[0] > _AUTH_FAIL_WINDOW:
            _AUTH_FAILS.popleft()
        _AUTH_FAILS.append(now)
        over = len(_AUTH_FAILS) > _AUTH_FAIL_MAX
        summary = _auth_fail_summary_locked(request, 429 if over else 401, now)
    if summary:
        _log_event("auth_failure", **summary)
    if over:
        raise HTTPException(status_code=429, detail="too many failed auth attempts; slow down")
    raise http_err(401, "AUTH_INVALID_TOKEN", "invalid bridge token")


async def _json_body(request: Request) -> dict:
    """Body-as-dict with empty/malformed JSON tolerated as {} — handlers then
    hit their own field validation (400) instead of the parser's 500."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}

HERMES_BIN = "/Users/xcash/apps/hermes-agent/runtime/venv/bin/hermes"
HOME_ROOT = "/Users/xcash/apps/hermes-agent/home"

# In-app terminal kill switch (TERMINAL_PTY_CONTRACT.md §安全). Paired devices
# get full shell access over /app/v1/terminal, so a self-hosted owner needs an
# escape hatch; "0" makes the endpoint refuse every handshake.
POCKET_TERMINAL_ENABLED = os.environ.get("POCKET_TERMINAL_ENABLED", "1") != "0"

# model id -> (display name, HERMES_HOME). id stays ascii for client URLs.
# G6 (wave 2): these four are the code-level BUILTINS; the personas table in
# canonical.db overlays them (rename / disable / soft-delete) and adds custom
# personas. PERSONAS itself stays a plain {id: (display, home)} dict mutated
# in place by _personas_reload(), so every existing consumer keeps working and
# CRUD takes effect without a restart.
_PERSONAS_BUILTIN = {
    "yuanfang":    ("袁方 (幕僚長/main)", HOME_ROOT),
    "pantianqing": ("潘天晴 (FLiPER)",    f"{HOME_ROOT}/profiles/fliper"),
    "xcash":       ("XCash (PocketAgent 協調)", f"{HOME_ROOT}/profiles/xcash"),
    "shuijing":    ("水鏡 (shuijing)",    f"{HOME_ROOT}/profiles/shuijing"),
}
PERSONAS = dict(_PERSONAS_BUILTIN)

# ── Persona 正典身分(TG 同源)─────────────────────────────────────────
# HOME_ROOT/avatars/ 是四人格(+自訂)的視覺與命名正典:manifest.json 提供
# name(TG bot 顯示名)/file(頭像檔)/tg(@username,可後補),圖檔供
# /app/v1/personas/<id>/avatar 直接下發。manifest 缺漏/損壞一律安靜退回
# 既有 builtins+db 行為(備援鐵律)。
_AVATARS_DIR = f"{HOME_ROOT}/avatars"
_avatar_manifest_cache = {"mtime": -1.0, "data": {}}


def _avatar_manifest() -> dict:
    path = os.path.join(_AVATARS_DIR, "manifest.json")
    try:
        mt = os.path.getmtime(path)
        if mt != _avatar_manifest_cache["mtime"]:
            with open(path, encoding="utf-8") as f:
                _avatar_manifest_cache["data"] = json.load(f).get("personas") or {}
            _avatar_manifest_cache["mtime"] = mt
    except Exception:  # noqa: BLE001 — 無 manifest = 無 overlay
        _avatar_manifest_cache["data"] = {}
        _avatar_manifest_cache["mtime"] = -1.0
    return _avatar_manifest_cache["data"]


def _avatar_path(pid: str):
    """頭像實檔路徑(manifest.file 優先,預設 <pid>.png);不存在/越界 → None。"""
    ent = _avatar_manifest().get(pid) or {}
    fn = ent.get("file") or f"{pid}.png"
    p = os.path.realpath(os.path.join(_AVATARS_DIR, fn))
    root = os.path.realpath(_AVATARS_DIR) + os.sep
    return p if p.startswith(root) and os.path.isfile(p) else None


def _personas_db_rows() -> list:
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        rows = con.execute("SELECT id,name,home,enabled,deleted FROM personas").fetchall()
        con.close()
        return rows
    except Exception:  # noqa: BLE001
        return []      # table not created yet (first boot) → builtins only


def _personas_reload() -> None:
    """Rebuild PERSONAS from builtins + the personas table. In-place mutation:
    all lookups (home_for, /sessions, message endpoints) see changes at once."""
    merged = dict(_PERSONAS_BUILTIN)
    for pid, name, home, enabled, deleted in _personas_db_rows():
        if deleted or not enabled:
            merged.pop(pid, None)
            continue
        base = merged.get(pid, (pid, HOME_ROOT))
        merged[pid] = (name or base[0], home or base[1])
    # TG 同源正典名 overlay(manifest 有名字就贏 — xcash 2026-07-05:同步是首要)
    for pid, ent in _avatar_manifest().items():
        if pid in merged and ent.get("name"):
            merged[pid] = (ent["name"], merged[pid][1])
    PERSONAS.clear()
    PERSONAS.update(merged)

# Per-(persona, conversation) hermes session name. Open WebUI doesn't send a
# stable conversation id in the OpenAI schema, so we key on persona only —
# one continuing conversation per persona (matches "talk to each persona").
def session_name(model: str) -> str:
    return f"owui-{model}"


def home_for(model: str) -> str:
    return PERSONAS.get(model, (None, HOME_ROOT))[1]


async def acp_full(model: str, prompt: str) -> str:
    """Collect a whole ACP turn into one string (non-streaming clients)."""
    session = await POOL.get(model, home_for(model))
    parts = []
    async for kind, val in session.prompt_stream(prompt):
        if kind == "text":
            parts.append(val)
    return ("".join(parts)).strip() or "(空回應)"


app = FastAPI(title="Hermes ↔ OpenAI bridge")


@app.middleware("http")
async def _body_size_guard(request: Request, call_next):
    """全域 request body 上限 — `await request.json()` 是整包進記憶體的,
    沒有這道閥一個超大 base64 就能把 bridge 打爆(修復單「附件限制」)。
    無 Content-Length(chunked)放行,由件數/單檔閥背書。"""
    try:
        cl = int(request.headers.get("content-length") or 0)
    except ValueError:
        cl = 0
    if cl > _BODY_MAX_BYTES:
        return JSONResponse(status_code=413,
                            content={"error": {"code": "BODY_TOO_LARGE",
                                               "message": f"body 上限 {_BODY_MAX_BYTES} bytes"}})
    return await call_next(request)


# ───────────────────────── structured error codes (issue #6) ────────────────
# Every HTTP error carries a machine-readable code so the app can localize
# (pocketagent#44) instead of string-matching English detail text. The legacy
# top-level `detail` field is PRESERVED for old clients.
class BridgeError(HTTPException):
    def __init__(self, status: int, code: str, message: str, detail: str = ""):
        super().__init__(status_code=status, detail=detail or message)
        self.code = code
        self.message = message


def http_err(status: int, code: str, message: str, detail: str = "") -> BridgeError:
    """Build a coded HTTP error: raise http_err(404, "SESSION_NOT_FOUND", ...)."""
    return BridgeError(status, code, message, detail)


# Fallback codes for plain HTTPException raises that haven't adopted http_err.
_GENERIC_ERROR_CODES = {
    400: "BAD_REQUEST", 401: "AUTH_INVALID_TOKEN", 403: "FORBIDDEN",
    404: "NOT_FOUND", 409: "CONFLICT", 413: "PAYLOAD_TOO_LARGE",
    429: "RATE_LIMITED", 500: "INTERNAL_ERROR", 502: "UPSTREAM_FAILED",
    504: "PROVIDER_TIMEOUT",
}

from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402


@app.exception_handler(StarletteHTTPException)
async def _bridge_http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, ensure_ascii=False)
    code = getattr(exc, "code", "") or _GENERIC_ERROR_CODES.get(exc.status_code, "HTTP_ERROR")
    message = getattr(exc, "message", "") or detail
    body = {
        "detail": exc.detail,   # backward compat: old clients read this
        "error": {"code": code, "message": message, "detail": detail},
    }
    headers = dict(getattr(exc, "headers", None) or {})
    headers["X-Error-Code"] = code
    return JSONResponse(status_code=exc.status_code, content=body, headers=headers)


@app.get("/")
async def root():
    """Unauthenticated liveness probe. The app/monitors hit GET / to decide
    "bridge up?" — a 404 here was read as bridge-down and fueled an endless
    reconnect banner loop on the phone (11,936 404s in one log). Cheap, no
    secrets, no auth."""
    return {"ok": True, "service": "pocket-bridge", "ts": int(time.time())}


@app.get("/v1/models")
async def list_models(request: Request):
    _check_auth(request)
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": mid, "object": "model", "created": now, "owned_by": "hermes",
             "name": disp}
            for mid, (disp, _home) in PERSONAS.items()
        ],
    }


async def run_hermes(model: str, prompt: str) -> str:
    home = PERSONAS.get(model, (None, HOME_ROOT))[1]
    env = dict(os.environ)
    env["HERMES_HOME"] = home
    # Cold fallback targets the SAME canonical Telegram session as the warm ACP
    # path (--resume takes a session id), so a fallback turn still lands where
    # the TG gateway looks — instead of a private owui-<persona> session the
    # phone/TG never see. Only without a mapping do we keep the old behaviour.
    sid = canonical_telegram_session(home)
    cont = ["--resume", sid] if sid else ["--continue", session_name(model)]
    proc = await asyncio.create_subprocess_exec(
        HERMES_BIN, "-z", prompt, *cont,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        return "⚠️ Hermes 回應逾時(180s)。"
    text = (out or b"").decode("utf-8", "replace").strip()
    if not text:
        text = (err or b"").decode("utf-8", "replace").strip() or "(空回應)"
    return text


# Where inbound attachments (images/files from the app's composer) land on the
# Studio box. We persist bytes here and hand the agent the path — every backend
# (Hermes persona / Claude Code / Codex) can Read a file, so this works across
# all three AND fixes the old "Claude sees the inline image but can't get the
# bytes" bug (HANDOFF known-issue #3).
UPLOAD_DIR = Path(os.path.expanduser("~/apps/hermes-agent/home/uploads"))

_MIME_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
             "image/webp": ".webp", "image/heic": ".heic", "application/pdf": ".pdf",
             "audio/m4a": ".m4a", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
             "audio/aac": ".m4a", "audio/mpeg": ".mp3", "audio/wav": ".wav",
             "audio/x-wav": ".wav", "audio/webm": ".webm"}


# 附件上限(修復單「附件限制」bridge 端):count 與 /app/v1/uploads 既有 12 件
# 一致,推廣到所有直送口;單檔上限給 app 檔案路 8MB 的 4 倍餘裕;全域 body
# 上限是記憶體防爆閥(12 檔 × 32MB 的 base64 膨脹仍在其下)。
_ATT_MAX_COUNT = 12
_ATT_MAX_FILE_BYTES = 32 * 1024 * 1024
_BODY_MAX_BYTES = 768 * 1024 * 1024


def _att_guard(attachments) -> None:
    """直送 attachments 的件數守門 — 超過即 413(之前只有 uploads 有擋)。"""
    if isinstance(attachments, list) and len(attachments) > _ATT_MAX_COUNT:
        raise http_err(413, "TOO_MANY_ATTACHMENTS",
                       f"attachments 最多 {_ATT_MAX_COUNT} 件")


def _data_uri_estimated_bytes(data_uri: str) -> int:
    """base64 內容的解碼後大小估算(不真的解碼)— uploads 預檢用。"""
    i = (data_uri or "").find(";base64,")
    return 0 if i < 0 else (len(data_uri) - i - 8) * 3 // 4


def _save_data_uri(data_uri: str, filename: str = "") -> str | None:
    """Decode a `data:<mime>;base64,<...>` URI to UPLOAD_DIR; return the path."""
    m = re.match(r"data:([^;]+);base64,(.*)$", data_uri or "", re.DOTALL)
    if not m:
        return None
    mime, b64 = m.group(1), m.group(2)
    # 單檔大小閥(所有 data-URI 落盤的唯一咽喉):超限不落盤,skip+log —
    # 對齊 iOS 端「超過上限先略過」的行為,不炸整包請求。
    if len(b64) * 3 // 4 > _ATT_MAX_FILE_BYTES:
        _log_event("save_data_uri_rejected", reason="too_large", mime=mime,
                   filename=(filename or "")[:80], est_bytes=len(b64) * 3 // 4)
        return None
    try:
        raw = base64.b64decode(b64)
    except Exception as e:  # noqa: BLE001
        _log_event("save_data_uri_failed", stage="b64decode", mime=mime,
                   filename=(filename or "")[:80], error=type(e).__name__)
        return None
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\-]", "_", os.path.basename(filename or "")) or "file"
    if "." not in safe:
        safe += _MIME_EXT.get(mime, "")
    path = UPLOAD_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}-{safe}"
    try:
        path.write_bytes(raw)
    except Exception as e:  # noqa: BLE001
        _log_event("save_data_uri_failed", stage="write", mime=mime,
                   path=str(path), bytes=len(raw),
                   error=type(e).__name__, error_message=str(e)[:160])
        return None
    return str(path)


def _upload_ref_path(value: str | None) -> str | None:
    """Accept only previously uploaded local files under UPLOAD_DIR."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.startswith("data:"):
        return None
    if raw.startswith("file://"):
        raw = raw[7:]
    try:
        root = UPLOAD_DIR.expanduser().resolve()
        path = Path(os.path.expanduser(raw)).resolve()
    except Exception:  # noqa: BLE001
        return None
    if not (path == root or root in path.parents):
        return None
    try:
        return str(path) if path.is_file() else None
    except Exception:  # noqa: BLE001
        return None


def _save_attachment(a: dict, default_filename: str = "file") -> str | None:
    """Return an uploaded attachment path, saving legacy dataURI payloads if needed."""
    if not isinstance(a, dict):
        return None
    for key in ("path", "local_path", "file_path"):
        path = _upload_ref_path(a.get(key))
        if path:
            return path
    url_path = _upload_ref_path(a.get("url"))
    if url_path:
        return url_path
    filename = a.get("filename") or default_filename
    data_uri = a.get("data") or a.get("data_uri") or ""
    return _save_data_uri(data_uri, filename)


def _save_part_payload(value: str | None, filename: str) -> str | None:
    return _upload_ref_path(value) or _save_data_uri(value or "", filename)


# ───────────────────────── voice transcription (語音訊息) ───────────────────
# LINE-style voice messages: the app sends the audio file, the bridge transcribes
# it, and the transcript becomes the turn text (the audio still shows in-chat).
# Uses OpenAI whisper-1 (the stt.openai provider Hermes is already configured
# with) — faster-whisper isn't installed on the box.
_OPENAI_CLIENT = None


def _openai_key() -> str:
    try:
        for line in open(os.path.expanduser("~/apps/hermes-agent/home/.env")):
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip()
    except Exception:  # noqa: BLE001
        pass
    return os.environ.get("OPENAI_API_KEY", "")


def _openai_client():
    global _OPENAI_CLIENT
    if _OPENAI_CLIENT is None:
        from openai import OpenAI
        _OPENAI_CLIENT = OpenAI(api_key=_openai_key())
    return _OPENAI_CLIENT


# 預設本地 faster-whisper(OSS 自架預設 = Mac Studio/mini,跑得動;與 hermes
# 同 venv 共用安裝與模型快取)。POCKET_STT=openai 才走雲端;本地失敗且有 key
# 時自動雲端備援。模型 POCKET_STT_MODEL(預設 large-v3-turbo:品質貼平
# large-v3、速度同 medium 級;首次使用下載 ~1.6GB)。
STT_PROVIDER = os.environ.get("POCKET_STT", "local")
STT_MODEL = os.environ.get("POCKET_STT_MODEL", "large-v3-turbo")
_WHISPER_MODEL = None

# app 介面語言 → whisper 語言碼 + 繁/簡輸出偏置(Whisper 對中文預設常吐簡體,
# initial_prompt 是標準治法;en 鎖英文;未知/空 = 自動偵測)。
_STT_LANG = {"zh-Hant": "zh", "zh-Hans": "zh", "zh": "zh", "en": "en"}
_STT_PROMPT = {"zh-Hant": "以下是繁體中文的對話內容。", "zh-Hans": "以下是简体中文的对话内容。"}


def _whisper_model():
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        from faster_whisper import WhisperModel
        _WHISPER_MODEL = WhisperModel(STT_MODEL, device="auto", compute_type="auto")
    return _WHISPER_MODEL


def _transcribe_openai(path: str, lang: str) -> str:
    with open(path, "rb") as f:
        kw = {}
        if _STT_LANG.get(lang):
            kw["language"] = _STT_LANG[lang]
        if _STT_PROMPT.get(lang):
            kw["prompt"] = _STT_PROMPT[lang]
        r = _openai_client().audio.transcriptions.create(model="whisper-1", file=f, **kw)
    return (r.text or "").strip()


def _transcribe(path: str, lang: str = "") -> str:
    """Audio file path → transcript (best-effort; '' on failure).
    lang = app 介面語言(zh-Hant/zh-Hans/en/'' = 自動)。其餘呼叫端(CC/CX
    語音流)不帶 lang = 自動偵測,行為不變。"""
    if STT_PROVIDER == "openai":
        try:
            return _transcribe_openai(path, lang)
        except Exception as e:  # noqa: BLE001
            print(f"[voice] openai transcription failed: {e}", flush=True)
            return ""
    try:
        segs, _info = _whisper_model().transcribe(
            path, language=_STT_LANG.get(lang),
            initial_prompt=_STT_PROMPT.get(lang), vad_filter=True)
        return "".join(seg.text for seg in segs).strip()
    except Exception as e:  # noqa: BLE001
        print(f"[voice] local transcription failed: {e}", flush=True)
        if _openai_key():   # 本地掛了(模型下載中斷等)→ 有 key 就雲端備援
            try:
                return _transcribe_openai(path, lang)
            except Exception as e2:  # noqa: BLE001
                print(f"[voice] openai fallback failed: {e2}", flush=True)
        return ""


async def _transcribe_attachments(attachments: list, lang: str = "") -> str:
    """Save + transcribe every audio attachment; return the joined transcript.
    Runs the blocking whisper call off the event loop."""
    texts = []
    for a in (attachments or []):
        if a.get("kind") != "audio":
            continue
        path = _save_attachment(a, a.get("filename") or "voice.m4a")
        if not path:
            continue
        t = await asyncio.to_thread(_transcribe, path, lang)
        if t:
            texts.append(t)
    return " ".join(texts).strip()


def _extract_user_parts(messages: list):
    """Last user message → (text, image_paths, [(label, file_path)]). Persists
    any attachments to UPLOAD_DIR."""
    for m in reversed(messages or []):
        if m.get("role") == "user":
            c = m.get("content")
            if not isinstance(c, list):
                return ((c or "").strip(), [], [])
            texts, images, files = [], [], []
            for p in c:
                if not isinstance(p, dict):
                    continue
                t = p.get("type")
                if t == "text" and p.get("text"):
                    texts.append(p["text"])
                elif t == "image_url":
                    path = _save_part_payload((p.get("image_url") or {}).get("url", ""), "image.jpg")
                    if path:
                        images.append(path)
                elif t == "file":
                    f = p.get("file") or {}
                    path = _save_part_payload(f.get("file_data") or f.get("path") or f.get("url"),
                                              f.get("filename", "file"))
                    if path:
                        files.append((f.get("filename") or "檔案", path))
            return (" ".join(texts).strip(), images, files)
    return ("", [], [])


def _last_user_message(messages: list) -> str:
    """Text + on-disk paths for the last user turn. Used by CC/Codex sub-sessions,
    which can Read image files natively, so images stay as path references."""
    text, images, files = _extract_user_parts(messages)
    notes = [f"- 圖片:{p}" for p in images] + [f"- {label}:{p}" for label, p in files]
    if notes:
        text = (text + "\n\n[使用者附了以下檔案,已存到本機。請先用 Read/檔案工具讀取再回答]\n"
                + "\n".join(notes)).strip()
    return text


async def _describe_image(path: str) -> str:
    """Hermes personas have no vision, so we pre-read images with Claude Code
    (which does) and hand the persona a text description instead of a bare path.
    This is what makes image attachments actually work for a persona turn."""
    proc = None
    try:
        argv = [CLAUDE_BIN, "-p",
                (f"請讀取圖片檔 {path},用繁體中文詳細描述內容;"
                 "若是截圖,把可見的關鍵文字與數字也讀出來。只回描述本身,不要客套。"),
                "--permission-mode", "bypassPermissions", "--output-format", "text"]
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
        return (out or b"").decode("utf-8", "replace").strip()
    except Exception as e:  # noqa: BLE001
        _log_event("describe_image_failed", path=path,
                   error=type(e).__name__, error_message=str(e)[:160])
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
        return ""


async def _resolve_persona_prompt(messages: list) -> str:
    """Prompt for a persona turn: text + file paths + vision descriptions of any
    images (so a non-vision Hermes persona can still 'see' the picture)."""
    text, images, files = _extract_user_parts(messages)
    notes = [f"- {label}:{p}(請用 Read 讀取)" for label, p in files]
    for path in images:
        desc = await _describe_image(path)
        notes.append(f"- 圖片內容({path}):{desc}" if desc
                     else f"- 圖片:{path}(自動描述失敗,請嘗試 Read)")
    if notes:
        text = (text + "\n\n[使用者附件]\n" + "\n".join(notes)).strip()
    return text


# ───────────────────────── canonical store (M20) ───────────────────────────
# Bridge-owned source of truth for app turns, so the iPhone is NOT the only copy
# — survives reinstall / new device and interleaves with the Telegram history.
# The app talks to it through the versioned /app/v1 API; it never touches the
# Hermes state.db schema or cron JSON directly.
CANON_DB = os.environ.get("POCKET_CANON_DB") \
    or os.path.expanduser("~/.local/share/pocket-agent/canonical.db")
ACCOUNTS_DB = os.path.expanduser("~/.local/share/pocket-agent/accounts.db")
REPORT_MEMORY_FILE = "REPORTS.md"
REPORT_MEMORY_ITEMS = 20
REPORT_MEMORY_CHARS = 2400
REPORT_CONTEXT_DEFAULT = 3
REPORT_CONTEXT_TRIGGERED = 8
REPORT_CONTEXT_CHARS = 18000
REPORT_CONTEXT_ITEM_CHARS = 5000


def _canon_init():
    import sqlite3
    os.makedirs(os.path.dirname(CANON_DB), exist_ok=True)
    con = sqlite3.connect(CANON_DB, timeout=30)
    # WAL: concurrent handlers no longer serialize writers against readers;
    # busy_timeout waits out short lock contention instead of erroring.
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("""CREATE TABLE IF NOT EXISTS messages(
        id TEXT PRIMARY KEY, session TEXT NOT NULL, role TEXT NOT NULL,
        content TEXT, attachments TEXT, created_at REAL NOT NULL, status TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_msg_session_time ON messages(session, created_at)")
    # client_id: stable per-logical-send id so a retry after a dropped network
    # connection replays the recorded reply instead of re-running the turn.
    cols = [r[1] for r in con.execute("PRAGMA table_info(messages)").fetchall()]
    if "client_id" not in cols:
        con.execute("ALTER TABLE messages ADD COLUMN client_id TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS idx_msg_client ON messages(session, client_id)")
    # Reaction overlay (G2, pocketagent#39): keyed by the message id the app
    # sees in GET /app/v1/messages — canonical mids AND tg-<ts> ids alike — so
    # one table syncs reactions on both app-sent and Telegram-side messages.
    con.execute("""CREATE TABLE IF NOT EXISTS reactions(
        msg_id TEXT PRIMARY KEY, session TEXT, reaction TEXT, updated_at REAL)""")
    # Canonical reactions/pins (G2, pocketagent#39 final contract): multi-emoji
    # reactions (JSON list) + per-message pin, keyed by the id the app sees in
    # GET /app/v1/messages. Supersedes the single-`reaction` overlay above,
    # which is kept for backward compatibility with older app builds.
    con.execute("""CREATE TABLE IF NOT EXISTS message_meta(
        message_id TEXT PRIMARY KEY, reactions TEXT, pinned INTEGER,
        updated_at REAL)""")
    # G4 tombstone (wave 2): deleted messages stay in the list, flagged. The
    # table may pre-date this column, so ALTER idempotently.
    meta_cols = [r[1] for r in con.execute("PRAGMA table_info(message_meta)").fetchall()]
    if "deleted" not in meta_cols:
        con.execute("ALTER TABLE message_meta ADD COLUMN deleted INTEGER")
    # G2/#39 canonical 化收尾:pin 要能按 session 讀回(PUT/GET
    # /app/v1/sessions/{id}/pin),overlay 列補 session 歸屬。回填只認
    # canonical messages 表 — tg-<ts>/報告 id 不在其中,維持 NULL,查詢端
    # 以「messages join」補洞(見 _session_pinned_ids)。冪等:WHERE IS NULL。
    if "session" not in meta_cols:
        con.execute("ALTER TABLE message_meta ADD COLUMN session TEXT")
    con.execute("UPDATE message_meta SET session="
                "(SELECT m.session FROM messages m WHERE m.id=message_meta.message_id)"
                " WHERE session IS NULL")
    # G6 (wave 2): persona registry — overlays/extends the code builtins so
    # personas can be added / renamed / disabled without editing bridge.py.
    con.execute("""CREATE TABLE IF NOT EXISTS personas(
        id TEXT PRIMARY KEY, name TEXT, home TEXT,
        enabled INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0,
        created_at REAL, updated_at REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS approvals(
        id TEXT PRIMARY KEY, title TEXT, source TEXT, risk TEXT, detail TEXT,
        created_at REAL, expires_at REAL, status TEXT, decided_at REAL, result TEXT)""")
    # B4 (issue #9): decision push-back — the creating skill can register a
    # callback URL that gets POSTed when the approval is decided/expired,
    # instead of having to poll GET /app/v1/approvals/{id}.
    approval_cols = [r[1] for r in con.execute("PRAGMA table_info(approvals)").fetchall()]
    if "callback" not in approval_cols:
        con.execute("ALTER TABLE approvals ADD COLUMN callback TEXT")
    # A1 (Approval Hub 遷移切片): 統一 approval 物件 — 新欄位 + 回填。
    # session_id/provider/kind/options 與 source 並存(source 相容期保留原樣);
    # options 存建立方宣告的鍵(JSON 文字)。回填帶 IS NULL 守門,冪等。
    # hermes 舊列的 source 是自由字串 → session_id 不硬造(拍板:留 NULL)。
    for _col in ("session_id", "provider", "kind", "options"):
        if _col not in approval_cols:
            con.execute(f"ALTER TABLE approvals ADD COLUMN {_col} TEXT")
    con.execute("UPDATE approvals SET provider=CASE"
                " WHEN source LIKE 'claude_code:%' THEN 'claude_code'"
                " WHEN source LIKE 'codex%' THEN 'codex'"
                " ELSE 'hermes' END WHERE provider IS NULL")
    con.execute("UPDATE approvals SET session_id=source WHERE session_id IS NULL"
                " AND (source LIKE 'claude_code:%' OR source LIKE 'codex:%')")
    con.execute("UPDATE approvals SET kind='permission' WHERE kind IS NULL")
    con.execute("""CREATE TABLE IF NOT EXISTS devices(
        token TEXT PRIMARY KEY, platform TEXT, created_at REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS report_events(
        id TEXT PRIMARY KEY, session TEXT NOT NULL, label TEXT, name TEXT,
        content TEXT NOT NULL, ts REAL NOT NULL,
        external_source TEXT, external_id TEXT UNIQUE, ingested_at REAL NOT NULL)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_report_session_time ON report_events(session, ts)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_report_external ON report_events(external_source, external_id)")
    # Sync engine P0 (docs/SYNC_ENGINE_REWRITE_PLAN_20260711.md §3):單一
    # append-only 事件日誌,id 即全域遞增 seq。P0/P1 只寫不讀(雙寫過渡,
    # 現有 canonical/state.db 讀取路徑不動),P2 起由 /app/v2/events 消費。
    # external_id 供來源鏡射去重(TG/cron 是重複掃描式接入,必須冪等)。
    con.execute("""CREATE TABLE IF NOT EXISTS event_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session TEXT NOT NULL,
        type TEXT NOT NULL,
        external_id TEXT UNIQUE,
        payload TEXT NOT NULL,
        created_at REAL NOT NULL)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_event_session_seq ON event_log(session, id)")
    # Sync engine P2:已讀游標的伺服器真相(取代 App 端 UserDefaults 計數
    # 器的長期方向)。一列 = 一個(session, device)的已讀位置 — 按裝置分列
    # 存,是為了「任一裝置讀過即全讀」(MAX over devices)與「每裝置各自
    # 記」兩種語意都能從同一份資料推導;多裝置語意由善彰拍板後在 App 端
    # (P3)選聚合方式,schema 不用改。
    con.execute("""CREATE TABLE IF NOT EXISTS read_cursors(
        session TEXT NOT NULL,
        device_id TEXT NOT NULL,
        last_read_seq INTEGER NOT NULL DEFAULT 0,
        last_read_ts REAL NOT NULL DEFAULT 0,
        message_id TEXT,
        updated_at REAL NOT NULL,
        PRIMARY KEY(session, device_id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS delegations(
        id TEXT PRIMARY KEY,
        work_order TEXT UNIQUE,
        parent_persona TEXT NOT NULL,
        parent_session TEXT,
        created_via TEXT,
        provider TEXT NOT NULL,
        title TEXT,
        objective TEXT,
        cwd TEXT,
        status TEXT,
        provider_session_id TEXT,
        codex_thread_id TEXT,
        cc_session_name TEXT,
        created_at REAL,
        updated_at REAL,
        last_error TEXT,
        meta TEXT,
        task_code TEXT,
        subtask_code TEXT)""")
    delegation_cols = [r[1] for r in con.execute("PRAGMA table_info(delegations)").fetchall()]
    for name, ddl in {
        "provider_session_id": "ALTER TABLE delegations ADD COLUMN provider_session_id TEXT",
        "codex_thread_id": "ALTER TABLE delegations ADD COLUMN codex_thread_id TEXT",
        "cc_session_name": "ALTER TABLE delegations ADD COLUMN cc_session_name TEXT",
        "last_error": "ALTER TABLE delegations ADD COLUMN last_error TEXT",
        "meta": "ALTER TABLE delegations ADD COLUMN meta TEXT",
        "task_code": "ALTER TABLE delegations ADD COLUMN task_code TEXT",
        "subtask_code": "ALTER TABLE delegations ADD COLUMN subtask_code TEXT",
    }.items():
        if name not in delegation_cols:
            con.execute(ddl)
    con.execute("CREATE INDEX IF NOT EXISTS idx_delegation_parent ON delegations(parent_persona, updated_at)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_delegation_provider ON delegations(provider, provider_session_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_delegation_task ON delegations(task_code, subtask_code)")
    # SUBSESSIONS persistence (issue #5, plan A): /dispatch sub-sessions used to
    # live only in the in-memory dict, so a bridge restart wiped them all —
    # transcript, resume target (cc_session) and isolate cwd included.
    con.execute("""CREATE TABLE IF NOT EXISTS subsessions(
        sid TEXT PRIMARY KEY, name TEXT, parent TEXT, tool TEXT, status TEXT,
        cwd TEXT, worktree TEXT, cc_session TEXT, last_user TEXT,
        last_at REAL, output_json TEXT)""")
    con.commit()
    con.close()


ACCOUNT_USER_COLUMNS = ("apple_user_id", "email", "display_name", "created_at", "last_seen_at")
ACCOUNT_DEVICE_COLUMNS = (
    "device_id", "apple_user_id", "device_token", "platform", "label",
    "paired_at", "last_seen_at", "revoked",
)


def _accounts_init():
    import sqlite3
    os.makedirs(os.path.dirname(ACCOUNTS_DB), exist_ok=True)
    con = sqlite3.connect(ACCOUNTS_DB, timeout=30)
    # Same WAL rationale as canonical.db (issue #7): auth reads happen on every
    # request, so writers (pair/claim, last_seen) must not lock readers out.
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("""CREATE TABLE IF NOT EXISTS users(
        apple_user_id TEXT PRIMARY KEY,
        email TEXT,
        display_name TEXT,
        created_at REAL NOT NULL,
        last_seen_at REAL NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS devices(
        device_id TEXT PRIMARY KEY,
        apple_user_id TEXT NOT NULL,
        device_token TEXT NOT NULL UNIQUE,
        platform TEXT,
        label TEXT,
        paired_at REAL NOT NULL,
        last_seen_at REAL,
        revoked INTEGER DEFAULT 0,
        FOREIGN KEY(apple_user_id) REFERENCES users(apple_user_id)
            ON UPDATE CASCADE ON DELETE CASCADE)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_account_devices_user ON devices(apple_user_id, revoked)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_account_devices_token ON devices(device_token)")
    con.commit()
    con.close()


def _account_user_row(row):
    return dict(zip(ACCOUNT_USER_COLUMNS, row)) if row else None


def _account_device_row(row):
    return dict(zip(ACCOUNT_DEVICE_COLUMNS, row)) if row else None


def _account_public_user(user: dict | None):
    if not user:
        return None
    return {
        "apple_user_id": user.get("apple_user_id"),
        "email": user.get("email"),
        "display_name": user.get("display_name"),
        "created_at": user.get("created_at"),
        "last_seen_at": user.get("last_seen_at"),
    }


def _account_public_device(device: dict | None):
    if not device:
        return None
    token = device.get("device_token")
    return {
        "device_id": device.get("device_id"),
        "apple_user_id": device.get("apple_user_id"),
        "platform": device.get("platform"),
        "label": device.get("label"),
        "paired_at": device.get("paired_at"),
        "last_seen_at": device.get("last_seen_at"),
        "revoked": bool(device.get("revoked")),
        "token_hash": _short_hash(token),
    }


def _account_upsert_user(apple_user_id: str, email: str | None = None,
                         display_name: str | None = None):
    import sqlite3
    now = time.time()
    con = sqlite3.connect(ACCOUNTS_DB, timeout=30)
    con.execute(
        """INSERT INTO users(apple_user_id,email,display_name,created_at,last_seen_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(apple_user_id) DO UPDATE SET
             email=COALESCE(excluded.email, users.email),
             display_name=COALESCE(excluded.display_name, users.display_name),
             last_seen_at=excluded.last_seen_at""",
        (apple_user_id, email or None, display_name or None, now, now))
    row = con.execute(
        f"SELECT {','.join(ACCOUNT_USER_COLUMNS)} FROM users WHERE apple_user_id=?",
        (apple_user_id,)).fetchone()
    con.commit()
    con.close()
    return _account_user_row(row)


def _account_get_user(apple_user_id: str, touch: bool = False):
    import sqlite3
    if not apple_user_id:
        return None
    con = sqlite3.connect(ACCOUNTS_DB, timeout=30)
    if touch:
        con.execute("UPDATE users SET last_seen_at=? WHERE apple_user_id=?",
                    (time.time(), apple_user_id))
        con.commit()
    row = con.execute(
        f"SELECT {','.join(ACCOUNT_USER_COLUMNS)} FROM users WHERE apple_user_id=?",
        (apple_user_id,)).fetchone()
    con.close()
    return _account_user_row(row)


def _account_devices_for_user(apple_user_id: str, include_revoked: bool = False):
    import sqlite3
    con = sqlite3.connect(f"file:{ACCOUNTS_DB}?mode=ro", uri=True, timeout=5)
    if include_revoked:
        rows = con.execute(
            f"SELECT {','.join(ACCOUNT_DEVICE_COLUMNS)} FROM devices "
            "WHERE apple_user_id=? ORDER BY paired_at DESC",
            (apple_user_id,)).fetchall()
    else:
        rows = con.execute(
            f"SELECT {','.join(ACCOUNT_DEVICE_COLUMNS)} FROM devices "
            "WHERE apple_user_id=? AND revoked=0 ORDER BY paired_at DESC",
            (apple_user_id,)).fetchall()
    con.close()
    return [_account_device_row(r) for r in rows]


def _account_device_put(apple_user_id: str, device_token: str, platform: str = "ios",
                        label: str = "device", device_id: str | None = None):
    import sqlite3
    now = time.time()
    device_id = device_id or "dev-" + uuid.uuid4().hex
    con = sqlite3.connect(ACCOUNTS_DB, timeout=30)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute(
        """INSERT INTO devices(device_id,apple_user_id,device_token,platform,label,
                               paired_at,last_seen_at,revoked)
           VALUES(?,?,?,?,?,?,?,0)
           ON CONFLICT(device_token) DO UPDATE SET
             apple_user_id=excluded.apple_user_id,
             platform=excluded.platform,
             label=excluded.label,
             last_seen_at=excluded.last_seen_at,
             revoked=0""",
        (device_id, apple_user_id, device_token, platform or "ios",
         (label or "device")[:80], now, now))
    row = con.execute(
        f"SELECT {','.join(ACCOUNT_DEVICE_COLUMNS)} FROM devices WHERE device_token=?",
        (device_token,)).fetchone()
    con.commit()
    con.close()
    return _account_device_row(row)


def _account_device_for_token(device_token: str, touch: bool = True):
    import sqlite3
    if not device_token:
        return None
    try:
        con = sqlite3.connect(ACCOUNTS_DB, timeout=30)
        row = con.execute(
            f"SELECT {','.join(ACCOUNT_DEVICE_COLUMNS)} FROM devices "
            "WHERE device_token=? AND revoked=0",
            (device_token,)).fetchone()
        if row and touch:
            con.execute("UPDATE devices SET last_seen_at=? WHERE device_token=?",
                        (time.time(), device_token))
            con.commit()
        con.close()
        return _account_device_row(row)
    except Exception:  # noqa: BLE001
        return None


def _account_device_by_id(apple_user_id: str, device_id: str):
    import sqlite3
    if not apple_user_id or not device_id:
        return None
    con = sqlite3.connect(f"file:{ACCOUNTS_DB}?mode=ro", uri=True, timeout=5)
    row = con.execute(
        f"SELECT {','.join(ACCOUNT_DEVICE_COLUMNS)} FROM devices "
        "WHERE apple_user_id=? AND device_id=?",
        (apple_user_id, device_id)).fetchone()
    con.close()
    return _account_device_row(row)


def _account_device_revoke(apple_user_id: str, device_id: str):
    import sqlite3
    con = sqlite3.connect(ACCOUNTS_DB, timeout=30)
    cur = con.execute(
        "UPDATE devices SET revoked=1, last_seen_at=? "
        "WHERE apple_user_id=? AND device_id=? AND revoked=0",
        (time.time(), apple_user_id, device_id))
    con.commit()
    revoked = cur.rowcount
    con.close()
    return revoked


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _account_session_create(apple_user_id: str):
    now = int(time.time())
    exp = now + ACCOUNT_SESSION_TTL
    payload = {"sub": apple_user_id, "iat": now, "exp": exp}
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(BRIDGE_TOKEN.encode("utf-8"), body, hashlib.sha256).digest()
    return ACCOUNT_SESSION_PREFIX + _b64u(body) + "." + _b64u(sig), exp


def _account_session_payload(token: str):
    if not token or not token.startswith(ACCOUNT_SESSION_PREFIX):
        raise HTTPException(status_code=401, detail="missing account session")
    try:
        body_part, sig_part = token[len(ACCOUNT_SESSION_PREFIX):].split(".", 1)
        body = _b64u_decode(body_part)
        sig = _b64u_decode(sig_part)
        expected = hmac.new(BRIDGE_TOKEN.encode("utf-8"), body, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            raise ValueError("bad signature")
        payload = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=401, detail="invalid account session")
    if int(payload.get("exp") or 0) < int(time.time()):
        raise HTTPException(status_code=401, detail="account session expired")
    if not payload.get("sub"):
        raise HTTPException(status_code=401, detail="invalid account session")
    return payload


def _account_session_token_from_request(request: Request, body: dict | None = None):
    token = (request.headers.get("x-pocket-account-session")
             or request.headers.get("x-account-session") or "").strip()
    if not token and body:
        token = str(body.get("account_session") or body.get("accountSession") or "").strip()
    if not token:
        auth = request.headers.get("authorization", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if bearer.startswith(ACCOUNT_SESSION_PREFIX):
            token = bearer
    return token


def _account_user_from_request(request: Request, body: dict | None = None,
                               required: bool = True):
    token = _account_session_token_from_request(request, body)
    if not token:
        if required:
            raise HTTPException(status_code=401, detail="missing account session")
        return None
    payload = _account_session_payload(token)
    user = _account_get_user(payload.get("sub") or "", touch=True)
    if not user:
        raise HTTPException(status_code=401, detail="unknown account session")
    return user


def _apple_jwk_client():
    global _APPLE_JWK_CLIENT
    if _APPLE_JWK_CLIENT is None:
        import jwt as pyjwt
        _APPLE_JWK_CLIENT = pyjwt.PyJWKClient(APPLE_JWKS_URL, cache_keys=True)
    return _APPLE_JWK_CLIENT


def _apple_verify_identity_token(identity_token: str, audience=None):
    import jwt as pyjwt
    expected_audience = audience or list(APPLE_ID_AUDIENCES)
    if not expected_audience:
        raise HTTPException(status_code=500, detail="APPLE_ID_AUDIENCES is not configured")
    try:
        header = pyjwt.get_unverified_header(identity_token)
        if header.get("alg") != "RS256" or not header.get("kid"):
            raise ValueError("unexpected jwt header")
        signing_key = _apple_jwk_client().get_signing_key_from_jwt(identity_token)
        return pyjwt.decode(
            identity_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=expected_audience,
            issuer=APPLE_ID_ISSUER,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        _log_event("apple_auth_invalid_token", error=type(e).__name__)
        raise HTTPException(status_code=401, detail="invalid apple identity token")


def _apple_web_config_error() -> str | None:
    required = {
        "APPLE_WEB_CLIENT_ID": APPLE_WEB_CLIENT_ID,
        "APPLE_WEB_REDIRECT_URI": APPLE_WEB_REDIRECT_URI,
        "APPLE_WEB_TEAM_ID": APPLE_WEB_TEAM_ID,
        "APPLE_WEB_KEY_ID": APPLE_WEB_KEY_ID,
        "APPLE_WEB_PRIVATE_KEY_PATH": APPLE_WEB_PRIVATE_KEY_PATH,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        return "missing " + ", ".join(missing)
    parsed = urllib.parse.urlparse(APPLE_WEB_REDIRECT_URI)
    if parsed.scheme != "https" or not parsed.netloc:
        return "APPLE_WEB_REDIRECT_URI must be an https URL"
    key_path = Path(APPLE_WEB_PRIVATE_KEY_PATH)
    if not key_path.is_file():
        return "APPLE_WEB_PRIVATE_KEY_PATH is not readable"
    return None


def _apple_web_cleanup_locked(now: float | None = None) -> None:
    now = now or time.time()
    expired = [
        flow_id for flow_id, flow in _APPLE_WEB_FLOWS.items()
        if float(flow.get("expires_at") or 0) <= now
    ]
    for flow_id in expired:
        _APPLE_WEB_FLOWS.pop(flow_id, None)


def _apple_web_start_client_hash(request: Request) -> str:
    # cloudflared supplies this header. Direct production access is localhost
    # only, so an internet client cannot choose this value without traversing
    # Cloudflare first.
    client = (
        request.headers.get("cf-connecting-ip", "").strip()
        or _client_host(request)
        or "unknown"
    )
    return _short_hash(client)


def _apple_web_check_start_rate(request: Request) -> str:
    now = time.monotonic()
    client_hash = _apple_web_start_client_hash(request)
    with _APPLE_WEB_FLOW_LOCK:
        stale_before = now - APPLE_WEB_START_RATE_WINDOW
        for key in list(_APPLE_WEB_STARTS):
            attempts = _APPLE_WEB_STARTS[key]
            while attempts and attempts[0] <= stale_before:
                attempts.popleft()
            if not attempts:
                _APPLE_WEB_STARTS.pop(key, None)
        attempts = _APPLE_WEB_STARTS.setdefault(client_hash, collections.deque())
        if len(attempts) >= APPLE_WEB_START_RATE_LIMIT:
            raise HTTPException(status_code=429, detail="too many sign-in attempts")
        attempts.append(now)
    return client_hash


def _apple_web_new_flow() -> dict:
    now = time.time()
    with _APPLE_WEB_FLOW_LOCK:
        _apple_web_cleanup_locked(now)
        if len(_APPLE_WEB_FLOWS) >= APPLE_WEB_FLOW_LIMIT:
            raise HTTPException(status_code=503, detail="too many active sign-in attempts")
        flow = {
            "flow_id": secrets.token_urlsafe(18),
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "poll_secret": secrets.token_urlsafe(32),
            "created_at": now,
            "expires_at": now + APPLE_WEB_FLOW_TTL,
            "status": "pending",
            "result": None,
            "error": None,
        }
        _APPLE_WEB_FLOWS[flow["flow_id"]] = flow
        return dict(flow)


def _apple_web_claim_flow(state: str) -> dict | None:
    now = time.time()
    with _APPLE_WEB_FLOW_LOCK:
        _apple_web_cleanup_locked(now)
        for flow in _APPLE_WEB_FLOWS.values():
            if hmac.compare_digest(str(flow.get("state") or ""), state):
                if flow.get("status") != "pending":
                    return None
                flow["status"] = "processing"
                return dict(flow)
    return None


def _apple_web_finish_flow(flow_id: str, status: str, result=None,
                           error: str | None = None) -> None:
    with _APPLE_WEB_FLOW_LOCK:
        flow = _APPLE_WEB_FLOWS.get(flow_id)
        if not flow or flow.get("status") != "processing":
            return
        flow["status"] = status
        flow["result"] = result
        flow["error"] = error


def _apple_web_client_secret() -> str:
    import jwt as pyjwt
    config_error = _apple_web_config_error()
    if config_error:
        raise RuntimeError(config_error)
    key_path = Path(APPLE_WEB_PRIVATE_KEY_PATH)
    if key_path.stat().st_size > 64 * 1024:
        raise RuntimeError("Sign in with Apple private key is unexpectedly large")
    private_key = key_path.read_text(encoding="utf-8")
    now = int(time.time())
    return pyjwt.encode(
        {
            "iss": APPLE_WEB_TEAM_ID,
            "iat": now - 5,
            "exp": now + 300,
            "aud": APPLE_ID_ISSUER,
            "sub": APPLE_WEB_CLIENT_ID,
        },
        private_key,
        algorithm="ES256",
        headers={"kid": APPLE_WEB_KEY_ID},
    )


async def _apple_web_exchange_code(code: str) -> dict:
    import httpx
    client_secret = await asyncio.to_thread(_apple_web_client_secret)
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            APPLE_WEB_TOKEN_URL,
            data={
                "client_id": APPLE_WEB_CLIENT_ID,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": APPLE_WEB_REDIRECT_URI,
            },
            headers={"Accept": "application/json"},
        )
    try:
        payload = response.json()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("Apple token endpoint returned invalid JSON") from e
    if response.status_code != 200:
        error_code = str(payload.get("error") or "unknown")
        _log_event("apple_web_token_exchange_failed",
                   status=response.status_code, apple_error=error_code[:80])
        raise RuntimeError("Apple authorization code validation failed")
    identity_token = str(payload.get("id_token") or "")
    if not identity_token:
        raise RuntimeError("Apple token response is missing id_token")
    return payload


def _apple_web_display_name(user_payload: dict) -> str | None:
    name = user_payload.get("name")
    if not isinstance(name, dict):
        return None
    parts = [
        str(name.get(key) or "").strip()
        for key in ("firstName", "lastName", "givenName", "familyName")
    ]
    # Apple uses firstName/lastName on the web. The second pair keeps this
    # tolerant of native-shaped fixtures without duplicating either value.
    if parts[0] or parts[1]:
        parts = parts[:2]
    else:
        parts = parts[2:]
    return " ".join(part for part in parts if part).strip() or None


def _apple_web_callback_page(kind: str) -> HTMLResponse:
    if kind == "success":
        title = "Pocket 登入完成"
        message = "已完成 Apple 登入，可以關閉這個頁面並回到 Pocket。"
    elif kind == "cancelled":
        title = "已取消登入"
        message = "你可以關閉這個頁面，回到 Pocket 後重新登入。"
    else:
        title = "登入未完成"
        message = "請關閉這個頁面，回到 Pocket 後重新嘗試。"
    html = f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
      font: 16px -apple-system, BlinkMacSystemFont, sans-serif;
      color: #15171a; background: #f5f6f8; }}
    main {{ width: min(34rem, calc(100% - 3rem)); }}
    h1 {{ margin: 0 0 .75rem; font-size: 1.75rem; letter-spacing: 0; }}
    p {{ margin: 0; color: #555b66; line-height: 1.6; }}
  </style>
</head>
<body><main><h1>{title}</h1><p>{message}</p></main></body>
</html>"""
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                       "base-uri 'none'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )


# canonical messages 的寫入版本計數(真事件推送,取代 SSE 每 2 秒重掃):
# _canon_add 成功寫入就 +1,followers 用 _canon_wait 盯版本、變了才重掃 DB。
# 純 int 比較、無鎖 — 就算極端併發丟失一次遞增,值仍有變化,喚醒不漏。
_CANON_VER: dict[str, int] = {}


def _canon_notify(session: str) -> None:
    _CANON_VER[session] = _CANON_VER.get(session, 0) + 1


async def _canon_wait(session: str, seen_ver: int) -> None:
    """等到該 session 的 canonical 版本離開 seen_ver(有新寫入)。0.2s 粒度
    的純記憶體輪詢 — 不碰 DB、不用 Condition(避開取消時的鎖重取競態);
    推送延遲 ≤0.2s,配合外層 wait_for(timeout=SSE_KEEPALIVE_SECS) 保持
    keepalive 節奏。"""
    while _CANON_VER.get(session, 0) == seen_ver:
        await asyncio.sleep(0.2)


# ── Sync engine P0:event_log 資料層(SYNC_ENGINE_REWRITE_PLAN §3/P0)────
# 單一事件日誌 + 游標訂閱的地基。這一層只提供 append / since 兩個原語;
# 誰來寫(P1 三來源鏡射)、誰來讀(P2 /app/v2/events)都在上層。
# _EVENT_VER 與 _CANON_VER 同款:純 int 版本號、無鎖,喚醒不漏即可。
_EVENT_VER: dict[str, int] = {}
# 記憶體去重快取:TG/cron 的接入是「重複掃描」式,同一批 external_id 每輪
# 都會再撞一次 DB 的 INSERT OR IGNORE;這層快取讓穩態掃描零寫入。重啟後
# 快取歸零沒關係 — DB 的 UNIQUE(external_id) 仍然守住冪等,只是第一輪
# 掃描多付幾次 no-op INSERT。
_EVENT_SEEN: dict[str, set] = {}
_EVENT_SEEN_CAP = 8192
# 全域版本計數(不分 session):/app/v2/events 省略 session 的全域訂閱
# (P3 契約 #2:App 首頁列表+未讀用單一條 SSE)靠這個喚醒,不用每 0.2s
# 掃整個 per-session dict。與 per-session 版同款:純 int、無鎖、喚醒不漏。
_EVENT_VER_ALL = 0


def _event_notify(session: str) -> None:
    global _EVENT_VER_ALL
    _EVENT_VER[session] = _EVENT_VER.get(session, 0) + 1
    _EVENT_VER_ALL += 1


async def _event_wait(session: str, seen_ver: int) -> None:
    """等到該 session 的 event_log 版本離開 seen_ver(有新事件)。與
    _canon_wait 同款 0.2s 純記憶體輪詢,不碰 DB、不用 Condition。"""
    while _EVENT_VER.get(session, 0) == seen_ver:
        await asyncio.sleep(0.2)


def _event_append(session: str, etype: str, payload: dict,
                  external_id: str | None = None) -> int:
    """Append 一筆事件,回傳全域 seq(=event_log.id);0 表示去重略過或寫入
    失敗。絕不 raise — 鏡射寫入掛在既有熱路徑上(_canon_add/_report_upsert/
    合併掃描),event_log 故障只能降級成「新路徑落後」,不准拖垮舊路徑。"""
    import sqlite3
    try:
        if external_id and external_id in _EVENT_SEEN.get(session, ()):
            return 0
        con = sqlite3.connect(CANON_DB, timeout=30)
        cur = con.execute(
            "INSERT OR IGNORE INTO event_log(session,type,external_id,payload,created_at) "
            "VALUES(?,?,?,?,?)",
            (session, etype, external_id,
             json.dumps(payload, ensure_ascii=False), time.time()))
        seq = int(cur.lastrowid or 0) if cur.rowcount else 0
        con.commit()
        con.close()
        if external_id:
            seen = _EVENT_SEEN.setdefault(session, set())
            if len(seen) >= _EVENT_SEEN_CAP:
                seen.clear()    # 粗略上限:清空後由 DB UNIQUE 繼續守冪等
            seen.add(external_id)
        if seq:
            _event_notify(session)
        return seq
    except Exception as e:  # noqa: BLE001
        _log_event("event_append_failed", session=session, type=etype,
                   error=type(e).__name__, error_message=str(e)[:160])
        return 0


def _event_since(session: str, since_seq: int = 0, limit: int = 500) -> list[dict]:
    """撈 id > since_seq 的事件(即時 + 補洞共用同一條查詢)。信封對齊
    /app/v2/sessions/{id}/events 的 {seq,ts,type,data} 形狀。"""
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        rows = con.execute(
            "SELECT id,type,payload,created_at FROM event_log "
            "WHERE session=? AND id>? ORDER BY id LIMIT ?",
            (session, int(since_seq or 0), max(1, limit))).fetchall()
        con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("event_since_failed", session=session,
                   error=type(e).__name__, error_message=str(e)[:160])
        return []
    out = []
    for r in rows:
        try:
            data = json.loads(r[2] or "{}")
        except Exception:  # noqa: BLE001
            data = {}
        out.append({"seq": r[0], "ts": r[3], "type": r[1], "data": data})
    return out


def _event_since_all(since_seq: int = 0, limit: int = 500) -> list[dict]:
    """全域版 _event_since:不分 session 撈 id > since_seq 的事件,餵
    /app/v2/events 省略 session 的全域訂閱。event_log.id 本來就是全域
    autoincrement,所以全域游標語意天然成立。信封比 per-session 版多帶
    session 欄位(App 端 SyncEvent 收 session|session_id 雙鍵)。
    SQL 限定 session IN 現任 PERSONAS — 落實拍板「v2 事件流只收 hermes
    人格 session」:被移除的 persona 與未來任何非人格寫入不會漏進全域流。"""
    import sqlite3
    sessions = list(PERSONAS)
    if not sessions:
        return []
    try:
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        rows = con.execute(
            "SELECT id,session,type,payload,created_at FROM event_log "
            f"WHERE id>? AND session IN ({','.join('?' * len(sessions))}) "
            "ORDER BY id LIMIT ?",
            (int(since_seq or 0), *sessions, max(1, limit))).fetchall()
        con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("event_since_all_failed",
                   error=type(e).__name__, error_message=str(e)[:160])
        return []
    out = []
    for r in rows:
        try:
            data = json.loads(r[3] or "{}")
        except Exception:  # noqa: BLE001
            data = {}
        out.append({"seq": r[0], "ts": r[4], "type": r[2],
                    "session": r[1], "data": data})
    return out


def _event_latest_seq(session: str) -> int:
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        row = con.execute("SELECT MAX(id) FROM event_log WHERE session=?",
                          (session,)).fetchone()
        con.close()
        return int(row[0] or 0)
    except Exception:  # noqa: BLE001
        return 0


# ── Sync engine P1:三來源鏡射(SYNC_ENGINE_REWRITE_PLAN §4 P1)─────────
# App 訊息(_canon_add)/ TG(state.db 合併掃描)/ cron 晨報(_report_upsert)
# 都額外鏡射一份進 event_log。雙寫過渡:現有讀取路徑一律不動,event_log
# 在 P2 之前只做影子累積。
#
# 鍵設計:{source}:{app可見id}:{sha1(role|status|content)[:16]}。
# - 同一則訊息重複掃到 → 同鍵 → 去重(TG/cron 是重複掃描式接入)
# - 同 id 但內容/狀態變了(報告改稿、訊息補寫)→ 新鍵 → 追加一筆新的
#   message.upsert 事件,client 端以 message id 做 last-write-wins 覆蓋
_EVENT_SYNC_TS: dict[str, float] = {}
_EVENT_SYNC_MIN_SECS = float(os.environ.get("POCKET_EVENT_SYNC_SECS", "10"))


def _event_msg_key(m: dict) -> str:
    basis = f"{m.get('role')}|{m.get('status')}|{m.get('content') or ''}"
    h = hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{m.get('source') or 'app'}:{m.get('id')}:{h}"


def _event_mirror_messages(session: str, msgs: list) -> int:
    """把一批 app-shape 訊息 dict 鏡射進 event_log(冪等,靠 external_id
    去重)。回傳真正新寫入的筆數。絕不 raise。"""
    n = 0
    for m in msgs or []:
        try:
            key = _event_msg_key(m)
        except Exception:  # noqa: BLE001
            continue
        if _event_append(session, "message.upsert", {"message": m},
                         external_id=key):
            n += 1
    return n


def _event_sync_session(session: str, limit: int = 200,
                        force: bool = False,
                        min_secs: float | None = None) -> None:
    """把 TG(state.db)+ cron 晨報拉進 event_log 的主動同步(P2 SSE 端點
    在訂閱期間週期呼叫)。合併/清洗/去重全部沿用 _hp_merged_messages —
    鏡射就掛在它的回傳路徑上,這裡只負責觸發 + 節流(同 session 至多每
    _EVENT_SYNC_MIN_SECS 掃一次,多個訂閱者共享)。min_secs 可換小節流:
    statedb watcher 喚醒路徑用 0.4s(配合呼叫端 0.5s 去抖)— 要穿越 10s
    週期節流即時拉,但多訂閱者同時醒時仍只掃一次。"""
    now = time.monotonic()
    floor = _EVENT_SYNC_MIN_SECS if min_secs is None else min_secs
    if not force and now - _EVENT_SYNC_TS.get(session, 0.0) < floor:
        return
    _EVENT_SYNC_TS[session] = now
    try:
        _hp_merged_messages(session, limit)   # 鏡射在合併函式內完成
    except Exception as e:  # noqa: BLE001
        _log_event("event_sync_failed", session=session,
                   error=type(e).__name__, error_message=str(e)[:160])


# ── TG/cron → state.db 寫入即時偵測(#tg-instant-sync)───────────────────
# 根因:App 自己送出/收到的訊息走上面 _canon_notify/_canon_wait,寫入當下
# 就 bump 版本、~0.2s 內喚醒 follower。但 Telegram 端訊息與 cron 晨報是
# **Hermes 官方 gateway 進程**寫進各 persona 自己的 `<home>/state.db`(WAL
# mode),那條寫入路徑在 hermes_cli 官方套件內部 —— 鐵律規定不准碰內核,
# 所以完全不能掛 hook/callback 在寫入那一刻觸發。
#
# 這裡改用「唯讀輕量輪詢」繞過去:WAL mode 下,真正的寫入落在
# `<home>/state.db-wal`(checkpoint 前主 db 檔案本身不太動),只要每
# ~0.15s 對這個檔案做一次 os.stat()(不開檔、不連 sqlite、不解析內容),
# mtime/size 一變就代表「剛剛有新內容寫進去」,立刻 bump 一個獨立的版本
# 計數器喚醒對應 persona 的 follower 去重掃 `_hp_merged_messages`。
# 這跟 `_canon_notify` 是同一種模式(純 int 版本號、無鎖),只是觸發源從
# 「我們自己呼叫 _canon_add」換成「別人的程序寫了這個檔案」。
#
# 30s 保險絲(_hp_canon_follower 裡的 timeout=30.0)完全保留 —— 這個 stat
# watcher 是「加速觸發」疊加在上面,不是取代:watcher 掛掉/漏抓(例如
# checkpoint 時序恰好卡在兩次 stat 中間、mtime 精度不足撞期）,30s 週期
# 還是會補上,同步不會因為單一機制失效就整個停擺。
_STATEDB_VER: dict[str, int] = {}
_STATEDB_VER_ALL = 0    # 全域計數,配 _EVENT_VER_ALL(全域訂閱喚醒用)
_STATEDB_STAT_CACHE: dict[str, tuple] = {}   # session -> (path, mtime_ns, size)
_STATEDB_POLL_SECS = float(os.environ.get("POCKET_STATEDB_POLL_SECS", "0.15"))


def _statedb_notify(session: str) -> None:
    global _STATEDB_VER_ALL
    _STATEDB_VER[session] = _STATEDB_VER.get(session, 0) + 1
    _STATEDB_VER_ALL += 1


def _statedb_stat_key(home: str) -> tuple:
    """只用 os.stat(),唯讀、不開檔、不連 DB。WAL 模式下實際寫入處是
    state.db-wal;沒有的話(已 checkpoint 或非 WAL)退回 state.db 本身。
    讀不到任何一個就回傳 (None, 0, 0),呼叫端據此跳過該 session 這一輪。"""
    for name in ("state.db-wal", "state.db"):
        p = os.path.join(home, name)
        try:
            st = os.stat(p)
            return (p, st.st_mtime_ns, st.st_size)
        except OSError:
            continue
    return (None, 0, 0)


async def _state_db_watcher_loop() -> None:
    """常駐背景迴圈:每輪對每個 persona home 的 state.db(-wal) 做一次
    os.stat(),偵測到 mtime/size 變動就判定「TG/cron 剛寫入」,bump
    `_STATEDB_VER` 喚醒 `_hp_canon_follower` 立刻重掃,不必等 30s 保險絲。
    例外全吞:這條 loop 死掉不影響既有的 30s 兜底路徑,只是退回原本
    的延遲,不會讓同步整個停擺(鐵律 #4)。"""
    while True:
        try:
            for session, (_, home) in list(PERSONAS.items()):
                key = _statedb_stat_key(home)
                path = key[0]
                if path is None:
                    continue
                prev = _STATEDB_STAT_CACHE.get(session)
                if prev is not None and prev[0] == path and (prev[1] != key[1] or prev[2] != key[2]):
                    _statedb_notify(session)
                _STATEDB_STAT_CACHE[session] = key
        except Exception as e:  # noqa: BLE001
            _log_event("state_db_watcher_error", error=type(e).__name__,
                       error_message=str(e)[:160])
        await asyncio.sleep(_STATEDB_POLL_SECS)


async def _canon_or_statedb_wait(session: str, seen_canon_ver: int,
                                 seen_state_ver: int) -> None:
    """`_canon_wait` 的擴充版:canonical 版本 *或* state.db stat 版本任一
    變動就返回。同款 0.2s 純記憶體輪詢,無鎖、無 Condition。"""
    while (_CANON_VER.get(session, 0) == seen_canon_ver
           and _STATEDB_VER.get(session, 0) == seen_state_ver):
        await asyncio.sleep(0.2)


async def _event_or_statedb_wait(session: str, seen_ver: int,
                                 seen_state_ver: int) -> None:
    """`_event_wait` 的擴充版(v2 事件迴圈用):event_log 版本 *或*
    state.db stat 版本任一變動就返回。statedb 醒 = TG/cron 剛寫入但還沒
    鏡射進 event_log,呼叫端要立刻 _event_sync_session 把它拉進來 —
    v2 訂閱者的 TG 延遲從節流上限(10s)壓到 ~0.4s。"""
    while (_EVENT_VER.get(session, 0) == seen_ver
           and _STATEDB_VER.get(session, 0) == seen_state_ver):
        await asyncio.sleep(0.2)


async def _event_or_statedb_wait_all(seen_ver: int,
                                     seen_state_ver: int) -> None:
    """全域版 _event_or_statedb_wait(/app/v2/events 省略 session 的訂閱用):
    任何 session 的 event_log 或 state.db 有動靜就返回。盯兩個全域 int,
    不掃 per-session dict。"""
    while (_EVENT_VER_ALL == seen_ver
           and _STATEDB_VER_ALL == seen_state_ver):
        await asyncio.sleep(0.2)


def _canon_add(session: str, role: str, content: str, attachments=None,
               mid: str | None = None, status: str = "done",
               client_id: str | None = None, created_at: float | None = None,
               push: bool = True) -> tuple[str, bool]:
    # created_at:TG 鏡像 ingest 帶事件原始時間戳 —— 重放同一事件落同一
    # (mid, ts),不會把訊息「頂」到現在。push=False:TG 端已送達的回覆
    # 不再推播(否則同一句話 TG 通知 + Pocket 通知各一次)。
    import sqlite3
    mid = mid or uuid.uuid4().hex
    now = created_at if created_at is not None else time.time()
    try:
        con = sqlite3.connect(CANON_DB)
        con.execute("INSERT OR REPLACE INTO messages"
                    "(id,session,role,content,attachments,created_at,status,client_id) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (mid, session, role, content, json.dumps(attachments or [], ensure_ascii=False),
                     now, status, client_id))
        con.commit()
        con.close()
        _canon_notify(session)
        # Sync engine P1:App 訊息寫入點順便鏡射進 event_log(雙寫過渡)。
        # payload 形狀對齊 _canon_messages 的輸出,client 兩邊看到同一種訊息。
        _event_mirror_messages(session, [{
            "id": mid, "role": role, "content": content,
            "attachments": attachments or [], "ts": now, "status": status,
            "client_id": client_id, "source": "app"}])
        # P1-3:人格完成一則回覆 → 推播把你叫回 app(前景由 app willPresent 抑制)。
        if push and role == "assistant" and status == "done":
            _push_persona_reply(session, content)
        return mid, True
    except Exception as e:  # noqa: BLE001
        _log_event("canonical_write_failed",
                   session=session, role=role, status=status,
                   client_id_hash=_short_hash(client_id),
                   content_chars=len(content or ""),
                   attachment_count=len(attachments or []),
                   error=type(e).__name__, error_message=str(e)[:160])
    return mid, False


def _canon_add_retry(session: str, role: str, content: str, attachments=None,
                     mid: str | None = None, status: str = "done",
                     client_id: str | None = None) -> tuple[str, bool]:
    """_canon_add + one retry (issue #9): a dropped canonical write makes the
    turn invisible to replay/idempotency, so it's worth a second attempt."""
    mid, ok = _canon_add(session, role, content, attachments, mid=mid,
                         status=status, client_id=client_id)
    if not ok:
        _log_event("canonical_write_retry", session=session, role=role,
                   client_id_hash=_short_hash(client_id))
        mid, ok = _canon_add(session, role, content, attachments, mid=mid,
                             status=status, client_id=client_id)
    return mid, ok


def _canon_reply_for_client(session: str, client_id: str):
    """If this logical send already produced a recorded assistant reply (e.g. the
    first attempt succeeded server-side but the client's network dropped), return
    it so a retry replays it instead of re-running the turn."""
    import sqlite3
    if not client_id:
        return None
    try:
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        row = con.execute(
            "SELECT content FROM messages WHERE session=? AND client_id=? "
            "AND role='assistant' AND status='done' AND content IS NOT NULL AND content!='' "
            "ORDER BY created_at DESC LIMIT 1", (session, client_id)).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:  # noqa: BLE001
        return None


def _canon_messages(session: str, limit: int = 200):
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        rows = con.execute("SELECT id,role,content,attachments,created_at,status,client_id FROM messages "
                           "WHERE session=? ORDER BY created_at DESC LIMIT ?", (session, limit)).fetchall()
        con.close()
    except Exception:  # noqa: BLE001
        return []
    rows.reverse()
    return [{"id": r[0], "role": r[1], "content": r[2],
             "attachments": json.loads(r[3] or "[]"), "ts": r[4],
             "status": r[5], "client_id": r[6], "source": "app"} for r in rows]


def _app_message_seq(m: dict) -> int:
    try:
        return int(float(m.get("ts") or 0) * 1000)
    except Exception:  # noqa: BLE001
        return int(time.time() * 1000)


def _app_message_event(m: dict) -> dict:
    return {"seq": _app_message_seq(m), "type": "message.upsert",
            "message_id": m.get("id"), "payload": {"message": m}}


def _canonical_reply_failure(reply: str) -> tuple[str, str, str] | None:
    """Classify bridge-generated terminal replies for recovery clients.

    A timeout is persisted as a canonical assistant message so every surface
    sees it, but that does not make the turn successful. Returning `done` here
    made Pocket hide its retry control and replay the same timed-out client id.
    """
    text = (reply or "").lower()
    if ("回合逾時" in reply or "回應逾時" in reply
            or "伺服器端 5 分鐘" in reply or "turn timed out" in text):
        return "timeout", "回合逾時", "persona turn timed out"
    return None


def _app_turn_status(session: str, client_id: str | None = None,
                     acp_busy: bool = False) -> dict:
    """Current app-turn recovery status for the mobile client.

    The POST /app/v1/messages stream can legitimately be detached by a mobile
    network drop. This status surface lets Pocket recover by stable client_id
    without re-running the persona turn.
    """
    now = time.monotonic()
    entry = None
    if client_id:
        entry = _APP_TURN_INFLIGHT.get((session, client_id))
    state = entry.get("state") if entry else {}
    task = entry.get("task") if entry else None
    acc = (state or {}).get("acc") or ""
    canonical_reply = _canon_reply_for_client(session, client_id) if client_id else None
    runner_error = (state or {}).get("runner_error") or (state or {}).get("stream_error") or ""
    canonical_failure = _canonical_reply_failure(canonical_reply or "")
    in_flight = bool(task is not None and not task.done())
    if canonical_failure:
        turn_state, label, canonical_error = canonical_failure
    elif canonical_reply:
        turn_state, label = "done", "已同步"
        canonical_error = ""
    elif in_flight:
        turn_state = "streaming" if acc else ("queued" if acp_busy else "running")
        label = (state or {}).get("step_label") or ("思考中" if acc else "處理中")
        canonical_error = ""
    elif task is not None and task.done():
        # The background task has ended and canonical lookup still found no
        # reply. There is nothing left for a detached client to wait for: mark
        # it retryable instead of reporting stream_detached forever.
        turn_state, label = "failed", "回合未能保存"
        canonical_error = runner_error or (
            "persona reply was not persisted" if acc else "persona returned no reply"
        )
    elif acp_busy:
        turn_state, label = "running", "處理中"
        canonical_error = ""
    else:
        turn_state, label = "idle", "閒置"
        canonical_error = ""
    status_error = canonical_error or ("" if canonical_reply else runner_error)
    elapsed = int(now - entry["ts"]) if entry and entry.get("ts") else None
    return {"session": session, "state": turn_state, "label": label,
            "in_flight": in_flight, "acp_busy": acp_busy,
            "elapsed_seconds": elapsed, "stale_seconds": elapsed,
            "output_chars": len(acc), "canonical_reply": bool(canonical_reply),
            "canonical_reply_chars": len(canonical_reply or ""),
            "error": status_error or None}


# ───────────────────── SUBSESSIONS persistence (issue #5) ───────────────────
_SUB_OUTPUT_JSON_CAP = 2 * 1024 * 1024   # ~2MB persisted transcript per sub
_SUB_TRUNC_MARKER = ("text", "_(前段已截斷)_\n\n")


def _sub_output_json(output: list) -> str:
    """Serialize a sub's transcript, truncating OLDEST items to stay ≤ ~2MB."""
    items = [[k, v] for k, v in (output or [])]
    js = json.dumps(items, ensure_ascii=False)
    if len(js) <= _SUB_OUTPUT_JSON_CAP:
        return js
    while items and len(js) > _SUB_OUTPUT_JSON_CAP:
        drop = max(1, len(items) // 10)      # shed in chunks, not one-by-one
        items = items[drop:]
        js = json.dumps([list(_SUB_TRUNC_MARKER)] + items, ensure_ascii=False)
    return js


def _subsession_persist(sid: str) -> bool:
    """Flush one SUBSESSIONS entry to canonical.db (insert-or-replace)."""
    import sqlite3
    sub = SUBSESSIONS.get(sid)
    if not sub:
        return False
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        con.execute("INSERT OR REPLACE INTO subsessions"
                    "(sid,name,parent,tool,status,cwd,worktree,cc_session,"
                    "last_user,last_at,output_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, sub.get("name"), sub.get("parent"), sub.get("tool"),
                     sub.get("status"), sub.get("cwd"), sub.get("worktree"),
                     sub.get("cc_session"), sub.get("last_user"),
                     sub.get("lastAt") or time.time(),
                     _sub_output_json(sub.get("output"))))
        con.commit()
        con.close()
        return True
    except Exception as e:  # noqa: BLE001
        _log_event("subsession_persist_failed", sid=sid,
                   error=type(e).__name__, error_message=str(e)[:160])
        return False


def _subsessions_load():
    """Rebuild SUBSESSIONS from canonical.db on startup. Anything that was
    status=running when the bridge died is marked interrupted, with a
    transcript note, so the app shows an honest state instead of a dead row."""
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        rows = con.execute(
            "SELECT sid,name,parent,tool,status,cwd,worktree,cc_session,"
            "last_user,last_at,output_json FROM subsessions").fetchall()
        interrupted = [r[0] for r in rows if r[4] == "running"]
        if interrupted:
            con.executemany("UPDATE subsessions SET status='interrupted' WHERE sid=?",
                            [(sid,) for sid in interrupted])
            con.commit()
        con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("subsessions_load_failed",
                   error=type(e).__name__, error_message=str(e)[:160])
        return
    loaded = 0
    for (sid, name, parent, tool, status, cwd, worktree, cc_session,
         last_user, last_at, output_json) in rows:
        try:
            output = [(k, v) for k, v in json.loads(output_json or "[]")]
        except Exception:  # noqa: BLE001
            output = []
        if status == "running":
            status = "interrupted"
            output.append(("text", "\n\n_(bridge 重啟,行程已中斷,可追問續跑)_\n"))
        SUBSESSIONS[sid] = {
            "name": name, "parent": parent, "tool": tool, "status": status,
            "cwd": cwd, "worktree": worktree, "cc_session": cc_session,
            "last_user": last_user, "lastAt": last_at, "proc": None,
            "output": output,
        }
        loaded += 1
    if loaded:
        _log_event("subsessions_loaded", count=loaded,
                   interrupted=len(interrupted))


_WORK_ORDER_PREFIX = {
    "xcash": "XW",
    "pantianqing": "PT",
    "shuijing": "SJ",
    "yuanfang": "YF",
}

_PROVIDER_ALIASES = {
    "codex": "codex",
    "cx": "codex",
    "codex-app": "codex",
    "codex_app": "codex",
    "claude": "claude_code",
    "claude-code": "claude_code",
    "claude_code": "claude_code",
    "cc": "claude_code",
}


def _normalise_provider(raw: str | None) -> str:
    key = (raw or "codex").strip().lower()
    provider = _PROVIDER_ALIASES.get(key)
    if not provider:
        raise HTTPException(status_code=400, detail="provider must be codex/cx or claude_code/cc")
    return provider


def _new_work_order(parent_persona: str, task_code: str = "", subtask_code: str = "") -> str:
    """Work order v2: AGENT-TASK-SUBTASK-YYYYMMDD-ID4

    AGENT    : persona prefix (XW/PT/SJ/YF), same as v1.
    TASK     : project/task code, shared across every delegation under the
               same initiative (e.g. POCKETCONN) so `grep`/filter by prefix
               finds the whole thread of work, including retries.
    SUBTASK  : this specific delegation's concrete deliverable (e.g.
               APPLELOGIN). Different subtasks under the same task share the
               TASK segment but not the SUBTASK segment.
    YYYYMMDD : full 8-digit date (v1 only had MMDD, which collides across
               years — fixed here).
    ID4      : 4 hex chars, collision guard.

    Falls back to a generic TASK/SUBTASK of "GEN" if the caller doesn't supply
    one (keeps the endpoint usable without breaking older callers), but new
    callers should always pass both — see docs/DELEGATION_CONTROL_PLANE.md.
    """
    prefix = _WORK_ORDER_PREFIX.get(parent_persona, "HW")
    day = datetime.now().astimezone().strftime("%Y%m%d")
    task = _work_order_segment(task_code, fallback="GEN", max_len=16)
    subtask = _work_order_segment(subtask_code, fallback="TASK", max_len=20)
    return f"{prefix}-{task}-{subtask}-{day}-{secrets.token_hex(2).upper()}"


def _work_order_segment(text: str, fallback: str, max_len: int) -> str:
    """Slugify a work-order TASK/SUBTASK segment: uppercase alnum only, no
    separators (the segment boundaries are the dashes between fields, so an
    embedded dash would silently shift field parsing for anyone splitting on
    '-')."""
    slug = re.sub(r"[^A-Za-z0-9]+", "", (text or "")).upper()
    return (slug or fallback)[:max_len]


def _delegation_display_title(row: dict) -> str:
    wo = row.get("work_order") or "WORK"
    title = (row.get("title") or row.get("objective") or "").strip()
    return f"{wo} - {title[:80]}" if title else wo


def _safe_session_slug(text: str, fallback: str = "task") -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", (text or "").lower()).strip("-")
    return (slug or fallback)[:48]


def _normalise_workdir(raw: str | None, *, create: bool = False) -> str:
    home = os.path.realpath(os.path.expanduser("~"))
    wd = os.path.realpath(os.path.expanduser(raw or HOME_ROOT))
    if not (wd == home or wd.startswith(home + os.sep)):
        raise HTTPException(status_code=400, detail="cwd must be under home")
    if wd == home:
        raise HTTPException(status_code=400, detail="pick a sub-folder, not your home directory")
    if create:
        try:
            os.makedirs(wd, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"cannot create cwd: {e}")
    elif not os.path.isdir(wd):
        raise HTTPException(status_code=400, detail="cwd does not exist")
    return wd


def _delegation_prompt(work_order: str, parent_persona: str, title: str,
                       objective: str, cwd: str, body: dict) -> str:
    lines = [
        f"[工號 {work_order}] {title}",
        "",
        "你是由 Hermes delegation control plane 派出的開發子程序。",
        f"- 父人格: {parent_persona}",
        f"- 工作目錄: {cwd}",
        f"- 目標: {objective}",
        "",
        "運作規則:",
        "- 每次回覆第一行保留工號，方便 Telegram、Pocket、官方 app 三邊對照。",
        "- 先提出可驗收計畫，再實作；不要改無關檔案。",
        "- 若涉及 production 寫入、正式通知、正式發文或真實使用者狀態變更，先停下等放行。",
        "- 完成時回報修改檔案、驗證命令與輸出、殘餘風險、下一步。",
        f"- 完成或到達里程碑時,執行 `studio-delegate report {work_order} \"<成果摘要>\" --status done`"
        "(進度回報用 --status running)把結果回流給派工方;此指令已在 PATH。",
    ]
    for label, key in (("規格文件", "spec_path"), ("限制", "constraints"),
                       ("驗收方式", "acceptance"), ("交接資訊", "handoff")):
        val = (body.get(key) or "").strip() if isinstance(body.get(key), str) else body.get(key)
        if val:
            lines.append(f"- {label}: {val}")
    return "\n".join(lines).strip()


def _delegation_takeover(row: dict) -> dict:
    provider = row.get("provider") or ""
    if provider == "codex":
        thread_id = row.get("codex_thread_id") or row.get("provider_session_id") or ""
        return {
            "pocket": {
                "surface": "bridge",
                "session_id": f"codex:{thread_id}" if thread_id else "",
                "input_endpoint": f"/codexsessions/{thread_id}/input" if thread_id else "",
                "stream_endpoint": f"/codexsessions/{thread_id}/stream" if thread_id else "",
                "history_endpoint": f"/codexsessions/{thread_id}/history" if thread_id else "",
                "status_endpoint": f"/codexsessions/{thread_id}/status" if thread_id else "",
                "interrupt_endpoint": f"/codexsessions/{thread_id}/interrupt" if thread_id else "",
            },
            "official": {
                "surface": "codex_app_server_thread",
                "thread_id": thread_id,
                "title": _delegation_display_title(row),
                "resume_hint": "Codex official surfaces should resume the native thread id/title created by codex app-server.",
            },
        }
    if provider == "claude_code":
        name = row.get("cc_session_name") or row.get("provider_session_id") or ""
        return {
            "pocket": {
                "surface": "bridge",
                "session_id": f"claude_code:{name}" if name else "",
                "input_endpoint": f"/ccsessions/{name}/input" if name else "",
                "stream_endpoint": f"/ccsessions/{name}/stream" if name else "",
                "history_endpoint": f"/ccsessions/{name}/history" if name else "",
                "status_endpoint": f"/ccsessions/{name}/status" if name else "",
                "interrupt_endpoint": f"/ccsessions/{name}/interrupt" if name else "",
                "key_endpoint": f"/ccsessions/{name}/key" if name else "",
            },
            "official": {
                "surface": "claude_code_remote_control",
                "session_name": name,
                "workdir": row.get("cwd") or "",
                "resume_hint": "Open/attach the same Claude Code remote-control session name or ccsess tmux session.",
            },
        }
    return {"pocket": {}, "official": {}}


def _delegation_public(row, runtime_status: str | None = None) -> dict:
    d = dict(row)
    try:
        meta = json.loads(d.get("meta") or "{}")
    except Exception:  # noqa: BLE001
        meta = {}
    d["display_title"] = _delegation_display_title(d)
    d["status"] = runtime_status or d.get("status") or "created"
    d["meta"] = meta
    d["takeover"] = _delegation_takeover(d)
    return d


def _delegation_rows(limit: int = 50, parent_persona: str = "", status: str = "",
                      task_code: str = "") -> list:
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        where, args = [], []
        if parent_persona:
            where.append("parent_persona=?")
            args.append(parent_persona)
        if status:
            where.append("status=?")
            args.append(status)
        if task_code:
            where.append("task_code=?")
            args.append(task_code.strip().upper())
        sql = "SELECT * FROM delegations"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(max(1, min(limit, 200)))
        rows = con.execute(sql, args).fetchall()
        con.close()
        return rows
    except Exception:  # noqa: BLE001
        return []


def _delegation_get(delegation_id: str):
    import sqlite3
    con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM delegations WHERE id=? OR work_order=?",
                      (delegation_id, delegation_id)).fetchone()
    con.close()
    return row


def _delegation_insert(row: dict) -> None:
    import sqlite3
    con = sqlite3.connect(CANON_DB, timeout=30)
    con.execute("""INSERT INTO delegations
        (id, work_order, parent_persona, parent_session, created_via, provider,
         title, objective, cwd, status, provider_session_id, codex_thread_id,
         cc_session_name, created_at, updated_at, last_error, meta,
         task_code, subtask_code)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (row.get("id"), row.get("work_order"), row.get("parent_persona"),
         row.get("parent_session"), row.get("created_via"), row.get("provider"),
         row.get("title"), row.get("objective"), row.get("cwd"), row.get("status"),
         row.get("provider_session_id"), row.get("codex_thread_id"),
         row.get("cc_session_name"), row.get("created_at"), row.get("updated_at"),
         row.get("last_error"), json.dumps(row.get("meta") or {}, ensure_ascii=False),
         row.get("task_code"), row.get("subtask_code")))
    con.commit()
    con.close()


def _delegation_update(delegation_id: str, **fields) -> None:
    if not fields:
        return
    import sqlite3
    allowed = {"status", "updated_at", "last_error", "provider_session_id",
               "codex_thread_id", "cc_session_name", "meta"}
    sets, args = [], []
    for key, val in fields.items():
        if key not in allowed:
            continue
        sets.append(f"{key}=?")
        if key == "meta" and not isinstance(val, str):
            val = json.dumps(val or {}, ensure_ascii=False)
        args.append(val)
    if not sets:
        return
    args.append(delegation_id)
    con = sqlite3.connect(CANON_DB, timeout=30)
    con.execute(f"UPDATE delegations SET {', '.join(sets)} WHERE id=?", args)
    con.commit()
    con.close()


async def _delegation_runtime_status(row) -> str:
    d = dict(row)
    provider = d.get("provider") or ""
    if provider == "codex":
        tid = d.get("codex_thread_id") or d.get("provider_session_id") or ""
        if tid and CODEX_APP.pending_approval_for_thread(tid):
            return "waiting_approval"
        if tid and CODEX_APP.is_active(tid):
            return "running"
        if d.get("status") in ("failed", "archived"):
            return d.get("status")
        return "idle"
    if provider == "claude_code":
        name = d.get("cc_session_name") or d.get("provider_session_id") or ""
        if name:
            st, _prompt = await _v2_cc_state(name)
            return st
    return d.get("status") or "created"


async def _delegation_app_sessions() -> list:
    out = []
    for row in _delegation_rows(limit=50):
        st = await _delegation_runtime_status(row)
        d = _delegation_public(row, st)
        out.append({
            "id": f"delegation:{d['id']}",
            "type": "delegation",
            "name": d["display_title"],
            "parent": d.get("parent_persona"),
            "tool": d.get("provider"),
            "preview": (d.get("objective") or "")[:160],
            "lastAt": d.get("updated_at"),
            "status": d.get("status"),
            "work_order": d.get("work_order"),
            "provider_session_id": d.get("provider_session_id"),
            "takeover": d.get("takeover"),
        })
    return out


async def _delegation_v2_sessions() -> list:
    out = []
    for row in _delegation_rows(limit=50):
        st = await _delegation_runtime_status(row)
        d = _delegation_public(row, st)
        caps = ["input", "attachments", "replay", "follow"]
        if d.get("provider") in ("codex", "claude_code"):
            caps.append("interrupt")
        if d.get("provider") in ("codex", "claude_code") and st == "waiting_approval":
            caps.append("approve")
        approval = None
        if d.get("provider") == "codex":
            tid = d.get("codex_thread_id") or d.get("provider_session_id") or ""
            approval = CODEX_APP._approval_public(CODEX_APP.pending_approval_for_thread(tid))
        out.append({
            "id": f"delegation:{d['id']}",
            "provider": d.get("provider"),
            "title": d["display_title"],
            "subtitle": f"{d.get('parent_persona')} · {d.get('cwd')}",
            "status": d.get("status"),
            "last_event_at": d.get("updated_at"),
            "capabilities": caps,
            "meta": {"delegation": d, "work_order": d.get("work_order"),
                     "takeover": d.get("takeover"), "approval": approval},
        })
    return out


def _delegated_codex_thread_ids() -> set:
    return {
        (dict(r).get("codex_thread_id") or dict(r).get("provider_session_id"))
        for r in _delegation_rows(limit=200)
        if dict(r).get("provider") == "codex"
    } - {""}


# ─── 委派生命週期回流(M1)+ CC↔CX 互調結果注回(M2)──────────────────────
# delegations 過去只存不回流:parent_session 存了沒用、完成無偵測,派工的人格
# 永遠不知道結果。現在:父是人格 → 寫 report_events 進該人格對話(卡片流本來
# 就會併入,Pocket 聊天串直接看到);父是另一個 delegation(CC↔CX 互調)→ 把
# 完成通知注回父 session 喚醒父代理。done/failed 另發推播。

def _delegation_meta(d: dict) -> dict:
    try:
        m = d.get("meta")
        return json.loads(m) if isinstance(m, str) else (m or {})
    except Exception:  # noqa: BLE001
        return {}


async def _delegation_notify(d: dict, event: str, summary: str = "") -> None:
    meta = _delegation_meta(d)
    wo = d.get("work_order") or d.get("id") or ""
    title = d.get("title") or ""
    status_txt = {"created": "已建立", "done": "已完成", "failed": "失敗",
                  "report": "進度回報"}.get(event, event)
    parent_dlg = str(meta.get("parent_delegation") or "")
    if parent_dlg:
        # CC↔CX 互調:結果注回父 delegation 的 session(喚醒父代理繼續),
        # 不再往人格灌(避免雙份)。
        prow = _delegation_get(parent_dlg)
        if prow:
            p = dict(prow)
            note = f"[子任務 {wo} {status_txt}] " + (summary.strip()[:800] or title)
            try:
                if p.get("provider") == "claude_code" and (p.get("cc_session_name") or ""):
                    await _cc_paste_text(p["cc_session_name"], note)
                elif p.get("provider") == "codex":
                    ptid = p.get("codex_thread_id") or p.get("provider_session_id") or ""
                    if ptid:
                        await CODEX_APP.start_turn(
                            ptid, await _codex_input_items(note, []),
                            client_id=f"dlg-notify-{d.get('id','')[:12]}-{event}")
            except Exception as e:  # noqa: BLE001
                _log_event("delegation_parent_notify_failed",
                           delegation=d.get("id"), error=str(e)[:160])
        return
    parent = d.get("parent_persona") or ""
    if parent in PERSONAS:
        lines = [f"[工號 {wo}] {title}", f"狀態:{status_txt}"]
        if summary.strip():
            lines += ["", summary.strip()[:2000]]
        tk = _delegation_takeover(d)
        sid = (tk.get("pocket") or {}).get("session_id") or ""
        if sid:
            lines += ["", f"接手:{sid}"]
        _report_upsert(parent, {
            "label": "委派任務", "name": f"dlg-{str(d.get('id') or '')[:12]}",
            "content": "\n".join(lines), "ts": time.time(),
            "external_source": "delegation",
            "external_id": f"dlg:{d.get('id')}:{event}:{int(time.time())}",
        })
    if event in ("done", "failed"):
        try:
            await push_notify(("✅ " if event == "done" else "❌ ") + f"[{wo}] {title[:40]}",
                              (summary.strip() or status_txt)[:160],
                              {"kind": "delegation_done",
                               "delegation_id": str(d.get("id") or "")})
        except Exception:  # noqa: BLE001
            pass


async def _delegation_codex_completed(tid: str, failed: bool, err_msg: str = "") -> None:
    """codex turn/completed → 對應委派 running→idle/failed 一次性回流。"""
    for row in _delegation_rows(limit=200):
        d = dict(row)
        if d.get("provider") != "codex":
            continue
        if (d.get("codex_thread_id") or d.get("provider_session_id") or "") != tid:
            continue
        if (d.get("status") or "") != "running":
            return                       # 只在 running→完成 的轉換回流一次
        new_status = "failed" if failed else "idle"
        _delegation_update(d["id"], status=new_status, updated_at=time.time(),
                           last_error=(err_msg[:300] if failed else ""))
        d["status"] = new_status
        await _delegation_notify(d, "failed" if failed else "done",
                                 summary=(err_msg if failed else ""))
        return


_DLG_CC_IDLE: dict = {}    # delegation id -> 連續 idle tick 數(debounce)


async def _delegation_cc_watcher():
    """15s 巡 created/running 的 CC 委派:busy→標 running;連兩 tick idle →
    判完成回流;tmux 不在 → failed。codex 靠 turn/completed 事件,不用巡。"""
    while True:
        await asyncio.sleep(15.0)
        try:
            for row in _delegation_rows(limit=100):
                d = dict(row)
                if d.get("provider") != "claude_code":
                    continue
                if (d.get("status") or "") not in ("created", "running"):
                    continue
                name = d.get("cc_session_name") or d.get("provider_session_id") or ""
                if not name:
                    continue
                st, _p = await _v2_cc_state(name)
                if st in ("running", "waiting_approval"):
                    _DLG_CC_IDLE.pop(d["id"], None)
                    if d.get("status") == "created":
                        _delegation_update(d["id"], status="running",
                                           updated_at=time.time())
                    continue
                if st == "failed":
                    _delegation_update(d["id"], status="failed",
                                       updated_at=time.time(),
                                       last_error="cc session not running")
                    d["status"] = "failed"
                    await _delegation_notify(d, "failed",
                                             summary="CC session 掛了(tmux 不在)")
                    _DLG_CC_IDLE.pop(d["id"], None)
                    continue
                n = _DLG_CC_IDLE.get(d["id"], 0) + 1
                _DLG_CC_IDLE[d["id"]] = n
                if n >= 2 and d.get("status") == "running":
                    _delegation_update(d["id"], status="idle",
                                       updated_at=time.time())
                    d["status"] = "idle"
                    await _delegation_notify(d, "done")
                    _DLG_CC_IDLE.pop(d["id"], None)
        except Exception as e:  # noqa: BLE001
            _log_event("delegation_cc_watch_error", error=str(e)[:160])


def _report_id(persona: str, name: str, sid: str, ts) -> str:
    raw = f"cron:{persona}:{name}:{sid}:{ts}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:24]


def _report_upsert(session: str, report: dict) -> str:
    import sqlite3
    rid = report.get("id") or _report_id(session, report.get("name") or "",
                                         report.get("session_id") or "",
                                         report.get("ts") or "")
    external_id = report.get("external_id") or rid
    content = report.get("content") or ""
    ts = float(report.get("ts") or time.time())
    label = report.get("label") or ""
    name = report.get("name") or ""
    con = sqlite3.connect(CANON_DB, timeout=30)
    existing = con.execute(
        "SELECT label,name,content,ts,external_id FROM report_events WHERE id=?",
        (rid,)).fetchone()
    if existing and existing == (label, name, content, ts, external_id):
        con.close()
        return ""
    con.execute(
        "INSERT OR REPLACE INTO report_events"
        "(id,session,label,name,content,ts,external_source,external_id,ingested_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (rid, session, label, name, content, ts,
         report.get("external_source") or "hermes-cron", external_id, time.time()))
    con.commit()
    con.close()
    # Sync engine P1:cron 晨報寫入點鏡射進 event_log(雙寫過渡)。形狀走
    # _report_msg_shape = app 在 /app/v1/messages 看到的同一種報告訊息;
    # 改稿(同 rid 新內容)→ 新鍵 → 追加新事件,同 message id 覆蓋。
    _event_mirror_messages(session, [_report_msg_shape(
        {"id": rid, "label": label, "content": content, "ts": ts})])
    return rid


def _report_events(session: str, limit: int = 20, newest_first: bool = False):
    import sqlite3
    order = "DESC" if newest_first else "ASC"
    try:
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        rows = con.execute(
            f"SELECT id,label,name,content,ts,external_source,external_id "
            f"FROM report_events WHERE session=? ORDER BY ts {order} LIMIT ?",
            (session, limit)).fetchall()
        con.close()
    except Exception:  # noqa: BLE001
        return []
    return [{
        "id": r[0], "label": r[1], "name": r[2], "content": r[3],
        "ts": r[4], "external_source": r[5], "external_id": r[6],
    } for r in rows]


def _report_msg_shape(r: dict) -> dict:
    """report_events 列 → app-shape 訊息。_report_messages(v1 讀取)與
    _report_upsert 的 event_log 鏡射(P1)共用,兩邊 payload/去重鍵一致。"""
    return {
        "id": f"rep-{r['id']}", "role": "assistant",
        "content": f"📰 **{r['label']}**\n\n{r['content']}",
        "attachments": [], "ts": r["ts"], "status": "done", "source": "report",
    }


def _report_messages(session: str, limit: int = 100):
    """最新 limit 筆(newest_first)——舊版 ASC LIMIT 拿的是「史上最舊 limit 筆」,
    report_events 一超過 limit,新報告就永遠進不了 preview/對話合併
    (2026-07-15 修:人格列表與對話凍結在舊訊息的根因之一)。
    呼叫端(preview 合併/兩處對話合併)都會事後按 ts 重排,順序不影響。"""
    return [_report_msg_shape(r)
            for r in _report_events(session, limit, newest_first=True)]


def _clip_text(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...[截斷，完整內容保存在 report_events]"


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:  # noqa: BLE001
        return ""


# ───────────────────────── APNs push (M23) ─────────────────────────────────
# Token-based (.p8) auth. The key lives UNDER Hermes management:
#   ~/apps/hermes-agent/home/credentials/AuthKey_86FF9D976T.p8  (chmod 600)
# See docs/HANDOFF_CREDENTIALS.md for the rotation procedure / inventory.
APNS_KEY_PATH = os.path.expanduser(
    "~/apps/hermes-agent/home/credentials/AuthKey_86FF9D976T.p8")
APNS_KEY_ID = "86FF9D976T"
APNS_TEAM_ID = "4F8B93R3SH"
# 正式 app 是 Pocket kernel(com.pocketagent.kernel,見 ship-kernel.sh)。apns-topic
# 必須對上 device token 所屬 app,否則 APNs 回 400 BadTopic / DeviceTokenNotForTopic
# → 推播全滅(2026-07 之前寫成舊 SUN 的 com.pocketagent.ios,推播在正式版 100% 死)。
# token-based(.p8)是 team 級,對同 team 任何 bundle 都有效,只要 topic 對。
APNS_BUNDLE_ID = "com.pocketagent.kernel"
APNS_HOST = "https://api.push.apple.com"   # production (TestFlight + App Store)
_apns_jwt_cache: list = [None, 0.0]        # [token, issued_at]


def _apns_jwt() -> str:
    """ES256 JWT for APNs, cached ~50 min (Apple requires < 60 min)."""
    import jwt as pyjwt
    now = time.time()
    if _apns_jwt_cache[0] and now - _apns_jwt_cache[1] < 3000:
        return _apns_jwt_cache[0]
    with open(APNS_KEY_PATH) as f:
        key = f.read()
    tok = pyjwt.encode({"iss": APNS_TEAM_ID, "iat": int(now)}, key,
                       algorithm="ES256", headers={"kid": APNS_KEY_ID})
    _apns_jwt_cache[0], _apns_jwt_cache[1] = tok, now
    return tok


def _devices() -> list:
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        rows = con.execute("SELECT token FROM devices").fetchall()
        con.close()
        return [r[0] for r in rows]
    except Exception as e:  # noqa: BLE001
        # An unreadable devices table means "push notifications silently off" —
        # log it so the failure is diagnosable (issue #7).
        _log_event("devices_read_failed", error=type(e).__name__,
                   error_message=str(e)[:160])
        return []


def _device_add(token: str, platform: str = "ios") -> None:
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB)
        con.execute("INSERT OR REPLACE INTO devices(token,platform,created_at) "
                    "VALUES(?,?,?)", (token, platform, time.time()))
        con.commit()
        con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("device_add_failed", platform=platform,
                   error=type(e).__name__, error_message=str(e)[:160])


def _device_remove(token: str) -> None:
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB)
        con.execute("DELETE FROM devices WHERE token=?", (token,))
        con.commit()
        con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("device_remove_failed", error=type(e).__name__,
                   error_message=str(e)[:160])


async def _apns_send(token: str, title: str, body: str, data: dict | None = None,
                     category: str | None = None, thread_id: str | None = None,
                     content_available: bool = False):
    import httpx
    headers = {"authorization": f"bearer {_apns_jwt()}",
               "apns-topic": APNS_BUNDLE_ID,
               "apns-push-type": "alert", "apns-priority": "10"}
    aps = {"alert": {"title": title, "body": body}, "sound": "default"}
    if content_available:
        # 讓通知本體也能喚醒 app 在背景拉新訊息(通知本身已帶 title/body,
        # 收到即最新;app 若在背景可順手 refresh 對話)。
        aps["content-available"] = 1
    if category:
        # 批次 3 斷點①:category 才會讓 iOS/手錶顯示 UNNotificationAction
        # 動作鈕(app 端已註冊同名 category)。
        aps["category"] = category
    if thread_id:
        aps["thread-id"] = thread_id
    payload = {"aps": aps}
    if data:
        payload.update(data)
    async with httpx.AsyncClient(http2=True, timeout=10) as client:
        r = await client.post(f"{APNS_HOST}/3/device/{token}",
                              headers=headers, json=payload)
        return r.status_code, r.text


async def push_notify(title: str, body: str, data: dict | None = None,
                      category: str | None = None,
                      thread_id: str | None = None,
                      content_available: bool = False) -> dict:
    """Fan a push to every registered device; prune dead tokens (410/BadToken).

    Returns {sent, total, failures:[{code,detail}]}. **不再吞錯** —— 非 200/410 的
    APNs 回應(400 BadTopic、403 bad key、429…)以前被靜默吃掉,推播死了好幾週都
    查不到。現在一律 _log_event,`/push/test` 也回傳真實 code。"""
    toks = _devices()
    sent = 0
    failures: list[dict] = []
    for tok in toks:
        try:
            code, text = await _apns_send(tok, title, body, data,
                                          category=category, thread_id=thread_id,
                                          content_available=content_available)
            if code == 200:
                sent += 1
            elif (code == 410 or "BadDeviceToken" in text or "Unregistered" in text
                  or "DeviceTokenNotForTopic" in text):
                # DeviceTokenNotForTopic = 舊 SUN(.ios)遺留 token,對 .kernel 永遠不合 → 清掉。
                _device_remove(tok)
                failures.append({"code": code, "detail": "wrong-app/unregistered→pruned",
                                 "token": tok[:8]})
            else:
                failures.append({"code": code, "detail": (text or "")[:160],
                                 "token": tok[:8]})
        except Exception as e:  # noqa: BLE001
            failures.append({"code": "exc", "detail": str(e)[:160], "token": tok[:8]})
    if failures:
        _log_event("push_notify_failed", title=title[:48], sent=sent,
                   total=len(toks), failures=str(failures)[:400])
    return {"sent": sent, "total": len(toks), "failures": failures}


# Scarf 契約遷移 Stage 1b(見 pocketagent/docs/SCARF_CONTRACT_MIGRATION_PLAN.md)。
# ⚠️ GATE:本分支只在「接受新 category 的 app(Stage 1a,pocketagent PR)」已
#    上架/普及後才可 merge+deploy。先翻 producer 會讓舊 app 收不到動作鈕。app
#    側 1a 已雙接受 POCKET_/SCARF_ 兩個 category 與 pocket/scarf 兩巢,故翻新後
#    舊 app 仍靠 scarf 巢運作,新 app 走 pocket 巢。
_APNS_APPROVAL_CATEGORY = "POCKET_PENDING_PERMISSION"


def _approval_push(aid: str, title: str, body: str, session_id: str = ""):
    """審核推播(批次 3 斷點①):category 出動作鈕、payload 巢與 app 端約定對齊、
    thread-id 以 session 分串。Stage 1b 翻新:新 `pocket.{kind, approvalId,
    sessionId}` 巢 + category;**相容期保留** 舊 `scarf` 巢與更舊頂層 {kind, id},
    讓尚未更新的 app 仍可解析(app 側 pocket 巢優先)。fire-and-forget。"""
    _approval_nest = {"kind": "approval", "approvalId": aid,
                      "sessionId": session_id}
    data = {"kind": "approval", "id": aid,   # 最舊頂層鍵(相容期保留)
            "pocket": _approval_nest,        # 新巢(app 優先讀)
            "scarf": _approval_nest}         # 舊巢(相容期保留;Stage 1c 移除)
    task = asyncio.create_task(push_notify(
        f"🔐 {title}", body[:120], data,
        category=_APNS_APPROVAL_CATEGORY,
        thread_id=session_id or "approvals"))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


def _push_persona_reply(session: str, content: str) -> None:
    """P1-3 人格回訊推播:人格(assistant)在 canonical 落地一則『完成』的回覆時,推播
    把你叫回 app。標題=人格顯示名,body=清過卡片/步驟的預覽。payload `pocket.kind=
    message` + sessionId 供 app deep-link 進該人格對話。content-available 讓背景也能
    順手刷新。app 前景時由 willPresent 抑制橫幅(你正在看,不吵);背景時系統自動顯示。
    fire-and-forget;無執行中 event loop(純 sync 匯入期)則跳過。"""
    if session not in PERSONAS:
        return
    disp = PERSONAS[session][0]
    try:
        clean, _bodies = carddigest.extract_studio_cards(content or "")
    except Exception:  # noqa: BLE001
        clean = content or ""
    clean = re.sub(r"<details>.*?</details>", "", clean, flags=re.S).strip()
    body = (clean or "傳了一則訊息")[:140]
    # sessionId 用 deep-link wire 格式 hermes:{persona}(app 點通知直達該人格對話);
    # app 的 willPresent 會剝前綴比對「正在看哪條」以決定前景是否彈橫幅。
    data = {"kind": "message",
            "pocket": {"kind": "message", "sessionId": f"hermes:{session}"}}
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    t = loop.create_task(push_notify(disp, body, data,
                                     thread_id=session, content_available=True))
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)


_canon_init()
_accounts_init()
_subsessions_load()   # issue #5: rebuild /dispatch subs after restart
_personas_reload()    # G6: apply persona overrides/customs from canonical.db


async def _persona_content_stream(model: str, prompt: str):
    """Core persona turn → yields ('content', str) pieces, ('keepalive', None)
    during gaps, ('usage', {used,size}) once. Shared by /v1/chat/completions and
    /app/v1/messages so both stream identically (the latter also records the
    accumulated reply to the canonical store)."""
    if not prompt:
        yield ("content", "(沒有收到訊息)")
        return
    q: asyncio.Queue = asyncio.Queue()
    session = await POOL.get(model, home_for(model))

    async def pump():
        try:
            async for kind, val in session.prompt_stream(prompt):
                await q.put((kind, val))
        except Exception as e:  # noqa: BLE001
            await q.put(("error", str(e)))
        finally:
            await q.put(("end", None))

    pump_task = asyncio.create_task(pump())
    pump_stopped = False
    got_text = False
    completed = False
    thought_buf: list[str] = []
    steps: list[dict] = []          # 工具步驟 — 不進正文,收尾摺疊附錄

    def flush_thought():
        if thought_buf:
            t = "".join(thought_buf).strip()
            thought_buf.clear()
            if t:
                return f"\n<details><summary>💭 思考</summary>\n\n{t}\n\n</details>\n\n"
        return None

    def flush_steps():
        """收尾一次性附上摺疊的步驟清單(預設看不到,點開才展開)——
        對話正文只留人話;canonical/歷史也存這個形狀。"""
        if not steps:
            return None
        lines = []
        for i, s in enumerate(steps, 1):
            head = f"{i}. **{s['name']}**" + (f" `{s['cmd']}`" if s["cmd"] else "")
            if s.get("note"):
                head += f" — {s['note']}"
            lines.append(head)
            if s.get("result"):
                lines.append(f"\n   ```\n{s['result']}\n   ```\n")
        body = "\n".join(lines)[:6000]
        n = len(steps)
        steps.clear()
        return f"\n\n<details><summary>🔧 執行步驟 ({n})</summary>\n\n{body}\n\n</details>\n"

    import time as _t
    last_event = _t.monotonic()

    async def stop_pump(*, reset: bool) -> None:
        """Stop the task that owns ACPSession._lock and optionally retire ACP."""
        nonlocal pump_stopped
        if pump_stopped:
            return
        pump_stopped = True
        try:
            await asyncio.wait_for(session.cancel(), timeout=2.0)
        except Exception:
            pass
        if not pump_task.done():
            pump_task.cancel()
        try:
            await asyncio.wait_for(pump_task, timeout=2.0)
        except asyncio.CancelledError:
            pass
        except Exception:
            reset = True
        # A five-minute silent provider is not safe to reuse even when task
        # cancellation released the Python lock: its RPC turn may still run.
        if reset or session.is_busy():
            try:
                await session.reset()
            except Exception:
                pass

    try:
        while True:
            try:
                kind, val = await asyncio.wait_for(q.get(), timeout=SSE_KEEPALIVE_SECS)
                last_event = _t.monotonic()
            except asyncio.TimeoutError:
                if _t.monotonic() - last_event > PERSONA_STALL_LIMIT_SECS:
                    await stop_pump(reset=True)
                    yield ("content", "\n\n⚠️ 回合逾時(伺服器端 5 分鐘無回應),已中止。")
                    completed = True
                    break
                yield ("keepalive", None)
                continue
            if kind == "text":
                if not got_text:
                    ft = flush_thought()
                    if ft:
                        yield ("content", ft)
                got_text = True
                yield ("content", val)
            elif kind == "thought":
                thought_buf.append(val)
            elif kind == "tool_start":
                # 工具步驟不再內聯進正文(使用者回報:指令洗版、跑完消失又
                # 湧一批)。改走 status label(app 底部 working bar 原樣顯示
                # 「執行步驟 N:工具」),細節收進收尾的摺疊附錄。
                name = val.get("name", "tool")
                cmd = (val.get("cmd") or "").strip().splitlines()
                cmd1 = (cmd[0] if cmd else "")[:TOOL_CMD_MAX]
                steps.append({"name": name, "cmd": cmd1, "result": "", "note": ""})
                yield ("status", {"state": "running",
                                  "label": f"執行步驟 {len(steps)}:{name}"})
            elif kind == "tool_result":
                res = (val.get("text") or "").strip()
                if res and steps:
                    short = res[:400]
                    if len(res) > 400:
                        short += "\n…(截斷)"
                    steps[-1]["result"] = short
            elif kind == "perm":
                if steps:
                    steps[-1]["note"] = f"🔐 自動允許 {val}"
                else:
                    steps.append({"name": str(val), "cmd": "", "result": "",
                                  "note": "🔐 自動允許"})
            elif kind == "status":
                yield ("status", val)
            elif kind == "usage":
                yield ("usage", val)
            elif kind == "error":
                if not got_text:
                    try:
                        yield ("content", await run_hermes(model, prompt))
                    except Exception as e2:  # noqa: BLE001
                        yield ("content", f"⚠️ {e2}")
                else:
                    yield ("content", f"\n\n⚠️ 串流中斷:{val}")
            else:
                completed = True
                break
        ft = flush_thought()
        if ft:
            yield ("content", ft)
        fs = flush_steps()
        if fs:
            yield ("content", fs)
    finally:
        if not completed:
            await stop_pump(reset=False)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    _check_auth(request)
    body = await request.json()
    model = body.get("model", "xcash")
    stream = bool(body.get("stream", False))
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())

    # Sub-session (dispatched CC/Codex) — replay + follow its work transcript.
    if model in SUBSESSIONS:
        sub = SUBSESSIONS[model]

        # Follow-up turn: a new, non-empty user message resumes the sub-agent.
        # Stream from the current tail so we don't re-replay the whole transcript.
        new_prompt = _last_user_message(body.get("messages", []))
        start_idx = 0
        if new_prompt and new_prompt != sub.get("last_user") and sub.get("status") != "running":
            start_idx = len(sub["output"])
            sub["last_user"] = new_prompt
            sub["status"] = "running"
            sub["output"].append(("text", f"\n\n---\n**追問:** {new_prompt}\n\n"))
            sub["lastAt"] = time.time()
            asyncio.create_task(_run_resume(model, new_prompt))

        def schunk(delta, finish=None):
            payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                       "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        async def sgen():
            yield schunk({"role": "assistant", "content": ""})
            idx = start_idx
            quiet = 0.0
            while True:
                while idx < len(sub["output"]):
                    kind, val = sub["output"][idx]
                    idx += 1
                    c = _fmt_item(kind, val)
                    if c:
                        yield schunk({"content": c})
                        quiet = 0.0
                if sub.get("status") != "running" and idx >= len(sub["output"]):
                    break
                await asyncio.sleep(0.4)
                quiet += 0.4
                if quiet >= SSE_KEEPALIVE_SECS:
                    quiet = 0.0
                    yield ": keepalive\n\n"
            yield schunk({}, finish="stop")
            yield "data: [DONE]\n\n"

        if stream:
            return StreamingResponse(sgen(), media_type="text/event-stream")
        text = "".join(v for k, v in sub["output"] if k == "text")
        return JSONResponse({"id": cid, "object": "chat.completion", "created": created,
                             "model": model,
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                          "finish_reason": "stop"}]})

    if model not in PERSONAS:
        model = "xcash"
    prompt = await _resolve_persona_prompt(body.get("messages", []))

    if stream:
        # Live streaming over a warm ACP session: a background pump feeds text
        # chunks onto a queue; the SSE generator drains it with a 2s timeout,
        # emitting keepalive comments during gaps (e.g. tool reasoning before
        # the first token) so the socket never goes idle long enough to drop.
        # Falls back to cold `hermes -z` only if ACP yields nothing.
        async def gen():
            def chunk(delta, finish=None):
                payload = {"id": cid, "object": "chat.completion.chunk",
                           "created": created, "model": model,
                           "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

            yield chunk({"role": "assistant", "content": ""})  # open the bubble
            last_usage = None
            async for k, v in _persona_content_stream(model, prompt):
                if k == "content":
                    yield chunk({"content": v})
                elif k == "keepalive":
                    yield ": keepalive\n\n"
                elif k == "usage":
                    last_usage = v
            final = {"index": 0, "delta": {}, "finish_reason": "stop"}
            payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                       "model": model, "choices": [final]}
            if last_usage and last_usage.get("size"):
                payload["usage"] = {"context_used": last_usage.get("used"),
                                    "context_size": last_usage.get("size")}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    try:
        content = "(沒有收到訊息)" if not prompt else await acp_full(model, prompt)
    except Exception:
        content = await run_hermes(model, prompt)
    return JSONResponse({
        "id": cid, "object": "chat.completion", "created": created, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })


CLAUDE_BIN = "/Users/xcash/.local/bin/claude"
# 用能讀「新版 thread」的 codex 當 app-server。VS Code 用 codex 0.142 建 thread,
# 舊的 standalone 0.137(~/.local/bin/codex)一讀其 full turns(thread/turns/list
# itemsView=full)就 crash → UPSTREAM_FAILED「codex app-server stopped」,整條 stdio
# 卡死,app 端該 session 空白且送不出。優先挑 Codex.app 內建的 0.142(VS Code 同款、
# 共用 ~/.codex 登入),對不上再退回 standalone。CODEX_BIN 環境變數可覆蓋。
def _resolve_codex_bin() -> str:
    # 2026-07-10 事故:ChatGPT.app 更新把 Codex Desktop 併入、/Applications/Codex.app
    # 整個消失 → 舊首選路徑失效,fallback 到 0.137 又是「讀新 thread 會 crash」地雷,
    # 手機 CX 全空數小時且無錯誤日誌。候選序補上 ChatGPT.app 的新家,且 spawn 時
    # 每次重新解析(見 _ensure_started_locked),桌面 app 更新不再需要重啟 bridge。
    for c in (os.environ.get("CODEX_BIN"),
              "/Applications/Codex.app/Contents/Resources/codex",
              "/Applications/ChatGPT.app/Contents/Resources/codex",
              os.path.expanduser("~/.local/bin/codex")):
        if c and os.path.exists(c):
            return c
    return "/Users/xcash/.local/bin/codex"


CODEX_BIN = _resolve_codex_bin()   # 僅供顯示/預設;spawn 走 _resolve_codex_bin()


class CodexAppServerError(RuntimeError):
    def __init__(self, message: str, code=None):
        super().__init__(message)
        self.code = code


class CodexAppServerClient:
    """Small JSON-RPC client for `codex app-server --stdio`.

    The bridge keeps one app-server connection warm and exposes Codex threads as
    PocketAgent-controllable sessions. This is the correct sync surface for
    Codex App/CLI threads; `codex exec` remains only a fallback path.
    """

    def __init__(self):
        self.proc = None
        self._lock = asyncio.Lock()
        self._next_id = 1
        self._pending = {}
        self._reader_task = None
        self._stderr_task = None
        self.thread_events = collections.defaultdict(list)
        self.thread_event_generations = collections.defaultdict(int)
        self.active_turns = {}
        self.last_event_at = {}
        self.thread_errors = {}
        self.loaded_threads = set()
        self.remote_status = None
        self._streamed_item_ids = set()
        self.pending_approvals = {}
        self.pending_approvals_by_thread = collections.defaultdict(dict)
        # wave 2: live token usage per thread (thread/tokenUsage/updated) —
        # thread/list reports tokenUsage: null, so this is the only source.
        self.token_usage = {}

    async def call(self, method: str, params: dict | None = None, timeout: float = 30.0):
        async with self._lock:
            await self._ensure_started_locked()
            rid = self._next_id
            self._next_id += 1
            fut = asyncio.get_running_loop().create_future()
            self._pending[rid] = fut
            await self._write_locked({"jsonrpc": "2.0", "id": rid,
                                      "method": method, "params": params or {}})
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(rid, None)
            raise CodexAppServerError(f"{method} timed out") from e

    async def notify(self, method: str, params: dict | None = None):
        async with self._lock:
            await self._ensure_started_locked()
            msg = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                msg["params"] = params
            await self._write_locked(msg)

    async def _ensure_started_locked(self):
        if self.proc and self.proc.returncode is None:
            return
        self._pending.clear()
        self.pending_approvals.clear()
        self.pending_approvals_by_thread.clear()
        self._expire_stale_codex_approvals()
        codex_bin = _resolve_codex_bin()   # 每次 spawn 重新解析:桌面 app 更新後路徑會變
        self.spawned_bin = codex_bin
        try:
            self.proc = await asyncio.create_subprocess_exec(
                codex_bin, "app-server", "--stdio",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
                cwd=HOME_ROOT,
                # StreamReader 單行上限。codex app-server 把每個 JSON-RPC 回應當「一行」
                # 送;thread/turns/list itemsView=full 若含 computer-use 截圖(base64)可能
                # 單行破 8MB → asyncio 讀取器丟 LimitOverrunError(「Separator is not found,
                # and chunk exceed the limit」)→ reader task 死 → app-server「stopped」→ 整條
                # codex 卡死(XCash 就是這樣)。放大到 128MB 吃得下含圖的大回應。
                limit=128 * 1024 * 1024,
            )
        except (FileNotFoundError, PermissionError) as e:
            _log_event("codex_spawn_failed", bin=codex_bin, error=type(e).__name__)
            raise CodexAppServerError(f"codex binary unavailable: {codex_bin}") from e
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        init = await self._call_started_locked(
            "initialize",
            {
                "clientInfo": {
                    "name": "pocketagent-bridge",
                    "title": "PocketAgent Bridge",
                    "version": "0.1",
                },
                "capabilities": {"experimentalApi": True},
            },
            timeout=10.0,
        )
        await self._write_locked({"jsonrpc": "2.0", "method": "initialized"})
        _log_event("codex_app_server_started",
                   user_agent=(init or {}).get("userAgent", ""),
                   codex_home=(init or {}).get("codexHome", ""),
                   bin=codex_bin)

    def _expire_stale_codex_approvals(self):
        import sqlite3
        try:
            con = sqlite3.connect(CANON_DB, timeout=30)
            cur = con.execute(
                "UPDATE approvals SET status='expired', decided_at=?, result=? "
                "WHERE status='pending' AND source LIKE 'codex%'",
                (time.time(), json.dumps({"reason": "codex app-server restarted"},
                                         ensure_ascii=False)))
            con.commit()
            changed = cur.rowcount
            con.close()
            if changed:
                _log_event("codex_approval_stale_expired", count=changed)
        except Exception as e:  # noqa: BLE001
            _log_event("codex_approval_stale_expire_failed",
                       error=type(e).__name__, error_message=str(e)[:160])

    async def _call_started_locked(self, method: str, params: dict, timeout: float):
        rid = self._next_id
        self._next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._write_locked({"jsonrpc": "2.0", "id": rid,
                                  "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(rid, None)
            raise CodexAppServerError(f"{method} timed out") from e

    async def _write_locked(self, msg: dict):
        if not self.proc or not self.proc.stdin:
            raise CodexAppServerError("codex app-server is not running")
        raw = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        self.proc.stdin.write(raw)
        await self.proc.stdin.drain()

    async def _read_stdout(self):
        try:
            while self.proc and self.proc.stdout:
                raw = await self.proc.stdout.readline()
                if not raw:
                    break
                try:
                    msg = json.loads(raw.decode("utf-8", "replace"))
                except Exception as e:  # noqa: BLE001
                    _log_event("codex_app_server_bad_json",
                               error=type(e).__name__,
                               line=raw.decode("utf-8", "replace")[:160])
                    continue
                if msg.get("method"):
                    await self._handle_server_message(msg)
                elif "id" in msg:
                    fut = self._pending.pop(msg.get("id"), None)
                    if not fut or fut.done():
                        _log_event("codex_app_server_unmatched_response",
                                   id_hash=_short_hash(str(msg.get("id"))))
                        continue
                    if "error" in msg:
                        err = msg.get("error") or {}
                        fut.set_exception(CodexAppServerError(
                            err.get("message") or "codex app-server error",
                            err.get("code")))
                    else:
                        fut.set_result(msg.get("result"))
                else:
                    _log_event("codex_app_server_unknown_message",
                               keys=",".join(sorted(str(k) for k in msg.keys()))[:120])
        except Exception as e:  # noqa: BLE001
            _log_event("codex_app_server_reader_failed", error=type(e).__name__,
                       error_message=str(e)[:160])
        finally:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(CodexAppServerError("codex app-server stopped"))
            self._pending.clear()

    async def _read_stderr(self):
        try:
            while self.proc and self.proc.stderr:
                raw = await self.proc.stderr.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", "replace").strip()
                if text and "WARNING: proceeding" not in text:
                    _log_event("codex_app_server_stderr", message=text[:240])
        except Exception:  # noqa: BLE001
            pass

    def _append(self, thread_id: str, item):
        if not thread_id:
            return
        self.last_event_at[thread_id] = time.time()
        buf = self.thread_events[thread_id]
        buf.append(item)
        if len(buf) > 2000:
            del buf[:500]

    async def _handle_server_message(self, msg: dict):
        method = msg.get("method")
        if "id" in msg:
            if method in (
                "execCommandApproval",
                "applyPatchApproval",
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            ):
                self._handle_approval_request(msg)
                return
            _log_event("codex_app_server_unhandled_request",
                       method=str(method or "")[:120],
                       id_hash=_short_hash(str(msg.get("id"))))
            await self._write_server_error(msg.get("id"), -32601,
                                           f"server request not implemented: {method}")
            return
        self._handle_notification(msg)

    async def _write_server_error(self, request_id, code: int, message: str):
        try:
            async with self._lock:
                if not self.proc or self.proc.returncode is not None:
                    return
                await self._write_locked({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": code, "message": message},
                })
        except Exception as e:  # noqa: BLE001
            _log_event("codex_app_server_error_response_failed",
                       error=type(e).__name__, error_message=str(e)[:160])

    async def _write_server_result(self, request_id, result: dict):
        async with self._lock:
            if not self.proc or self.proc.returncode is not None:
                raise CodexAppServerError("codex app-server is not running")
            await self._write_locked({"jsonrpc": "2.0", "id": request_id,
                                      "result": result})

    def _approval_thread_id(self, method: str, params: dict) -> str:
        if method in ("execCommandApproval", "applyPatchApproval"):
            return str(params.get("conversationId") or "")
        return str(params.get("threadId") or "")

    def _approval_title(self, method: str, params: dict) -> str:
        if method in ("execCommandApproval", "item/commandExecution/requestApproval"):
            return "Codex command approval"
        return "Codex file-change approval"

    def _approval_command_text(self, params: dict) -> str:
        cmd = params.get("command")
        if isinstance(cmd, list):
            return shlex.join(str(x) for x in cmd)
        if isinstance(cmd, str):
            return cmd
        return ""

    def _approval_detail(self, method: str, params: dict) -> str:
        lines = [self._approval_title(method, params)]
        reason = params.get("reason")
        cwd = params.get("cwd")
        if cwd:
            lines.append(f"cwd: {cwd}")
        if reason:
            lines.append(f"reason: {reason}")
        command = self._approval_command_text(params)
        if command:
            lines.append("")
            lines.append("command:")
            lines.append(command)
        file_changes = params.get("fileChanges")
        if isinstance(file_changes, dict) and file_changes:
            lines.append("")
            lines.append("files:")
            for path, change in list(file_changes.items())[:30]:
                kind = ""
                if isinstance(change, dict):
                    kind_obj = change.get("kind")
                    kind = kind_obj.get("type") if isinstance(kind_obj, dict) else str(kind_obj or "")
                lines.append(f"- {path}" + (f" ({kind})" if kind else ""))
            if len(file_changes) > 30:
                lines.append(f"- ...and {len(file_changes) - 30} more")
        grant_root = params.get("grantRoot")
        if grant_root:
            lines.append(f"grant_root: {grant_root}")
        return "\n".join(lines).strip()

    def _approval_public(self, record: dict | None) -> dict | None:
        if not record:
            return None
        return {
            "id": record.get("id"),
            "method": record.get("method"),
            "title": record.get("title"),
            "detail": record.get("detail"),
            "risk": record.get("risk"),
            "created_at": record.get("created_at"),
            "thread_id": record.get("thread_id"),
            "options": record.get("options"),   # 發起方宣告的選項(去二元);None → app 用二元預設
        }

    def _approval_db_upsert(self, record: dict) -> None:
        import sqlite3
        con = sqlite3.connect(CANON_DB, timeout=30)
        now = record.get("created_at") or time.time()
        # A1:統一欄位落庫。DB 的 options style 收斂為規範字彙(deny→danger);
        # 記憶體 record 保持原樣 — 現行 app 以 style=="deny" 判拒絕鍵,既有
        # 曝露面(v2 meta.approval、卡片流)相容期不動(A4 收斂)。
        src = str(record.get("source") or "")
        options = [({**o, "style": "danger"} if o.get("style") == "deny" else dict(o))
                   for o in (record.get("options") or [])]
        con.execute("INSERT OR REPLACE INTO approvals"
                    "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result,callback,"
                    "session_id,provider,kind,options) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (record["id"], record["title"], record["source"], record["risk"],
                     record["detail"], now, now + 3600, "pending", None, None, None,
                     src if ":" in src else None, "codex", "permission",
                     json.dumps(options, ensure_ascii=False) if options else None))
        con.commit()
        con.close()

    def _approval_db_decide(self, approval_id: str, status: str, result: dict | str) -> None:
        import sqlite3
        if not isinstance(result, str):
            result = json.dumps(result or {}, ensure_ascii=False)
        con = sqlite3.connect(CANON_DB, timeout=30)
        con.execute("UPDATE approvals SET status=?, decided_at=?, result=? WHERE id=?",
                    (status, time.time(), result, approval_id))
        con.commit()
        con.close()

    def _handle_approval_request(self, msg: dict) -> None:
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        request_id = msg.get("id")
        thread_id = self._approval_thread_id(method, params)
        created = time.time()
        stable = json.dumps([thread_id, method, request_id], sort_keys=True,
                            ensure_ascii=False, default=str)
        approval_id = "codex-" + hashlib.sha1(stable.encode("utf-8", "replace")).hexdigest()[:24]
        record = {
            "id": approval_id,
            "request_id": request_id,
            "method": method,
            "params": params,
            "thread_id": thread_id,
            "title": self._approval_title(method, params),
            "source": f"codex:{thread_id}" if thread_id else "codex",
            "risk": "high" if "command" in method.lower() or method == "execCommandApproval" else "medium",
            "detail": self._approval_detail(method, params),
            "created_at": created,
        }
        # 選項由發起方宣告(method-aware),不再由 carddigest 寫死二元。允許鈕
        # 依動作類型給語意標籤;style=="deny" 是唯一「拒絕」判準(app 依此送
        # approve=false)。三態第三顆 = for_session(一律允許此類、本 session 不再
        # 問)→ 對 command/fileChange 映射 Codex 原生 acceptForSession。
        _mlow = method.lower()
        _allow_label = ("允許執行" if "command" in _mlow
                        else "允許修改" if "filechange" in _mlow.replace("_", "")
                        else "允許")
        record["options"] = [
            {"key": "approve", "label": _allow_label, "style": "primary"},
        ]
        # 只有支援 acceptForSession 的 method 才給第三顆(見 _approval_response_result)。
        # key 用 approve_for_session:非 deny-ish → 舊 App(build ≤44,只認 approve/
        # deny)會安全退成「一般允許」,不會誤送拒絕;新 App 認得此 key → 送
        # for_session=true。style=secondary(較軟的允許)。
        if method in ("item/commandExecution/requestApproval",
                      "item/fileChange/requestApproval"):
            record["options"].append(
                {"key": "approve_for_session", "label": "本次全允許", "style": "secondary"})
        record["options"].append(
            {"key": "deny", "label": "拒絕", "style": "deny"})
        self.pending_approvals[approval_id] = record
        if thread_id:
            self.pending_approvals_by_thread[thread_id][approval_id] = record
            self.last_event_at[thread_id] = created
        try:
            self._approval_db_upsert(record)
        except Exception as e:  # noqa: BLE001
            _log_event("codex_approval_db_upsert_failed",
                       approval_id=approval_id,
                       error=type(e).__name__, error_message=str(e)[:160])
        try:
            _cx_cards_feed_approval(record)   # S2:approval 卡 + 等待核准 status
        except Exception as e:  # noqa: BLE001
            _log_event("cx_cards_feed_error", error=str(e)[:160])
        try:
            # 批次 3 斷點③:CX 審核進推播管線(記錄本來就進 approvals DB,
            # decide 走既有 codex 分支回流 app-server)。
            _approval_push(approval_id, record["title"],
                           record["detail"].splitlines()[0] if record["detail"] else "點開查看並決定",
                           f"codex:{thread_id}" if thread_id else "codex")
        except Exception as e:  # noqa: BLE001
            _log_event("approval_push_error", error=str(e)[:160])
        _log_event("codex_approval_request",
                   approval_id=approval_id,
                   method=method,
                   thread_id_hash=_short_hash(thread_id),
                   request_id_hash=_short_hash(str(request_id)))

    def pending_approval_for_thread(self, thread_id: str) -> dict | None:
        if not thread_id:
            return None
        pending = self.pending_approvals_by_thread.get(thread_id) or {}
        for aid, record in list(pending.items()):
            if aid in self.pending_approvals:
                return record
            pending.pop(aid, None)
        return None

    def _drop_approval(self, approval_id: str, status: str = "expired") -> dict | None:
        record = self.pending_approvals.pop(approval_id, None)
        if not record:
            return None
        thread_id = record.get("thread_id") or ""
        if thread_id:
            self.pending_approvals_by_thread.get(thread_id, {}).pop(approval_id, None)
        try:
            self._approval_db_decide(approval_id, status, {"reason": "server request no longer live"})
        except Exception as e:  # noqa: BLE001
            _log_event("codex_approval_db_decide_failed",
                       approval_id=approval_id,
                       error=type(e).__name__, error_message=str(e)[:160])
        try:
            _cx_cards_feed_approval(record, resolved=status)   # S2:approval 卡收尾
        except Exception as e:  # noqa: BLE001
            _log_event("cx_cards_feed_error", error=str(e)[:160])
        return record

    def _drop_approval_by_request(self, request_id) -> None:
        for aid, record in list(self.pending_approvals.items()):
            if record.get("request_id") == request_id:
                self._drop_approval(aid, status="expired")
                return

    def _drop_thread_approvals(self, thread_id: str) -> None:
        for aid in list((self.pending_approvals_by_thread.get(thread_id) or {}).keys()):
            self._drop_approval(aid, status="expired")

    def _approval_response_result(self, record: dict, approved: bool,
                                  for_session: bool = False) -> dict:
        method = record.get("method")
        if method in ("item/commandExecution/requestApproval",
                      "item/fileChange/requestApproval"):
            if approved:
                decision = "acceptForSession" if for_session else "accept"
            else:
                decision = "decline"
            return {"decision": decision}
        if approved:
            decision = "approved_for_session" if for_session else "approved"
        else:
            decision = "denied"
        return {"decision": decision}

    async def decide_approval(self, approval_id: str, approved: bool,
                              for_session: bool = False) -> dict:
        record = self.pending_approvals.get(approval_id)
        if not record:
            raise CodexAppServerError("codex approval is no longer pending", code=404)
        result = self._approval_response_result(record, approved, for_session=for_session)
        await self._write_server_result(record.get("request_id"), result)
        self.pending_approvals.pop(approval_id, None)
        thread_id = record.get("thread_id") or ""
        if thread_id:
            self.pending_approvals_by_thread.get(thread_id, {}).pop(approval_id, None)
            self.last_event_at[thread_id] = time.time()
        status = "approved" if approved else "rejected"
        try:
            self._approval_db_decide(approval_id, status, result)
        except Exception as e:  # noqa: BLE001
            _log_event("codex_approval_db_decide_failed",
                       approval_id=approval_id,
                       error=type(e).__name__, error_message=str(e)[:160])
        try:
            _cx_cards_feed_approval(record, resolved=status)   # S2:approval 卡收尾
        except Exception as e:  # noqa: BLE001
            _log_event("cx_cards_feed_error", error=str(e)[:160])
        _log_event("codex_approval_decision",
                   approval_id=approval_id,
                   status=status,
                   method=record.get("method"),
                   thread_id_hash=_short_hash(thread_id),
                   request_id_hash=_short_hash(str(record.get("request_id"))))
        return {"id": approval_id, "status": status, "result": result,
                "thread_id": thread_id, "method": record.get("method")}

    async def decide_thread_approval(self, thread_id: str, approved: bool,
                                     for_session: bool = False) -> dict:
        record = self.pending_approval_for_thread(thread_id)
        if not record:
            raise CodexAppServerError("no pending Codex approval for thread", code=404)
        return await self.decide_approval(record["id"], approved,
                                          for_session=for_session)

    def _handle_notification(self, msg: dict):
        method = msg.get("method")
        params = msg.get("params") or {}
        try:
            _cx_cards_feed(method, params)   # S2 卡片 digest(有訂閱的 thread 才有)
        except Exception as e:  # noqa: BLE001
            _log_event("cx_cards_feed_error", error=str(e)[:160])
        if method == "remoteControl/status/changed":
            self.remote_status = params
            return
        if method == "thread/started":
            thread = params.get("thread") or {}
            tid = thread.get("id")
            if tid:
                self.loaded_threads.add(tid)
            return
        tid = params.get("threadId")
        if method == "thread/tokenUsage/updated" and tid:
            self.token_usage[tid] = params.get("tokenUsage") or params
            return
        if method == "turn/started" and tid:
            turn = params.get("turn") or {}
            self.active_turns[tid] = turn.get("id") or True
            self.last_event_at[tid] = time.time()
            self.thread_errors.pop(tid, None)
            _codex_history_invalidate(tid)   # cached /history page is now stale
            return
        if method == "turn/completed" and tid:
            self.active_turns.pop(tid, None)
            self.last_event_at[tid] = time.time()
            _codex_history_invalidate(tid)   # cached /history page is now stale
            self._drop_thread_approvals(tid)
            turn = params.get("turn") or {}
            err = turn.get("error") if isinstance(turn, dict) else None
            if err:
                msg = err.get("message", err)
                self.thread_errors[tid] = str(msg)
                self._append(tid, ("text", f"\n⚠️ Codex turn failed: {msg}\n"))
            else:
                self.thread_errors.pop(tid, None)
            # M1:是委派 thread → 回流父對話(running→idle/failed 轉換內部去重)。
            try:
                t = asyncio.create_task(_delegation_codex_completed(
                    tid, bool(err),
                    str(err.get("message", err))[:300] if err else ""))
                _BG_TASKS.add(t)
                t.add_done_callback(_BG_TASKS.discard)
            except RuntimeError:
                pass
            return
        if method == "error":
            _log_event("codex_app_server_error",
                       message=str(params.get("message") or params)[:240])
            return
        if method == "serverRequest/resolved":
            self._drop_approval_by_request(params.get("requestId"))
            return
        if not tid:
            return
        if method == "item/agentMessage/delta":
            item_id = params.get("itemId")
            delta = params.get("delta") or ""
            if item_id:
                # First delta of a NEW agent message carries the **🤖 助手:**
                # marker (same as _codex_format_item's non-streamed path) — the
                # app splits turns on it; without it streamed replies fold into
                # the user's bubble (issue #16). Later deltas of the same item
                # append bare.
                if item_id not in self._streamed_item_ids and delta:
                    delta = f"\n\n**🤖 助手:** {delta}"
                self._streamed_item_ids.add(item_id)
            self._append(tid, ("text", delta))
            return
        if method == "item/started":
            item = params.get("item") or {}
            if item.get("type") == "userMessage":
                return
            c = _codex_format_item(item, phase="started",
                                   skip_agent_ids=self._streamed_item_ids)
            if c:
                self._append(tid, ("text", c))
            return
        if method == "item/completed":
            item = params.get("item") or {}
            c = _codex_format_item(item, phase="completed",
                                   skip_agent_ids=self._streamed_item_ids)
            if c:
                self._append(tid, ("text", c))
            return
        if method == "item/fileChange/patchUpdated":
            changes = params.get("changes") or []
            c = _codex_format_file_changes(changes, "inProgress")
            if c:
                self._append(tid, ("text", c))

    async def ensure_thread_loaded(self, thread_id: str, cwd: str | None = None):
        if thread_id in self.loaded_threads:
            return
        params = {"threadId": thread_id, "excludeTurns": True}
        if cwd:
            params["cwd"] = cwd
        await self.call("thread/resume", params, timeout=30.0)
        self.loaded_threads.add(thread_id)

    async def start_turn(self, thread_id: str, input_items: list, client_id: str | None = None,
                         cwd: str | None = None):
        self.thread_event_generations[thread_id] += 1
        self.thread_events[thread_id].clear()
        await self.ensure_thread_loaded(thread_id, cwd=cwd)
        params = {"threadId": thread_id, "input": input_items}
        if client_id:
            params["clientUserMessageId"] = client_id
        if cwd:
            params["cwd"] = cwd
        res = await self.call("turn/start", params, timeout=30.0)
        turn = (res or {}).get("turn") or {}
        self.active_turns[thread_id] = turn.get("id") or True
        return res

    async def interrupt_turn(self, thread_id: str):
        turn_id = self.active_turns.get(thread_id)
        if not isinstance(turn_id, str) or not turn_id:
            raise CodexAppServerError("no active Codex turn to interrupt", code=-32600)
        return await self.call("turn/interrupt", {
            "threadId": thread_id,
            "turnId": turn_id,
        }, timeout=15.0)

    def events_for(self, thread_id: str) -> list:
        return self.thread_events.get(thread_id, [])

    def is_active(self, thread_id: str) -> bool:
        return thread_id in self.active_turns


CODEX_APP = CodexAppServerClient()


def _codex_usage_map(tu) -> dict | None:
    """app-server token usage (thread dict or tokenUsage/updated params) →
    the app's {used, size} meter shape. Defensive: field names probed on
    codex-cli 0.142.2 (totalTokens / inputTokens / cachedInputTokens /
    outputTokens, window in modelContextWindow); unknown shapes → None."""
    if not isinstance(tu, dict):
        return None
    inner = tu.get("tokenUsage") if isinstance(tu.get("tokenUsage"), dict) else tu
    total = inner.get("totalTokens")
    if total is None:
        total = sum(int(inner.get(k) or 0)
                    for k in ("inputTokens", "cachedInputTokens", "outputTokens"))
    try:
        total = int(total or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    usage = {"used": total}
    window = inner.get("modelContextWindow") or tu.get("modelContextWindow")
    try:
        if window:
            usage["size"] = int(window)
    except (TypeError, ValueError):
        pass
    return usage


def _codex_enrich_summary(summary: dict) -> dict:
    tid = summary.get("thread_id") or summary.get("id") or ""
    summary["activeTurn"] = CODEX_APP.is_active(tid)
    usage = _codex_usage_map(CODEX_APP.token_usage.get(tid))
    if usage:
        summary["usage"] = usage
    approval = CODEX_APP.pending_approval_for_thread(tid)
    if approval:
        summary["awaitingApproval"] = True
        summary["status"] = "waiting_approval"
        summary["approval"] = CODEX_APP._approval_public(approval)
    if tid in CODEX_APP.last_event_at:
        summary["lastEventAt"] = CODEX_APP.last_event_at[tid]
    if tid in CODEX_APP.thread_errors:
        summary["error"] = CODEX_APP.thread_errors[tid]
    return summary


def _codex_status_type(status) -> str:
    if isinstance(status, dict):
        return status.get("type") or "unknown"
    if isinstance(status, str):
        return status
    return "unknown"


def _codex_source_label(source) -> str:
    if isinstance(source, str):
        return source
    if isinstance(source, dict):
        if "custom" in source:
            return str(source.get("custom") or "custom")
        if "subAgent" in source:
            return "subAgent"
    return "unknown"


def _codex_session_summary(thread: dict) -> dict:
    tid = thread.get("id") or ""
    name = (thread.get("name") or "").strip()
    preview = (thread.get("preview") or "").strip()
    out = {
        "name": name or preview[:180] or (tid[:12] or "codex"),
        "thread_id": tid,
        "session_id": thread.get("sessionId") or "",
        "workdir": thread.get("cwd") or "",
        "preview": preview[:180],
        "status": _codex_status_type(thread.get("status")),
        "source": _codex_source_label(thread.get("source")),
        "updatedAt": thread.get("updatedAt"),
        "modelProvider": thread.get("modelProvider") or "",
    }
    # 0.142.2 returns tokenUsage: null from thread/list, but map it when a
    # future version populates it; the live overlay in _codex_enrich_summary
    # (thread/tokenUsage/updated) wins either way.
    usage = _codex_usage_map(thread.get("tokenUsage"))
    if usage:
        out["usage"] = usage
    return out


def _codex_user_input_text(content: list) -> str:
    parts = []
    for item in content or []:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "text" and item.get("text"):
            parts.append(item["text"])
        elif t == "localImage" and item.get("path"):
            parts.append(f"[圖片: {item['path']}]")
        elif t == "image" and item.get("url"):
            parts.append(f"[圖片: {item['url']}]")
        elif item.get("path"):
            parts.append(f"[{t or 'file'}: {item['path']}]")
    return "\n".join(parts).strip()


def _codex_format_file_changes(changes: list, status: str = "") -> str:
    if not changes:
        return ""
    rows = []
    for c in changes[:8]:
        if not isinstance(c, dict):
            continue
        kind = c.get("kind") or {}
        k = kind.get("type") if isinstance(kind, dict) else str(kind)
        rows.append(f"- {k or 'change'} `{c.get('path', '')}`")
    more = f"\n- ...and {len(changes) - 8} more" if len(changes) > 8 else ""
    label = f"fileChange {status}".strip()
    return f"\n› 📝 **{label}**\n" + "\n".join(rows) + more + "\n"


def _codex_format_tool_result(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    short = text[:1200]
    more = "\n...(truncated)" if len(text) > 1200 else ""
    return f"<details><summary>↳ result</summary>\n\n```\n{short}{more}\n```\n\n</details>\n"


def _codex_format_item(item: dict, phase: str = "completed", skip_agent_ids=None) -> str:
    if not isinstance(item, dict):
        return ""
    skip_agent_ids = skip_agent_ids or set()
    t = item.get("type")
    if t == "userMessage":
        text = _codex_user_input_text(item.get("content") or [])
        return f"\n\n**🧑 你:** {text}\n\n" if text else ""
    if t == "agentMessage":
        if item.get("id") in skip_agent_ids:
            return ""
        text = item.get("text") or ""
        # Must carry the same **🤖 助手:** marker CC's _fmt_cc_event already emits.
        # Without it, conversationTurns() (app-side, splits on **🧑 你:**) can't
        # tell where the user's turn ends and the reply begins, so the whole
        # agent reply gets folded into the SAME turn as the preceding userMessage
        # and renders inside the user's (right-aligned, brand-coloured) bubble
        # instead of its own left-aligned assistant block.
        return f"\n\n**🤖 助手:** {text}\n\n" if text else ""
    if t == "plan":
        text = item.get("text") or ""
        return f"\n<details><summary>Plan</summary>\n\n{text}\n\n</details>\n" if text else ""
    if t == "reasoning":
        summary = "\n".join(item.get("summary") or []).strip()
        return f"\n<details><summary>Reasoning</summary>\n\n{summary}\n\n</details>\n" if summary else ""
    if t == "commandExecution":
        cmd = (item.get("command") or "").strip().splitlines()
        cmd1 = (cmd[0] if cmd else "")[:TOOL_CMD_MAX]
        status = item.get("status") or phase
        head = f"\n› 🔧 **command** `{cmd1}` [{status}]\n" if cmd1 else f"\n› 🔧 **command** [{status}]\n"
        return head + _codex_format_tool_result(item.get("aggregatedOutput") or "")
    if t == "fileChange":
        return _codex_format_file_changes(item.get("changes") or [], item.get("status") or phase)
    if t == "mcpToolCall":
        label = f"{item.get('server', 'mcp')}.{item.get('tool', 'tool')}"
        status = item.get("status") or phase
        err = item.get("error") or {}
        out = f"\n› 🔧 **{label}** [{status}]\n"
        if err.get("message"):
            out += f"⚠️ {err['message']}\n"
        return out
    if t == "dynamicToolCall":
        label = item.get("tool") or "tool"
        ns = item.get("namespace")
        if ns:
            label = f"{ns}.{label}"
        return f"\n› 🔧 **{label}** [{item.get('status') or phase}]\n"
    if t == "webSearch":
        return f"\n› 🔎 **webSearch** `{str(item.get('query') or '')[:160]}`\n"
    if t == "imageGeneration":
        return f"\n› 🖼 **imageGeneration** [{item.get('status') or phase}]\n"
    return ""


def _codex_format_turns(turns: list) -> str:
    parts = []
    for turn in turns or []:
        for item in (turn.get("items") or []):
            c = _codex_format_item(item)
            if c:
                parts.append(c)
    return "".join(parts)


async def _codex_input_items(text: str, attachments: list) -> list:
    text = (text or "").strip()
    _att_guard(attachments)   # 修復單「附件限制」:直送口件數閥
    note_paths = []
    images = []
    voice_lines = []
    for a in (attachments or []):
        path = _save_attachment(a, a.get("filename") or "file")
        if not path:
            continue
        if a.get("kind") == "audio":
            t = await asyncio.to_thread(_transcribe, path)
            if t:
                voice_lines.append(t)
        elif a.get("kind") == "image":
            images.append({"type": "localImage", "path": path})
        else:
            note_paths.append(path)
    if voice_lines:
        text = (text + " " + " ".join(voice_lines)).strip()
    if note_paths:
        text = (text + "\n\n[附件已存到本機,請讀取: "
                + " ".join(note_paths) + "]").strip()
    items = []
    if text:
        items.append({"type": "text", "text": text})
    items.extend(images)
    return items


def _codex_http_error(e: Exception):
    _log_event("codex_provider_error", error=type(e).__name__,
               error_message=str(e)[:200],
               code=getattr(e, "code", None))
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        raise http_err(504, "PROVIDER_TIMEOUT", "codex app-server timeout", str(e))
    if isinstance(e, CodexAppServerError):
        code = 409 if e.code == -32600 else 502
        raise HTTPException(status_code=code, detail=str(e))
    raise HTTPException(status_code=502, detail=str(e))


@app.get("/codex/status")
async def codex_status(request: Request):
    _check_auth(request)
    try:
        status = await CODEX_APP.call("remoteControl/status/read", {}, timeout=15.0)
        return {"ok": True, "remoteControl": status}
    except Exception as e:  # noqa: BLE001
        _codex_http_error(e)


# Codex 進場延遲止血 (B1): /codexsessions/{id}/history is zero-cache and its
# thread/turns/list itemsView:"full" call is expensive; the app fires it
# several times while entering a session. A short TTL cache absorbs those
# duplicates — same pattern as _PANE_CACHE for tmux capture-pane.
_CODEX_HISTORY_TTL = 4.0
_CODEX_HISTORY_CACHE: dict = {}   # (thread_id, limit, cursor) -> (cached_at_monotonic, payload)


def _codex_history_invalidate(thread_id: str) -> None:
    """Drop cached history pages for one thread (new input / turn activity)."""
    for k in [k for k in _CODEX_HISTORY_CACHE if k[0] == thread_id]:
        _CODEX_HISTORY_CACHE.pop(k, None)


def _codex_stream_turn_finished(seen_turn_activity: bool, active: bool,
                                event_index: int, event_count: int) -> bool:
    """A follow stream may stay open while idle, but once it has observed a
    turn it must finish as soon as that turn reaches a terminal state and all
    buffered events have been emitted."""
    return seen_turn_activity and not active and event_index >= event_count


async def _codex_warm_threads(thread_ids: list) -> None:
    """B3 light warmup: pre-run thread/resume for the sessions the user is most
    likely to tap next, so entering one skips the cold load. Strictly
    sequential and skip-if-loaded, so it never amplifies app-server queueing —
    at most one warm call is in the single _lock queue at a time."""
    # 風險控管:若 spawn 到的是 ~/.local/bin 的舊 standalone(0.137 地雷版),
    # 停用 warmup 的 thread/resume —— 0.137 resume/讀 0.142+ 建的 thread 會
    # 引爆 app-server crash 連鎖(下一次「CX 全空」最可能的引信)。
    if str(getattr(CODEX_APP, "spawned_bin", "")).endswith("/.local/bin/codex"):
        return
    for tid in thread_ids:
        if not tid or tid in CODEX_APP.loaded_threads:
            continue
        try:
            await CODEX_APP.ensure_thread_loaded(tid)
            _log_event("codex_thread_warmed", thread=tid[:16])
        except Exception as e:  # noqa: BLE001
            _log_event("codex_thread_warm_failed", thread=tid[:16],
                       error=type(e).__name__)


@app.get("/codexsessions")
async def codex_sessions(request: Request, limit: int = 40, cwd: str | None = None,
                         archived: bool = False, cursor: str | None = None):
    _check_auth(request)
    params = {
        "limit": max(1, min(limit, 100)),
        "archived": archived,
        "sourceKinds": ["cli", "vscode", "exec", "appServer"],
        "sortKey": "updated_at",
        "sortDirection": "desc",
        "useStateDbOnly": False,
    }
    if cwd:
        params["cwd"] = cwd
    if cursor:
        params["cursor"] = cursor
    try:
        res = await CODEX_APP.call("thread/list", params, timeout=45.0)
        data = list((res or {}).get("data", []))
        # B3: warm the few most-recent threads in the background (fire and
        # forget — the list response is NOT delayed by this).
        warm_ids = [t.get("id") for t in data[:4] if t.get("id")]
        if warm_ids:
            task = asyncio.create_task(_codex_warm_threads(warm_ids))
            _BG_TASKS.add(task)
            task.add_done_callback(_BG_TASKS.discard)
        return {
            "sessions": [_codex_enrich_summary(_codex_session_summary(t))
                         for t in data],
            "nextCursor": (res or {}).get("nextCursor"),
        }
    except Exception as e:  # noqa: BLE001
        _codex_http_error(e)


@app.get("/codexsessions/{thread_id}/status")
async def codex_session_status(thread_id: str, request: Request):
    _check_auth(request)
    try:
        res = await CODEX_APP.call("thread/read", {
            "threadId": thread_id,
            "includeTurns": False,
        }, timeout=20.0)
        thread = (res or {}).get("thread") or {}
        summary = _codex_enrich_summary(_codex_session_summary(thread))
        return {"session": summary}
    except Exception as e:  # noqa: BLE001
        _codex_http_error(e)


@app.post("/codexsessions/{thread_id}/name")
async def codex_session_set_name(thread_id: str, request: Request):
    _check_auth(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    try:
        await CODEX_APP.call("thread/name/set", {
            "threadId": thread_id,
            "name": name,
        }, timeout=15.0)
        res = await CODEX_APP.call("thread/read", {
            "threadId": thread_id,
            "includeTurns": False,
        }, timeout=20.0)
        thread = (res or {}).get("thread") or {}
        summary = _codex_enrich_summary(_codex_session_summary(thread))
        return {"ok": True, "session": summary}
    except Exception as e:  # noqa: BLE001
        _codex_http_error(e)


@app.post("/codexsessions/{thread_id}/archive")
async def codex_session_archive(thread_id: str, request: Request):
    """Archive (or unarchive) a Codex thread."""
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    _check_auth(request)
    archived = body.get("archived", True)
    # Try the known method names in order (the app server build varies).
    last = None
    methods = (
        (
            ("thread/archive/set", {"threadId": thread_id, "archived": True}),
            ("thread/setArchived", {"threadId": thread_id, "archived": True}),
            ("thread/archive", {"threadId": thread_id}),
        )
        if archived
        else (
            ("thread/archive/set", {"threadId": thread_id, "archived": False}),
            ("thread/setArchived", {"threadId": thread_id, "archived": False}),
            ("thread/unarchive", {"threadId": thread_id}),
        )
    )
    for method, params in methods:
        try:
            await CODEX_APP.call(method, params, timeout=15.0)
            return {"ok": True, "method": method}
        except Exception as e:  # noqa: BLE001
            last = e
    _codex_http_error(last or Exception("archive failed"))


@app.post("/codexsessions")
async def codex_session_create(request: Request):
    _check_auth(request)
    body = await request.json()
    text = (body.get("text") or body.get("task") or "").strip()
    attachments = body.get("attachments") or []
    input_items = await _codex_input_items(text, attachments)
    if not input_items:
        raise HTTPException(status_code=400, detail="text or attachment required")
    cwd = body.get("cwd") or HOME_ROOT
    params = {
        "cwd": cwd,
        "ephemeral": False,
        "threadSource": "user",
    }
    if body.get("model"):
        params["model"] = body.get("model")
    try:
        res = await CODEX_APP.call("thread/start", params, timeout=30.0)
        thread = (res or {}).get("thread") or {}
        thread_id = thread.get("id")
        if not thread_id:
            raise CodexAppServerError("thread/start returned no thread id")
        CODEX_APP.loaded_threads.add(thread_id)
        await CODEX_APP.start_turn(thread_id, input_items,
                                   client_id=body.get("client_id"), cwd=cwd)
        return {"ok": True, "thread_id": thread_id,
                "session": _codex_enrich_summary(_codex_session_summary(thread))}
    except Exception as e:  # noqa: BLE001
        _codex_http_error(e)


@app.get("/file")
async def serve_file(request: Request, path: str):
    """Serve a local file (image/pdf) by path so the app can render image paths
    that appear in transcripts (your attachments + files the agent references).
    Restricted to a small set of safe roots (home + the temp dirs agents write
    scratch files to), must be a regular file."""
    _check_auth(request)
    p = os.path.realpath(os.path.expanduser(path))
    roots = [os.path.realpath(os.path.expanduser("~"))]
    # Agents (incl. Claude Code's scratchpad) often emit files under the system
    # temp dirs; allow those too so generated artifacts render instead of 404ing.
    for t in ("/tmp", "/private/tmp", "/var/folders"):
        rt = os.path.realpath(t)
        if rt not in roots:
            roots.append(rt)
    if not any(p == r or p.startswith(r + os.sep) for r in roots) or not os.path.isfile(p):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(p)


async def _git_capture(*args, cwd=None, timeout: float = 20.0):
    """git 子行程一次呼叫 → (returncode, stdout 文字)。逾時殺行程回 (124, "")
    — git on a wedged repo/mount must not hang the handler (issue #7)。
    /filediff 與 session diff 端點(S2 / #38)共用。"""
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        _log_event("filediff_git_timeout", args=" ".join(args[:4]),
                   timeout_s=timeout)
        return 124, ""
    return proc.returncode, (out or b"").decode("utf-8", "replace")


@app.get("/filediff")
async def serve_filediff(request: Request, path: str):
    """Diff/content for a file an agent touched (S2, pocketagent#38). Finds the
    enclosing git repo from the file's own location, returns `git diff HEAD`
    for it; a file with no pending diff (or outside any repo) falls back to its
    current content, so the app always has something to show. Same safe-root
    policy as /file.

    目錄模式（#38 缺口）：path 是目錄 → 整個目錄的 pending diff（合併
    unified）＋ `files[]` 變更檔清單，app 的多檔選單直接吃；目錄乾淨或
    不在 repo 裡 → 404 人話（目錄沒有「當前內容」可退）。"""
    _check_auth(request)
    p = os.path.realpath(os.path.expanduser(path))
    roots = [os.path.realpath(os.path.expanduser("~"))]
    for t in ("/tmp", "/private/tmp", "/var/folders"):
        rt = os.path.realpath(t)
        if rt not in roots:
            roots.append(rt)
    if (not any(p == r or p.startswith(r + os.sep) for r in roots)
            or not (os.path.isfile(p) or os.path.isdir(p))):
        raise HTTPException(status_code=404, detail="not found")

    if os.path.isdir(p):
        rc, top = await _git_capture("git", "-C", p, "rev-parse", "--show-toplevel")
        if rc != 0 or not top.strip():
            raise HTTPException(status_code=404, detail="目錄不在 git repo 裡，沒有 diff 可看")
        top = top.strip()
        rc2, out = await _git_capture("git", "-C", top, "diff", "HEAD", "--", p)
        diff = out if rc2 == 0 else ""
        if not diff:
            raise HTTPException(status_code=404, detail="目錄內沒有待提交的變更")
        files = []
        rc3, names = await _git_capture("git", "-C", top, "diff", "HEAD",
                                "--name-status", "--", p)
        if rc3 == 0:
            for line in names.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and parts[0]:
                    # rename 列是「R100\told\tnew」— 取最後一欄（現名）。
                    files.append({"status": parts[0][:1],
                                  "path": os.path.join(top, parts[-1])})
        if len(diff) > 200_000:
            diff = diff[:200_000] + "\n...(truncated)"
        return {"kind": "diff", "path": p, "text": diff, "files": files}

    d = os.path.dirname(p)
    rc, top = await _git_capture("git", "-C", d, "rev-parse", "--show-toplevel")
    diff = ""
    if rc == 0 and top.strip():
        # HEAD..worktree for this file — covers staged + unstaged edits.
        rc2, out = await _git_capture("git", "-C", top.strip(), "diff", "HEAD", "--", p)
        if rc2 == 0:
            diff = out
    if diff:
        if len(diff) > 200_000:
            diff = diff[:200_000] + "\n...(truncated)"
        return {"kind": "diff", "path": p, "text": diff}
    # No pending diff (already committed / untracked / not a repo) → current
    # content so "看檔案" still works. Reject binaries by a NUL sniff.
    try:
        with open(p, "rb") as f:
            head = f.read(200_000)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e)[:200])
    if b"\x00" in head:
        raise HTTPException(status_code=415, detail="binary file")
    text = head.decode("utf-8", "replace")
    if len(head) == 200_000:
        text += "\n...(truncated)"
    return {"kind": "content", "path": p, "text": text}


# --- Session-scoped diff(S2 / pocketagent#38)------------------------------
# /filediff 吃「絕對路徑、從檔案自身找 repo」;這組端點吃「session + workdir
# 相對路徑」— transcript/卡片帶的常是相對路徑,由 bridge 用該 session 的
# workdir 解析,並把 realpath 圈死在 workdir 內(防 ../ 逃逸)。三個入口共用
# 一個核心:v1 /ccsessions|/codexsessions(issue 原文形)+ v2 統一路由。

_SESSION_DIFF_MAX = 200_000     # 截斷上限,與 /filediff 同一數字


async def _codex_thread_workdir(thread_id: str) -> str:
    try:
        res = await CODEX_APP.call("thread/read", {
            "threadId": thread_id,
            "includeTurns": False,
        }, timeout=20.0)
    except Exception as e:  # noqa: BLE001
        _codex_http_error(e)
    return ((res or {}).get("thread") or {}).get("cwd") or ""


async def _session_workdir_diff(workdir: str, path: str) -> dict:
    """Session workdir 內單檔的 pending diff 核心。

    tracked 檔走 `git diff HEAD -- <p>`(staged+unstaged 一次都在);乾淨但
    untracked 的新檔用 no-index 對 /dev/null 合成 new-file diff,app 端一樣
    有 +行綠可看(/filediff 這種情況只退回全文,是 #38 驗收的缺口)。
    回 {path, workdir, diff, truncated};diff 為空字串 = 該檔沒有待定變更。"""
    wd = os.path.realpath(os.path.expanduser(workdir or ""))
    if not workdir or not os.path.isdir(wd):
        raise HTTPException(status_code=404, detail="session 沒有可用的工作目錄")
    raw = os.path.expanduser((path or "").strip())
    if not raw:
        raise HTTPException(status_code=400, detail="path required")
    p = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(wd, raw))
    if p != wd and not p.startswith(wd + os.sep):
        raise HTTPException(status_code=400, detail="path 不在 session 工作目錄內")
    rc, top = await _git_capture("git", "-C", wd, "rev-parse", "--show-toplevel")
    if rc != 0 or not top.strip():
        raise HTTPException(status_code=404,
                            detail="工作目錄不在 git repo 裡,沒有 diff 可看")
    top = top.strip()
    rel = os.path.relpath(p, top)
    rc2, out = await _git_capture("git", "-C", top, "diff", "HEAD", "--", rel)
    diff = out if rc2 == 0 else ""
    if not diff and os.path.isfile(p):
        # tracked 且乾淨 vs untracked 新檔:porcelain 分辨;新檔合成 no-index
        # diff(它的 rc=1 是「有差異」,不是錯)。
        rcs, st = await _git_capture("git", "-C", top, "status", "--porcelain",
                                     "--untracked-files=all", "--", rel)
        if rcs == 0 and st.lstrip().startswith("??"):
            _rcn, out_n = await _git_capture("git", "-C", top, "diff",
                                             "--no-index", "--", os.devnull, rel)
            diff = out_n
    truncated = False
    if len(diff) > _SESSION_DIFF_MAX:
        diff = diff[:_SESSION_DIFF_MAX]
        truncated = True
    return {"path": p, "workdir": wd, "diff": diff, "truncated": truncated}


@app.get("/ccsessions/{name}/diff")
async def cc_session_diff(name: str, request: Request, path: str):
    _check_auth(request)
    row = next((r for r in _cc_conf_rows() if r[0] == name), None)
    if not row:
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    return await _session_workdir_diff(row[1], path)


@app.get("/codexsessions/{thread_id}/diff")
async def codex_session_diff(thread_id: str, request: Request, path: str):
    _check_auth(request)
    return await _session_workdir_diff(await _codex_thread_workdir(thread_id), path)


@app.get("/app/v2/sessions/{session_id}/diff")
async def v2_session_diff(session_id: str, request: Request, path: str):
    """統一路由 diff(卡片流表面直接用 store 的 v2 session id 打):cc=conf
    workdir、cx/delegation=thread cwd;hermes 沒有工作目錄 → 400。"""
    _check_auth(request)
    src = _v2_card_source(session_id)
    if src[0] == "cc":
        return await _session_workdir_diff(src[2], path)
    if src[0] == "cx":
        return await _session_workdir_diff(await _codex_thread_workdir(src[1]), path)
    raise http_err(400, "UNSUPPORTED_PROVIDER", "persona session 沒有工作目錄")


# --- Client error log ------------------------------------------------------
# The app ships every error it hits (failed send, dropped stream, crash, …) here
# the moment it happens. We append to ONE file on the Mac so Claude can fetch +
# review client-side bugs each session and confirm whether they're resolved.
CLIENT_LOG = os.path.expanduser("~/.pocket/pocket-client.jsonl")


def _pair_code_meta(value):
    if isinstance(value, dict):
        return {
            "expiry": float(value.get("expiry") or 0),
            "apple_user_id": value.get("apple_user_id"),
        }
    try:
        return {"expiry": float(value), "apple_user_id": None}
    except Exception:  # noqa: BLE001
        return {"expiry": 0.0, "apple_user_id": None}


def _pair_code_reject(request: Request):
    with _AUTH_LOCK:
        now = time.monotonic()
        while _AUTH_FAILS and now - _AUTH_FAILS[0] > _AUTH_FAIL_WINDOW:
            _AUTH_FAILS.popleft()
        _AUTH_FAILS.append(now)
        over = len(_AUTH_FAILS) > _AUTH_FAIL_MAX
        summary = _auth_fail_summary_locked(request, 429 if over else 400, now)
    if summary:
        _log_event("pair_claim_failure", **summary)
    raise HTTPException(status_code=429 if over else 400,
                        detail="invalid or expired pairing code")


@app.post("/app/v1/pair/new")
@app.post("/pair/new")
async def pair_new(request: Request):
    """Desktop-only (needs the master token): mint a one-time pairing code that
    the QR embeds. The phone exchanges it at /pair/claim. Never returns the token."""
    _check_auth(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    required_account = request.url.path.startswith("/app/v1/")
    user = _account_user_from_request(request, body, required=required_account)
    now = time.monotonic()
    code = secrets.token_urlsafe(9)
    with _PAIR_LOCK:
        for c in [c for c, v in _PAIR_CODES.items() if _pair_code_meta(v)["expiry"] < now]:
            _PAIR_CODES.pop(c, None)          # prune expired
        _PAIR_CODES[code] = {
            "expiry": now + _PAIR_CODE_TTL,
            "apple_user_id": (user or {}).get("apple_user_id"),
        }
    return {"code": code, "ttl": int(_PAIR_CODE_TTL),
            "account_bound": bool(user)}


@app.post("/app/v1/pair/claim")
@app.post("/pair/claim")
async def pair_claim(request: Request):
    """Phone exchanges a one-time code for its OWN device token. The code IS the
    credential (no bearer needed); it's single-use and expires in 5 minutes."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    code = (body.get("code") or "").strip()
    name = (str(body.get("device_name") or "iPhone"))[:60]
    platform = (str(body.get("platform") or "ios"))[:32]
    now = time.monotonic()
    with _PAIR_LOCK:
        meta = _pair_code_meta(_PAIR_CODES.get(code))
        if not code or not meta["expiry"] or meta["expiry"] < now:
            _pair_code_reject(request)
    bound_user_id = meta.get("apple_user_id")
    is_app_pair_claim = request.url.path.startswith("/app/v1/")
    user = _account_user_from_request(request, body, required=False)
    if is_app_pair_claim and not bound_user_id:
        raise HTTPException(status_code=400, detail="pairing code is not account-bound")
    if bound_user_id and user and user.get("apple_user_id") != bound_user_id:
        _log_event("pair_claim_account_mismatch",
                   code_hash=_short_hash(code),
                   expected_user_hash=_short_hash(bound_user_id),
                   actual_user_hash=_short_hash(user.get("apple_user_id")))
        raise HTTPException(status_code=403, detail="pairing code belongs to another account")
    claim_user_id = bound_user_id or (user or {}).get("apple_user_id")

    with _PAIR_LOCK:
        meta = _pair_code_meta(_PAIR_CODES.get(code))
        if not code or not meta["expiry"] or meta["expiry"] < time.monotonic():
            _pair_code_reject(request)
        _PAIR_CODES.pop(code, None)           # one-time
        token = "pdev-" + secrets.token_urlsafe(32)
        device = None
        if claim_user_id:
            device = _account_device_put(claim_user_id, token, platform=platform, label=name)
        _DEVICE_TOKENS[token] = {
            "name": name,
            "platform": platform,
            "created": time.time(),
            "last_seen": time.time(),
            "apple_user_id": claim_user_id,
            "device_id": (device or {}).get("device_id"),
        }
        _save_device_tokens(_DEVICE_TOKENS)
    _log_event("pair_claim",
               device=name,
               platform=platform,
               account_bound=bool(claim_user_id),
               apple_user_hash=_short_hash(claim_user_id),
               token_hash=_short_hash(token))
    return {"token": token,
            "device_id": (device or {}).get("device_id"),
            "account_bound": bool(claim_user_id)}


@app.get("/pair/devices")
async def pair_devices(request: Request):
    """List paired devices (desktop only). Tokens are returned hashed, not raw."""
    _check_auth(request)
    with _PAIR_LOCK:
        out = [
            {"id": _short_hash(t), "name": d.get("name", "device"),
             "platform": d.get("platform"), "device_id": d.get("device_id"),
             "account_bound": bool(d.get("apple_user_id")),
             "created": d.get("created"), "last_seen": d.get("last_seen")}
            for t, d in _DEVICE_TOKENS.items()
        ]
    return {"devices": out}


@app.post("/pair/revoke")
async def pair_revoke(request: Request):
    """Revoke a paired device by its short id (from /pair/devices). Desktop only."""
    _check_auth(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    dev_id = (body.get("id") or "").strip()
    removed = 0
    with _PAIR_LOCK:
        for t in [t for t in _DEVICE_TOKENS if _short_hash(t) == dev_id]:
            dev = _DEVICE_TOKENS.pop(t, None)
            if dev and dev.get("apple_user_id") and dev.get("device_id"):
                _account_device_revoke(dev.get("apple_user_id"), dev.get("device_id"))
            removed += 1
        if removed:
            _save_device_tokens(_DEVICE_TOKENS)
    return {"revoked": removed}


# --- In-app terminal (bridge PTY) --------------------------------------------
# Contract: studio-os/docs/TERMINAL_PTY_CONTRACT.md v0. One WebSocket = one
# local PTY shell on this Mac (the bridge already runs here — no SSH). Text
# JSON both directions, UTF-8, no base64. Kernel/OSS feature too (self-serve
# ops), so it is unconditionally present. Gated by POCKET_TERMINAL_ENABLED.

def _terminal_enabled() -> bool:
    """Default ON. POCKET_TERMINAL_ENABLED=0/false/no/off/'' → endpoint 403s."""
    return os.environ.get("POCKET_TERMINAL_ENABLED", "1").strip().lower() \
        not in ("0", "false", "no", "off", "")


def _ws_bearer_token(websocket: WebSocket) -> str:
    """Same token as every other /app/v1/* call: Authorization: Bearer <t>, or
    ?token=<t> query fallback (the contract lets the bridge accept either, since
    setting headers on a WS handshake isn't always convenient on the client)."""
    auth = websocket.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (websocket.query_params.get("token") or "").strip()


def _ws_token_authorized(token: str) -> bool:
    """Accept branches mirror _check_auth: master token, a paired device token,
    or an account-bound device token. No rate-limit bookkeeping here (the WS
    handshake is not a brute-force surface the way the JSON gate is)."""
    if not token:
        return False
    if hmac.compare_digest(token, BRIDGE_TOKEN):
        return True
    with _PAIR_LOCK:
        dev = _DEVICE_TOKENS.get(token)
        if dev is not None:
            if not dev.get("apple_user_id") or _account_device_for_token(token) is not None:
                dev["last_seen"] = time.time()
                return True
    if _account_device_for_token(token) is not None:
        return True
    return False


@app.websocket("/app/v1/terminal")
async def app_v1_terminal(websocket: WebSocket):
    # Reject BEFORE accept() so Starlette answers the handshake with HTTP 403 —
    # the iOS client keys "終端機已停用"/no-retry off that status code.
    if not _terminal_enabled():
        _log_event("terminal_rejected", reason="disabled")
        await websocket.close(code=1008)
        return
    token = _ws_bearer_token(websocket)
    if not _ws_token_authorized(token):
        _log_event("terminal_rejected", reason="auth")
        await websocket.close(code=1008)
        return

    await websocket.accept()

    shell = os.environ.get("SHELL") or "/bin/zsh"
    home = os.path.expanduser("~")
    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env.pop("POCKET_TERMINAL_ENABLED", None)  # bridge-internal, don't leak into the shell

    # tmux-backed so the shell survives WS disconnects: reconnecting with the
    # same ?session=<name> re-attaches the SAME live tmux session (running agents,
    # state, scrollback all intact) instead of spawning a fresh shell. `-A` =
    # attach-or-create. Killing the client (killpg below) only detaches — the tmux
    # server keeps the session alive for the next attach. No ?session → a stable
    # default, so even the current single-terminal UX becomes persistent.
    raw_sess = (websocket.query_params.get("session") or "").strip()
    if raw_sess and await _tmux_alive(raw_sess):
        # 既有 tmux session(如 ccsess 的 "Ops"/"FLiPER")→ 直接 attach 進去,
        # 讓 app 的 SSH 連線能接到那個跑著 Claude Code/Codex 的 session。
        sess = raw_sess
    elif raw_sess:
        sess = "pocket-" + re.sub(r"[^A-Za-z0-9_-]", "_", raw_sess)[:60]
    else:
        sess = "pocket-term"

    try:
        master_fd, slave_fd = pty.openpty()
    except OSError as e:
        await websocket.send_text(json.dumps({"type": "error", "message": f"openpty failed: {e}"}))
        await websocket.close()
        return

    try:
        proc = subprocess.Popen(
            [TMUX_BIN, "new-session", "-A", "-s", sess, "-c", home],
            preexec_fn=os.setsid,               # own session+pgroup → killpg reaps only the client
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            cwd=home, env=env, close_fds=True,
        )
    except Exception as e:  # noqa: BLE001
        os.close(master_fd)
        os.close(slave_fd)
        await websocket.send_text(json.dumps({"type": "error", "message": f"spawn failed: {e}"}))
        await websocket.close()
        return
    os.close(slave_fd)  # parent keeps only the master end
    _log_event("terminal_open", device=_short_hash(token), shell=shell, tmux=sess)  # no keystrokes/output

    loop = asyncio.get_running_loop()

    def _read_master() -> bytes:
        try:
            return os.read(master_fd, 65536)
        except OSError:
            return b""                          # EIO on macOS when the child's side closes

    async def pump_output():
        """PTY → client. Ends (returns) on EOF, i.e. the shell exited."""
        while True:
            data = await loop.run_in_executor(None, _read_master)
            if not data:
                return
            try:
                await websocket.send_text(json.dumps(
                    {"type": "output", "data": data.decode("utf-8", "replace")}))
            except Exception:  # noqa: BLE001 — socket went away mid-send
                return

    async def pump_input():
        """client → PTY. Ends (returns) when the socket closes/errors."""
        while True:
            try:
                raw = await websocket.receive_text()
            except (WebSocketDisconnect, RuntimeError):
                return
            try:
                msg = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            mtype = msg.get("type")
            if mtype == "input":
                data = msg.get("data") or ""
                if data:
                    try:
                        os.write(master_fd, data.encode("utf-8"))
                    except OSError:
                        return
            elif mtype == "resize":
                try:
                    cols = max(1, min(int(msg.get("cols") or 80), 1000))
                    rows = max(1, min(int(msg.get("rows") or 25), 1000))
                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                                struct.pack("HHHH", rows, cols, 0, 0))
                except (OSError, ValueError, TypeError):
                    pass

    out_task = asyncio.create_task(pump_output())
    in_task = asyncio.create_task(pump_input())
    try:
        done, pending = await asyncio.wait(
            {out_task, in_task}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        shell_exited = out_task in done
    finally:
        # Reap the shell + its process group; closing the master fd unblocks any
        # os.read still parked in the executor thread.
        try:
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        _log_event("terminal_close", device=_short_hash(token))
    # Tell the client the shell died (only meaningful if it, not the socket, ended).
    if shell_exited and websocket.client_state == WebSocketState.CONNECTED:
        try:
            code = proc.returncode if proc.returncode is not None else 0
            await websocket.send_text(json.dumps({"type": "exit", "code": code}))
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


@app.post("/clientlog")
async def client_log_write(request: Request):
    _check_auth(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="bad json")
    os.makedirs(os.path.dirname(CLIENT_LOG), exist_ok=True)
    entry = {
        "server_ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ts": body.get("ts"),
        "level": str(body.get("level", "error"))[:16],
        "build": str(body.get("build", "?"))[:16],
        "context": str(body.get("context", ""))[:120],
        "msg": str(body.get("msg", ""))[:1000],
        "detail": str(body.get("detail", ""))[:4000],
    }
    with open(CLIENT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    # Cap the file so it can't grow unbounded (keep newest ~3000 lines).
    try:
        lines = open(CLIENT_LOG, encoding="utf-8").read().splitlines()
        if len(lines) > 3000:
            with open(CLIENT_LOG, "w", encoding="utf-8") as f:
                f.write("\n".join(lines[-3000:]) + "\n")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True}


@app.get("/clientlog")
async def client_log_read(request: Request, limit: int = 100, level: str = ""):
    _check_auth(request)
    if not os.path.exists(CLIENT_LOG):
        return {"entries": []}
    out = []
    for line in open(CLIENT_LOG, encoding="utf-8").read().splitlines()[-1000:]:
        try:
            e = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if level and e.get("level") != level:
            continue
        out.append(e)
    return {"entries": out[-limit:]}


@app.post("/codexsessions/{thread_id}/input")
async def codex_session_input(thread_id: str, request: Request):
    # 註:此端點呼叫 start_turn() → ensure_thread_loaded() → thread/resume,
    # 這裡「有」呼叫 resume,但跟 /stream 舊坑不同類——送新訊息本來就是
    # 要在這條 app-server 上真正「接管」該 thread 才能執行 turn/start,
    # resume 對這個操作是必要、無法避免的(這是 Codex 單一 writer 的本質
    # 限制,不是這支端點自己的 bug)。/stream 是唯讀回放,完全不需要接管
    # 就能用 thread/turns/list 讀到內容,所以那裡才是純粹的誤用。若使用者
    # 真的對一個「正被別的 codex app-server(ChatGPT 桌面 App/VS Code)持有」
    # 的 thread 送訊息,resume 仍可能卡住——但那是搶奪同一 thread 寫入權的
    # 固有衝突,防呆方式是 UI 層提示/衝突偵測,不是在這裡跳過 resume(跳過
    # 就送不出訊息了)。
    _check_auth(request)
    body = await _json_body(request)
    input_items = await _codex_input_items((body.get("text") or "").strip(),
                                           body.get("attachments") or [])
    if not input_items:
        raise HTTPException(status_code=400, detail="empty")
    _codex_history_invalidate(thread_id)     # new user turn → history changed
    try:
        res = await CODEX_APP.start_turn(thread_id, input_items,
                                         client_id=body.get("client_id"),
                                         cwd=body.get("cwd"))
        return {"ok": True, "thread_id": thread_id, "turn": (res or {}).get("turn")}
    except Exception as e:  # noqa: BLE001
        _codex_http_error(e)


# S3 (wave 2): Codex-side model / approval-policy switching. The app-server
# exposes `thread/settings/update` (needs the experimentalApi capability the
# bridge already requests at initialize). Live-probed against codex-cli
# 0.142.2: accepted fields include `model` and `approvalPolicy`; the policy
# enum is validated server-side as below. No global setter exists — settings
# are per-thread.
_CODEX_APPROVAL_POLICIES = ("untrusted", "on-failure", "on-request",
                            "granular", "never")


@app.post("/codexsessions/{thread_id}/settings")
async def codex_session_settings(thread_id: str, request: Request):
    """Update per-thread Codex settings. body {"model": str?,
    "approvalPolicy": "untrusted"|"on-failure"|"on-request"|"granular"|"never"}
    — at least one field required."""
    _check_auth(request)
    body = await request.json()
    params = {"threadId": thread_id}
    model = str(body.get("model") or "").strip()
    policy = str(body.get("approvalPolicy") or "").strip()
    if model:
        params["model"] = model
    if policy:
        if policy not in _CODEX_APPROVAL_POLICIES:
            raise HTTPException(status_code=400,
                                detail="approvalPolicy must be one of "
                                       + "|".join(_CODEX_APPROVAL_POLICIES))
        params["approvalPolicy"] = policy
    if len(params) == 1:
        raise HTTPException(status_code=400, detail="model or approvalPolicy required")
    try:
        await CODEX_APP.ensure_thread_loaded(thread_id)
        await CODEX_APP.call("thread/settings/update", params, timeout=15.0)
    except Exception as e:  # noqa: BLE001
        _codex_http_error(e)
    applied = {k: v for k, v in params.items() if k != "threadId"}
    _log_event("codex_settings_update", thread=thread_id[:16], **applied)
    return {"ok": True, "thread_id": thread_id, "applied": applied}


@app.post("/codexsessions/{thread_id}/interrupt")
async def codex_session_interrupt(thread_id: str, request: Request):
    _check_auth(request)
    try:
        await CODEX_APP.interrupt_turn(thread_id)
        return {"ok": True, "thread_id": thread_id}
    except Exception as e:  # noqa: BLE001
        _codex_http_error(e)


@app.get("/codexsessions/{thread_id}/history")
async def codex_session_history(thread_id: str, request: Request, limit: int = 40,
                                cursor: str | None = None):
    _check_auth(request)
    lim = max(1, min(limit, 100))
    key = (thread_id, lim, cursor or "")
    hit = _CODEX_HISTORY_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < _CODEX_HISTORY_TTL:
        _log_event("codex_history_cache_hit", thread=thread_id[:16],
                   limit=lim, cursor=bool(cursor))
        return hit[1]
    try:
        params = {
            "threadId": thread_id,
            "limit": lim,
            "itemsView": "full",
            "sortDirection": "desc",
        }
        if cursor:
            params["cursor"] = cursor
        res = await CODEX_APP.call("thread/turns/list", params, timeout=45.0)
        turns = list((res or {}).get("data", []))
        turns.reverse()
        payload = {"text": _codex_format_turns(turns),
                   "more": bool((res or {}).get("nextCursor")),
                   "nextCursor": (res or {}).get("nextCursor")}
        _CODEX_HISTORY_CACHE[key] = (time.monotonic(), payload)
        return payload
    except Exception as e:  # noqa: BLE001
        _codex_http_error(e)


@app.get("/codexsessions/{thread_id}/stream")
async def codex_session_stream(thread_id: str, request: Request, replay: int = 20,
                               follow: bool = False):
    _check_auth(request)
    cid = "codexsess-" + uuid.uuid4().hex[:16]

    def chunk(delta, finish=None):
        payload = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                   "model": thread_id, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    async def gen():
        yield chunk({"role": "assistant", "content": ""})
        # 只讀 replay 不做 thread/resume(同 _cx_card_digest 的防呆):resume 會
        # 「接管」該 thread,若它正被別的 codex app-server(如 ChatGPT 桌面
        # App、VS Code,thread source=vscode/appServer)持有就會卡死整條
        # stdio → 之後所有 codex 呼叫一起 hang(Pocket app「連線中...」卡死
        # 就是這樣引爆的)。thread/turns/list 本來就不需 resume 也讀得到
        # (/codexsessions/{id}/history 就是這樣讀的),所以這裡直接列
        # turns,不呼叫 ensure_thread_loaded()/thread/resume。真正要送
        # 新訊息時 /codexsessions/{id}/input → start_turn() 才需要
        # resume(那是必要的,因為要在這條 app-server 上真的接管+送 turn)。
        if replay > 0:
            try:
                res = await CODEX_APP.call("thread/turns/list", {
                    "threadId": thread_id,
                    "limit": max(1, min(replay, 50)),
                    "itemsView": "full",
                    "sortDirection": "desc",
                }, timeout=30.0)
                turns = list((res or {}).get("data", []))
                turns.reverse()
                text = _codex_format_turns(turns)
                if text:
                    yield chunk({"content": text})
            except Exception as e:  # noqa: BLE001
                yield chunk({"content": f"\n⚠️ history failed: {e}\n"})
        idx = 0
        if replay <= 0 and not CODEX_APP.is_active(thread_id):
            idx = len(CODEX_APP.events_for(thread_id))
        event_generation = CODEX_APP.thread_event_generations[thread_id]
        seen_turn_activity = CODEX_APP.is_active(thread_id)
        idle = 0
        idle_limit = 120 if follow else 0
        while True:
            if await request.is_disconnected():
                break
            events = CODEX_APP.events_for(thread_id)
            current_generation = CODEX_APP.thread_event_generations[thread_id]
            if current_generation != event_generation:
                event_generation = current_generation
                idx = 0
                seen_turn_activity = True
            # Defensive fallback for buffer compaction or an older producer
            # that clears the list without bumping the generation.
            if idx > len(events):
                idx = 0
                seen_turn_activity = True
            while idx < len(events):
                kind, val = events[idx]
                idx += 1
                c = _fmt_item(kind, val)
                if c:
                    yield chunk({"content": c})
            active = CODEX_APP.is_active(thread_id)
            if active:
                seen_turn_activity = True
            if _codex_stream_turn_finished(seen_turn_activity, active,
                                           idx, len(events)):
                break
            if not active and idx >= len(events) and not follow:
                break
            await asyncio.sleep(0.5)
            idle += 1
            if idle >= max(1, int(SSE_KEEPALIVE_SECS / 0.5)):
                idle = 0
                yield ": keepalive\n\n"
            if follow and idle_limit > 0 and not active:
                idle_limit -= 1
                if idle_limit <= 0:
                    break
            elif follow and active:
                idle_limit = 120
        yield chunk({}, finish="stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def _claude_argv(parent: str, prompt: str, resume: str | None = None):
    """Build a headless Claude Code argv. `resume` continues an existing CC
    session id so follow-up turns keep the sub-agent's full context."""
    mem_home = home_for(parent or "yuanfang")
    mcp_cfg = json.dumps({"mcpServers": {"studio-memory": {
        "command": "python3",
        "args": ["/Users/xcash/apps/hermes-openwebui-bridge/studio_memory_mcp.py"],
        "env": {"STUDIO_MEMORY_HOME": mem_home}}}}, ensure_ascii=False)
    hint = ("你可以用 studio-memory MCP 的 read_memory / search_memory 讀善彰的"
            "Hermes 長期記憶(身份、持倉、專案、人脈),做任務前先讀以對齊脈絡;"
            "有值得長期記住的新事實再用 write_memory 寫回。")
    argv = [CLAUDE_BIN, "-p", prompt, "--output-format", "stream-json", "--verbose",
            "--permission-mode", "bypassPermissions",
            "--mcp-config", mcp_cfg, "--append-system-prompt", hint]
    if resume:
        argv += ["--resume", resume]
    return argv


# A follow stream that has sent ZERO data (keepalives don't count) for this
# long gets disconnected — a client that hangs without reading otherwise pins
# the generator forever.
_STREAM_IDLE_CUTOFF_SECS = 1800.0

# A sub-agent that produces NOTHING on stdout for this long is stalled: kill it
# so its _BG_TASKS entry finishes instead of leaking a forever-pending task.
_AGENT_STALL_SECS = 1800.0


async def _stream_agent(sid: str, argv: list, cwd: str, fail_label: str):
    """Run a sub-agent subprocess, append its transcript to the sub's output
    buffer, capture the Claude Code session id (for later --resume), and mark
    the sub done when it exits."""
    sub = SUBSESSIONS[sid]
    out = sub["output"]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        sub["proc"] = proc
        while True:
            # No-progress watchdog: a wedged provider (network black-hole, dead
            # MCP…) otherwise streams nothing forever.
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(),
                                             timeout=_AGENT_STALL_SECS)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                sub["status"] = "stalled"
                out.append(("text", "\n⚠️ (超過 30 分鐘無輸出,已強制中止子代理行程)"))
                _log_event("subagent_stalled", sid=sid, cwd=cwd,
                           tool=sub.get("tool"))
                break
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception as e:  # noqa: BLE001
                _log_event("subagent_bad_json", sid=sid,
                           error=type(e).__name__, line=line[:160])
                continue
            sess = ev.get("session_id") if isinstance(ev, dict) else None
            if sess:
                sub["cc_session"] = sess          # latest id → resume target
            for item in _parse_agent_event(ev):
                out.append(item)
            sub["lastAt"] = time.time()
        await proc.wait()
    except Exception as e:                                  # noqa: BLE001
        out.append(("text", f"\n⚠️ {fail_label}:{e}"))
        _log_event("subagent_stream_failed", sid=sid,
                   error=type(e).__name__, error_message=str(e)[:160])
    finally:
        if sub.get("status") != "stalled":
            sub["status"] = "done"
        sub["lastAt"] = time.time()
        # Isolated dispatch: reclaim the worktree if the agent left it clean.
        if sub.get("worktree"):
            await _cleanup_worktree(sid, sub)
        _subsession_persist(sid)   # issue #5: flush transcript + resume target
        # M23: push when a dispatched CC/Codex task finishes, so the app surfaces
        # it even when backgrounded (Telegram is the fallback now, not the primary
        # signal). Fire-and-forget; failures are swallowed inside push_notify.
        _label = sub.get("name") or sub.get("tool") or "任務"
        asyncio.create_task(push_notify(
            "✅ 任務完成", str(_label)[:120],
            {"kind": "task_done", "session_id": sid}))


async def _run_dispatch(sid: str, tool: str, task: str, cwd: str, isolate: bool = False):
    """Spawn a headless Claude Code / Codex sub-agent for the initial task."""
    sub = SUBSESSIONS[sid]
    run_cwd = cwd
    if isolate:
        wt = await _make_worktree(cwd, sid)
        if wt != cwd:
            run_cwd = wt
            sub["worktree"] = wt
            sub["base_cwd"] = cwd   # fall back here if the worktree is reclaimed
            sub["cwd"] = wt   # follow-ups stay in the same isolated tree
            sub["output"].append(("text", f"_(隔離工作區 worktree:`{wt}` · 分支 `pocket/{sid}`)_\n\n"))
    if tool == "codex":
        argv = [_resolve_codex_bin(), "exec", "--json", task]
    else:
        argv = _claude_argv(sub.get("parent", "yuanfang"), task)
    await _stream_agent(sid, argv, run_cwd, "dispatch 失敗")


async def _run_resume(sid: str, prompt: str):
    """Follow-up turn into an existing sub-session — resumes the CC session so
    the sub-agent keeps its full prior context."""
    sub = SUBSESSIONS[sid]
    cwd = sub.get("cwd") or HOME_ROOT
    if sub.get("tool") == "codex":
        argv = [_resolve_codex_bin(), "exec", "--json", prompt]   # codex: new exec in same cwd
    else:
        argv = _claude_argv(sub.get("parent", "yuanfang"), prompt, resume=sub.get("cc_session"))
    await _stream_agent(sid, argv, cwd, "追問失敗")


def _parse_agent_event(ev: dict):
    """Map a Claude-Code / Codex stream-json event → transcript items."""
    items = []
    t = ev.get("type")
    if t == "assistant":
        for c in ((ev.get("message") or {}).get("content") or []):
            if c.get("type") == "text" and c.get("text"):
                items.append(("text", c["text"]))
            elif c.get("type") == "tool_use":
                name = c.get("name", "tool")
                inp = c.get("input") or {}
                cmd = inp.get("command") or inp.get("file_path") or inp.get("path") \
                    or (json.dumps(inp, ensure_ascii=False)[:120] if inp else "")
                items.append(("tool_start", {"name": name, "cmd": cmd}))
    elif t == "user":
        for c in ((ev.get("message") or {}).get("content") or []):
            if c.get("type") == "tool_result":
                res = c.get("content")
                if isinstance(res, list):
                    res = " ".join(p.get("text", "") for p in res if isinstance(p, dict))
                if res:
                    items.append(("tool_result", {"text": str(res), "status": "done"}))
    elif t in ("item.completed", "message"):  # codex-ish fallback
        txt = ev.get("text") or ev.get("content")
        if isinstance(txt, str) and txt:
            items.append(("text", txt))
    return items


def _persona_preview_tg(home: str):
    """Latest user-visible message of the persona's Telegram session → (text, ts).
    Walks back past rows that are pure runtime injection so the conversation list
    never previews machine-facing preamble."""
    import sqlite3
    db = os.path.join(home, "state.db")
    if not os.path.exists(db):
        return (None, None)
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        cur = con.execute(
            "SELECT m.role, m.content, m.timestamp FROM messages m "
            "JOIN sessions s ON s.id = m.session_id "
            "WHERE s.source='telegram' AND m.role IN ('user','assistant') "
            "AND m.content IS NOT NULL AND m.content != '' "
            "ORDER BY m.timestamp DESC LIMIT 10")
        rows = cur.fetchall()
        con.close()
        for role, content, ts in rows:
            text, _atts = _tg_extract_attachments(str(content))
            if role == "user":
                text = _tg_clean_content(text)
                if text is None:
                    continue
            if text:
                return (text[:80], ts)
    except Exception:
        pass
    return (None, None)


def _persona_preview_canon(session: str):
    """Latest user-visible message from the persona's CANONICAL store (app turns)
    → (text, ts). studio-card fences collapse to their fallback text and the
    folded 〈執行步驟〉 appendix is stripped, matching what the conversation view
    shows — so the list preview never leaks a raw fence or tool log."""
    try:
        msgs = _canon_messages(session, 20)
    except Exception:  # noqa: BLE001
        return (None, None)
    for m in reversed(msgs):
        if m.get("role") not in ("user", "assistant"):
            continue
        content = (m.get("content") or "")
        if not content.strip():
            continue
        clean, _bodies = carddigest.extract_studio_cards(content)
        clean = re.sub(r"<details>.*?</details>", "", clean, flags=re.S).strip()
        if clean:
            return (clean[:80], m.get("ts"))
    return (None, None)


def _persona_preview(home: str, session: str | None = None):
    """Conversation-list preview for a persona → (text, ts).

    The list must mirror the merged conversation view (/app/v1/messages =
    canonical ⊕ Telegram): reading TG only left app-side turns invisible, so the
    preview stayed stale AND the app's preview-change unread detector never fired
    for in-app messages. Take the newer of the two sources (both epoch seconds)."""
    cands = []
    tg_text, tg_ts = _persona_preview_tg(home)
    if tg_text and tg_ts is not None:
        cands.append((tg_ts, tg_text))
    if session:
        cn_text, cn_ts = _persona_preview_canon(session)
        if cn_text and cn_ts is not None:
            cands.append((cn_ts, cn_text))
    if not cands:
        return (None, None)
    ts, text = max(cands, key=lambda x: x[0])
    return (text, ts)


def _persona_preview_merged(session: str, home: str):
    """Conversation-list preview drawn from the SAME merged source as the card
    stream (canonical ⊕ Telegram ⊕ cron reports), PLUS who sent the latest line
    and the last *inbound* (persona) line.

    Returns (latest_text, latest_ts, sender, inbound_text, inbound_ts) where
    `sender` is "persona" (assistant) | "user" | None.

    Why the extra fields: in a two-sided TG-style chat the newest message is
    often the user's own send. The client's bell/unread detector keys off
    `inbound_ts` so a self-send never lights the dot, and the notification
    subtitle shows `inbound_text` so it never echoes the user's own words.

    Skips the report *sync* (`_sync_persona_reports`) — the 30s card follower
    already keeps report_events fresh — so this is just a few cheap read-only
    sqlite queries, safe to run on every /sessions poll."""
    msgs = []
    try:
        msgs.extend(_canon_messages(session, 30))          # app turns (canonical.db)
    except Exception:  # noqa: BLE001
        pass
    try:
        msgs.extend(_persona_history(home, 30))            # Telegram (state.db), user-cleaned
    except Exception:  # noqa: BLE001
        pass
    try:
        msgs.extend(_report_messages(session, 10))         # cron briefs (role=assistant)
    except Exception:  # noqa: BLE001
        pass

    def _visible(m) -> str | None:
        if m.get("role") not in ("user", "assistant"):
            return None
        clean, _bodies = carddigest.extract_studio_cards(m.get("content") or "")
        clean = re.sub(r"<details>.*?</details>", "", clean, flags=re.S).strip()
        return clean or None

    msgs.sort(key=lambda m: m.get("ts") or 0)
    latest_text = latest_ts = sender = None
    inbound_text = inbound_ts = None
    for m in reversed(msgs):
        text = _visible(m)
        if not text:
            continue
        if latest_text is None:
            latest_text, latest_ts = text[:120], m.get("ts")
            sender = "persona" if m.get("role") == "assistant" else "user"
        if m.get("role") == "assistant" and inbound_text is None:
            inbound_text, inbound_ts = text[:120], m.get("ts")
        if latest_text is not None and inbound_text is not None:
            break
    return (latest_text, latest_ts, sender, inbound_text, inbound_ts)


# TG→app media (N4, pocketagent#36): Hermes' image_routing appends a
# `[Image attached at: /local/path]` hint line to the stored user text for
# every photo the TG gateway downloads (into <home>/image_cache). state.db has
# no media column, so those hint lines ARE the media record — parse them back
# into real attachments.
_TG_IMAGE_MARKER = re.compile(r"\[Image attached at: ([^\]\n]+)\]")
# Replied-to media cached by the TG gateway (gateway/platforms/telegram.py:5796):
# [Replied-to image 'file_36.jpg' saved at: /path]
_TG_REPLIED_MEDIA = re.compile(r"\[Replied-to (\w+) '([^']*)' saved at: ([^\]\n]+)\]")
# TG→app files (承 N4 思路,補齊照片以外的三類):gateway 對 document/audio/
# video 各寫一種 saved-at 提示行(gateway/run.py:1838-1848/8646/8665),同樣
# 「提示行即媒體記錄」— 解析回一等附件。app 端 Attachment.Kind 只有
# image/file/audio:document/video → file(video 帶 video/* mime),audio → audio。
_TG_TEXTDOC_NOTE = re.compile(
    r"\[The user sent a text document: '([^']*)'\.(?s:.*?)also saved at: ([^\]\n]+)\]")
_TG_FILE_NOTE = re.compile(
    r"\[The user sent (a document|an audio file attachment|a video attachment): "
    r"'([^']*)'\. It is saved at: (.+?)\.\s(?s:.*?)\]")


def _tg_extract_attachments(content: str):
    """Split a TG-side message into (display_text, attachments).

    Attachments use the SAME shape the app already renders for app-sent turns
    ({kind, filename, mime, path} — see att_meta in POST /app/v1/messages);
    the app fetches `path` through the existing GET /file endpoint, so no new
    media endpoint and no app-side decoding change is needed. Markers whose
    file has since been pruned from image_cache become a short human-readable
    note — the raw path marker is engineering language the app must not show."""
    attachments: list = []

    def _repl(m):
        path = m.group(1).strip()
        if path and os.path.isfile(path):
            attachments.append({
                "kind": "image",
                "filename": os.path.basename(path),
                "mime": mimetypes.guess_type(path)[0] or "image/jpeg",
                "path": path,
            })
            return ""
        return "（附件圖片已失效）"

    def _repl_replied(m):
        kind, name, path = m.group(1), m.group(2).strip(), m.group(3).strip()
        if path and os.path.isfile(path):
            att_kind = {"image": "image", "audio": "audio", "voice": "audio"}.get(kind, "file")
            attachments.append({
                "kind": att_kind,
                "filename": name or os.path.basename(path),
                "mime": mimetypes.guess_type(path)[0]
                        or ("image/jpeg" if att_kind == "image" else "application/octet-stream"),
                "path": path,
            })
        return ""      # engineering note either way — never shown as text

    def _att_for(path: str, name: str, kind_hint: str) -> str:
        """共用落點:檔案在就掛附件(回空字串),不在就人話註記。"""
        if path and os.path.isfile(path):
            mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            kind = "audio" if kind_hint == "audio" else "file"
            attachments.append({"kind": kind,
                                "filename": name or os.path.basename(path),
                                "mime": mime, "path": path})
            return ""
        return f"（附件『{name or os.path.basename(path or '')}』已失效）"

    def _repl_textdoc(m):
        # 內文已 inline 在下方 → 只把提示行變附件,正文保留。
        return _att_for(m.group(2).strip(), m.group(1).strip(), "file")

    def _repl_file(m):
        what, name, path = m.group(1), m.group(2).strip(), m.group(3).strip()
        hint = "audio" if what.startswith("an audio") else "file"
        return _att_for(path, name, hint)

    text = _TG_IMAGE_MARKER.sub(_repl, content or "")
    text = _TG_REPLIED_MEDIA.sub(_repl_replied, text)
    text = _TG_TEXTDOC_NOTE.sub(_repl_textdoc, text)
    text = _TG_FILE_NOTE.sub(_repl_file, text)
    if text != (content or ""):     # something was extracted or replaced
        # Collapse the blank lines the removed hint lines leave behind.
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text, attachments
    return content, attachments


# ── persona message de-noising ──────────────────────────────────────────────
# Hermes (gateway/run.py, acp_adapter/server.py, tools/process_registry.py) and
# this bridge itself wrap the user's actual words in machine-facing preambles
# before storing them in state.db. The app must show ONLY what the user really
# said. Every rule below is anchored to the exact producer format found in the
# Hermes source; anything unrecognized passes through UNCHANGED (better to leak
# a wrapper than to eat a user's words).

# Block wrappers: (open marker, end-of-preamble marker). The user text is what
# follows the end marker.
_TG_TEMPORAL_OPEN = "[Internal runtime time context"          # gateway/run.py:727, acp_adapter/server.py:143
_TG_TEMPORAL_CLOSE = "[/Internal runtime time context]"
_TG_TEMPORAL_BLOCK = re.compile(
    re.escape(_TG_TEMPORAL_OPEN) + r"(?s:.*?)" + re.escape(_TG_TEMPORAL_CLOSE))
_TG_REPORT_OPEN = "【PocketAgent 近期報告上下文】"                 # bridge _report_context_for_prompt
_TG_REPORT_USER = "【使用者現在的訊息】"
_TG_REPORT_BLOCK = re.compile(
    re.escape(_TG_REPORT_OPEN) + r"(?s:.*?)" + re.escape(_TG_REPORT_USER) + r"\n?")
_TG_OBSERVED_OPEN = "[Observed Telegram group context - context only, not requests]"   # gateway/run.py:691
_TG_OBSERVED_USER = ("[Current addressed message - answer only this unless it "
                     "explicitly asks you to use the observed context]")               # gateway/run.py:692

# Inline / whole-message patterns.
_TG_VOICE_TRANSCRIPT = re.compile(                    # gateway/run.py:12812
    r'\[The user sent a voice message~\s*Here\'s what they said: "((?s:.*?))"\]')
_TG_VOICE_NOTE = re.compile(                          # path/duration + failure variants, run.py:12786-12843
    r"\[The user sent a voice message(?: but |: )(?s:[^\]]*)\]")
_TG_REPLY_QUOTE = re.compile(                         # gateway/run.py:8713, whatsapp.py:1154
    r'\[Replying to: "(?s:.*?)"\][ \t]*\n*')          # unanchored: merged rows carry it mid-text
_TG_BG_PROCESS_OPEN = "[IMPORTANT: Background process "   # tools/process_registry.py:1637/1668
_TG_TIMESTAMP_PREFIX = re.compile(                    # gateway/message_timestamps.py:85 (config-gated)
    r"^\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [^\]]{1,12}\]\s*")
# Header lines left behind once a block wrapper is stripped, and the image
# placeholder Hermes stores in place of raw pixels (run_agent.py:1632).
_TG_NOISE_LINES = {"[User message]", "[User message and attachments follow]",
                   "[screenshot]"}


def _tg_clean_content(text: str):
    """Strip machine-facing wrappers from a TG-side USER message.

    Returns the user's actual words, or None when the whole row is internal
    (pure runtime injection with no user content). Unrecognized formats are
    returned unchanged — this function may under-clean, never over-delete."""
    if not text:
        return None
    out = text
    # Temporal-context blocks: removed WHEREVER they sit — normally a prefix,
    # but queued/merged turns leave them mid-text and some rows carry them as
    # a suffix after the user's words. An open marker without its close is an
    # unknown format → left untouched.
    out = _TG_TEMPORAL_BLOCK.sub("\n\n", out)
    # Bridge report-context injections: each block runs from its exact header
    # to the 【使用者現在的訊息】 marker; merged multi-turn rows carry SEVERAL
    # such blocks with real user text between them, so remove every pair and
    # keep all the text segments. A trailing header with no marker after it
    # (older append-style prompt) is cut to end-of-text.
    out = _TG_REPORT_BLOCK.sub("\n\n", out)
    i = out.find(_TG_REPORT_OPEN)
    if i != -1 and _TG_REPORT_USER not in out[i:]:
        out = out[:i]
    # Observed-group-context preamble (prefix-anchored, per producer).
    s = out.lstrip()
    if s.startswith(_TG_OBSERVED_OPEN):
        i = s.find(_TG_OBSERVED_USER)
        if i != -1:
            out = s[i + len(_TG_OBSERVED_USER):]
    s = out.lstrip()
    # Whole-row internal notification (tool → agent, zero user content).
    if s.startswith(_TG_BG_PROCESS_OPEN):
        return None
    # Voice: keep the transcript (that IS what the user said), drop the frame;
    # untranscribable-voice notes are agent-facing → noise.
    out = _TG_VOICE_TRANSCRIPT.sub(r"\1", out)
    out = _TG_VOICE_NOTE.sub("", out)
    # Reply-quote preamble (the quoted snippet is the OTHER side's text).
    out = _TG_REPLY_QUOTE.sub("", out.lstrip())
    # Optional per-message timestamp prefix (config-gated, default off).
    out = _TG_TIMESTAMP_PREFIX.sub("", out)
    # Line-level scrub of leftover header/placeholder lines.
    lines = [ln for ln in out.splitlines() if ln.strip() not in _TG_NOISE_LINES]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out or None


def _persona_history(home: str, limit: int = 100):
    """Full recent transcript of the persona's canonical Telegram session, so a
    fresh app install / new device can render the conversation instead of a
    blank thread. Returns oldest→newest [{role, content, ts, attachments}]."""
    import sqlite3
    db = os.path.join(home, "state.db")
    if not os.path.exists(db):
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        cur = con.execute(
            "SELECT m.role, m.content, m.timestamp FROM messages m "
            "JOIN sessions s ON s.id = m.session_id "
            "WHERE s.source='telegram' AND m.role IN ('user','assistant') "
            "AND m.content IS NOT NULL AND m.content != '' "
            "ORDER BY m.timestamp DESC LIMIT ?", (limit,))
        rows = cur.fetchall()
        con.close()
        rows.reverse()  # oldest → newest for natural top-to-bottom rendering
        out = []
        for r in rows:
            text, atts = _tg_extract_attachments(r[1])
            if r[0] == "user":
                # 前台只呈現使用者真正說的話:剝掉 runtime context 等機器面
                # 包裹;整條都是內部注入(剝完全空)且無附件 → 不出現。
                text = _tg_clean_content(text)
                if text is None and not atts:
                    continue
            out.append({"role": r[0], "content": text or "", "ts": r[2],
                        "attachments": atts})
        return out
    except Exception as e:  # noqa: BLE001
        # A broken state.db read renders the persona thread empty on every
        # device; that deserves a log line, not silence (issue #7).
        _log_event("persona_history_read_failed", home=home,
                   error=type(e).__name__, error_message=str(e)[:160])
        return []


@app.get("/sessions")
async def list_sessions(request: Request):
    """Unified conversation list: personas (pinned) + dispatched sub-sessions."""
    _check_auth(request)
    out = []
    for mid, (disp, home) in PERSONAS.items():
        text, ts, sender, in_text, in_ts = _persona_preview_merged(mid, home)
        out.append({"id": mid, "type": "persona", "name": disp,
                    "preview": text, "lastAt": ts, "status": "idle",
                    # who sent `preview` (persona|user) + the last inbound line,
                    # so the app's bell/unread + notification subtitle stay role-
                    # aware instead of echoing the user's own last message.
                    "sender": sender, "inboundPreview": in_text, "inboundAt": in_ts})
    out.extend(await _delegation_app_sessions())
    for key, s in SUBSESSIONS.items():
        out.append({"id": key, "type": "subprocess", "name": s.get("name"),
                    "parent": s.get("parent"), "tool": s.get("tool"),
                    "preview": s.get("preview"), "lastAt": s.get("lastAt"),
                    "status": s.get("status", "running")})
    return {"sessions": out}


@app.get("/sessions/{persona}/messages")
async def persona_messages(persona: str, request: Request, limit: int = 100):
    """Server-side persona history (from Hermes state.db) so the app can seed a
    conversation that survives reinstall / new device, not just local storage."""
    _check_auth(request)
    if persona not in PERSONAS:
        raise HTTPException(status_code=404, detail="unknown persona")
    _, home = PERSONAS[persona]
    return {"messages": _persona_history(home, max(1, min(limit, 500)))}


# ───────────────────────── ccsess remote Claude Code sessions ──────────────
# Persistent `claude --remote-control` sessions (managed by ~/.local/bin/ccsess
# in tmux). The app reads each session's live transcript jsonl directly and can
# type into it via tmux send-keys — same live view/control as SSH-ing in.

CCSESS_CONF = os.path.expanduser(os.environ.get("CCSESS_CONF", "~/.config/ccsess/sessions.conf"))
TMUX_BIN = "/opt/homebrew/bin/tmux" if os.path.exists("/opt/homebrew/bin/tmux") else "tmux"
POCKET_CC_TMUX = os.environ.get("POCKET_CC_TMUX", "pocket-cc")
POCKET_AGENT_LANES = os.path.join(os.path.dirname(CCSESS_CONF), "pocket-agent-lanes.json")
_CC_HOOK_STATE: dict[str, dict] = {}
_CC_HOOK_TTL = 600.0

# P0 修復(2026-07-10,root cause #3 — "Escape 打錯 turn"):
# 每個 session 一個單調遞增的 turn 世代編號,由 UserPromptSubmit hook 事件遞增
# (代表一個新 turn 開始了)。_cc_interrupt_core 在自己的 3 次重試迴圈中,每次
# 送出 Escape 前後都會比對這個世代編號 —— 如果編號在等待期間變了,代表原本
# 想中斷的那個 turn 已經結束、一個新 turn 已經開始,這時再送 Escape 極可能誤
# 打進新 turn 的 Bash 工具執行期間,讓 CLI 誤判為「使用者拒絕工具呼叫」,進而
# 讓那個新 turn 掉進無限期等待使用者回覆的假死狀態(過去實測卡過 5.4 / 13.5
# 小時)。偵測到世代已變就立刻停手,不再送下一次 Escape。
_CC_TURN_GEN: dict[str, int] = {}

# Hard ceiling for any single tmux invocation. tmux normally answers in ms; a
# hung tmux server used to hang the handler (and its _BG_TASKS entry) forever.
_TMUX_TIMEOUT = 15.0


async def _tmux_run(*args, timeout: float = _TMUX_TIMEOUT):
    """Run one tmux command with a kill-on-timeout guard.
    Returns (rc, stdout_str, stderr_str); rc=124 on timeout."""
    p = await asyncio.create_subprocess_exec(
        TMUX_BIN, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            p.kill()
        except ProcessLookupError:
            pass
        _log_event("tmux_timeout", args=" ".join(str(a) for a in args[:4]),
                   timeout_s=timeout)
        return 124, "", "tmux timed out"
    return (p.returncode,
            (out or b"").decode("utf-8", "replace"),
            (err or b"").decode("utf-8", "replace").strip())


# TTL cache for capture-pane (issue #8): the home list polls every session on
# every request — 50 sessions used to mean 50 subprocess spawns per poll.
_PANE_CACHE_TTL = 5.0
_PANE_CACHE: dict = {}   # name -> (cached_at_monotonic, pane_text)


async def _tmux_capture_cached(name: str) -> str:
    now = time.monotonic()
    hit = _PANE_CACHE.get(name)
    if hit and now - hit[0] < _PANE_CACHE_TTL:
        return hit[1]
    _, pane, _ = await _tmux_run("capture-pane", "-p", "-t", name)
    _PANE_CACHE[name] = (now, pane)
    return pane


def _cc_project_dir(workdir: str) -> str:
    return os.path.expanduser("~/.claude/projects/" + workdir.replace("/", "-"))


def _cc_latest_jsonl(workdir: str):
    files = glob.glob(os.path.join(_cc_project_dir(workdir), "*.jsonl"))
    return max(files, key=os.path.getmtime) if files else None


# ─── CC session 身分(per-session jsonl)────────────────────────────────────
# 「session 身分 = 工作目錄」是身分混淆 bug 的根:同 workdir 的兩個 tmux session
# (如 Main 與 cc-51a85f55)全被 dir-latest jsonl 代表,誰最後寫誰就是全目錄——
# 清單/status/stream/卡片流全部混流。正解:從 tmux pane 的子行程樹找 claude 的
# cmdline,parse --resume/--session-id 的 uuid → <projects>/<slug>/<uuid>.jsonl。
# 實測本機 pgrep -P 對部分 pane 回空,所以用一次性 ps 快照(TTL 共用)。
_CC_SID_RE = re.compile(r"--(?:resume|session-id)\s+([0-9a-fA-F][0-9a-fA-F-]{7,63})")
_CC_SID_CACHE: dict = {}   # name -> (cached_at_monotonic, sid_or_None)
_CC_SID_TTL = 30.0         # claude 行程在 session 生命週期內穩定;None 也快取避免狂掃
_CC_SID_PINS: dict[str, str] = {}     # name -> hook-confirmed current sid
_CC_SID_HISTORY: dict[str, list[str]] = {}  # name -> recent known sid chain
_CC_SID_HISTORY_MAX = 8
_PS_SNAP = (0.0, {})       # (cached_at, {pid: (ppid, command)})
_PS_SNAP_TTL = 5.0


def _cc_valid_sid(sid: str | None) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F][0-9a-fA-F-]{7,63}", sid or ""))


def _cc_note_sid(name: str, sid: str | None) -> None:
    if not name or not _cc_valid_sid(sid):
        return
    hist = _CC_SID_HISTORY.setdefault(name, [])
    if sid in hist:
        hist.remove(sid)
    hist.append(str(sid))
    del hist[:-_CC_SID_HISTORY_MAX]


def _cc_cache_sid(name: str, sid: str | None, *, now: float | None = None,
                  pin: bool = False) -> None:
    now = time.monotonic() if now is None else now
    _CC_SID_CACHE[name] = (now, sid)
    _cc_note_sid(name, sid)
    if pin and _cc_valid_sid(sid):
        _CC_SID_PINS[name] = str(sid)


def _cc_write_resume_pin(name: str, sid: str) -> None:
    if not name or not _cc_valid_sid(sid):
        return
    pdir = os.path.expanduser("~/.config/ccsess/resume")
    os.makedirs(pdir, exist_ok=True)
    ptmp = os.path.join(pdir, name + ".tmp")
    with open(ptmp, "w") as f:
        f.write(sid + "\n")
    os.replace(ptmp, os.path.join(pdir, name))


def _cc_remote_control_pin_path(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name or "")
    return os.path.expanduser(f"~/.config/ccsess/remote-control/{safe}")


def _cc_remote_debug_path(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name or "")
    path = os.path.expanduser(f"~/.local/share/ccsess/logs/remote-control-{safe}.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _cc_write_remote_control_pin(name: str, display_name: str | None = None) -> bool:
    """Enable Claude App remote-control for a ccsess-managed tmux lane.

    Returns True when the pin changed. A running bare `claude --resume` process
    still needs a restart before the official app can see the lane.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name or "")
    if not safe:
        return False
    pdir = os.path.expanduser("~/.config/ccsess/remote-control")
    os.makedirs(pdir, exist_ok=True)
    path = os.path.join(pdir, safe)
    value = (display_name or name).strip() or safe
    old = None
    try:
        with open(path, encoding="utf-8") as f:
            old = f.read().strip()
    except FileNotFoundError:
        pass
    if old == value:
        return False
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(value + "\n")
    os.replace(tmp, path)
    return True


def _cc_remote_resume_argv(name: str, sid: str) -> list[str]:
    return [
        CLAUDE_BIN,
        "--resume", sid,
        "--remote-control", name,
        "--debug-file", _cc_remote_debug_path(name),
    ]


def _cc_reseed_pins_from_files() -> int:
    """啟動時把 ~/.config/ccsess/resume/<name> 的 sid 重載回 _CC_SID_PINS。

    根因(2026-07-16 cc-51a85f55 不同步案):`claude --resume <舊id>` 續聊會
    寫進**新**的 session 檔,但行程 cmdline 永遠停在啟動時的 `--resume <舊id>`。
    於是 _cc_pane_session_id 解 cmdline 拿到凍結的舊 sid,服務凍結的舊 jsonl。
    hook(UserPromptSubmit)每回合帶真正當前 session_id 覆寫 _CC_SID_PINS 並
    落地 resume-pin 檔來補這個洞——但 _CC_SID_PINS 是**記憶體態**,bridge 一
    重啟就清空,直到該 session 下次送 prompt 才重建。這中間的盲窗會讓 app 顯示
    舊內容(這次正是部署 scope-v2 重啟後、使用者剛好在盲窗內開 cc-51a85f55)。
    修法:啟動即從 pin 檔 reseed,盲窗歸零;pin 指向的 jsonl 若不存在,
    _cc_session_jsonl 仍會優雅 fallback,所以這裡只驗 sid 格式。"""
    pdir = os.path.expanduser("~/.config/ccsess/resume")
    seeded = 0
    try:
        names = os.listdir(pdir)
    except Exception:  # noqa: BLE001
        return 0
    for name in names:
        if name.endswith(".tmp"):
            continue
        try:
            with open(os.path.join(pdir, name)) as f:
                sid = f.read().strip()
        except Exception:  # noqa: BLE001
            continue
        if _cc_valid_sid(sid):
            _CC_SID_PINS[name] = sid
            _cc_note_sid(name, sid)
            seeded += 1
    return seeded


async def _ps_snapshot():
    global _PS_SNAP
    now = time.monotonic()
    if now - _PS_SNAP[0] < _PS_SNAP_TTL:
        return _PS_SNAP[1]
    p = await asyncio.create_subprocess_exec(
        "/bin/ps", "-axo", "pid=,ppid=,command=",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), 10.0)
    except asyncio.TimeoutError:
        try:
            p.kill()
        except ProcessLookupError:
            pass
        return _PS_SNAP[1]
    procs = {}
    for line in (out or b"").decode("utf-8", "replace").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            procs[int(parts[0])] = (int(parts[1]), parts[2])
    _PS_SNAP = (now, procs)
    return procs


async def _cc_pane_session_id(name: str):
    """tmux pane 子行程樹裡 claude 的 --resume/--session-id uuid;失敗回 None。"""
    now = time.monotonic()
    pinned = _CC_SID_PINS.get(name)
    if pinned:
        _cc_cache_sid(name, pinned, now=now)
        return pinned
    hit = _CC_SID_CACHE.get(name)
    if hit and now - hit[0] < _CC_SID_TTL:
        return hit[1]
    sid = None
    try:
        rc, out, _ = await _tmux_run("list-panes", "-t", name, "-F", "#{pane_pid}")
        pane_pid = int(out.split()[0]) if rc == 0 and out.strip() else 0
        if pane_pid:
            procs = await _ps_snapshot()
            kids: dict = {}
            for pid, (ppid, _cmd) in procs.items():
                kids.setdefault(ppid, []).append(pid)
            stack, seen = [pane_pid], set()
            while stack:                      # 走整棵子孫樹(claude 可能包在 zsh 下)
                pid = stack.pop()
                if pid in seen:
                    continue
                seen.add(pid)
                cmd = procs.get(pid, (0, ""))[1]
                if "claude" in cmd:
                    m = _CC_SID_RE.search(cmd)
                    if m:
                        sid = m.group(1)
                        break
                stack.extend(kids.get(pid, []))
    except Exception:  # noqa: BLE001
        sid = None
    _cc_cache_sid(name, sid, now=now)
    return sid


async def _cc_pane_has_remote_control(name: str) -> bool:
    """Return True when the live Claude process under this pane advertises
    Claude App remote-control for the expected ccsess name."""
    try:
        rc, out, _ = await _tmux_run("list-panes", "-t", name, "-F", "#{pane_pid}")
        pane_pid = int(out.split()[0]) if rc == 0 and out.strip() else 0
        if not pane_pid:
            return False
        procs = await _ps_snapshot()
        kids: dict = {}
        for pid, (ppid, _cmd) in procs.items():
            kids.setdefault(ppid, []).append(pid)
        stack, seen = [pane_pid], set()
        remote_arg = f"--remote-control {name}"
        remote_eq = f"--remote-control={name}"
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            cmd = procs.get(pid, (0, ""))[1]
            if "claude" in cmd and (remote_arg in cmd or remote_eq in cmd):
                return True
            stack.extend(kids.get(pid, []))
    except Exception:  # noqa: BLE001
        return False
    return False


async def _cc_session_jsonl(name: str, workdir: str):
    """這個 ccsess 的專屬 jsonl:pane 行程 sid 優先,失敗才 fallback dir-latest。
    已知限制:TUI 內 /clear 或 /resume 會讓 cmdline 的 uuid 過期(行程不重啟、
    實際寫新 jsonl)——hook(/ccsessions/_hook)帶的 session_id 會即時覆寫
    _CC_SID_CACHE 來補這個洞(UserPromptSubmit 每回合都帶最新 sid,權威)。"""
    sid = await _cc_pane_session_id(name)
    if sid:
        p = os.path.join(_cc_project_dir(workdir), sid + ".jsonl")
        if os.path.exists(p):
            return p
        p = _cchist_find(sid)      # slug 正規化差異時跨 project glob
        if p:
            return p
    return _cc_latest_jsonl(workdir)   # 向下相容:找不到行程/parse 失敗/裸 claude


def _cc_conf_rows():
    rows = []
    try:
        with open(CCSESS_CONF) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                if len(parts) >= 3:
                    rows.append((parts[0], parts[1], parts[2].strip()))
    except Exception:  # noqa: BLE001
        pass
    return rows


def _cc_conf_upsert(name: str, workdir: str, enabled: str = "1") -> None:
    """Update sessions.conf with the same lock convention as ccsess.

    Used only for explicit `--resume <sid>` sessions when ccsess' same-workdir
    guard rejects a fixed lane. Those launches do not rely on `--continue`, so
    the original same-workdir footgun does not apply.
    """
    if not name:
        return
    os.makedirs(os.path.dirname(CCSESS_CONF), exist_ok=True)
    lock = CCSESS_CONF + ".lock"
    deadline = time.monotonic() + 5.0
    while True:
        try:
            os.mkdir(lock)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise http_err(502, "CCSESS_CONF_LOCKED",
                               "ccsess config lock timeout",
                               "sessions.conf lock timeout")
            time.sleep(0.1)
    try:
        try:
            with open(CCSESS_CONF, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            lines = []
        out = []
        found = False
        for line in lines:
            if not line or line.startswith("#"):
                out.append(line)
                continue
            parts = line.split("|")
            if parts and parts[0] == name:
                out.append(f"{name}|{workdir}|{enabled}")
                found = True
            else:
                out.append(line)
        if not found:
            out.append(f"{name}|{workdir}|{enabled}")
        tmp = f"{CCSESS_CONF}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(out).rstrip() + "\n")
        os.replace(tmp, CCSESS_CONF)
    finally:
        try:
            os.rmdir(lock)
        except OSError:
            pass


async def _cc_register_explicit_resume(name: str, workdir: str) -> None:
    try:
        await _run_ccsess("register", name, workdir)
        return
    except HTTPException as e:
        detail = str(getattr(e, "detail", ""))
        if "同目錄" not in detail and "workdir" not in detail:
            raise
        _log_event("ccsess_register_duplicate_workdir_resume_upsert",
                   session=name, cwd_hash=_short_hash(workdir))
    _cc_conf_upsert(name, workdir, "1")


# App-owned CC sessions registry. CCSESS_CONF is shared with the ccsess CLI
# (daemon sessions like "Culture Supply"/"Ops"/"FLiPER" live there too), and its
# `name|workdir|enabled` format is read by many 3-tuple callers — so instead of
# adding a 4th field we keep a SEPARATE bridge-managed list of the CC sessions
# THIS app created (via POST /ccsessions). The approval watcher only pushes for
# these, so a foreign ccsess session's TUI prompt never reaches the app's審核中心
# / push. One name per line.
APP_OWNED_CC = os.path.join(os.path.dirname(CCSESS_CONF), "app-owned.txt")
# 審核作用域 v2(2026-07-16):舊制「只掃 app 開的 session」在那批 session 死光
# 後名單清空,watcher 六天零產出 —— 聊天窗選項卡/審核中心/推播整條斷炊。
# 新制:enabled 的 ccsess 一律在作用域內,除非列進排除檔(一行一名,# 註解)。
# app-owned 仍保留(app 開的必收,即使之後改預設也不受影響)。
APPROVALS_EXCLUDE = os.path.join(os.path.dirname(CCSESS_CONF), "approvals-exclude.txt")


def _cc_app_owned_names() -> set:
    try:
        with open(APP_OWNED_CC) as f:
            return {ln.strip() for ln in f if ln.strip() and not ln.startswith("#")}
    except Exception:  # noqa: BLE001
        return set()


def _cc_approvals_excluded() -> set:
    try:
        with open(APPROVALS_EXCLUDE) as f:
            return {ln.strip() for ln in f if ln.strip() and not ln.startswith("#")}
    except Exception:  # noqa: BLE001
        return set()


def _cc_approval_scope_names() -> set:
    """審核外送作用域 = (enabled ccsess ∪ app-owned) − 排除檔。"""
    enabled = {name for name, _wd, en in _cc_conf_rows() if en == "1"}
    return (enabled | _cc_app_owned_names()) - _cc_approvals_excluded()


def _cc_mark_app_owned(name: str) -> None:
    """Record that the app opened this CC session (idempotent append)."""
    name = (name or "").strip()
    if not name or name in _cc_app_owned_names():
        return
    try:
        os.makedirs(os.path.dirname(APP_OWNED_CC), exist_ok=True)
        with open(APP_OWNED_CC, "a") as f:
            f.write(name + "\n")
    except Exception as e:  # noqa: BLE001
        _log_event("cc_app_owned_write_failed", session=name, error=str(e)[:160])


def _norm_cc_workdir(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path or "")))


def _cc_name_for_cwd(cwd: str | None):
    if not cwd:
        return None
    target = _norm_cc_workdir(cwd)
    for name, workdir, _enabled in _cc_conf_rows():
        if _norm_cc_workdir(workdir) == target:
            return name
    return None


def _cc_names_for_cwd(cwd: str | None) -> list:
    """同 workdir 的所有 conf 名(撞 workdir 時 hook 需要用 session_id 消歧)。"""
    if not cwd:
        return []
    target = _norm_cc_workdir(cwd)
    return [n for n, w, _e in _cc_conf_rows() if _norm_cc_workdir(w) == target]


def _cc_fresh_hook_state(name: str):
    state = _CC_HOOK_STATE.get(name)
    if not state:
        return None
    try:
        if time.time() - float(state.get("updated_at") or 0) <= _CC_HOOK_TTL:
            return state
    except Exception:  # noqa: BLE001
        return None
    return None


async def _tmux_alive(name: str) -> bool:
    try:
        rc, _, _ = await _tmux_run("has-session", "-t", name)
        return rc == 0
    except Exception:  # noqa: BLE001
        return False


_cc_tail_cache: dict = {}   # jsonl path -> (mtime, preview)


def _cc_tail_preview(jsonl: str) -> str:
    """Transcript 尾巴 64KB 反向掃,抽最後一則 user/assistant 可讀文字。
    tool_result/系統包裹(list 無 text 塊、'<'開頭)自然跳過。"""
    try:
        size = os.path.getsize(jsonl)
        with open(jsonl, "rb") as f:
            if size > 65536:
                f.seek(-65536, os.SEEK_END)
            chunk = f.read().decode("utf-8", "replace")
        for line in reversed(chunk.splitlines()):
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if d.get("type") not in ("user", "assistant"):
                continue
            content = (d.get("message") or {}).get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "text" \
                            and (blk.get("text") or "").strip():
                        text = blk["text"]
                        break
            text = (text or "").strip()
            if text and not text.startswith("<") and not text.startswith("Caveat:"):
                return text[:160]
    except Exception:  # noqa: BLE001
        pass
    return ""


def _cc_last_activity(jsonl):
    """(mtime, preview) — mtime 供 recency 排序;preview 改為真的從 transcript
    尾巴抽最後訊息(2026-07-15 前這裡永遠回空字串,app 端 SentLog 優先又讓
    列表凍結在「你上次從 app 送的那句」——終端機工作的 session 預覽永不更新)。
    以 (path, mtime) 快取,檔案沒動就零讀取。"""
    if not jsonl:
        return (0.0, "")
    try:
        mtime = os.path.getmtime(jsonl)
    except Exception:  # noqa: BLE001
        return (0.0, "")
    cached = _cc_tail_cache.get(jsonl)
    if cached and cached[0] == mtime:
        return (mtime, cached[1])
    preview = _cc_tail_preview(jsonl)
    if len(_cc_tail_cache) > 512:
        _cc_tail_cache.clear()
    _cc_tail_cache[jsonl] = (mtime, preview)
    return (mtime, preview)


_cc_head_cache: dict = {}   # jsonl path -> (sessionId, title)


def _cc_session_head(jsonl):
    """(sessionId, title) for the Claude session this remote is running, so the app
    can map a Pocket remote ("Main") to its Claude-app session ("Session review…").
    sessionId = jsonl basename (free). title = first real user message, read from the
    top and stopped early. Cached BY PATH: both are stable for the life of the session
    file, so even a huge actively-appended jsonl is read at most once (never re-read
    per poll like _cchist_meta would).
    收 per-session jsonl(_cc_session_jsonl 解析),不再吃 dir-latest。"""
    if not jsonl:
        return (None, None)
    cached = _cc_head_cache.get(jsonl)
    if cached:
        return cached
    sid = os.path.basename(jsonl)[:-len(".jsonl")]
    title = ""
    try:
        with open(jsonl, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 200:            # first real user msg is near the top; bound the scan
                    break
                try:
                    d = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if d.get("type") == "user":
                    c = (d.get("message") or {}).get("content")
                    if isinstance(c, list):
                        c = next((x.get("text") for x in c
                                  if isinstance(x, dict) and x.get("type") == "text"), "")
                    if isinstance(c, str):
                        t = c.strip()
                        if t and not t.startswith("<") and not t.startswith("Caveat:"):
                            title = t[:120]
                            break
    except Exception:  # noqa: BLE001
        pass
    res = (sid, title or None)
    _cc_head_cache[jsonl] = res
    return res


async def _cc_sessions():
    out = []
    for name, workdir, enabled in _cc_conf_rows():
        if enabled != "1":
            continue
        alive = await _tmux_alive(name)
        busy = False
        awaiting = False
        if alive:
            hook_state = _cc_fresh_hook_state(name)
            # Mid-turn? Capture the pane and look for the working spinner — so the
            # home list can animate a running CC session (parity with Codex).
            try:
                pane = await _tmux_capture_cached(name)
                if hook_state:
                    busy = bool(hook_state.get("busy"))
                else:
                    busy = bool(_CC_BUSY_RE.search(pane)) or ("esc to interrupt" in pane.lower())
                # Parked on a permission / approval prompt → the home list flags it
                # ("待放行") so a session waiting on you is never invisible.
                if not busy and _cc_prompt(pane) is not None:
                    awaiting = True
            except Exception:  # noqa: BLE001
                busy = False
        jsonl = await _cc_session_jsonl(name, workdir)
        mtime, preview = _cc_last_activity(jsonl)
        sid, stitle = _cc_session_head(jsonl)
        # Claude 標題(桌面 App rename 改的就是這個,存在終端 pane_title)→ app 當
        # 副標題,主名仍是 ccsess 名。取不到就 None,不影響列表。
        claude_title = await _cc_pane_title(name) if alive else None
        out.append({"name": name, "workdir": workdir,
                    "status": "running" if alive else "down", "busy": busy,
                    "awaiting": awaiting, "updatedAt": mtime, "preview": preview,
                    "sessionId": sid, "sessionTitle": stitle,
                    "claudeTitle": claude_title})
    return out


# CC session 顯示副標題:Claude(桌面 App rename / CLI 自動任務摘要)寫進終端標題,
# tmux 存成 pane_title;前面帶狀態字元(✳ / braille spinner ⠂⠐ / ✓ …),剝掉取乾淨標題。
_CC_TITLE_STRIP_RE = re.compile(r"^[\s☀-➿⠀-⣿·•⏺*]+")


async def _cc_pane_title(name: str):
    """這條 CC session 的 Claude 標題(終端 pane_title,剝掉前置狀態字元)。桌面
    App 的 rename 改的就是這個。給 app 當副標題;取不到/空 → None。絕不 raise。"""
    try:
        rc, out, _ = await _tmux_run("display-message", "-t", name, "-p", "#{pane_title}")
    except Exception:  # noqa: BLE001
        return None
    if rc != 0:
        return None
    t = _CC_TITLE_STRIP_RE.sub("", (out or "").strip()).strip()
    return t or None


def _blocks_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") in (None, "text"))
    return ""


def _cc_time(ts) -> str:
    if not ts:
        return ""
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().strftime("%m/%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return ""


def _fmt_cc_event(d: dict) -> str:
    """One transcript jsonl event → display markdown the app's TranscriptView
    already renders (tool rows, collapsible thinking/results, answer text)."""
    t = d.get("type")
    msg = d.get("message") or {}
    if t == "user":
        content = msg.get("content")
        if isinstance(content, str):
            head = content.lstrip()[:80]
            if any(tag in head for tag in ("<task-notification>", "<system-reminder>",
                                           "[Internal", "<command-name>", "<local-command")):
                return ""           # harness/system plumbing, not something 善彰 typed
            ts = _cc_time(d.get("timestamp"))
            stamp = f" _{ts}_" if ts else ""
            return f"\n\n**🧑 你:**{stamp} {content}\n\n"
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    txt = _blocks_text(b.get("content"))
                    if txt:
                        short = txt[:900]
                        more = "\n…(截斷)" if len(txt) > 900 else ""
                        parts.append(f"<details><summary>↳ 結果</summary>\n\n```\n{short}{more}\n```\n\n</details>\n")
            return "".join(parts)
        return ""
    if t == "assistant":
        content = msg.get("content")
        if not isinstance(content, list):
            return ""
        out = []
        # Stamp the reply time on assistant messages that carry visible text, so
        # the app can show when each answer came back. App-only marker (CC
        # sessions never go to Telegram); the app extracts and strips it.
        has_text = any(isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                       for b in content)
        ts = _cc_time(d.get("timestamp"))
        if has_text and ts:
            out.append(f"**🤖 助手:** _{ts}_")
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text" and b.get("text"):
                out.append(b["text"])
            elif bt == "thinking" and b.get("thinking"):
                out.append(f"\n<details><summary>💭 思考</summary>\n\n{b['thinking']}\n\n</details>\n")
            elif bt == "tool_use":
                name = b.get("name", "tool")
                inp = b.get("input") or {}
                if name == "ExitPlanMode" and isinstance(inp, dict) and inp.get("plan"):
                    # wave 2: the plan IS the content — a 140-char one-liner
                    # (the generic cmd path below) buried it. Full markdown.
                    out.append(f"\n› 🔧 **ExitPlanMode**\n\n📋 **計畫**\n\n{inp['plan']}\n")
                    continue
                cmd = (inp.get("command") or inp.get("file_path") or inp.get("path")
                       or inp.get("pattern") or "")
                if not cmd and isinstance(inp, dict):
                    cmd = next((str(v) for v in inp.values() if isinstance(v, (str, int))), "")
                cmd = str(cmd).splitlines()[0][:TOOL_CMD_MAX] if cmd else ""
                out.append(f"\n› 🔧 **{name}**" + (f" `{cmd}`" if cmd else "") + "\n")
        return "\n".join(out)
    return ""


# wave 2: CC usage meter + full plan text, both read from the session's
# transcript jsonl tail (last 256KB — a turn's final assistant event always
# lands near EOF). mtime-keyed cache so the app's 1.2s status poll doesn't
# re-scan an unchanged file.
_CC_CONTEXT_WINDOW = 200_000
_CC_JSONL_TAIL_BYTES = 262_144
_CC_JSONL_SCAN_CACHE: dict = {}   # jsonl path -> (jsonl, mtime, usage, plan)


def _cc_jsonl_tail_events(jsonl: str):
    with open(jsonl, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - _CC_JSONL_TAIL_BYTES))
        data = f.read().decode("utf-8", "replace")
    events = []
    for line in data.splitlines():
        try:
            events.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return events


def _cc_scan_jsonl(jsonl):
    """→ (usage_dict_or_None, latest_plan_or_None) for the session's live jsonl.
    收 per-session jsonl(_cc_session_jsonl 解析);快取以 jsonl path 為 key,
    同 workdir 的兩個 session 不再共用同一筆(身分混淆 bug)。"""
    if not jsonl:
        return (None, None)
    try:
        mt = os.path.getmtime(jsonl)
    except OSError:
        return (None, None)
    hit = _CC_JSONL_SCAN_CACHE.get(jsonl)
    if hit and hit[0] == jsonl and hit[1] == mt:
        return (hit[2], hit[3])
    usage = plan = None
    try:
        for d in reversed(_cc_jsonl_tail_events(jsonl)):
            if d.get("type") != "assistant":
                continue
            msg = d.get("message") or {}
            if usage is None:
                u = msg.get("usage") or {}
                used = sum(int(u.get(k) or 0) for k in
                           ("input_tokens", "cache_creation_input_tokens",
                            "cache_read_input_tokens", "output_tokens"))
                if used:
                    # The jsonl doesn't state the context window. Default to
                    # 200k; a session already past that is on a long-context
                    # model (observed live: 224k used on this box) → report
                    # the 1M window so the meter never reads >100%.
                    size = 1_000_000 if used > _CC_CONTEXT_WINDOW else _CC_CONTEXT_WINDOW
                    usage = {"used": used, "size": size}
            if plan is None:
                for b in (msg.get("content") or []):
                    if (isinstance(b, dict) and b.get("type") == "tool_use"
                            and b.get("name") == "ExitPlanMode"
                            and (b.get("input") or {}).get("plan")):
                        plan = str(b["input"]["plan"])
                        break
            if usage is not None and plan is not None:
                break
    except Exception as e:  # noqa: BLE001
        _log_event("cc_jsonl_scan_failed", jsonl=os.path.basename(jsonl or ""),
                   error=type(e).__name__, error_message=str(e)[:120])
    _CC_JSONL_SCAN_CACHE[jsonl] = (jsonl, mt, usage, plan)
    return (usage, plan)


def _cc_pending_ask(jsonl):
    """讀 jsonl 尾巴,找「已發出但還沒被回答」的 AskUserQuestion(tool_use 無對應
    tool_result)→ 回完整結構化 ask(問題全文 + 每個選項 label+description)。

    這是 _cc_prompt 螢幕擷取的內容取代:終端只渲染截斷的 label(砍到終端寬/一行),
    jsonl 的 tool_use input 有全文,app 才判斷得了(否則使用者得回 Claude app 看)。
    偵測靠「tool_use 無 tool_result」比掃畫面錨點可靠(掃畫面在忙/捲動/多個 ask 連發
    時會漏)。None = 沒有 pending ask。絕不 raise。"""
    if not jsonl:
        return None
    try:
        events = _cc_jsonl_tail_events(jsonl)
    except Exception:  # noqa: BLE001
        return None
    answered = set()          # 已有 tool_result 的 tool_use_id
    for d in events:
        if d.get("type") != "user":
            continue
        c = (d.get("message") or {}).get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tid = b.get("tool_use_id")
                    if tid:
                        answered.add(tid)
    for d in reversed(events):
        if d.get("type") != "assistant":
            continue
        for b in ((d.get("message") or {}).get("content") or []):
            if (isinstance(b, dict) and b.get("type") == "tool_use"
                    and b.get("name") == "AskUserQuestion"
                    and b.get("id") not in answered):
                qs = (b.get("input") or {}).get("questions") or []
                if not qs:
                    continue
                q0 = qs[0]        # app 的 CCPrompt 是單問;多問先送第一題(multi 標記)
                opts = []
                for i, op in enumerate(q0.get("options") or []):
                    if not isinstance(op, dict):
                        continue
                    opts.append({"key": str(i + 1),      # 對齊 TUI 選項編號(送鍵用)
                                 "label": str(op.get("label") or "").strip(),
                                 "description": str(op.get("description") or "").strip()})
                if len(opts) < 2:
                    continue
                return {"kind": "menu", "semantic": "question",
                        "title": str(q0.get("question") or "").strip(),
                        "header": str(q0.get("header") or "").strip() or None,
                        "options": opts, "multi": len(qs) > 1}
    return None


@app.get("/ccsessions")
async def cc_list(request: Request, archived: bool = False):
    _check_auth(request)
    if archived:
        # Archived = disabled (enabled != 1) in the ccsess config.
        return {"sessions": [{"name": n, "workdir": w, "status": "archived", "busy": False}
                             for n, w, e in _cc_conf_rows() if e != "1"]}
    return {"sessions": await _cc_sessions()}


@app.post("/ccsessions/{name}/rename")
async def cc_session_rename(name: str, request: Request):
    _check_auth(request)
    body = await request.json()
    new_name = (body.get("name") or "").strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="name required")
    if any(ch in new_name for ch in "/|:\n\r\t"):
        raise HTTPException(status_code=400, detail="unsupported session name")
    rows = _cc_conf_rows()
    current = next((r for r in rows if r[0] == name), None)
    if not current:
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    try:
        p = await asyncio.create_subprocess_exec(
            os.path.expanduser("~/.local/bin/ccsess"), "rename", name, new_name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await p.communicate()
        if p.returncode != 0:
            detail = (err or out or b"rename failed").decode("utf-8", "replace")[:300]
            raise HTTPException(status_code=502, detail=detail)
        status = "running" if await _tmux_alive(new_name) else "down"
        return {"ok": True, "session": {"name": new_name, "workdir": current[1], "status": status}}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


async def _run_ccsess(*args):
    p = await asyncio.create_subprocess_exec(
        os.path.expanduser("~/.local/bin/ccsess"), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), 30)
    except asyncio.TimeoutError:
        try:
            p.kill()
        except ProcessLookupError:
            pass
        _log_event("ccsess_timeout", args=" ".join(str(a) for a in args[:3]))
        raise http_err(502, "TMUX_FAILED", "ccsess timed out", "ccsess timed out (30s)")
    if p.returncode != 0:
        detail = (err or out or b"ccsess failed").decode("utf-8", "replace")[:300]
        raise http_err(502, "TMUX_FAILED", "ccsess failed", detail)
    return (out or b"").decode("utf-8", "replace")


@app.post("/ccsessions/{name}/archive")
async def cc_session_archive(name: str, request: Request):
    """Archive a Claude Code session (saves scrollback, kills tmux, disables), or
    unarchive it when the body has {"archived": false} (re-enable + relaunch)."""
    _check_auth(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if body.get("archived") is False:
        await _run_ccsess("enable", name)
        await _run_ccsess("ensure")          # relaunch the now-enabled session
        return {"ok": True, "archived": False}
    await _run_ccsess("archive", name)
    return {"ok": True, "archived": True}


@app.post("/ccsessions/{name}/login")
async def cc_session_login(name: str, request: Request):
    """Open Claude Code's official login flow for a managed session.

    This endpoint is intentionally user initiated. It does not rotate tokens,
    switch providers, or fall back to an API key; `ccsess login` owns the tmux
    recovery needed to put `/login` into the correct Claude TUI.
    """
    _check_auth(request)
    if not any(row[0] == name for row in _cc_conf_rows()):
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    out = (await _run_ccsess("login", name)).strip()
    return {
        "ok": True,
        "session": name,
        "action": "login",
        "message": out or f"已在 {name} 開啟登入流程",
    }


def _pretrust_claude_dir(path: str):
    """Mark a directory as trusted in ~/.claude.json so Claude Code doesn't open
    a brand-new session on the "Do you trust the files in this folder?" dialog
    (which the app would surface as an endless review prompt). Read-modify-write
    preserves all existing config; atomic replace avoids torn writes."""
    cfg = os.path.expanduser("~/.claude.json")
    try:
        with open(cfg) as f:
            d = json.load(f)
    except Exception:
        d = {}
    projs = d.setdefault("projects", {})
    proj = projs.setdefault(path, {})
    proj["hasTrustDialogAccepted"] = True
    try:
        tmp = cfg + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, cfg)
    except Exception:
        pass


async def _cc_wait_ready(name: str, timeout: float = 12.0):
    """Poll until the new session's Claude TUI is actually up, so the app opens a
    live session instead of flashing offline while it boots."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await _tmux_alive(name):
            _, pane, _ = await _tmux_run("capture-pane", "-p", "-t", name)
            pane = pane.lower()
            if "for shortcuts" in pane or "esc to interrupt" in pane or "❯" in pane:
                return True
        await asyncio.sleep(0.6)
    return False


def _pocket_lane_bindings() -> dict:
    try:
        with open(POCKET_AGENT_LANES, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        _log_event("pocket_lane_bindings_read_failed", error=str(e)[:160])
        return {}


def _pocket_lane_note(provider: str, tmux_name: str, native_id: str, cwd: str,
                      title: str = "") -> None:
    d = _pocket_lane_bindings()
    d[provider] = {
        "tmux": tmux_name,
        "native_id": native_id,
        "cwd": cwd,
        "title": title,
        "updated_at": time.time(),
    }
    try:
        os.makedirs(os.path.dirname(POCKET_AGENT_LANES), exist_ok=True)
        tmp = POCKET_AGENT_LANES + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, POCKET_AGENT_LANES)
    except Exception as e:  # noqa: BLE001
        _log_event("pocket_lane_bindings_write_failed",
                   provider=provider, tmux=tmux_name, error=str(e)[:160])


def _pocket_existing_dir(raw: str | None, fallback: str = HOME_ROOT) -> str:
    for cand in (raw, fallback):
        if not cand:
            continue
        wd = os.path.realpath(os.path.abspath(os.path.expanduser(str(cand))))
        if os.path.isdir(wd):
            return wd
    raise http_err(409, "WORKDIR_MISSING",
                   "session workdir does not exist",
                   f"workdir missing: {raw or fallback or '(none)'}")


async def _pocket_tmux_replace(name: str, cwd: str, argv: list[str]) -> None:
    if await _tmux_alive(name):
        rc, _, err = await _tmux_run("kill-session", "-t", name)
        if rc != 0:
            raise http_err(502, "TMUX_FAILED", "tmux kill-session failed",
                           (err or "tmux kill-session failed")[:200])
    rc, _, err = await _tmux_run("new-session", "-d", "-s", name, "-c", cwd, *argv)
    if rc != 0:
        raise http_err(502, "TMUX_FAILED", "tmux new-session failed",
                       (err or "tmux new-session failed")[:200])
    # Keep the lane visible/re-attachable even after the app disconnects or the
    # agent exits, matching the manual `codex-current` recovery setup.
    await _tmux_run("set-option", "-t", name, "remain-on-exit", "on")
    await _tmux_run("set-option", "-t", name, "destroy-unattached", "off")
    _PANE_CACHE.pop(name, None)


async def _pocket_selected_cc(body: dict) -> tuple[str, str, str, str]:
    sid = str(body.get("session_id") or body.get("sessionId")
              or body.get("sid") or "").strip()
    cwd = str(body.get("cwd") or body.get("workdir") or "").strip()
    source_name = str(body.get("name") or body.get("session_name")
                      or body.get("sessionName") or "").strip()
    title = str(body.get("sessionTitle") or body.get("claudeTitle")
                or body.get("title") or source_name or "").strip()

    if source_name:
        row = next((r for r in _cc_conf_rows() if r[0] == source_name), None)
        if row:
            cwd = cwd or row[1]
            jsonl = await _cc_session_jsonl(source_name, row[1])
            head_sid, head_title = _cc_session_head(jsonl)
            if not _cc_valid_sid(sid):
                sid = head_sid or sid
            title = title or head_title or source_name

    if not _cc_valid_sid(sid):
        raise http_err(409, "SESSION_ID_MISSING",
                       "Claude session id is required to bind the fixed lane",
                       "this CC row has no resolved Claude session id yet")

    if not cwd:
        path = _cchist_find(sid)
        meta = _cchist_meta(path) if path else None
        cwd = (meta or {}).get("cwd") or ""
        title = title or (meta or {}).get("title") or ""
    cwd = _pocket_existing_dir(cwd, "")
    return sid, cwd, title, source_name


async def _pocket_bind_cc_source(name: str, sid: str, cwd: str,
                                 title: str) -> dict:
    """Bind Pocket to an existing ccsess without replacing its remote control.

    A live Claude App session is already the single owner of its transcript.
    Pocket controls that same tmux pane; cloning the sid into `pocket-cc` would
    archive the original remote-control card and create two transcript writers.
    """
    _cc_write_remote_control_pin(name)
    running = await _tmux_alive(name)
    status = "running"
    if running:
        current_sid = await _cc_pane_session_id(name)
        if current_sid and current_sid != sid:
            raise http_err(409, "SOURCE_SESSION_CHANGED",
                           "Claude session changed; refresh and reconnect",
                           f"{name} now points at a different session id")
        if not await _cc_pane_has_remote_control(name):
            _CC_HOOK_STATE.pop(name, None)
            _CC_SID_CACHE.pop(name, None)
            _CC_SID_PINS.pop(name, None)
            await _pocket_tmux_replace(name, cwd, _cc_remote_resume_argv(name, sid))
            ready = await _cc_wait_ready(name)
            status = "running" if ready else "starting"
    else:
        _CC_HOOK_STATE.pop(name, None)
        _CC_SID_CACHE.pop(name, None)
        _CC_SID_PINS.pop(name, None)
        await _pocket_tmux_replace(name, cwd, _cc_remote_resume_argv(name, sid))
        ready = await _cc_wait_ready(name)
        status = "running" if ready else "starting"

    await _cc_register_explicit_resume(name, cwd)
    _cc_write_resume_pin(name, sid)
    _cc_cache_sid(name, sid, pin=True)
    _cc_mark_app_owned(name)
    _pocket_lane_note("claude_code", name, sid, cwd, title)
    _log_event("pocket_cc_source_bound", tmux=name,
               native_hash=_short_hash(sid), cwd_hash=_short_hash(cwd),
               reused=running)
    return {"name": name, "workdir": cwd, "status": status,
            "sessionId": sid, "sessionTitle": title or None}


async def _pocket_activate_cc_lane(body: dict) -> dict:
    sid, cwd, title, source_name = await _pocket_selected_cc(body)
    if source_name and source_name != POCKET_CC_TMUX:
        return await _pocket_bind_cc_source(source_name, sid, cwd, title)

    lane = POCKET_CC_TMUX
    _cc_write_remote_control_pin(lane)
    if await _tmux_alive(lane):
        current_sid = await _cc_pane_session_id(lane)
        if current_sid == sid:
            status = "running"
            if not await _cc_pane_has_remote_control(lane):
                _CC_HOOK_STATE.pop(lane, None)
                _CC_SID_CACHE.pop(lane, None)
                _CC_SID_PINS.pop(lane, None)
                await _pocket_tmux_replace(lane, cwd, _cc_remote_resume_argv(lane, sid))
                ready = await _cc_wait_ready(lane)
                status = "running" if ready else "starting"
            await _cc_register_explicit_resume(lane, cwd)
            _cc_write_resume_pin(lane, sid)
            _cc_cache_sid(lane, sid, pin=True)
            _cc_mark_app_owned(lane)
            _pocket_lane_note("claude_code", lane, sid, cwd, title)
            return {"name": lane, "workdir": cwd, "status": status,
                    "sessionId": sid, "sessionTitle": title or None}

    _CC_HOOK_STATE.pop(lane, None)
    _CC_SID_CACHE.pop(lane, None)
    _CC_SID_PINS.pop(lane, None)
    await _pocket_tmux_replace(lane, cwd, _cc_remote_resume_argv(lane, sid))
    await _cc_register_explicit_resume(lane, cwd)
    _cc_write_resume_pin(lane, sid)
    _cc_cache_sid(lane, sid, pin=True)
    _cc_mark_app_owned(lane)
    ready = await _cc_wait_ready(lane)
    _pocket_lane_note("claude_code", lane, sid, cwd, title)

    _log_event("pocket_lane_activate", provider="claude_code",
               tmux=lane, native_hash=_short_hash(sid), cwd_hash=_short_hash(cwd))
    return {"name": lane, "workdir": cwd,
            "status": "running" if ready else "starting",
            "sessionId": sid, "sessionTitle": title or None}


async def _pocket_activate_cx_lane(body: dict) -> dict:
    thread_id = str(body.get("thread_id") or body.get("threadId")
                    or body.get("id") or "").strip()
    if not thread_id:
        raise http_err(400, "THREAD_ID_REQUIRED", "thread_id required")
    cwd = _pocket_existing_dir(body.get("cwd") or body.get("workdir"), HOME_ROOT)
    title = str(body.get("name") or body.get("title") or thread_id[:12] or "codex").strip()
    # Pocket already controls Codex through the app-server endpoints. Starting
    # `codex resume <thread_id>` in another tmux would make that CLI and the
    # official app-server compete for the same thread. Record only the logical
    # binding; leaving Pocket then has no process or archive side effect.
    _pocket_lane_note("codex", "", thread_id, cwd, title)
    _log_event("pocket_lane_activate", provider="codex",
               control="app_server", native_hash=_short_hash(thread_id),
               cwd_hash=_short_hash(cwd))
    return {"thread_id": thread_id, "session_id": None, "name": title, "workdir": cwd,
            "preview": body.get("preview") or "", "status": body.get("status") or "idle",
            "source": "codex-app-server", "updatedAt": body.get("updatedAt"),
            "activeTurn": bool(body.get("activeTurn", False))}


@app.post("/app/v1/agent-lanes/{provider}/activate")
async def app_agent_lane_activate(provider: str, request: Request):
    """Bind Pocket's provider page to a native session.

    Claude Code reuses an existing named tmux in place so the Claude App remote
    control remains alive. A fixed `pocket-cc` fallback is created only for a
    history sid with no live/source session name. Codex keeps the existing
    app-server control path without spawning a competing CLI process.
    """
    _check_auth(request)
    body = await _json_body(request)
    p = (provider or "").lower().replace("-", "_")
    if p in ("cc", "claude", "claude_code"):
        session = await _pocket_activate_cc_lane(body)
        return {"ok": True, "provider": "claude_code", "tmux": session["name"],
                "session": session}
    if p in ("cx", "codex"):
        session = await _pocket_activate_cx_lane(body)
        return {"ok": True, "provider": "codex", "tmux": None,
                "session": session}
    raise http_err(404, "PROVIDER_NOT_FOUND", "unknown agent lane provider")


@app.post("/ccsessions")
async def cc_session_create(request: Request):
    """Create + start a new Claude Code session."""
    _check_auth(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    workdir = (body.get("workdir") or body.get("cwd") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    if any(ch in name for ch in "/|:\n\r\t"):
        raise HTTPException(status_code=400, detail="unsupported session name")
    # Resolve + create the workdir. Without this, a non-existent path makes ccsess
    # silently fall back to $HOME and (because $HOME has history) launch with
    # --continue — hijacking the home conversation. So: require a real dir under
    # home, create it, and pre-trust it so there's no startup review prompt.
    home = os.path.realpath(os.path.expanduser("~"))
    wd = os.path.realpath(os.path.expanduser(workdir)) if workdir else os.path.join(home, "apps", re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-").lower() or "session")
    if not (wd == home or wd.startswith(home + os.sep)):
        raise HTTPException(status_code=400, detail="workdir must be under home")
    if wd == home:
        raise HTTPException(status_code=400, detail="pick a sub-folder, not your home directory")
    try:
        os.makedirs(wd, exist_ok=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"cannot create workdir: {e}")
    _pretrust_claude_dir(wd)
    # P0 派工分級(2026-07-10):model 參數對應 ccsess 的 per-session model
    # pin(`ccsess model <name> <model>`),讓企劃/大局思考類任務可指定旗艦
    # 模型、機械性任務指定輕量模型,不必全域切換 delegation.model。
    cc_model = (body.get("model") or "").strip()
    _cc_write_remote_control_pin(name)
    new_args = ["new", name, wd] + ([cc_model] if cc_model else [])
    await _run_ccsess(*new_args)
    _cc_mark_app_owned(name)   # 這條是 app 開的 → 只有它的審核會進 app(見 _cc_approval_watcher)
    ready = await _cc_wait_ready(name)
    return {"ok": True, "session": {"name": name, "workdir": wd,
                                    "status": "running" if ready else "starting",
                                    "model": cc_model or None}}


@app.put("/app/v1/owned-cc-sessions")
async def cc_set_app_owned(request: Request):
    """App 宣告「我 SSH 列表裡的 CC session」——覆寫 app-owned.txt 為權威清單。
    _cc_approval_watcher 只推這些 session 的審核(hermes 另計),別處的 ccsess
    (Culture Supply/FLiPER…)不外漏。app 端於 load 時用 sshStore 的 CC 記錄呼叫,
    所以「審核中心 = 這台 app 的 SSH 清單 ⊕ hermes」恆等對齊(含之前接進來的 Ops)。"""
    _check_auth(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    names = body.get("names") or body.get("sessions") or []
    clean = []
    seen = set()
    for n in names if isinstance(names, list) else []:
        s = str(n or "").strip()
        # 容錯:傳 "claude_code:Ops" 也接受,取冒號後段當 ccsess 名。
        if s.startswith("claude_code:"):
            s = s.split(":", 1)[1]
        if s and s not in seen:
            seen.add(s); clean.append(s)
    try:
        os.makedirs(os.path.dirname(APP_OWNED_CC), exist_ok=True)
        with open(APP_OWNED_CC, "w") as f:
            f.write("".join(x + "\n" for x in clean))
    except Exception as e:  # noqa: BLE001
        raise http_err(500, "WRITE_FAILED", f"could not write app-owned list: {e}")
    _log_event("cc_app_owned_set", count=len(clean))
    return {"ok": True, "count": len(clean), "names": clean}


@app.get("/ccsessions/{name}/stream")
async def cc_session_stream(name: str, request: Request, replay: int = 80):
    """Live transcript of a ccsess session: replay the recent tail of its
    Claude Code jsonl, then follow it in real time (OpenAI-style SSE so the app
    reuses its chat stream parser)."""
    _check_auth(request)
    row = next((r for r in _cc_conf_rows() if r[0] == name), None)
    if not row:
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    workdir = row[1]
    cid = "ccsess-" + uuid.uuid4().hex[:16]

    def chunk(delta, finish=None):
        payload = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                   "model": name, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    async def gen():
        yield chunk({"role": "assistant", "content": ""})
        jsonl = await _cc_session_jsonl(name, workdir)
        pos = 0
        if jsonl and os.path.exists(jsonl):
            try:
                lines = open(jsonl, encoding="utf-8", errors="replace").read().splitlines()
            except Exception:  # noqa: BLE001
                lines = []
            # replay=0 means "follow only" (reconnect). Guard against Python's
            # lines[-0:] == lines[0:] which would replay the ENTIRE file on every
            # reconnect — that ballooned the app's buffer and made it scroll
            # forever after an idle stream drop.
            for line in (lines[-replay:] if replay > 0 else []):
                try:
                    c = _fmt_cc_event(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
                if c:
                    yield chunk({"content": c})
            pos = os.path.getsize(jsonl)
        # follow
        idle = 0
        last_data = time.monotonic()
        while True:
            if await request.is_disconnected():
                break
            if time.monotonic() - last_data >= _STREAM_IDLE_CUTOFF_SECS:
                # 30min without a single data chunk → cut the stream cleanly
                # (keepalive comments don't count as data).
                _log_event("cc_stream_idle_cutoff", session=name)
                yield chunk({}, finish="stop")
                yield "data: [DONE]\n\n"
                break
            await asyncio.sleep(1.0)
            cur = await _cc_session_jsonl(name, workdir)
            if cur != jsonl:                      # session rotated to a new jsonl
                jsonl, pos = cur, 0
            if jsonl and os.path.exists(jsonl):
                size = os.path.getsize(jsonl)
                if size > pos:
                    with open(jsonl, encoding="utf-8", errors="replace") as f:
                        f.seek(pos)
                        new = f.read()
                        pos = f.tell()
                    for line in new.splitlines():
                        if not line.strip():
                            continue
                        try:
                            c = _fmt_cc_event(json.loads(line))
                        except Exception:  # noqa: BLE001
                            continue
                        if c:
                            yield chunk({"content": c})
                            last_data = time.monotonic()
                    idle = 0
            idle += 1
            if idle >= max(1, int(SSE_KEEPALIVE_SECS)):   # quiet → keepalive comment.
                # Frequent so any HTTP/tunnel buffering flushes the last data chunk
                # promptly — an idle session shouldn't leave the transcript's tail
                # held in a buffer (looked "stuck" on entry until something poked it).
                idle = 0
                yield ": keepalive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def _cc_format_lines(lines):
    parts = []
    for line in lines:
        if not line.strip():
            continue
        try:
            c = _fmt_cc_event(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
        if c:
            parts.append(c)
    return "".join(parts)


@app.get("/ccsessions/{name}/history")
async def cc_session_history(name: str, request: Request, offset: int = 0, limit: int = 150):
    """A page of older transcript events for scroll-back: the `limit` events that
    end `offset` events from the newest. `more` is true if older events remain."""
    _check_auth(request)
    row = next((r for r in _cc_conf_rows() if r[0] == name), None)
    if not row:
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    jsonl = await _cc_session_jsonl(name, row[1])
    if not jsonl or not os.path.exists(jsonl):
        return {"text": "", "more": False}
    try:
        lines = open(jsonl, encoding="utf-8", errors="replace").read().splitlines()
    except Exception:  # noqa: BLE001
        return {"text": "", "more": False}
    total = len(lines)
    end = max(0, total - max(0, offset))
    start = max(0, end - max(1, min(limit, 500)))
    return {"text": _cc_format_lines(lines[start:end]), "more": start > 0}


# ---- CC history (S1, pocketagent#37): browse ALL past sessions & resume ----
# ~/.claude/projects/<slug>/<session-uuid>.jsonl is Claude Code's own store —
# one file per session, the true `cwd` recorded inside (no lossy slug
# reversal). We surface them read-only, and resume by spawning
# `claude --resume <id>` in a fresh tmux session registered in CCSESS_CONF —
# it then IS a normal live ccsession, so the app's existing live view / send /
# interrupt / status all apply unchanged.

CC_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
_cchist_meta_cache: dict = {}   # path -> (mtime, meta); title needs a file read


def _cchist_meta(path: str):
    try:
        mtime = os.path.getmtime(path)
    except Exception:  # noqa: BLE001
        return None
    cached = _cchist_meta_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    sid = os.path.basename(path)[:-len(".jsonl")]
    title, cwd, events = "", "", 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                events += 1
                if title and cwd:
                    continue
                try:
                    d = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if not cwd and d.get("cwd"):
                    cwd = d["cwd"]
                if not title and d.get("type") == "user":
                    c = (d.get("message") or {}).get("content")
                    if isinstance(c, list):
                        c = next((x.get("text") for x in c
                                  if isinstance(x, dict) and x.get("type") == "text"), "")
                    if isinstance(c, str):
                        t = c.strip()
                        # skip harness noise (<local-command…>, Caveat banners)
                        if t and not t.startswith("<") and not t.startswith("Caveat:"):
                            title = t[:120]
    except Exception:  # noqa: BLE001
        return None
    meta = {"id": sid, "title": title or "(無標題)", "cwd": cwd,
            "project": os.path.basename(os.path.dirname(path)),
            "last_at": mtime, "events": events}
    _cchist_meta_cache[path] = (mtime, meta)
    return meta


def _cchist_find(sid: str):
    """jsonl path for a session id — id is validated so it can't traverse."""
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", sid or ""):
        return None
    hits = glob.glob(os.path.join(CC_PROJECTS_DIR, "*", sid + ".jsonl"))
    return hits[0] if hits else None


@app.get("/cchistory")
async def cc_history_list(request: Request, limit: int = 50, offset: int = 0, q: str = ""):
    """All past Claude Code sessions across every project, newest first.
    `q` filters on title/project. Metas are cached by (path, mtime)."""
    _check_auth(request)
    files = glob.glob(os.path.join(CC_PROJECTS_DIR, "*", "*.jsonl"))

    def _mt(p):
        try:
            return os.path.getmtime(p)
        except Exception:  # noqa: BLE001
            return 0.0
    files.sort(key=_mt, reverse=True)
    needle = (q or "").strip().lower()
    out = []
    for p in files:
        m = _cchist_meta(p)
        if not m:
            continue
        if needle and needle not in m["title"].lower() and needle not in m["project"].lower():
            continue
        out.append(m)
    lim = max(1, min(limit, 200))
    page = out[max(0, offset): max(0, offset) + lim]
    return {"sessions": page, "total": len(out), "more": max(0, offset) + len(page) < len(out)}


@app.get("/cchistory/{sid}/transcript")
async def cc_history_transcript(sid: str, request: Request, limit: int = 200):
    """Read-only tail of a past session, in the SAME transcript text format as
    the live view (**🧑 你:** markers) so the app renders it with zero new code."""
    _check_auth(request)
    path = _cchist_find(sid)
    if not path:
        raise HTTPException(status_code=404, detail="unknown history session")
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e)[:200])
    take = lines[-max(1, min(limit, 1000)):]
    meta = _cchist_meta(path) or {}
    return {"text": _cc_format_lines(take), "more": len(lines) > len(take),
            "cwd": meta.get("cwd", ""), "title": meta.get("title", "")}


@app.post("/cchistory/{sid}/resume")
async def cc_history_resume(sid: str, request: Request):
    """Resume a past session: `claude --resume <id>` in a new tmux session at
    the session's own cwd, registered in CCSESS_CONF → it becomes a normal live
    ccsession the app can talk to immediately. Returns its name."""
    _check_auth(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    path = _cchist_find(sid)
    if not path:
        raise HTTPException(status_code=404, detail="unknown history session")
    meta = _cchist_meta(path) or {}
    cwd = meta.get("cwd") or ""
    if not cwd or not os.path.isdir(cwd):
        raise HTTPException(status_code=409, detail=f"workdir missing: {cwd or '(none)'}")
    # Unique tmux/conf name (user-suggested or cc-<id8>), never clobbering.
    base = re.sub(r"[^A-Za-z0-9_-]", "-", (body.get("name") or "").strip()) or f"cc-{sid[:8]}"
    existing = {r[0] for r in _cc_conf_rows()}
    name, i = base, 2
    while name in existing or await _tmux_alive(name):
        name, i = f"{base}-{i}", i + 1
    _cc_write_remote_control_pin(name)
    rc, _, err = await _tmux_run("new-session", "-d", "-s", name, "-c", cwd,
                                 *_cc_remote_resume_argv(name, sid))
    if rc != 0:
        raise http_err(502, "TMUX_FAILED", "tmux new-session failed",
                       (err or "tmux new-session failed")[:200])
    # conf 單一寫者:走 ccsess register(內含 conf 鎖),不再直接 append ——
    # 裸 append 會被 ccsess 端 mktemp+mv 全檔重寫蓋掉,或讓 rename 讀到半新不舊。
    await _cc_register_explicit_resume(name, cwd)
    # 精準 resume pin:這條 session 是明確 --resume <sid> 起的,直接落 pin,
    # 重開機後 ensure 走 --resume 接回同一條對話(不再靠 --continue 猜目錄)。
    try:
        pdir = os.path.expanduser("~/.config/ccsess/resume")
        os.makedirs(pdir, exist_ok=True)
        tmp = os.path.join(pdir, name + ".tmp")
        with open(tmp, "w") as f:
            f.write(sid + "\n")
        os.replace(tmp, os.path.join(pdir, name))
    except Exception:  # noqa: BLE001
        pass
    _log_event("cc_history_resume", session_id=sid, name=name, cwd=cwd)
    return {"ok": True, "name": name, "cwd": cwd}


@app.post("/ccsessions/{name}/input")
async def cc_session_input(name: str, request: Request):
    """Type a line into the live Claude Code session (tmux send-keys), exactly
    as if you SSH-attached and typed it. Sent literally, then Enter."""
    _check_auth(request)
    if not any(r[0] == name for r in _cc_conf_rows()):
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    body = await request.json()
    return await _cc_input_core(name, body)


async def _cc_input_core(name: str, body: dict) -> dict:
    """cc 輸入核心 — /ccsessions/{name}/input 與 v2 統一路由 input 共用。
    附件轉存＋語音轉寫＋tmux bracketed paste。"""
    text = (body.get("text") or body.get("content") or "").strip()
    _att_guard(body.get("attachments"))   # 修復單「附件限制」:直送口件數閥
    # Relay layer (like the persona attachment path): persist any attachments and
    # inject their on-disk paths into the typed line. Claude Code can Read files
    # (and sees images natively), so a bare path is enough — no vision pre-pass.
    # Audio attachments are transcribed (voice message → typed command).
    saved = []
    voice_lines = []
    for a in (body.get("attachments") or []):
        path = _save_attachment(a, a.get("filename") or "file")
        if not path:
            continue
        if a.get("kind") == "audio":
            t = await asyncio.to_thread(_transcribe, path)
            if t:
                voice_lines.append(t)
        else:
            saved.append(path)
    if voice_lines:
        text = (text + " " + " ".join(voice_lines)).strip()
    if saved:
        # SINGLE-LINE reference (no embedded newlines — a newline in send-keys/
        # paste submits the prompt early). Claude Code's Read tool handles image
        # files too, so a bare path is enough.
        refs = " ".join(saved)
        text = (text + f"  [附件已存到本機,請用 Read 讀取/檢視: {refs}]").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty")
    await _cc_paste_text(name, text)
    # 排隊空窗回音:輸入落地「當下」就發 status 事件,不等 transcript digest。
    # session 若正忙上一輪,pane 不會立即轉 busy,舊行為是 app 一路顯示
    # 「待命」直到真正接手(可能好幾分鐘)——使用者看起來就是沒反應。
    # follower 在 queued 寬限內不以 idle 蓋掉;真 busy 一出現即交還正常路徑。
    store = _cc_card_store(name)
    store.queued_until = time.time() + _CC_QUEUED_GRACE_SECS
    store.set_status({"busy": True, "mode": None, "prompt": None,
                      "phase": "queued", "label": "已排入佇列,等待接手…"})
    return {"ok": True}


async def _tmux_run_stdin(args: list, data: bytes,
                          timeout: float = _TMUX_TIMEOUT):
    """同 _tmux_run,但把 data 餵進 stdin(load-buffer 用)。"""
    p = await asyncio.create_subprocess_exec(
        TMUX_BIN, *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(input=data), timeout)
    except asyncio.TimeoutError:
        try:
            p.kill()
        except ProcessLookupError:
            pass
        _log_event("tmux_timeout", args=" ".join(str(a) for a in args[:4]),
                   timeout_s=timeout)
        return 124, "", "tmux timed out"
    return (p.returncode,
            (out or b"").decode("utf-8", "replace"),
            (err or b"").decode("utf-8", "replace").strip())


async def _cc_paste_text(name: str, text: str) -> None:
    """tmux 貼字唯一原語(B1):bracketed paste,buffer 內容改走 load-buffer
    stdin——set-buffer 把整段文字放 argv,長貼文/特殊字元踩 exec 邊界就 502,
    這一整類從此消失。每步 rc/stderr 失敗即進 log(502 先可觀測再談修)。"""
    if not await _tmux_alive(name):
        raise http_err(409, "SESSION_NOT_RUNNING", "session not running")

    buf = "pa-" + uuid.uuid4().hex[:8]
    try:
        rc_clear, _, e_clear = await _tmux_run("send-keys", "-t", name, "C-u")
        rc_load, _, e_load = await _tmux_run_stdin(
            ["load-buffer", "-b", buf, "-"],
            text.encode("utf-8", "replace"))
        rc_paste, _, e_paste = await _tmux_run("paste-buffer", "-t", name,
                                               "-b", buf, "-p", "-d")
        await asyncio.sleep(0.25)                    # let the editor settle
        rc_enter, _, e_enter = await _tmux_run("send-keys", "-t", name, "Enter")
        # Enter 落地驗證+重試:長貼文(尤其 CJK)0.25s 未必夠,TUI 還在消化
        # 貼上時 Enter 會被吞——結果是 API 回 200 但訊息掛在輸入框沒送出,
        # 使用者只看到「連線逾時」以為壞掉。送完 Enter 後檢查 composer 是否
        # 真的清空(live ❯ 之後還看得到貼文開頭 = 沒送出),沒清就補 Enter,
        # 最多三次。誤判補送的 Enter 對空 composer 是 no-op,安全。
        probe = text[:24].strip()
        submitted = True
        if not rc_enter and probe:
            for attempt in range(5):
                await asyncio.sleep(0.8)
                pane_now = await _cc_capture_pane_fresh(name)
                marker = pane_now.rfind("❯")
                if marker < 0 or probe not in pane_now[marker:]:
                    submitted = True
                    break                            # composer 已清空 → 已送出
                submitted = False
                _log_event("cc_paste_enter_retry", session=name,
                           text_chars=len(text), attempt=attempt + 1)
                await _tmux_run("send-keys", "-t", name, "Enter")
        # 2026-07-14 草稿擱淺事故:重試耗盡後文字仍掛在輸入框(TUI 處於吃
        # Enter 的狀態,如提示框/選取態),舊行為回 200 = 沉默失敗——手機
        # 以為送出了,文字卻變成跨重開機的殭屍草稿,使用者的指令無聲蒸發。
        # 改為誠實回 502:app 端會顯示送出失敗,使用者知道要重送。
        if not submitted:
            _log_event("cc_paste_not_submitted", session=name,
                       text_chars=len(text))
            raise http_err(502, "PASTE_NOT_SUBMITTED",
                           "message pasted but Enter not accepted by the TUI",
                           "text is stranded in the composer — session may be "
                           "showing a prompt/overlay; resolve it and resend")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        _log_event("cc_paste_failed", session=name, text_chars=len(text),
                   step="exec", error=f"{type(e).__name__}: {str(e)[:160]}")
        raise HTTPException(status_code=500, detail=str(e))
    if rc_load or rc_paste or rc_enter:              # don't false-report success
        _log_event("cc_paste_failed", session=name, text_chars=len(text),
                   step=("load" if rc_load else "paste" if rc_paste else "enter"),
                   rc_clear=rc_clear, rc_load=rc_load, rc_paste=rc_paste,
                   rc_enter=rc_enter,
                   stderr=(e_load or e_paste or e_enter or "")[:200])
        detail = (e_load or e_paste or e_enter or "tmux paste failed")[:200]
        raise http_err(502, "TMUX_FAILED", "tmux paste failed", detail)
    if rc_clear:
        _log_event("cc_paste_clear_warn", session=name,
                   rc=rc_clear, stderr=(e_clear or "")[:120])


async def _cc_capture_pane_fresh(name: str) -> str:
    """Capture the tmux pane RIGHT NOW (no cache) — used where staleness would
    lie, e.g. verifying an interrupt actually landed."""
    _, pane, _ = await _tmux_run("capture-pane", "-p", "-t", name)
    return pane


def _cc_pane_busy(pane: str) -> bool:
    return bool(_CC_BUSY_RE.search(pane)) or ("esc to interrupt" in pane.lower())


# CC interrupt + busy status (parity with Codex's stop/active). The app uses
# these to offer a stop button and to detect a running turn reliably instead of
# guessing from stream silence (which mis-fires on long, quiet commands).
@app.post("/ccsessions/{name}/interrupt")
async def cc_session_interrupt(name: str, request: Request):
    """Send Escape to the live TUI — same as pressing Esc to interrupt — then
    VERIFY via the pane's busy spinner that the turn actually stopped, retrying
    up to 3 Escapes. Previously this blind-fired one Escape and returned ok,
    so the app's stop button could 200 six times while the turn kept running."""
    _check_auth(request)
    if not any(r[0] == name for r in _cc_conf_rows()):
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    return await _cc_interrupt_core(name)


async def _cc_interrupt_core(name: str) -> dict:
    """cc 中斷核心(Esc + 驗證重試 3 次)— v1 與 v2 統一路由共用。

    P0 修復(2026-07-10,root cause #3 —「Escape 打錯 turn」):
    之前這裡完全沒有機制確保 Escape 打中「當下正在跑的那個 turn」。3 次重試
    迴圈横跨最多約 2.1 秒(3 × (送鍵 + 0.7s 觀察)),如果在這段時間裡原本的
    turn 已經自然結束、且緊接著一個新 turn 開始了(UserPromptSubmit),後續的
    Escape 就可能打進新 turn 的 Bash 工具執行期間,讓 CLI 誤判成「使用者中途
    拒絕這次工具呼叫」,新 turn 因而進入無限期等待使用者回覆的假死狀態(過去
    實測卡過 5.4 小時、13.5 小時)。

    修法:兩層防護,擇一命中就不再盲送 Escape。
      1) 送出第一個 Escape 前,若 hook 回報的 busy 狀態新鮮且為 False(代表
         目前根本沒有活躍 turn),直接視為「沒有需要中斷的對象」並跳過整個
         tmux 操作 —— 這同時涵蓋「使用者快速連按兩次停止鍵」:第一次已經真的
         中斷成功並同步了 busy=False(見前次 P0 修復),第二次點擊此時應該
         被判定為無事可做,而不是再送一個 Escape 去賭運氣。
      2) 重試迴圈中,每次送出 Escape 之前、以及送出後等待驗證之前,都比對
         _CC_TURN_GEN 的世代編號是否還等於呼叫開始時記下的 gen0。世代編號由
         UserPromptSubmit hook 遞增,代表「一個新 turn 開始了」。只要世代變了
         就代表原本要中斷的 turn 已經結束、新 turn 已經開始 —— 立刻停止重試,
         不再送下一個 Escape,並在回應中標記 stale_turn=True 讓呼叫端知道這次
         interrupt 沒有(也不應該)打中任何東西。
    """
    if not await _tmux_alive(name):
        raise http_err(409, "SESSION_NOT_RUNNING", "session not running")
    fresh = _cc_fresh_hook_state(name)
    if fresh is not None and fresh.get("busy") is False:
        # hook 有新鮮資料且明確說「不忙碌」→ 沒有活躍 turn 可中斷,不送 Escape。
        _log_event("cc_interrupt", session=name, interrupted=True, attempts=0,
                   reason="already_idle_per_hook")
        return {"ok": True, "interrupted": True, "attempts": 0,
                "reason": "already_idle"}
    gen0 = _CC_TURN_GEN.get(name, 0)
    attempts = 0
    interrupted = False
    stale = False
    for _ in range(3):
        if _CC_TURN_GEN.get(name, 0) != gen0:
            # 世代已變:原本要中斷的 turn 已結束、新 turn 已開始,不該再送 Escape。
            stale = True
            break
        attempts += 1
        rc, _, err = await _tmux_run("send-keys", "-t", name, "Escape")
        if rc:
            raise http_err(502, "TMUX_FAILED", "tmux send-keys failed",
                           err[:200] or "interrupt failed")
        _PANE_CACHE.pop(name, None)              # the cached pane is now stale
        await asyncio.sleep(0.7)                 # let the TUI react before checking
        if _CC_TURN_GEN.get(name, 0) != gen0:
            # 送出後才變:接下來的 pane 忙碌判斷可能量到的是新 turn 的狀態,
            # 不可信,不當作「打中原 turn」的證據,也不再送下一次 Escape。
            stale = True
            break
        pane = await _cc_capture_pane_fresh(name)
        if not _cc_pane_busy(pane):
            interrupted = True
            break
    if interrupted:
        # P0 修復(2026-07-10):interrupt 成功時 pane 已確認不忙,但 busy 的
        # 權威真相來源是 _CC_HOOK_STATE(hook 沒發 Stop 事件就不會更新),
        # 若這裡不主動同步,對外 busy 會維持 true 直到 600s TTL 到期才 fallback
        # 去看 pane —— 這正是「interrupt 回真成功但 busy 卡好幾分鐘」的根因之一。
        # 一併清掉可能殘留的 queued 提示文字,避免下次 status 誤讀成忙碌。
        _CC_HOOK_STATE[name] = {
            "busy": False,
            "updated_at": time.time(),
            "source": "interrupt",
        }
    _log_event("cc_interrupt", session=name, interrupted=interrupted,
               attempts=attempts, stale_turn=stale)
    return {"ok": True, "interrupted": interrupted, "attempts": attempts,
            "stale_turn": stale}


# Claude Code's TUI shows a working spinner like "· Fermenting… (1m 51s · ↓ 6.5k
# tokens)" while a turn runs — capture the pane and look for it. Covers long,
# silent commands (the spinner stays up), which a stream-silence heuristic misses.
_CC_BUSY_RE = re.compile(r"\((?:\d+m\s*)?\d+(?:\.\d+)?s\s*·.*tokens", re.IGNORECASE)
_CC_OPT_NUM_RE = re.compile(r"^(\d+)[.)]\s+(.{1,60})$")
_CC_OPT_LABEL_RE = re.compile(r"^(allow once|always allow|don.t allow|allow|deny|yes,|yes\b|no,|no\b)", re.IGNORECASE)


def _cc_jsonl_sid(path: str | None) -> str:
    base = os.path.basename(str(path or ""))
    if not base.endswith(".jsonl"):
        return ""
    sid = base[:-len(".jsonl")]
    return sid if _cc_valid_sid(sid) else ""


def _cc_hook_transcript_path(body: dict) -> str:
    return str(body.get("transcript_path") or body.get("transcriptPath") or "").strip()


def _cc_hook_sid(body: dict) -> tuple[str, str]:
    hook_sid = str(body.get("session_id") or "").strip()
    path_sid = _cc_jsonl_sid(_cc_hook_transcript_path(body))
    if hook_sid and not _cc_valid_sid(hook_sid):
        return "", "bad_sid"
    if hook_sid and path_sid and hook_sid != path_sid:
        return "", "sid_transcript_mismatch"
    return hook_sid or path_sid, ""


def _cc_transcript_path_matches_cwd(path: str, cwd: str | None) -> bool:
    if not path or not cwd:
        return True
    try:
        real_path = os.path.realpath(os.path.expanduser(path))
        expected_dir = os.path.realpath(_cc_project_dir(_norm_cc_workdir(cwd)))
        if os.path.dirname(real_path) == expected_dir:
            return True
        meta = _cchist_meta(real_path)
        if meta and _norm_cc_workdir(meta.get("cwd") or "") == _norm_cc_workdir(cwd):
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _cc_unique_names(names: list[str]) -> list[str]:
    out, seen = [], set()
    for name in names:
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


async def _cc_busy_hook_candidates(names: list[str], *, attempts: int = 6,
                                   delay: float = 0.35) -> list[str]:
    """For same-cwd hooks, sample live panes briefly and return busy names.

    UserPromptSubmit can reach the bridge just before the TUI paints its spinner,
    so a short bounded poll is a better discriminator than a single cached pane.
    """
    for i in range(max(1, attempts)):
        busy = []
        for name in names:
            try:
                if _cc_pane_busy(await _cc_capture_pane_fresh(name)):
                    busy.append(name)
            except Exception:  # noqa: BLE001
                continue
        busy = _cc_unique_names(busy)
        if busy or i == attempts - 1:
            return busy
        await asyncio.sleep(delay)
    return []


async def _cc_disambiguate_hook_name(body: dict, names: list[str],
                                     hook_sid: str) -> tuple[str | None, str]:
    names = _cc_unique_names(names)
    if not names:
        return None, "no_cwd_candidate"
    if len(names) == 1:
        return names[0], "cwd_unique"
    if hook_sid:
        matched = []
        for name in names:
            cached_sid = (_CC_SID_CACHE.get(name) or (0, None))[1]
            if (cached_sid == hook_sid or _CC_SID_PINS.get(name) == hook_sid
                    or hook_sid in (_CC_SID_HISTORY.get(name) or [])):
                matched.append(name)
        matched = _cc_unique_names(matched)
        if len(matched) == 1:
            return matched[0], "sid_history"
        if len(matched) > 1:
            return None, "sid_history_ambiguous"
    event = body.get("hook_event_name")
    if event == "Stop":
        active = [n for n in names if (_cc_fresh_hook_state(n) or {}).get("busy")]
        active = _cc_unique_names(active)
        if len(active) == 1:
            return active[0], "fresh_busy_state"
        if len(active) > 1:
            return None, "fresh_busy_state_ambiguous"
    if event == "UserPromptSubmit":
        # UserPromptSubmit 送達時 claude 行程還卡在等這個 hook 的 HTTP 回應
        # (hook 是 turn 開跑前同步執行的),TUI spinner 根本還沒畫出來——在
        # handler 裡同步輪詢 busy 永遠等不到(2026-07-15 實測:全數落
        # ambiguous_same_cwd)。改成 hook 回應後的延後輪詢,見
        # _cc_hook_deferred_disambiguate。
        return None, "needs_busy_poll"
    return None, "ambiguous_same_cwd"


def _cc_hook_commit(name: str, event: str, body: dict, hook_sid: str,
                    resolution: str) -> dict:
    """消歧成功後的統一寫入:sid 快取+pin、busy 狀態、log。"""
    if hook_sid:
        # 身分有把握 → hook sid 是權威。寫入 pin 後,即使 claude cmdline 還
        # 掛著舊 --resume uuid,下一輪 _cc_pane_session_id 也不會把 cache 洗回去。
        _cc_cache_sid(name, hook_sid, pin=True)
        try:
            _cc_write_resume_pin(name, hook_sid)
        except Exception:  # noqa: BLE001
            pass
    state = {"busy": event == "UserPromptSubmit", "updated_at": time.time(),
             "source": "hook"}
    if event == "Stop":
        state["last_assistant_message"] = body.get("last_assistant_message")
    if event == "UserPromptSubmit":
        # P0 修復(root cause #3):新 turn 開始 → 世代編號 +1,讓仍在跑的
        # _cc_interrupt_core 重試迴圈偵測「原目標 turn 已結束」,不再誤送
        # Escape 進新 turn。放在 commit 統一寫入點 → 延後消歧路徑也會 bump。
        _CC_TURN_GEN[name] = _CC_TURN_GEN.get(name, 0) + 1
    _CC_HOOK_STATE[name] = state
    _log_event("cc_hook_state",
               name=name,
               hook_event_name=event,
               busy=state["busy"],
               resolution=resolution,
               hook_sid_hash=_short_hash(hook_sid),
               cwd_hash=_short_hash(str(body.get("cwd") or "")),
               last_assistant_message_chars=len(str(body.get("last_assistant_message") or "")))
    return state


# 延後 busy 輪詢的參數:hook 回 200 後 claude 才會開跑,spinner 通常在
# ~0.5-2.5s 內出現;拉 8s 窗口涵蓋慢機器。測試會把這兩個值 patch 小。
_CC_HOOK_BUSY_POLL_ATTEMPTS = 20
_CC_HOOK_BUSY_POLL_DELAY = 0.4
_CC_HOOK_BG_TASKS: set = set()   # 防 GC;done_callback 自清


async def _cc_hook_deferred_disambiguate(body: dict, names: list[str],
                                         hook_sid: str) -> None:
    """UserPromptSubmit 的同 cwd 消歧延後版:等 hook 已回應、TUI 真正開跑
    畫出 busy 後再輪詢候選 pane;唯一 busy 者即為事主。"""
    try:
        busy = _cc_unique_names(await _cc_busy_hook_candidates(
            names, attempts=_CC_HOOK_BUSY_POLL_ATTEMPTS,
            delay=_CC_HOOK_BUSY_POLL_DELAY))
        if len(busy) == 1:
            _cc_hook_commit(busy[0], "UserPromptSubmit", body, hook_sid,
                            "pane_busy_deferred")
            return
        _log_event("cc_hook_ambiguous",
                   hook_event_name="UserPromptSubmit",
                   candidate_count=len(names),
                   reason=("pane_busy_deferred_ambiguous" if busy
                           else "pane_busy_deferred_none"),
                   hook_sid_hash=_short_hash(hook_sid),
                   cwd_hash=_short_hash(str(body.get("cwd") or "")))
    except Exception as e:  # noqa: BLE001
        _log_event("cc_hook_deferred_error", error=type(e).__name__,
                   error_message=str(e)[:160])


def _cc_prompt(pane: str):
    """Detect a Claude Code interactive choice prompt so the app can render real
    buttons. Returns {kind,title,options:[{key,label}]} or None.
    Two shapes: (1) AskUserQuestion / generic numbered menu — anchored on the
    "Enter to select" footer, labels can be ANY language (a keyword filter here
    made every Chinese question invisible to the app); (2) permission prompts —
    the original STRICT keyword path, kept as fallback for older layouts.
    Never when working."""
    low = pane.lower()
    if "esc to interrupt" in low or _CC_BUSY_RE.search(pane):
        return None
    lines = pane.splitlines()
    tail = lines[-16:]                  # the prompt always sits at the bottom
    tail_low = "\n".join(tail).lower()
    # (1) generic choice menu: the selection footer only exists while a menu is
    # live, so numbered lines above it ARE the options — no keyword gate needed.
    if "enter to select" in tail_low:
        wide = lines[-28:]              # room for options with description lines
        opts, first_opt_at = [], None
        for i, ln in enumerate(wide):
            s = ln.strip().lstrip("❯>•· ").strip()
            m = _CC_OPT_NUM_RE.match(s)
            if m:
                if first_opt_at is None:
                    first_opt_at = i
                opts.append({"key": m.group(1), "label": m.group(2).strip()})
        if len(opts) >= 2:
            title = ""
            for ln in reversed(wide[:first_opt_at or 0]):
                s = ln.strip()
                if not s or set(s) <= {"─", "-", "═"} or s[0] in "☐☑":
                    continue
                title = s[:140]
                break
            # A1 semantic:泛選單(AskUserQuestion 等)= Approval Hub 的 question,
            # 不是 permission — app 端永不再用 label 猜語意。kind 欄位維持
            # "menu"(app 現行相容),語意走新增的 semantic 欄位。
            return {"kind": "menu", "semantic": "question", "title": title,
                    "options": opts[:6]}
    has_context = any(k in tail_low for k in ("wants to", "do you want", "proceed?", "would you like"))
    if has_context:
        opts = []
        for ln in tail:
            s = ln.strip().lstrip("❯>•· ").strip()
            m = _CC_OPT_NUM_RE.match(s)
            if m and re.search(r"allow|deny|yes|no|proceed|don.t|reject|approve", s, re.IGNORECASE):
                opts.append({"key": m.group(1), "label": m.group(2).strip()})
            elif _CC_OPT_LABEL_RE.match(s):
                opts.append({"key": str(len(opts) + 1), "label": s[:50]})
        if opts:
            title = next((ln.strip()[:140] for ln in tail
                          if "wants to" in ln.lower() or "do you want" in ln.lower()), "")
            return {"kind": "menu", "semantic": "permission", "title": title,
                    "options": opts[:5]}
    if re.search(r"\(y/n\)|press y\b|y to (confirm|continue|proceed)", tail_low):
        return {"kind": "yesno", "semantic": "permission", "title": "",
                "options": [{"key": "y", "label": "是"}, {"key": "n", "label": "否"}]}
    return None


@app.post("/ccsessions/_hook")
async def cc_session_hook(request: Request):
    host = _client_host(request)
    if host not in ("127.0.0.1", "::1", "localhost"):
        _check_auth(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": True, "ignored": True}
    if not isinstance(body, dict):
        return {"ok": True, "ignored": True}
    event = body.get("hook_event_name")
    if event not in ("UserPromptSubmit", "Stop"):
        return {"ok": True, "ignored": True}
    # 同 workdir 撞名時只在能唯一消歧時才把 hook 記到某個 name;拒絕
    # names[:1] 猜測,避免 Main/cc-* 同 cwd 時把 busy 與 sid 寫到錯的 session。
    hook_sid, sid_error = _cc_hook_sid(body)
    if sid_error:
        return {"ok": True, "ignored": True, "reason": sid_error}
    transcript_path = _cc_hook_transcript_path(body)
    if transcript_path and not _cc_transcript_path_matches_cwd(
            transcript_path, body.get("cwd")):
        return {"ok": True, "ignored": True, "reason": "transcript_cwd_mismatch"}
    all_names = _cc_names_for_cwd(body.get("cwd"))
    name, resolution = await _cc_disambiguate_hook_name(body, all_names, hook_sid)
    if not name and resolution == "needs_busy_poll":
        # 同 cwd 多候選、快速判據都對不上 → 回應 hook 放行 claude 開跑,
        # 背景任務等 spinner 出現後用「唯一 busy pane」認人。
        task = asyncio.create_task(
            _cc_hook_deferred_disambiguate(body, list(all_names), hook_sid))
        _CC_HOOK_BG_TASKS.add(task)
        task.add_done_callback(_CC_HOOK_BG_TASKS.discard)
        return {"ok": True, "deferred": True, "reason": "busy_poll_deferred"}
    if not name:
        _log_event("cc_hook_ambiguous",
                   hook_event_name=event,
                   candidate_count=len(all_names),
                   reason=resolution,
                   hook_sid_hash=_short_hash(hook_sid),
                   cwd_hash=_short_hash(str(body.get("cwd") or "")))
        return {"ok": True, "ignored": True, "reason": resolution}
    state = _cc_hook_commit(name, event, body, hook_sid, resolution)
    return {"ok": True, "session": name, "busy": state["busy"], "source": "hook"}


# ── TG→Pocket 鏡像 ingest(XW-BRIDGE-TGMIRROR-20260714-340A)─────────────
# 四個 hermes gateway 的 pocket_mirror hook(hermes-agent home*/hooks/
# pocket_mirror/handler.py)對每則 TG 往來 POST 一筆事件到這裡:
# inbound(agent:start, role=user)= 使用者在 TG 說的話;
# outbound(agent:end, role=assistant)= 人格的 TG 回覆。
# 寫進 canonical store 後,GET /app/v1/messages 與卡片流的三來源合併
# 自然把它帶進 Pocket 人格對話,不用等 state.db watcher 的掃描週期。
#
# 冪等/防回聲雙寫(這條路和 state.db 掃描是同一則訊息的兩個來源):
# 1. mid 由事件內容決定(tgm-<sha1(session|chat|thread|anchor|role|
#    content-hash)>)→ hook 重送/重放同一事件 INSERT OR REPLACE 落同一列。
# 2. state.db 掃出的同一則訊息由合併端 10 分鐘同文壓重擋掉(_tg_dup,
#    雙 role — 見 _hp_merged_messages / GET /app/v1/messages)。
# 3. user 內容先過 _tg_extract_attachments + _tg_clean_content —— 與
#    _persona_history 的 state.db 讀取路徑同一套清洗,兩條路落出同一種
#    文字,同文壓重才對得上。
@app.post("/internal/v1/mirror/telegram-event")
async def tg_mirror_event(request: Request):
    host = _client_host(request)
    if host not in ("127.0.0.1", "::1", "localhost"):
        _check_auth(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    if not isinstance(body, dict):
        return {"ok": True, "ignored": True, "reason": "bad_body"}
    if (body.get("platform") or "") != "telegram":
        return {"ok": True, "ignored": True, "reason": "platform"}
    session = str(body.get("session") or "")
    role = str(body.get("role") or "")
    content = str(body.get("content") or "")
    if session not in PERSONAS:
        _log_event("tg_mirror_unknown_session", session=session)
        return {"ok": True, "ignored": True, "reason": "session"}
    if role not in ("user", "assistant"):
        return {"ok": True, "ignored": True, "reason": "role"}
    attachments: list = []
    if role == "user":
        content, attachments = _tg_extract_attachments(content)
        content = _tg_clean_content(content) or ""
    if not content.strip() and not attachments:
        # 整條都是 runtime 注入(剝完全空)或 gateway 送了空事件 → 不落地。
        return {"ok": True, "ignored": True, "reason": "empty"}
    try:
        ts = float(body.get("ts") or 0)
    except Exception:  # noqa: BLE001
        ts = 0.0
    now = time.time()
    if not (now - 86400 * 366 < ts < now + 3600):
        ts = now      # 時間戳缺席/離譜(時鐘歪掉)→ 用收件時間,別排進遠古
    # anchor = gateway 的 reply-anchor message_id(inbound/outbound 同一turn
    # 共用,role 區分)。缺席時退回 10 分鐘時間桶 —— 不同回合的同文不能
    # 互相覆蓋,而 hook 重放帶原 ts,同桶仍冪等。
    chash = hashlib.sha1(content.encode("utf-8", "replace")).hexdigest()
    anchor = str(body.get("message_id") or "") or f"t{int(ts // 600)}"
    basis = "|".join((session, str(body.get("chat_id") or ""),
                      str(body.get("thread_id") or ""), anchor, role, chash))
    mid = "tgm-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:24]
    _, ok = _canon_add(session, role, content, attachments, mid=mid,
                       created_at=ts, push=False)
    _log_event("tg_mirror_event", session=session, role=role, stored=ok,
               mid=mid, event_type=str(body.get("event_type") or ""),
               content_chars=len(content), attachment_count=len(attachments))
    return {"ok": ok, "session": session, "id": mid, "stored": ok}


async def _cc_status_core(name: str) -> dict:
    """CC session 的 busy/mode/prompt 判讀 — /ccsessions status 端點與
    Phase 0 卡片 follower 共用同一份真相。"""
    if not await _tmux_alive(name):
        return {"busy": False, "running": False, "mode": None, "prompt": None}
    pane = await _tmux_capture_cached(name)
    hook_state = _cc_fresh_hook_state(name)
    if hook_state:
        busy = bool(hook_state.get("busy"))
    else:
        busy = bool(_CC_BUSY_RE.search(pane)) or ("esc to interrupt" in pane.lower())
    low = pane.lower()
    # S3 (wave 2): this box's Claude Code cycles FOUR states on shift+tab —
    # normal → accept edits → plan → auto mode → normal. "accept edits" and
    # "auto mode" used to both report as "auto", which made the app's mode
    # picker snap back; they are distinct now (contract: normal|acceptEdits|
    # plan|auto).
    if "plan mode on" in low:
        mode = "plan"
    elif "accept edits on" in low:
        mode = "acceptEdits"
    elif "auto mode on" in low or "bypass" in low:
        mode = "auto"
    elif busy:
        # A running turn replaces the bottom bar with "esc to interrupt" — the
        # mode marker is hidden, not absent. Claiming "normal" here made the app
        # snap the user's pick back to 一般 on the next 1.2s reconcile.
        mode = None
    else:
        mode = "normal"
    prompt = _cc_prompt(pane)
    # wave 2: usage meter + full plan text from the transcript jsonl.
    row = next((r for r in _cc_conf_rows() if r[0] == name), None)
    jsonl = await _cc_session_jsonl(name, row[1]) if row else None
    # AskUserQuestion 完整內容(問題全文 + 選項 description)從 jsonl 讀,取代終端
    # 截斷的螢幕擷取。pane-scrape 偵測到 question 選單 → 換成 jsonl 全文(修「太簡略」);
    # pane 漏抓且沒在忙 → 也用 jsonl 補上(修「沒跳出來」);忙碌時不補,避免被跳脫的
    # 殘留 ask 誤觸。權限 y/n(非 tool_use)仍走 pane-scrape。
    if jsonl:
        ask = _cc_pending_ask(jsonl)
        if ask and (
            (isinstance(prompt, dict) and prompt.get("semantic") == "question")
            or (prompt is None and not busy)
        ):
            prompt = ask
    st = {"busy": busy, "running": True, "mode": mode, "prompt": prompt}
    if jsonl:
        usage, plan = _cc_scan_jsonl(jsonl)
        if usage:
            st["usage"] = usage
        if prompt and plan and "plan" in low:
            # The live prompt is a plan approval — hand the app the COMPLETE
            # plan markdown (the pane preview is truncated by the TUI).
            prompt["plan"] = plan
    return st


@app.get("/ccsessions/{name}/status")
async def cc_session_status(name: str, request: Request):
    _check_auth(request)
    if not any(r[0] == name for r in _cc_conf_rows()):
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    st = await _cc_status_core(name)
    if not st["running"]:
        return {"busy": False, "running": False}
    return st


# Send a single control key into the live TUI (arrows / Enter / Esc / Tab /
# Shift-Tab / y / n / digits) so interactive prompts, menus and plan-mode toggle
# can be driven from the phone — closing the gap vs the desktop Claude Code app.
_CC_KEYS = {
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "enter": "Enter", "escape": "Escape", "esc": "Escape",
    "tab": "Tab", "btab": "BTab", "shift-tab": "BTab", "space": "Space",
}


@app.post("/ccsessions/{name}/key")
async def cc_session_key(name: str, request: Request):
    _check_auth(request)
    if not any(r[0] == name for r in _cc_conf_rows()):
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    body = await request.json()
    raw = str(body.get("key") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="key required")
    return await _cc_key_core(name, raw)


async def _cc_key_core(name: str, raw: str) -> dict:
    """cc 控制鍵核心 — v1 與 v2 統一路由(key/approve)共用。"""
    if not await _tmux_alive(name):
        raise http_err(409, "SESSION_NOT_RUNNING", "session not running")
    args = ["send-keys", "-t", name]
    mapped = _CC_KEYS.get(raw.lower())
    submit_after_key = False
    if mapped:
        args.append(mapped)                  # named control key
    elif len(raw) == 1 and raw.isprintable():
        # 選單/是否題的單字元答案(y/n/1-9)—— 送出前先拿「現在」的畫面重驗一次
        # 是不是真的還有相符的選項在等。App 端的選單清單可能是稍早抓的:如果
        # CLI 這段時間自己把提示解掉了(auto-accept、逾時、或已經被別的方式
        # 回掉),畫面上其實已經沒有選單、focus 落在自由輸入框 —— 這時候盲送
        # 字元只會變成打進聊天框的垃圾字(而且送不出去,因為沒送 Enter),使用
        # 者會看到同一顆「核准」不斷冒出來、字元越疊越多卻永遠沒有真的解掉。
        # 沒有相符選項就直接拒絕,好過默默打錯地方。
        _PANE_CACHE.pop(name, None)           # 強制拿最新畫面,不吃快取
        pane_now = await _tmux_capture_cached(name)
        prompt_now = _cc_prompt(pane_now)
        # Keep key validation in sync with /status: AskUserQuestion details may
        # only be visible in the transcript jsonl, while the pane has a trimmed
        # or transient rendering. Still avoid resurrecting stale asks mid-turn.
        low_now = pane_now.lower()
        busy_now = bool(_CC_BUSY_RE.search(pane_now)) or ("esc to interrupt" in low_now)
        row = next((r for r in _cc_conf_rows() if r[0] == name), None)
        if row and (
            (isinstance(prompt_now, dict) and prompt_now.get("semantic") == "question")
            or (prompt_now is None and not busy_now)
        ):
            jsonl = await _cc_session_jsonl(name, row[1])
            ask = _cc_pending_ask(jsonl) if jsonl else None
            if ask:
                prompt_now = ask
        valid_keys = {str(o.get("key") or "").lower()
                      for o in (prompt_now or {}).get("options", [])}
        if not prompt_now or raw.lower() not in valid_keys:
            raise http_err(409, "PROMPT_STALE", "no matching live prompt right now",
                           "the on-screen menu may already be resolved — refresh and retry")
        # AskUserQuestion / generic question menus need a real submit. Sending
        # only "1"/"2"/"3" leaves the digit in the TUI selection field on some
        # Claude Code layouts; permission prompts keep the old single-key path.
        submit_after_key = (prompt_now or {}).get("semantic") == "question"
        args += ["-l", raw]                  # literal single char (y / n / 1-3)
    else:
        raise HTTPException(status_code=400, detail="unsupported key")
    rc, _, err = await _tmux_run(*args)
    if rc:
        raise http_err(502, "TMUX_FAILED", "tmux send-keys failed",
                       err[:200] or "send-keys failed")
    if submit_after_key:
        await asyncio.sleep(0.08)
        rc_enter, _, err_enter = await _tmux_run("send-keys", "-t", name, "Enter")
        if rc_enter:
            raise http_err(502, "TMUX_FAILED", "tmux send-keys enter failed",
                           err_enter[:200] or "send-keys Enter failed")
    # The key just changed the TUI (mode toggle, menu pick) — a cached pane
    # would feed the app a pre-keystroke mode/prompt for up to TTL seconds.
    _PANE_CACHE.pop(name, None)
    return {"ok": True}


# ─────────── 批次 3 斷點③:CC waiting_approval → approval feed + 推播 ────────
# persona(Approval Center)/CX(app-server request)本來就有 approval 記錄;CC 的
# 「審核」是 TUI prompt,這裡補一個常駐 watcher:prompt 出現 → 建記錄+推播,
# prompt 消失 → 過期。decide 回流在 _approval_decide_core 的 claude_code 分支
# (送 TUI 鍵),三線同一條 approval 管線(批次 3 完成判準)。

_CC_APPROVAL_ACTIVE: dict = {}      # name -> {"aid": str, "sig": str}
_CC_APPROVAL_POLL_SECS = 1.5   # 4.0→1.5:審核偵測延遲主項;只巡 app-owned,可負擔
_CC_APPROVAL_TTL = 900.0

_CC_ALLOW_RE = re.compile(r"^(always allow|allow|yes)", re.IGNORECASE)
_CC_DENY_RE = re.compile(r"^(don.t allow|deny|no)", re.IGNORECASE)


def _cc_prompt_sig(prompt: dict) -> str:
    """同一個 prompt 的穩定簽名 — watcher 每 tick 都看到它,不能重複建。"""
    raw = json.dumps([prompt.get("title"),
                      [(o.get("key"), o.get("label"))
                       for o in prompt.get("options") or []]],
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def _cc_find_pending_aid(name: str, sig: str) -> str | None:
    """重啟/記憶體遺失後,認領 DB 裡同一 live prompt 的既有 pending 審核。
    背景:`_CC_APPROVAL_ACTIVE` 是行程內狀態,重啟即清空,但 DB 的 pending 列
    還在;watcher 若 active=None 就盲建新 aid,App 手上的舊 aid 立刻變孤兒——
    按了 `active.aid != aid` 吃 409、鍵送不進 TUI(2026-07-16 使用者回報「在
    Pocket 按 CC 審核沒反應,得回 CC 裡按」的根因)。這裡以重建的 prompt sig
    對映既有 pending 列;找到就認領該 aid(不另建),找不到回 None。"""
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        rows = con.execute(
            "SELECT id,title,options FROM approvals "
            "WHERE source=? AND status='pending' ORDER BY created_at DESC",
            (f"claude_code:{name}",)).fetchall()
        con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("cc_approval_adopt_query_error", session=name, error=str(e)[:160])
        return None
    for rid, title, options in rows:
        try:
            opts = json.loads(options) if options else []
        except Exception:  # noqa: BLE001
            opts = []
        if _cc_prompt_sig({"title": title, "options": opts}) == sig:
            return rid
    return None


def _cc_reseed_approvals_from_db() -> int:
    """啟動時把 DB 的 pending CC 審核重新灌回 `_CC_APPROVAL_ACTIVE`,補上重啟
    清空記憶體與 watcher 首巡(≤1.5s)之間的空窗。同一 session 多筆 pending 時
    留最新一筆、其餘標 expired(收孤兒重複列)。回重灌筆數。"""
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        rows = con.execute(
            "SELECT id,source,title,options FROM approvals "
            "WHERE source LIKE 'claude_code:%' AND status='pending' "
            "ORDER BY created_at DESC").fetchall()
        con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("cc_approval_reseed_error", error=str(e)[:160])
        return 0
    seen: set = set()
    n = 0
    for rid, source, title, options in rows:
        name = source.split(":", 1)[1] if ":" in source else source
        if name in seen:
            _cc_approval_set_status(rid, "expired")   # 同 session 舊的重複列 → 收掉
            continue
        seen.add(name)
        try:
            opts = json.loads(options) if options else []
        except Exception:  # noqa: BLE001
            opts = []
        sig = _cc_prompt_sig({"title": title, "options": opts})
        _CC_APPROVAL_ACTIVE[name] = {"aid": rid, "sig": sig}
        n += 1
    if n:
        _log_event("cc_approval_reseeded", count=n)
    return n


def _cc_choice_key(prompt: dict, approve: bool) -> str:
    """approve 布林 → prompt option key。認得 allow/deny 字樣就精準選;
    認不得時 approve=第一個選項、deny=Esc(TUI 的通用取消)。"""
    options = prompt.get("options") or []
    pat = _CC_ALLOW_RE if approve else _CC_DENY_RE
    for o in options:
        if pat.match(str(o.get("label") or "").strip()):
            return str(o.get("key") or "")
    if approve and options:
        return str(options[0].get("key") or "")
    return "esc"


def _cc_approval_set_status(aid: str, status: str) -> bool:
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        cur = con.execute("UPDATE approvals SET status=?, decided_at=? "
                          "WHERE id=? AND status='pending'",
                          (status, time.time(), aid))
        con.commit()
        con.close()
        return bool(cur.rowcount)
    except Exception as e:  # noqa: BLE001
        _log_event("cc_approval_db_error", approval_id=aid, error=str(e)[:160])
        return False


def _cc_approval_create(name: str, prompt: dict) -> str:
    import sqlite3
    aid = "cc-" + uuid.uuid4().hex[:24]
    title = (prompt.get("title") or "").strip() or f"{name} 等待核准"
    opts_txt = " / ".join(str(o.get("label") or "")[:30]
                          for o in (prompt.get("options") or [])[:4])
    detail = f"session: {name}\n{title}" + (f"\n選項: {opts_txt}" if opts_txt else "")
    # A1:語意在誕生點標好 — _cc_prompt 的分支即分類(泛選單=question、
    # 權限/yesno=permission);permission 的鍵由 bridge 標 style,app 只渲染,
    # 不再用 label 猜「哪顆是拒絕」。question 無 danger 語意(spec §2)。
    kind = "question" if prompt.get("semantic") == "question" else "permission"
    options = []
    for o in (prompt.get("options") or [])[:6]:
        okey = str(o.get("key") or "").strip()
        if not okey:
            continue
        ent = {"key": okey, "label": str(o.get("label") or "")[:80]}
        if kind == "permission":
            lab = ent["label"].strip()
            if _CC_DENY_RE.match(lab):
                ent["style"] = "danger"
            elif _CC_ALLOW_RE.match(lab):
                ent["style"] = "primary"
            else:
                ent["style"] = "secondary"
        options.append(ent)
    now = time.time()
    con = sqlite3.connect(CANON_DB, timeout=30)
    con.execute("INSERT OR REPLACE INTO approvals"
                "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result,callback,"
                "session_id,provider,kind,options) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (aid, title, f"claude_code:{name}",
                 "high" if kind == "permission" else "low", detail,
                 now, now + _CC_APPROVAL_TTL, "pending", None, None, None,
                 f"claude_code:{name}", "claude_code", kind,
                 json.dumps(options, ensure_ascii=False) if options else None))
    con.commit()
    con.close()
    return aid


# ── 2b:人格 choices 卡 → 審核中心(2026-07-16 XCash 拍板:所有 choices 卡進、
#    中心只放決策鈕、純連結選項留聊天)。FLiPER 複審卡等走選擇閘道契約,原本只是
#    人格報告/訊息、不進 approvals 表 → 中心看不到。這裡定期掃 report_events,把
#    kind:choices 卡同步成 hermes pending 審核;決議時把選項 send 文字當人格回合送回。
_HP_CHOICES_SCAN_WINDOW = 6 * 3600     # 掃近 6h 的卡(含已解除的,好把殭屍審核收掉);
_HP_CHOICES_TTL = 12 * 3600            # 建了之後 pending 最多留 12h(未決自動過期)。
_HP_CHOICES_POLL_SECS = 30.0
_HP_CHOICES_FENCE = "```studio-card"

# 即時待檢討真相來源:report 卡是快照,審核可能已在 FLiPER/TG 那邊解除(resume)。
# 只靠 report 建審核會產生殭屍(2026-07-16 使用者回報:審查已結束卻還能按)。
# review_pipeline.json 是 FLiPER 待檢討狀態機的落地檔(resume/hold 都寫它),
# 用它驗證某貼文『當下是否真的還在 held(待檢討)』。
FLIPER_REVIEW_STATE = os.path.expanduser(
    "~/apps/lobster-tg/workspace/state/review_pipeline.json")


def _fliper_review_state() -> dict | None:
    """FLiPER 待檢討狀態(貼文 id → 記錄);讀不到回 None(狀態未知,不動作)。"""
    try:
        with open(FLIPER_REVIEW_STATE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _fliper_ref_held(state: dict, ref: str) -> bool:
    """該貼文當下是否還在待檢討(任一審查階段 held=true)。"""
    v = state.get(ref) if state else None
    return bool(isinstance(v, dict)
                and (v.get("first_review_held") or v.get("second_review_held")))


def _hp_extract_choices(content: str) -> dict | None:
    """從內容抽第一張 kind:choices 的 studio-card;無/壞則 None。"""
    if _HP_CHOICES_FENCE not in content or '"choices"' not in content:
        return None
    i = content.find(_HP_CHOICES_FENCE)
    after = content[i + len(_HP_CHOICES_FENCE):]
    end = after.find("```")
    if end < 0:
        return None
    try:
        card = json.loads(after[:end].strip())
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(card, dict) or card.get("kind") != "choices" or not card.get("options"):
        return None
    return card


def _hp_choices_stable_id(persona: str, card: dict) -> str:
    ref = str(card.get("ref") or card.get("title") or "")
    return "hpc-" + hashlib.sha1(f"{persona}|{ref}".encode("utf-8", "replace")).hexdigest()[:24]


def _hp_choices_upsert(persona: str, card: dict) -> str | None:
    """建一筆人格 choices 審核(session_id=hermes:{persona},kind=question)。只收
    帶 send 的決策鈕(純連結選項有 url → 留聊天,不進中心)。已決議的同卡不復活。"""
    import sqlite3
    if persona not in PERSONAS:
        return None
    decision_opts = []
    for o in card.get("options") or []:
        if o.get("url"):
            continue                                   # 純連結鈕不進中心(拍板)
        key = str(o.get("key") or "").strip()
        send = o.get("send") or o.get("label")
        if not key or not send:
            continue
        decision_opts.append({"key": key, "label": str(o.get("label") or "")[:80],
                              "style": o.get("style") or "primary", "send": str(send)})
    if not decision_opts:
        return None
    aid = _hp_choices_stable_id(persona, card)
    now = time.time()
    con = sqlite3.connect(CANON_DB, timeout=30)
    row = con.execute("SELECT status FROM approvals WHERE id=?", (aid,)).fetchone()
    if row:
        con.close()
        return aid if row[0] == "pending" else None    # 已在/已決議 → 不重寫、不復活
    title = str(card.get("title") or "需要你選擇")[:200]
    detail = str(card.get("detail") or "")[:400]
    con.execute("INSERT INTO approvals"
                "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result,callback,"
                "session_id,provider,kind,options) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (aid, title, f"hermes:{persona}", "low", detail,
                 now, now + _HP_CHOICES_TTL, "pending", None, None, None,
                 f"hermes:{persona}", "hermes", "question",
                 json.dumps(decision_opts, ensure_ascii=False)))
    con.commit()
    con.close()
    _log_event("hp_choices_approval_created", session=persona, approval_id=aid,
               title=title[:60], options=len(decision_opts))
    return aid


def _hp_choices_expire(aid: str) -> None:
    """把一筆殭屍 choices 審核收掉(已在 FLiPER 解除,不再需要決策)。"""
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        cur = con.execute("UPDATE approvals SET status='expired', decided_at=? "
                          "WHERE id=? AND status='pending'", (time.time(), aid))
        con.commit()
        n = cur.rowcount
        con.close()
        if n:
            _log_event("hp_choices_approval_expired", approval_id=aid, reason="resolved_upstream")
    except Exception as e:  # noqa: BLE001
        _log_event("hp_choices_expire_failed", approval_id=aid, error=str(e)[:160])


def _hp_choices_scan() -> int:
    """掃近 _HP_CHOICES_SCAN_WINDOW 的人格報告,同步 choices 卡到審核中心。
    真相以 FLiPER review_pipeline.json 的即時 held 狀態為準:確實還在待檢討 → 建;
    已解除 → 收掉殭屍。無法對映 FLiPER 貼文(非複審卡/查不到狀態)→ 保守不進中心
    (避免無從得知解除的殭屍),那類卡的按鈕仍在聊天視窗可用。回新建數。"""
    import sqlite3
    since = time.time() - _HP_CHOICES_SCAN_WINDOW
    state = _fliper_review_state()
    if state is None:
        return 0                            # 狀態未知 → 這輪不動作(不建、不亂 expire)
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        rows = con.execute(
            "SELECT session, content FROM report_events WHERE ts > ? "
            "AND content LIKE '%```studio-card%' AND content LIKE '%\"choices\"%' "
            "ORDER BY rowid DESC LIMIT 100", (since,)).fetchall()
        con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("hp_choices_scan_failed", error=str(e)[:160])
        return 0
    created, seen = 0, set()
    for session, content in rows:
        if session not in PERSONAS:
            continue
        card = _hp_extract_choices(content or "")
        if not card:
            continue
        ref = str(card.get("ref") or "").strip()
        if not ref or ref not in state:
            continue                        # 非 FLiPER 貼文 / 查不到 → 不進中心
        sid = _hp_choices_stable_id(session, card)
        if sid in seen:
            continue
        seen.add(sid)
        if _fliper_ref_held(state, ref):
            if _hp_choices_upsert(session, card):
                created += 1
        else:
            _hp_choices_expire(sid)         # 已解除 → 收掉殭屍(若存在且 pending)
    return created


async def _hp_choices_watcher():
    """常駐:定期把人格 choices 卡同步成審核中心 pending 列。"""
    while True:
        await asyncio.sleep(_HP_CHOICES_POLL_SECS)
        try:
            _hp_choices_scan()
        except Exception as e:  # noqa: BLE001
            _log_event("hp_choices_watcher_error", error=str(e)[:160])


async def _cc_approval_watcher():
    """常駐:每 1.5s 巡一輪 owned CC sessions,強制拿新 pane(不吃 5s 快取)。
    舊配置(4s 間隔 + 5s 舊 pane)讓「prompt 出現→建 approval」最壞 ~9 秒;
    現在 ≤1.5s。只巡 app-owned(通常 1-7 條),capture-pane 一次 ~10-20ms,
    可負擔;順帶把新鮮 pane 回填快取給首頁清單用。"""
    while True:
        await asyncio.sleep(_CC_APPROVAL_POLL_SECS)
        # 作用域 v2:enabled ccsess 一律掃(訂閱制),排除靠 approvals-exclude.txt。
        # 舊制只掃 app-owned,那批 session 死光後名單空,watcher 靜默斷炊六天
        # (2026-07-10~16 零 approval)——聊天窗選項卡/審核中心/推播整條跟著死。
        scope = _cc_approval_scope_names()
        for name, _workdir, enabled in _cc_conf_rows():
            if enabled != "1":
                continue
            if name not in scope:
                continue
            try:
                _PANE_CACHE.pop(name, None)   # 審核偵測不能吃舊畫面
                st = await _cc_status_core(name)
                prompt = st.get("prompt")
                active = _CC_APPROVAL_ACTIVE.get(name)
                if prompt:
                    sig = _cc_prompt_sig(prompt)
                    if active and active["sig"] == sig:
                        continue                     # 同一個 prompt,已建過
                    if active:
                        _cc_approval_set_status(active["aid"], "expired")
                        try:
                            _cc_cards_feed_approval(
                                name, _approval_get_row(active["aid"]) or {},
                                resolved="expired")
                        except Exception as e:  # noqa: BLE001
                            _log_event("cc_cards_feed_error", error=str(e)[:160])
                    # 記憶體沒有(常見於重啟後)→ 先認領 DB 既有的同一 prompt
                    # pending 列,別另建新 aid 讓 App 手上的舊 aid 變孤兒(按了
                    # 吃 409、TUI 收不到鍵)。認領到就沿用該 aid,不重推。
                    adopted = _cc_find_pending_aid(name, sig)
                    if adopted:
                        _CC_APPROVAL_ACTIVE[name] = {"aid": adopted, "sig": sig}
                        _log_event("cc_approval_adopted", session=name,
                                   approval_id=adopted)
                        continue
                    aid = _cc_approval_create(name, prompt)
                    _CC_APPROVAL_ACTIVE[name] = {"aid": aid, "sig": sig}
                    opts = " / ".join(str(o.get("label") or "")[:20]
                                      for o in (prompt.get("options") or [])[:3])
                    _approval_push(aid, prompt.get("title") or f"{name} 等待核准",
                                   f"{name}" + (f" · {opts}" if opts else ""),
                                   f"claude_code:{name}")
                    try:
                        # A3:CC 卡片流補齊 — pending → approval 卡(三 provider
                        # 同一組 wire shape,見 carddigest.ApprovalCardMixin)。
                        _cc_cards_feed_approval(name, _approval_get_row(aid) or {})
                    except Exception as e:  # noqa: BLE001
                        _log_event("cc_cards_feed_error", error=str(e)[:160])
                    _log_event("cc_approval_created", session=name,
                               approval_id=aid)
                elif active:
                    # prompt 消失(TUI 上被回掉/回合結束)→ 記錄過期,feed 不留殭屍
                    rec = _approval_get_row(active["aid"]) or {}
                    _cc_approval_set_status(active["aid"], "expired")
                    try:
                        _cc_cards_feed_approval(name, rec, resolved="expired")
                    except Exception as e:  # noqa: BLE001
                        _log_event("cc_cards_feed_error", error=str(e)[:160])
                    _CC_APPROVAL_ACTIVE.pop(name, None)
            except Exception as e:  # noqa: BLE001
                _log_event("cc_approval_watch_error", session=name,
                           error=str(e)[:160])


# S3 (wave 2): one-tap CC permission-mode / model switching. shift+tab cycles
# FOUR states on this box (normal → accept edits → plan → auto mode → normal),
# so instead of blind-counting presses we close the loop: press → fresh pane →
# check, up to 6 presses. Immune to cycle-order drift across CC versions.
_CC_MODES = ("normal", "acceptEdits", "plan", "auto")
_CC_MODE_ALIASES = {"default": "normal"}   # older app builds say "default"
_CC_MODE_MAX_PRESSES = 6


async def _cc_mode_fresh(name: str) -> str | None:
    _PANE_CACHE.pop(name, None)
    st = await _cc_status_core(name)
    return st.get("mode")


@app.post("/ccsessions/{name}/mode")
async def cc_session_mode(name: str, request: Request):
    """Switch the CC permission mode. body {"mode": "normal"|"acceptEdits"|
    "plan"|"auto"}. Sends shift+tab (BTab) and VERIFIES via the pane's bottom
    bar after each press; replies with the mode actually reached."""
    _check_auth(request)
    if not any(r[0] == name for r in _cc_conf_rows()):
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    if not await _tmux_alive(name):
        raise http_err(409, "SESSION_NOT_RUNNING", "session not running")
    body = await request.json()
    raw = str(body.get("mode") or "").strip()
    target = _CC_MODE_ALIASES.get(raw, raw)
    if target not in _CC_MODES:
        raise HTTPException(status_code=400,
                            detail=f"mode must be one of {'|'.join(_CC_MODES)}")
    mode = await _cc_mode_fresh(name)
    if mode is None:
        # A running turn hides the bottom-bar mode marker — a blind toggle
        # could not be verified, so refuse instead of guessing.
        raise http_err(409, "CC_BUSY", "turn running; mode bar hidden — retry when idle")
    presses = 0
    while mode != target and presses < _CC_MODE_MAX_PRESSES:
        rc, _, err = await _tmux_run("send-keys", "-t", name, "BTab")
        if rc:
            raise http_err(502, "TMUX_FAILED", "tmux send-keys failed",
                           err[:200] or "send-keys failed")
        presses += 1
        await asyncio.sleep(0.35)          # let the TUI repaint the bottom bar
        mode = await _cc_mode_fresh(name)
    _log_event("cc_mode_switch", session=name, target=target,
               reached=mode, presses=presses)
    if mode != target:
        raise http_err(502, "MODE_UNREACHED",
                       f"sent {presses} shift+tab, pane reports {mode or 'unknown'}")
    return {"ok": True, "mode": mode, "presses": presses}


_CC_MODEL_RE = re.compile(r"^[A-Za-z0-9 ._/-]{1,60}$")


@app.post("/ccsessions/{name}/model")
async def cc_session_model(name: str, request: Request):
    """Switch the CC model by typing the /model slash command into the live
    TUI. body {"model": "opus"|"sonnet"|full model name}. Confirmation is
    best-effort: we re-capture the pane and report whether the requested name
    shows up (confirmed), but the command is sent either way."""
    _check_auth(request)
    if not any(r[0] == name for r in _cc_conf_rows()):
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    if not await _tmux_alive(name):
        raise http_err(409, "SESSION_NOT_RUNNING", "session not running")
    body = await request.json()
    model = str(body.get("model") or "").strip()
    if not _CC_MODEL_RE.match(model):
        raise HTTPException(status_code=400, detail="invalid model name")
    pane_before = await _cc_capture_pane_fresh(name)
    if _cc_pane_busy(pane_before):
        raise http_err(409, "CC_BUSY", "turn running; model switch needs an idle prompt")
    await _cc_paste_text(name, f"/model {model}")
    await asyncio.sleep(0.8)               # slash command feedback repaint
    _PANE_CACHE.pop(name, None)
    pane = await _cc_capture_pane_fresh(name)
    tail = "\n".join(pane.strip().splitlines()[-12:])
    confirmed = model.lower() in tail.lower()
    _log_event("cc_model_switch", session=name, model=model, confirmed=confirmed)
    return {"ok": True, "model": model, "confirmed": confirmed}


# ───────────────────────── /app/v2 control-plane facade ─────────────────────
# Additive: aggregates claude_code / codex / hermes into one Session shape
# (docs/CONTROL_PLANE_V2.md). CC sessions awaiting a permission prompt surface as
# status=waiting_approval so the app can list them. v1/ccsessions/codexsessions
# stay untouched.

async def _v2_cc_state(name: str):
    if not await _tmux_alive(name):
        return ("failed", None)
    _, pane, _ = await _tmux_run("capture-pane", "-p", "-t", name)
    prompt = _cc_prompt(pane)
    if prompt:
        return ("waiting_approval", prompt)
    busy = bool(_CC_BUSY_RE.search(pane)) or ("esc to interrupt" in pane.lower())
    return ("running" if busy else "idle", None)


@app.get("/app/v2/agents")
async def v2_agents(request: Request):
    _check_auth(request)
    return {"agents": [
        {"provider": "claude_code", "name": "Claude Code", "kind": "code_agent",
         "status": "ready", "auth": {"connected": True, "account": None}, "can_create": False},
        {"provider": "codex", "name": "Codex", "kind": "code_agent",
         "status": "ready", "auth": {"connected": True, "account": None}, "can_create": True},
        {"provider": "hermes", "name": "Hermes", "kind": "persona",
         "status": "ready", "auth": {"connected": True, "account": None}, "can_create": False},
    ]}


@app.get("/app/v2/sessions")
async def v2_sessions(request: Request, provider: str = "", status: str = ""):
    _check_auth(request)
    out = []
    degraded = []   # 取清單失敗的 provider(目前只有 codex 分支會標)
    out.extend(await _delegation_v2_sessions())
    for name, workdir, enabled in _cc_conf_rows():
        if enabled != "1":
            continue
        st, prompt = await _v2_cc_state(name)
        caps = ["input", "interrupt", "keys", "attachments", "replay", "follow"]
        if prompt:
            caps.append("approve")
        meta = {}
        if prompt:
            meta["prompt"] = prompt   # 相容期保留(A4 刪),app 舊版仍讀這裡
            # A1:meta.approval 統一物件 — 由 watcher 建的 DB 列對回。watcher
            # 巡週期 1.5s,prompt 剛出現的窄縫可能還沒有列 → 只給 prompt。
            active = _CC_APPROVAL_ACTIVE.get(name)
            d = _approval_get_row(str(active.get("aid"))) if active else None
            if d and d.get("status") == "pending":
                meta["approval"] = d
        out.append({"id": f"claude_code:{name}", "provider": "claude_code", "title": name,
                    "subtitle": workdir, "status": st, "last_event_at": None,
                    "capabilities": caps, "meta": meta})
    # A1(spec §7-5):hermes persona 有 pending 待審 → waiting_approval +
    # meta.approval 統一物件(之前恆 idle 是 spec 點名的缺口)。
    hp_pending = _hermes_pending_by_session()
    for mid, (disp, _home) in PERSONAS.items():
        pend = hp_pending.get(f"hermes:{mid}")
        out.append({"id": f"hermes:{mid}", "provider": "hermes", "title": disp,
                    "subtitle": None,
                    "status": "waiting_approval" if pend else "idle",
                    "last_event_at": pend.get("created_at") if pend else None,
                    "capabilities": ["input", "attachments", "replay", "follow", "approve"],
                    "meta": ({"approval": pend} if pend else {})})
    delegated_codex_ids = _delegated_codex_thread_ids()
    try:
        res = await CODEX_APP.call("thread/list",
                                   {"limit": 20, "archived": False, "sortKey": "updated_at",
                                    "sortDirection": "desc", "useStateDbOnly": False}, timeout=10.0)
        for t in (res or {}).get("data", [])[:20]:
            s = _codex_enrich_summary(_codex_session_summary(t))
            if (s.get("thread_id") or s.get("id")) in delegated_codex_ids:
                continue
            thread_id = s.get("thread_id") or s.get("id")
            approval = CODEX_APP.pending_approval_for_thread(thread_id)
            active = bool(s.get("activeTurn")) or s.get("status") in ("active", "running")
            caps = ["input", "interrupt", "attachments", "replay", "follow"]
            if approval:
                caps.append("approve")
            pub = CODEX_APP._approval_public(approval) if approval else None
            if pub:
                # A1:疊上統一欄位(session_id/provider/kind/status…)。options
                # 相容期保留記憶體版 — 現行 app 以 style=="deny" 判拒絕鍵;
                # method/thread_id 為 codex 專屬欄位,照舊(A4 收斂)。
                drow = _approval_get_row(str(pub.get("id") or ""))
                if drow:
                    pub = {**drow,
                           **{k: pub[k] for k in ("method", "thread_id") if k in pub},
                           "options": pub.get("options") or drow.get("options")}
            out.append({"id": f"codex:{thread_id}", "provider": "codex",
                        "title": s.get("name") or "codex", "subtitle": s.get("workdir"),
                        "status": "waiting_approval" if approval else ("running" if active else "idle"),
                        "last_event_at": s.get("lastEventAt"),
                        "capabilities": caps,
                        "meta": {"approval": pub}})
    except Exception as e:  # noqa: BLE001
        # 不再無聲吞錯:codex app-server 掛掉時 CX 區直接消失、log 零痕跡,
        # 「CX 全空」查不到原因(2026-07-10 ChatGPT.app 併購式更新事故)。
        _log_event("v2_codex_list_failed", error=type(e).__name__,
                   error_message=str(e)[:200])
        degraded.append("codex")
    if provider:
        out = [s for s in out if s["provider"] == provider]
    if status:
        out = [s for s in out if s["status"] == status]
    # degraded_providers:清單為空 ≠ 沒有 session,可能是 provider 暫時掛了。
    # 舊 app 忽略新欄位,向後相容;新 app 可據此顯示「清單暫時無法取得」。
    return {"sessions": out, "degraded_providers": degraded}


def _approval_bool_from_body(body: dict) -> bool:
    if "approve" in body:
        return bool(body.get("approve"))
    raw = str(body.get("decision") or body.get("status") or body.get("action") or "").strip().lower()
    if raw in ("approve", "approved", "accept", "accepted", "allow", "yes", "true"):
        return True
    if raw in ("reject", "rejected", "deny", "denied", "decline", "cancel", "no", "false"):
        return False
    raise HTTPException(status_code=400, detail="approve boolean or decision required")


def _codex_thread_from_v2_session_id(session_id: str) -> str:
    if session_id.startswith("codex:"):
        return session_id.split(":", 1)[1]
    if session_id.startswith("delegation:"):
        row = _delegation_get(session_id.split(":", 1)[1])
        if not row:
            raise HTTPException(status_code=404, detail="unknown delegation")
        d = dict(row)
        if d.get("provider") != "codex":
            raise HTTPException(status_code=400, detail="session is not a Codex session")
        thread_id = d.get("codex_thread_id") or d.get("provider_session_id") or ""
        if not thread_id:
            raise HTTPException(status_code=409, detail="delegation has no Codex thread")
        return thread_id
    raise HTTPException(status_code=400, detail="unsupported session id")


@app.post("/app/v2/sessions/{session_id}/approve")
async def v2_session_approve(session_id: str, request: Request):
    """統一路由 approve(契約 §4.4):cx=app-server 決議、cc=TUI 鍵(body
    {key},即 approval/prompt 的 option key)、hermes=Approval Center 決議
    (body {approval_id, approve})。"""
    _check_auth(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    # A1(spec §3.3):統一 body {approval_id, key}(approve bool 相容糖)—
    # 三 provider 一律轉呼 _approval_decide_core;三種舊 body({key}/{approve}/
    # {approval_id})走下方原分支,相容期照收(A4 刪)。
    uni_aid = str(body.get("approval_id") or "").strip()
    if uni_aid and ("key" in body or "approve" in body):
        result = await _approval_decide_core(uni_aid, body)
        return {"ok": True, "session_id": session_id, **result}
    src = _v2_card_source(session_id)
    if src[0] == "cc":
        key = str(body.get("key") or "").strip()
        if not key:
            raise http_err(400, "KEY_REQUIRED",
                           "cc approve 需要 key(approval 卡/prompt 的 option key)")
        res = await _cc_key_core(src[1], key)
        return {"ok": True, "session_id": session_id, **res}
    if src[0] == "hp":
        aid = str(body.get("approval_id") or "").strip()
        if not aid:
            raise http_err(400, "APPROVAL_ID_REQUIRED",
                           "hermes approve 需要 approval_id(approval 卡或 GET /app/v1/approvals)")
        result = await _approval_decide_core(aid, body)
        return {"ok": True, "session_id": session_id, **result}
    approved = _approval_bool_from_body(body)
    for_session = bool(body.get("for_session") or body.get("approve_for_session") or
                       body.get("remember"))
    thread_id = src[1]
    try:
        result = await CODEX_APP.decide_thread_approval(thread_id, approved,
                                                        for_session=for_session)
        return {"ok": True, "session_id": session_id, "thread_id": thread_id, **result}
    except CodexAppServerError as e:
        if e.code == 404:
            raise http_err(409, "APPROVAL_NOT_PENDING",
                           "no pending Codex approval for this session")
        _codex_http_error(e)


@app.post("/app/v2/sessions/{session_id}/input")
async def v2_session_input(session_id: str, request: Request):
    """統一路由 input(契約 §4.4):cc=tmux bracketed paste、cx=turn/start、
    hermes=fire-and-forget 回合(回覆走 S3 卡片事件流,不在此串流)。
    body {content|text, attachments?, client_id?}。"""
    _check_auth(request)
    body = await _json_body(request)
    src = _v2_card_source(session_id)
    if src[0] == "cc":
        res = await _cc_input_core(src[1], body)
        return {"session_id": session_id, **res}
    if src[0] == "cx":
        content = (body.get("content") or body.get("text") or "").strip()
        attachments = body.get("attachments") or []
        if not content and not attachments:
            raise HTTPException(status_code=400, detail="empty")
        items = await _codex_input_items(content, attachments)
        try:
            await CODEX_APP.start_turn(src[1], items,
                                       client_id=body.get("client_id"))
        except CodexAppServerError as e:
            _codex_http_error(e)
        return {"ok": True, "session_id": session_id, "accepted": True}
    return await _v2_persona_input(src[1], session_id, body, request)


async def _v2_persona_input(session: str, session_id: str, body: dict,
                            request: Request):
    """hermes input:與 v1 POST /app/v1/messages 同一套前置/冪等/回合機器,
    差別只在回應——不開 SSE,立即回 {accepted};deltas/收尾全走 S3 卡片
    事件流(進行中裝置與其他裝置看到同一份)。"""
    content = (body.get("content") or body.get("text") or "").strip()
    attachments = body.get("attachments") or []
    if not content and not attachments:
        raise HTTPException(status_code=400, detail="empty")
    client_id = body.get("client_id")
    cid = "appmsg-" + uuid.uuid4().hex[:20]
    turn_started = time.monotonic()
    common_log = {
        "cid": cid,
        "session": session,
        "client_id_hash": _short_hash(client_id),
        "client": _client_host(request),
        "dry_run": False,
        "input_chars": len(content),
        **_attachment_stats(attachments),
        "via": "v2_input",
    }
    _log_event("app_turn_received", **common_log)

    inflight_entry = None
    if client_id:
        # 冪等(與 v1 同款):已完成 → replayed;進行中 → in_flight,不重跑。
        prior = _canon_reply_for_client(session, client_id)
        if prior is not None:
            return {"ok": True, "session_id": session_id, "replayed": True}
        async with _APP_TURN_INFLIGHT_LOCK:
            _now = time.monotonic()
            for k in [k for k, e in _APP_TURN_INFLIGHT.items()
                      if _now - e["ts"] > _APP_TURN_INFLIGHT_TTL]:
                _APP_TURN_INFLIGHT.pop(k, None)
            if _APP_TURN_INFLIGHT.get((session, client_id)) is not None:
                return {"ok": True, "session_id": session_id, "in_flight": True}
            inflight_entry = {"ts": _now, "task": None, "state": None}
            _APP_TURN_INFLIGHT[(session, client_id)] = inflight_entry

    content, att_meta, prompt = await _persona_prepare_turn(
        session, content, attachments, stt_lang=str(body.get("stt_lang") or ""))
    acp_session = await POOL.get(session, home_for(session))
    queued = acp_session.is_busy()
    user_mid, canonical_user_ok = _canon_add_retry(session, "user", content,
                                                   att_meta, client_id=client_id)
    _hp_cards_turn_start(session, cid, user_mid, content, att_meta)
    task, state, _q = _persona_launch_turn(session, prompt, client_id, common_log,
                                           turn_started, canonical_user_ok, cid)
    if inflight_entry is not None:
        inflight_entry["task"] = task
        inflight_entry["state"] = state
    return {"ok": True, "session_id": session_id, "accepted": True,
            "queued": queued, "message_id": user_mid}


@app.post("/app/v2/sessions/{session_id}/interrupt")
async def v2_session_interrupt(session_id: str, request: Request):
    """統一路由 interrupt(契約 §4.4):cc=Esc 驗證重試、cx=turn/interrupt、
    hermes=ACP cancel 驗證重試。無活躍 turn 一律 409。"""
    _check_auth(request)
    src = _v2_card_source(session_id)
    if src[0] == "cc":
        res = await _cc_interrupt_core(src[1])
        return {"session_id": session_id, **res}
    if src[0] == "hp":
        res = await _persona_interrupt_core(src[1])
        return {"session_id": session_id, **res}
    try:
        await CODEX_APP.interrupt_turn(src[1])
    except CodexAppServerError as e:
        if "no active" in str(e).lower():
            raise http_err(409, "NO_ACTIVE_TURN", "no active Codex turn")
        _codex_http_error(e)
    return {"ok": True, "session_id": session_id, "interrupted": True}


@app.post("/app/v2/sessions/{session_id}/key")
async def v2_session_key(session_id: str, request: Request):
    """統一路由 key(契約 §4.4,capability keys):僅 claude_code。"""
    _check_auth(request)
    body = await request.json()
    src = _v2_card_source(session_id)
    if src[0] != "cc":
        raise http_err(400, "UNSUPPORTED_PROVIDER",
                       "key 僅支援 claude_code(capability: keys)")
    raw = str(body.get("key") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="key required")
    res = await _cc_key_core(src[1], raw)
    return {"session_id": session_id, **res}


# ─────────── Phase 0 S1：CC 卡片事件流 / snapshot（契約 §1-§3, issue #15）───────────
# digest 本體在 carddigest.py（單一模組,S2 codex / S3 persona / 衛星終端共用）;
# 這裡只做 CC jsonl 的 tail-follow 接線與兩個 v2 端點。

_CC_CARD_STORES: dict = {}      # name -> carddigest.SessionCardStore
_CC_CARD_FOLLOWERS: dict = {}   # name -> asyncio.Task
_CC_CARD_SEED_LINES = 200       # 冷載種子:最新 jsonl 的尾端行數
_CC_QUEUED_GRACE_SECS = 120     # input 送達後,「已排入佇列」狀態最長維持秒數


def _cc_card_store(name: str):
    store = _CC_CARD_STORES.get(name)
    if store is None:
        store = _CC_CARD_STORES[name] = carddigest.SessionCardStore()
    return store


def _cc_cards_feed_approval(name: str, record: dict, resolved: str = "") -> None:
    """A3(APPROVAL_HUB_SPEC §4/§6):approval 建立/決議 → 對應 CC session 的
    approval 卡。同 `_cx_cards_feed_approval` 的形狀,只是 CC 沒有獨立
    digest 物件 —— 直接對 `SessionCardStore`(掛了 `ApprovalCardMixin`)呼叫。
    只餵「已有人訂閱過」的 store(`_CC_CARD_STORES` 有登記);沒人在看的
    session 不必為了一張卡片就建 store。"""
    store = _CC_CARD_STORES.get(name)
    if not store:
        return
    if resolved:
        store.resolve_approval(record, resolved)
    else:
        store.handle_approval(record)


def _cc_card_uid(d: dict, jsonl_path: str, lineno: int) -> str:
    u = d.get("uuid")
    if u:
        return str(u)[:32]
    fh = hashlib.md5((jsonl_path or "").encode()).hexdigest()[:6]
    return f"{fh}-L{lineno}"


def _cc_digest_lines(store, lines, jsonl_path: str, start_lineno: int) -> int:
    """把 jsonl 行灌進卡片庫;回傳新增/更新的卡數。順手維護人話 label 素材。"""
    n = 0
    for off, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        uid = _cc_card_uid(d, jsonl_path, start_lineno + off)
        for card in carddigest.cc_event_to_cards(d, uid, turn_id=store.turn_id):
            store.upsert_card(card)
            n += 1
            if card["kind"] == "tool_call":
                store.last_tool = card["body"].get("tool") or ""
            elif card["kind"] == "markdown":
                store.saw_output = True
                store.last_tool = ""
    return n


async def _cc_card_seed(store, name: str, workdir: str):
    """冷載種子(冪等):最新 jsonl 尾端 → 卡片庫,並設好 tail 游標。"""
    if store.seeded:
        return
    store.seeded = True
    jsonl = await _cc_session_jsonl(name, workdir)
    if not jsonl or not os.path.exists(jsonl):
        return
    try:
        text = await asyncio.to_thread(
            lambda: open(jsonl, encoding="utf-8", errors="replace").read())
    except Exception as e:  # noqa: BLE001
        _log_event("cc_card_seed_error", session=name, error=str(e)[:200])
        return
    lines = text.splitlines()
    seed = lines[-_CC_CARD_SEED_LINES:]
    _cc_digest_lines(store, seed, jsonl, len(lines) - len(seed))
    store.tail_file = jsonl
    store.tail_pos = len(text.encode("utf-8", errors="replace"))
    store.tail_lineno = len(lines)
    # A3 冷載:pending approval 不在 jsonl 裡(它活在 watcher + DB),seed 時
    # 從 DB 對回 —— 與 codex seed 補 pending_approval_for_thread 同一精神。
    active = _CC_APPROVAL_ACTIVE.get(name)
    if active:
        try:
            rec = _approval_get_row(str(active.get("aid") or ""))
            if rec and rec.get("status") == "pending":
                _cc_cards_feed_approval(name, rec)
        except Exception as e:  # noqa: BLE001
            _log_event("cc_cards_feed_error", error=str(e)[:160])


async def _cc_card_follower(name: str, workdir: str):
    """每秒 tail 該 session 的 jsonl → digest 進卡片庫;有訂閱者時再巡
    busy/mode/prompt(tmux capture 有成本)發 session.status / turn 事件。"""
    store = _cc_card_store(name)
    await _cc_card_seed(store, name, workdir)
    prev_busy = None
    while True:
        await asyncio.sleep(1.0)
        try:
            cur = await _cc_session_jsonl(name, workdir)
            if cur != store.tail_file:               # session 換了新 jsonl
                store.tail_file, store.tail_pos, store.tail_lineno = cur or "", 0, 0
            j = store.tail_file
            if j and os.path.exists(j):
                size = os.path.getsize(j)
                if size > store.tail_pos:
                    def _read_new():
                        with open(j, encoding="utf-8", errors="replace") as f:
                            f.seek(store.tail_pos)
                            return f.read(), f.tell()
                    new, store.tail_pos = await asyncio.to_thread(_read_new)
                    nl = new.splitlines()
                    _cc_digest_lines(store, nl, j, store.tail_lineno)
                    store.tail_lineno += len(nl)
            if store.subscribers > 0:
                st = await _cc_status_core(name)
                busy = bool(st.get("busy"))
                if busy:
                    store.queued_until = 0.0   # 真忙了 → 排隊寬限交還正常路徑
                # 新訂閱者(開啟/重連時可能在「忙碌中途」接入)→ 強制重發一次當前
                # 狀態,否則 set_status「有變才發」會讓中途接入者停在舊的「待命」,
                # 整段回覆期看起來像沒反應(snapshot 冷載不帶 status)。
                if store.subscribers > getattr(store, "_last_subs", 0):
                    store.status = None
                store._last_subs = store.subscribers
                if prev_busy is not None and busy != prev_busy:
                    if busy:
                        store.turn_id = "turn-" + uuid.uuid4().hex[:12]
                        store.saw_output = False
                        store.last_tool = ""
                        store.push_turn("begin", store.turn_id)
                    else:
                        store.push_turn("end", store.turn_id)
                        store.turn_id = ""
                prev_busy = busy
                if not busy and time.time() < getattr(store, "queued_until", 0.0):
                    # input 已送達但 session 還沒接手(忙上一輪/思考中):
                    # 不用 idle 蓋掉「已排入佇列」,避免 UI 誤示「待命」死寂。
                    store.set_status({"busy": True, "mode": st.get("mode"),
                                      "prompt": st.get("prompt"),
                                      "phase": "queued",
                                      "label": "已排入佇列,等待接手…"})
                else:
                    label = carddigest.cc_status_label(busy, st.get("prompt"),
                                                       store.last_tool, store.saw_output)
                    store.set_status({"busy": busy, "mode": st.get("mode"),
                                      "prompt": st.get("prompt"),
                                      "phase": "run" if busy else "idle",
                                      "label": label})
        except Exception as e:  # noqa: BLE001
            _log_event("cc_card_follower_error", session=name, error=str(e)[:200])
            await asyncio.sleep(2.0)


def _ensure_cc_card_follower(name: str, workdir: str):
    t = _CC_CARD_FOLLOWERS.get(name)
    if t and not t.done():
        return
    _CC_CARD_FOLLOWERS[name] = asyncio.create_task(_cc_card_follower(name, workdir))


def _v2_card_source(session_id: str) -> tuple:
    """v2 session id 路由 → ('cc', name, workdir) / ('cx', thread_id) /
    ('hp', persona)。

    S1 = claude_code:{name}（或裸 CC session 名）；S2 = codex:{thread_id} 與
    delegation:{id}(provider=codex)；S3 = hermes:{persona}。
    """
    if ":" in session_id:
        prov, _, rest = session_id.partition(":")
        if prov == "codex":
            return ("cx", rest)
        if prov == "delegation":
            return ("cx", _codex_thread_from_v2_session_id(session_id))
        if prov == "hermes":
            if rest not in PERSONAS:
                raise http_err(404, "SESSION_NOT_FOUND", "unknown persona")
            return ("hp", rest)
        if prov != "claude_code":
            raise http_err(400, "UNSUPPORTED_PROVIDER",
                           f"不支援的 provider: {prov}")
        session_id = rest
    row = next((r for r in _cc_conf_rows() if r[0] == session_id), None)
    if not row:
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    return ("cc", row[0], row[1])


# ─────────── Phase 0 S2:codex 卡片事件流（同兩端點,事件驅動無輪詢）────────

_CX_CARD_DIGESTS: dict = {}     # thread_id -> carddigest.CodexThreadDigest
_CX_CARD_SEED_TURNS = 50        # 冷載種子:thread/turns/list 的 turn 數


def _cx_cards_feed(method: str, params: dict) -> None:
    """CodexAppServerClient 通知 → 有訂閱過的 thread 餵進 digest。
    digest 只在首次 cards/events 請求時建立,之前的歷史由 seed 補。"""
    tid = str(params.get("threadId") or "")
    d = _CX_CARD_DIGESTS.get(tid) if tid else None
    if d:
        d.handle(method, params)


def _cx_cards_feed_approval(record: dict, resolved: str = "") -> None:
    """approval 建立/決議 → 對應 thread 的 approval 卡。"""
    d = _CX_CARD_DIGESTS.get(str(record.get("thread_id") or ""))
    if not d:
        return
    if resolved:
        d.resolve_approval(record, resolved)
    else:
        d.handle_approval(record)


async def _cx_card_digest(thread_id: str):
    """取得(必要時建立+seed)該 thread 的 digest。先註冊再 seed——seed 期間的
    live 事件與 seed 產同一批卡 id,重疊只是 rev 遞增,不會漏也不會雙份。"""
    d = _CX_CARD_DIGESTS.get(thread_id)
    if d is None:
        d = _CX_CARD_DIGESTS[thread_id] = carddigest.CodexThreadDigest()
    if not d.seeded:
        d.seeded = True
        try:
            # 只讀 seed 不做 thread/resume:resume 會「接管」該 thread,若它正被
            # 別的 codex app-server(如 VS Code,thread source=vscode)持有就會卡死
            # 整條 stdio → 之後所有 codex 呼叫一起 hang(XCash 就是這樣空白+送不出)。
            # thread/turns/list 本來就不需 resume 也讀得到(/codexsessions/{id}/history
            # 就是這樣讀的),所以這裡直接列 turns。
            res = await CODEX_APP.call("thread/turns/list", {
                "threadId": thread_id, "limit": _CX_CARD_SEED_TURNS,
                "itemsView": "full", "sortDirection": "desc"}, timeout=45.0)
            turns = list((res or {}).get("data", []))
            turns.reverse()
            d.seed_turns(turns)
            rec = CODEX_APP.pending_approval_for_thread(thread_id)
            if rec:
                d.handle_approval(rec)
        except Exception as e:  # noqa: BLE001
            d.seeded = False   # 下次請求重試 seed
            _log_event("cx_card_seed_error", thread=thread_id[:16],
                       error=str(e)[:200])
            _codex_http_error(e)
    return d


# ─────────── Phase 0 S3:persona 卡片事件流（canonical + live turn 掛鉤）─────

_HP_CARD_DIGESTS: dict = {}     # persona -> carddigest.PersonaDigest
_HP_CARD_FOLLOWERS: dict = {}   # persona -> asyncio.Task
_HP_CARD_SEED_MSGS = 200        # 冷載種子:canonical 訊息數


def _hp_cards_feed_approval(session_id: str, record: dict, resolved: str = "") -> None:
    """A3(APPROVAL_HUB_SPEC §4/§6):hermes persona 的 approval 建立/決議 →
    對應 persona 卡片流的 approval 卡。`session_id` 是統一物件的
    `hermes:{persona}` 形狀(approval_create 落庫時已經這樣寫);只餵
    「已有人訂閱過」的 digest(`_HP_CARD_DIGESTS` 有登記),沒訂閱的
    persona 不必為了一張卡就建 digest。"""
    if not session_id.startswith("hermes:"):
        return
    persona = session_id.split(":", 1)[1]
    d = _HP_CARD_DIGESTS.get(persona)
    if not d:
        return
    if resolved:
        d.resolve_approval(record, resolved)
    else:
        d.handle_approval(record)


def _hp_digest_maybe(session: str):
    """live turn 掛鉤用:有訂閱過才回 digest,否則 None(不建立)。"""
    return _HP_CARD_DIGESTS.get(session)


def _hp_cards_turn_start(session: str, cid: str, user_mid: str | None,
                         content: str, att_meta: list):
    """回合起點掛鉤:user 卡即時出(canonical mid → follower 不重出)+
    turn begin。無訂閱者時 no-op。"""
    d = _hp_digest_maybe(session)
    if d is None:
        return
    try:
        if user_mid:
            d.message_card({"id": user_mid, "role": "user", "content": content,
                            "attachments": att_meta, "ts": time.time()})
        d.turn_begin(cid)
    except Exception as e:  # noqa: BLE001
        _log_event("hp_card_turn_error", session=session, error=str(e)[:160])


def _hp_merged_messages(session: str, limit: int = 200):
    """人格卡片流的訊息來源:canonical(app 回合)⊕ Telegram(state.db)⊕ cron 晨報,
    與 /app/v1/messages 同一套合併/去重。之前卡片流只讀 canonical,所以你在 TG 講的
    和晨報都不會進 Pocket 人格聊天(卡在最後一次 app 內回合 = 7/6)。改吃這個合併後,
    TG 對話 + 晨報/午報都會出現。"""
    out = _canon_messages(session, limit)
    if session not in PERSONAS:
        return out
    _, home = PERSONAS[session]
    def _steps_stripped(t: str) -> str:
        return re.sub(r"<details>.*?</details>", "", t or "", flags=re.S).strip()
    # 雙 role 同文壓重:assistant 是 app 回合雙寫(canonical+state.db)的老
    # 案例;user 是 TG 鏡像 ingest(/internal/v1/mirror/telegram-event)落
    # canonical 後,state.db 掃描會再掃到同一句 —— 同 role+同文+10 分鐘內
    # 視為同一則,壓掉 tg 側。app 端純 TG 舊訊息(canonical 無副本)不受影響。
    canon_recent = [((m.get("ts") or 0), m.get("role"),
                     _steps_stripped(m.get("content") or ""))
                    for m in out if m.get("role") in ("user", "assistant")]
    def _tg_dup(m) -> bool:
        body = _steps_stripped(m["content"])
        ts = m["ts"] or 0
        return bool(body) and any(r == m["role"] and c == body and abs(ts - cts) < 600
                                  for cts, r, c in canon_recent)
    try:
        for m in _persona_history(home, limit):
            if _tg_dup(m):
                continue
            out.append({"id": f"tg-{m['ts']}", "role": m["role"], "content": m["content"],
                        "attachments": m.get("attachments") or [], "ts": m["ts"],
                        "status": "done", "source": "telegram"})
        _sync_persona_reports(session, 50)
        out.extend(_report_messages(session, limit))
    except Exception as e:  # noqa: BLE001
        # TG/cron 合併失敗不能拖垮卡片流 → 退回只有 canonical(至少不會壞掉整頁)。
        _log_event("hp_merge_error", session=session, error=str(e)[:200])
    out.sort(key=lambda m: m.get("ts") or 0)
    out = out[-limit:]
    # Sync engine P1:TG 訊息沒有 bridge 端寫入點(Hermes 官方 gateway 直寫
    # state.db),接入點就是這裡的合併掃描 — 卡片 follower 重掃 / v2 events
    # 的 _event_sync_session 都會經過。掃描是重複式的,靠 external_id 冪等,
    # 穩態時 _EVENT_SEEN 快取讓這行零寫入。順帶把 event_log 出生前的舊訊息
    # 回填進日誌(§3.3:任何裝置從 seq=0 重放即可重建歷史)。
    _event_mirror_messages(session, out)
    return out


async def _hp_canon_follower(session: str):
    """canonical 寫入版本喚醒(#28 的 _canon_wait)⊕ state.db stat 版本喚醒
    (#tg-instant-sync 的 _statedb_notify)→ 補掃出卡。known_mids 去重;
    兩條喚醒源任一觸發都立刻重掃,30s 仍是最後保險絲(見 timeout=30.0
    註解)。"""
    d = _HP_CARD_DIGESTS[session]
    ver = _CANON_VER.get(session, 0)
    sver = _STATEDB_VER.get(session, 0)
    while True:
        try:
            await asyncio.wait_for(_canon_or_statedb_wait(session, ver, sver),
                                   timeout=30.0)
        except asyncio.TimeoutError:
            pass
        except Exception as e:  # noqa: BLE001
            _log_event("hp_card_follower_error", session=session,
                       error=str(e)[:200])
            await asyncio.sleep(2.0)
        ver = _CANON_VER.get(session, 0)
        sver = _STATEDB_VER.get(session, 0)
        try:
            # 保險絲重掃:同時把 TG/state.db + cron 晨報合併進來。正常情況下
            # 這一段已經是被 state.db stat watcher 立刻(~0.2-0.3s)喚醒觸發
            # 的,不是等 30s timeout —— watcher 偵測不到才會落回 30s 週期
            # (見 _state_db_watcher_loop 註解:TG/cron 不寫 canonical,不會
            # 觸發 _canon_notify,靠 stat 版本或最終這條 timeout 補上)。
            d.seed_messages(_hp_merged_messages(session, 80))
        except Exception as e:  # noqa: BLE001
            _log_event("hp_card_follower_error", session=session,
                       error=str(e)[:200])


def _ensure_hp_card_follower(session: str):
    t = _HP_CARD_FOLLOWERS.get(session)
    if t and not t.done():
        return
    _HP_CARD_FOLLOWERS[session] = asyncio.create_task(_hp_canon_follower(session))


async def _hp_card_digest(session: str):
    d = _HP_CARD_DIGESTS.get(session)
    if d is None:
        d = _HP_CARD_DIGESTS[session] = carddigest.PersonaDigest()
    if not d.seeded:
        d.seeded = True
        try:
            msgs = await asyncio.to_thread(_hp_merged_messages, session,
                                           _HP_CARD_SEED_MSGS)
            d.seed_messages(msgs)
            # A3 冷載:pending approval 從 DB 對回(與 cc/cx seed 同精神)——
            # 卡誕生時沒人訂閱這條的話,feed 是 no-op,全靠這裡補。
            pend = _hermes_pending_by_session().get(f"hermes:{session}")
            if pend:
                d.handle_approval(pend)
        except Exception as e:  # noqa: BLE001
            d.seeded = False
            _log_event("hp_card_seed_error", session=session, error=str(e)[:200])
            raise HTTPException(status_code=500, detail="persona card seed failed")
    _ensure_hp_card_follower(session)
    return d


async def _v2_card_store(session_id: str):
    """cards/events 共用:session id → 已 seed 的 SessionCardStore。"""
    src = _v2_card_source(session_id)
    if src[0] == "cx":
        return (await _cx_card_digest(src[1])).store
    if src[0] == "hp":
        return (await _hp_card_digest(src[1])).store
    _, name, workdir = src
    store = _cc_card_store(name)
    await _cc_card_seed(store, name, workdir)
    _ensure_cc_card_follower(name, workdir)
    return store


@app.get("/app/v2/sessions/{session_id}/cards")
async def v2_session_cards(session_id: str, request: Request, limit: int = 100,
                           before_seq: int | None = None):
    """契約 §3 冷載 snapshot:{cards, latest_seq} → app 渲染後從 since_seq 接流。"""
    _check_auth(request)
    store = await _v2_card_store(session_id)
    return store.snapshot(limit=max(1, min(limit, 500)), before_seq=before_seq)


@app.get("/app/v2/sessions/{session_id}/events")
async def v2_session_events(session_id: str, request: Request, since_seq: int = 0,
                            profile: str = "phone"):
    """契約 §1 SSE 事件流:信封 {seq,ts,type,data};since_seq 補洞;超範圍 410。"""
    _check_auth(request)
    if profile != "phone":
        raise http_err(400, "UNSUPPORTED_PROFILE", "v0 只實作 profile=phone(契約 §4)")
    store = await _v2_card_store(session_id)
    backlog = store.since(since_seq)
    if backlog is None:
        raise http_err(410, "SEQ_GONE",
                       "since_seq 超出 ring buffer 範圍,請走 snapshot 冷載")

    async def gen():
        cursor = since_seq
        store.subscribers += 1
        try:
            for ev in backlog:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                cursor = ev["seq"]
            idle = 0.0
            while True:
                if await request.is_disconnected():
                    break
                if store.seq > cursor:
                    fresh = store.since(cursor)
                    if fresh is None:      # ring 已滾過游標(理論上不會) → 斷線重載
                        break
                    for ev in fresh:
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                        cursor = ev["seq"]
                    idle = 0.0
                else:
                    await asyncio.sleep(0.5)
                    idle += 0.5
                    if idle >= max(1.0, float(SSE_KEEPALIVE_SECS)):
                        idle = 0.0
                        yield f"data: {json.dumps(store.ping(), ensure_ascii=False)}\n\n"
        finally:
            store.subscribers -= 1

    return StreamingResponse(gen(), media_type="text/event-stream")


# ───────────────────────── scheduled reports + notification toggles ─────────
# Hermes runs the daily briefs via cron (jobs.json); each job already has an
# enabled/paused state the scheduler honours, and `hermes cron pause/resume`
# toggles it safely. The app surfaces the reports (so they land in Pocket Agent,
# not just Telegram) and exposes per-notification on/off switches.

CRON_JOBS_JSON = os.path.expanduser("~/apps/hermes-agent/home/cron/jobs.json")
HERMES_HOME_DIR = os.path.expanduser("~/apps/hermes-agent/home")
STATE_DB = os.path.join(HERMES_HOME_DIR, "state.db")

# User-facing notification jobs → friendly label. Everything else (signal
# collector, session reset/hygiene) is internal and hidden from the app.
NOTIFY_LABELS = {
    "morning-brief-0700": "晨報",
    "stock-premarket-0850": "盤前速覽",
    "afternoon-brief-1330": "午報",
    "memory-consolidation-2200": "晚間三省",
}

# Per-persona daily notification jobs (cron job *name* -> in-chat label) that
# should appear INSIDE that persona's conversation, like Telegram shows them —
# not only in the separate Reports tab. Each persona reads its OWN home's
# state.db + cron/jobs.json, so adding a new daily job (e.g. a 潘天晴 "今日精選"
# / 限動 cron) here is all it takes for it to surface in-chat automatically.
PERSONA_REPORTS = {
    "yuanfang": NOTIFY_LABELS,
    "pantianqing": {
        "fliper-editorial-brief-0715": "編輯台晨報",
        "TNH 名家觀點自動建稿": "台北文創名家觀點掃描",
    },
    "xcash": {
        "xcash-morning-dev-brief-0730": "開發晨報",
    },
    "shuijing": {
        "shuijing-sunrise-oracle": "水鏡晨卦",
    },
}

# A3-3:哪些 cron 報告在同步進 report_events 時順手建 kind=notice approval
# (app 通知中心的入口 + 已讀 ack)。先行兩個試跑,穩了再擴。
NOTICE_REPORT_JOBS = {
    "xcash": {"xcash-morning-dev-brief-0730"},
    "shuijing": {"shuijing-sunrise-oracle"},
}
_NOTICE_REPORT_MAX_AGE = 12 * 3600.0   # 只通知 12h 內的報告(防冷庫回灌灌爆)
_NOTICE_REPORT_TTL = 86400.0           # 晨報 ack 給一天;過期由掃描同卡收尾


def _notice_for_report(session: str, report: dict) -> None:
    """A3-3:新 cron 報告 → kind=notice approval(不推播 —— 報告本體已由
    TG/推播管道送達;這裡補的是 app 通知中心的入口與已讀 ack)。
    approval id 錨在 report id 上:同 id 已存在(不論狀態)就不重建,
    報告內容修訂不會把已 ack 的通知翻回 pending。"""
    import sqlite3
    name = report.get("name") or ""
    if name not in NOTICE_REPORT_JOBS.get(session, ()):
        return
    rid = str(report.get("id") or "")
    if not rid:
        return
    if time.time() - float(report.get("ts") or 0) > _NOTICE_REPORT_MAX_AGE:
        return
    aid = "ntc-" + hashlib.sha1(rid.encode()).hexdigest()[:20]
    sid = f"hermes:{session}"
    title = report.get("label") or name or "報告"
    detail = _clip_text(report.get("content") or "", 200)
    options = [{"key": "ack", "label": "知道了", "style": "primary"}]
    now = time.time()
    con = sqlite3.connect(CANON_DB, timeout=30)
    try:
        if con.execute("SELECT 1 FROM approvals WHERE id=?", (aid,)).fetchone():
            return
        con.execute(
            "INSERT INTO approvals"
            "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result,callback,"
            "session_id,provider,kind,options) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, title, sid, "", detail, now, now + _NOTICE_REPORT_TTL,
             "pending", None, None, None, sid, "hermes", "notice",
             json.dumps(options, ensure_ascii=False)))
        con.commit()
    finally:
        con.close()
    _log_event("notice_created", id=aid, session=session, report=name)
    rec = {"id": aid, "title": title, "detail": detail, "options": options,
           "kind": "notice"}
    loop = _MAIN_LOOP

    def _feed():
        try:
            _hp_cards_feed_approval(sid, rec)
        except Exception as e:  # noqa: BLE001
            _log_event("hp_cards_feed_error", error=str(e)[:160])
    if loop and loop.is_running():
        # 報告同步常跑在 to_thread(卡片流 seed / v1 merge)—— 卡片庫不上鎖,
        # 一律排回主圈做 feed(同圈呼叫也安全:排到下一輪跑)。
        loop.call_soon_threadsafe(_feed)


def _cron_jobs():
    try:
        data = json.load(open(CRON_JOBS_JSON, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for j in data.get("jobs", []):
        name = j.get("name", "")
        on = bool(j.get("enabled", True)) and j.get("state") != "paused"
        out.append({"id": j.get("id"), "name": name, "label": NOTIFY_LABELS.get(name),
                    "schedule": j.get("schedule_display") or j.get("schedule", {}).get("display", ""),
                    "enabled": on, "notify": name in NOTIFY_LABELS})
    return out


async def _hermes_cron(action: str, job_id: str):
    env = dict(os.environ)
    env["HERMES_HOME"] = HERMES_HOME_DIR
    try:
        p = await asyncio.create_subprocess_exec(
            HERMES_BIN, "cron", action, job_id, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(), timeout=30)
        return p.returncode == 0, (out or b"").decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return False, str(e)


_REPORT_START = re.compile(r"(🌅|🌙|☀️|🌇|🌃|📊|🗓️|善彰[，,、]?\s*(早安|午安|晚安)|早安|午安|晚安)")
_REPORT_INLINE_START = re.compile(
    r"(\*\*[^*\n]*(晨報|午報|晚間|速覽|掃描|晨卦)[^*\n]*\*\*|"
    r"#{1,3}\s*[^\n]*(晨報|午報|晚間|速覽|掃描|晨卦)|"
    r"(🌅|🌙|☀️|🌇|🌃|📊|🗓️)[^\n]*|"
    r"善彰[，,、]?\s*(早安|午安|晚安))"
)


def _clean_report(s: str) -> str:
    """Trim a leading English working-note preamble some cron runs leak before
    the actual brief ("I have all the data… Now composing…"), WITHOUT eating the
    real title. Keep from the first line that carries real content: any CJK, a
    markdown header, or one of the known report openers (🌅/善彰早安…)."""
    text = s.strip()
    inline = _REPORT_INLINE_START.search(text)
    if inline and inline.start() > 0:
        return text[inline.start():].lstrip(" -—\n").strip()
    lines = text.split("\n")
    for i, line in enumerate(lines):
        t = line.strip()
        if not t:
            continue
        has_cjk = any("一" <= c <= "鿿" for c in t)
        if has_cjk or t.startswith("#") or _REPORT_START.match(t):
            return "\n".join(lines[i:]).strip()
    return text


def _reports(limit: int = 20):
    """Latest delivered report per recent cron run (the session's final assistant
    message), newest first — only the user-facing notification jobs."""
    import sqlite3
    if not os.path.exists(STATE_DB):
        return []
    jobs = {j["id"]: j for j in _cron_jobs()}
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=5)
        sids = con.execute(
            "SELECT m.session_id, MAX(m.timestamp) ts FROM messages m "
            "JOIN sessions s ON s.id = m.session_id WHERE s.source='cron' "
            "GROUP BY m.session_id ORDER BY ts DESC LIMIT ?", (limit * 3,)).fetchall()
        out = []
        for sid, _ts in sids:
            mobj = re.search(r"cron_([0-9a-f]+)_", str(sid))
            job = jobs.get(mobj.group(1)) if mobj else None
            if not (job and job.get("notify")):
                continue                       # skip internal / unknown jobs
            last = con.execute(
                "SELECT content, timestamp FROM messages WHERE session_id=? "
                "AND role='assistant' AND content IS NOT NULL AND content!='' "
                "ORDER BY timestamp DESC LIMIT 1", (sid,)).fetchone()
            if last and last[0]:
                out.append({"label": job.get("label") or job.get("name"),
                            "name": job.get("name"), "content": _clean_report(last[0]),
                            "ts": last[1]})
            if len(out) >= limit:
                break
        con.close()
        return out
    except Exception:  # noqa: BLE001
        return []


def _cron_names_for(home: str) -> dict:
    """job_id -> job name, from a given persona home's own cron/jobs.json."""
    try:
        data = json.load(open(os.path.join(home, "cron", "jobs.json"), encoding="utf-8"))
        jobs = data.get("jobs", data) if isinstance(data, dict) else data
        return {j.get("id"): j.get("name", "") for j in jobs}
    except Exception:  # noqa: BLE001
        return {}


def _persona_reports(persona: str, limit: int = 20):
    """Daily notification reports for ANY persona, read from that persona's OWN
    home state.db + cron/jobs.json (not just the main 袁方 home). Mirrors
    _reports but generalised so 潘天晴's 編輯台晨報 (and future 今日精選 / 限動)
    surface in-conversation too. Returns newest-first cleaned briefs."""
    import sqlite3
    labels = PERSONA_REPORTS.get(persona)
    if not labels:
        return []
    home = home_for(persona)
    db = os.path.join(home, "state.db")
    if not os.path.exists(db):
        return []
    idname = _cron_names_for(home)
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        sids = con.execute(
            "SELECT m.session_id, MAX(m.timestamp) ts FROM messages m "
            "JOIN sessions s ON s.id = m.session_id WHERE s.source='cron' "
            "GROUP BY m.session_id ORDER BY ts DESC LIMIT ?", (limit * 3,)).fetchall()
        out = []
        for sid, _ts in sids:
            mobj = re.search(r"cron_([0-9a-f]+)_", str(sid))
            name = idname.get(mobj.group(1)) if mobj else None
            label = labels.get(name)
            if not label:
                continue                       # not a user-facing daily job
            last = con.execute(
                "SELECT content, timestamp FROM messages WHERE session_id=? "
                "AND role='assistant' AND content IS NOT NULL AND content!='' "
                "ORDER BY timestamp DESC LIMIT 1", (sid,)).fetchone()
            if last and last[0]:
                external_id = f"cron:{persona}:{name}:{sid}"
                out.append({"id": _report_id(persona, name or "", str(sid), last[1]),
                            "external_id": external_id,
                            "external_source": "hermes-cron",
                            "session_id": sid, "label": label, "name": name,
                            "content": _clean_report(last[0]), "ts": last[1]})
            if len(out) >= limit:
                break
        con.close()
        return out
    except Exception:  # noqa: BLE001
        return []


def _write_report_memory(session: str, reports: list[dict]) -> None:
    if session not in PERSONAS:
        return
    home = home_for(session)
    memdir = os.path.join(home, "memories")
    try:
        os.makedirs(memdir, exist_ok=True)
        latest = sorted(reports, key=lambda r: r.get("ts") or 0, reverse=True)[:REPORT_MEMORY_ITEMS]
        lines = [
            "# REPORTS.md",
            "",
            "PocketAgent/Hermes bridge 維護的近期報告索引。全文 canonical 存在",
            "`~/.local/share/pocket-agent/canonical.db` 的 `report_events` 表；",
            "此檔提供 persona / studio-memory 快速讀取最近報告脈絡。",
            "",
        ]
        for r in latest:
            lines += [
                f"## {_fmt_ts(r.get('ts'))} {r.get('label') or r.get('name') or '報告'}",
                f"- session: {session}",
                f"- source: {r.get('external_source') or 'hermes-cron'}",
                f"- external_id: {r.get('external_id') or r.get('id')}",
                "",
                _clip_text(r.get("content") or "", REPORT_MEMORY_CHARS),
                "",
            ]
        tmp = os.path.join(memdir, f".{REPORT_MEMORY_FILE}.tmp")
        final = os.path.join(memdir, REPORT_MEMORY_FILE)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")
        os.replace(tmp, final)
    except Exception as e:  # noqa: BLE001
        _log_event("report_memory_write_failed", session=session,
                   error=type(e).__name__, error_message=str(e)[:160])


def _sync_persona_reports(session: str, limit: int = 50) -> list[dict]:
    reports = _persona_reports(session, limit)
    if not reports:
        return _report_events(session, limit, newest_first=True)
    upserted = 0
    for r in reports:
        try:
            if _report_upsert(session, r):
                upserted += 1
                # A3-3:名單內的新報告 → kind=notice approval(進通知中心/
                # 卡片流;approval id 錨在 report id,重同步/改稿不重建)。
                _notice_for_report(session, r)
        except Exception as e:  # noqa: BLE001
            _log_event("report_event_write_failed", session=session,
                       report_id=r.get("id"), label=r.get("label"),
                       error=type(e).__name__, error_message=str(e)[:160])
    latest = _report_events(session, max(limit, REPORT_MEMORY_ITEMS), newest_first=True)
    _write_report_memory(session, latest)
    if upserted:
        _log_event("report_events_synced", session=session, count=upserted)
    return latest


_REPORT_QUERY_RE = re.compile(
    r"(報告|晨報|午報|盤前|晚間|三省|晨卦|卦|速覽|編輯台|名家觀點|"
    r"剛剛|今天|今日|第二點|第三點|上面|前面|剛才)"
)


def _report_context_for_prompt(session: str, user_text: str) -> str:
    reports = _sync_persona_reports(session, 50)
    if not reports:
        return ""
    want_more = bool(_REPORT_QUERY_RE.search(user_text or ""))
    max_items = REPORT_CONTEXT_TRIGGERED if want_more else REPORT_CONTEXT_DEFAULT
    selected = reports[:max_items]  # newest-first from _sync_persona_reports
    budget = REPORT_CONTEXT_CHARS
    blocks = []
    for r in selected:
        title = f"{_fmt_ts(r.get('ts'))} {r.get('label') or r.get('name') or '報告'}"
        content = _clip_text(r.get("content") or "", min(REPORT_CONTEXT_ITEM_CHARS, budget))
        block = f"### {title}\n{content}".strip()
        if len(block) > budget:
            block = _clip_text(block, budget)
        blocks.append(block)
        budget -= len(block)
        if budget <= 1200:
            break
    if not blocks:
        return ""
    return (
        "【PocketAgent 近期報告上下文】\n"
        "以下是這個 persona 最近已投遞到 PocketAgent 對話窗的報告內容。"
        "使用者若提到報告、晨報、午報、盤前、晚間、晨卦、剛剛、上面或第幾點，"
        "必須優先以這些報告內容回答；若資訊不足，再明確說需要查原始來源。\n\n"
        + "\n\n".join(blocks)
    )


@app.get("/cron/jobs")
async def cron_jobs(request: Request):
    _check_auth(request)
    return {"jobs": _cron_jobs()}


@app.post("/cron/jobs/{job_id}/{action}")
async def cron_toggle(job_id: str, action: str, request: Request):
    _check_auth(request)
    if action not in ("pause", "resume"):
        raise HTTPException(status_code=400, detail="action must be pause|resume")
    if not any(j["id"] == job_id for j in _cron_jobs()):
        raise HTTPException(status_code=404, detail="unknown job")
    ok, msg = await _hermes_cron(action, job_id)
    if not ok:
        raise HTTPException(status_code=500, detail=msg[:300] or "toggle failed")
    return {"ok": True, "enabled": action == "resume"}


@app.get("/reports")
async def reports(request: Request, limit: int = 20):
    _check_auth(request)
    return {"reports": _reports(max(1, min(limit, 50)))}


# ───────────────────────── versioned app API (M20) ─────────────────────────
# The app's stable contract. Wraps the Hermes internals (state.db, cron JSON,
# ACP) so the client never depends on them directly.

@app.post("/app/v1/uploads")
async def app_uploads(request: Request):
    """Pre-upload composer attachments and return stable local file references.

    Mobile clients should call this before sending a turn with images/files, then
    submit the lightweight returned `path` fields to persona/CC/Codex endpoints.
    Legacy clients can still send `data` directly to those endpoints.
    """
    _check_auth(request)
    body = await request.json()
    attachments = body.get("attachments") or []
    if not isinstance(attachments, list):
        raise HTTPException(status_code=400, detail="attachments must be a list")
    if len(attachments) > _ATT_MAX_COUNT:
        raise HTTPException(status_code=413, detail="too many attachments")
    # 修復單「附件限制」:單檔/總量預檢(base64 長度估算,不先解碼)——
    # 之前 size 只進 log,從不比對任何上限。
    total_est = 0
    for idx, a in enumerate(attachments):
        if not isinstance(a, dict):
            raise HTTPException(status_code=400, detail=f"attachment {idx} must be an object")
        est = _data_uri_estimated_bytes(str(a.get("data") or ""))
        total_est += est
        if est > _ATT_MAX_FILE_BYTES:
            raise HTTPException(status_code=413,
                                detail=f"attachment {idx} ({a.get('filename') or 'file'}) "
                                       f"超過單檔上限 {_ATT_MAX_FILE_BYTES} bytes")
    if total_est > _ATT_MAX_COUNT * _ATT_MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="attachments 總量超過上限")
    saved = []
    for idx, a in enumerate(attachments):
        if not isinstance(a, dict):
            raise HTTPException(status_code=400, detail=f"attachment {idx} must be an object")
        path = _save_attachment(a, a.get("filename") or "file")
        if not path:
            raise HTTPException(status_code=400, detail=f"attachment {idx} upload failed")
        try:
            size = Path(path).stat().st_size
        except Exception:  # noqa: BLE001
            size = 0
        saved.append({
            "kind": a.get("kind") or "file",
            "filename": a.get("filename") or Path(path).name,
            "mime": a.get("mime") or "application/octet-stream",
            "path": path,
            "size": size,
        })
    _log_event("app_uploads_saved", attachment_count=len(saved),
               bytes=sum(int(a.get("size") or 0) for a in saved),
               client=_client_host(request))
    return {"ok": True, "attachments": saved}


# ───────────────────────── in-app terminal (PTY over WS) ────────────────────
# docs/TERMINAL_PTY_CONTRACT.md is the authority; keep this section in sync
# with it. One WS = one local PTY login shell running as the bridge's own
# execution identity (no privilege escalation, no user switch). A paired
# device token therefore equals full shell access — see POCKET_TERMINAL_ENABLED
# above for the kill switch, and §日誌 below for what never gets logged.

def _terminal_token_from_ws(websocket: WebSocket) -> str:
    """Same device-token contract as every other /app/v1/* endpoint
    (`Authorization: Bearer <token>`), plus a `?token=` query fallback for
    WS clients that can't set a header on the upgrade request."""
    auth = websocket.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        token = (websocket.query_params.get("token") or "").strip()
    return token


def _terminal_device_id_for_token(token: str) -> str | None:
    """Mirrors _check_auth's token membership checks (master token, per-device
    token, account-bound device) but returns a device id for logging instead
    of raising — a WS handshake rejects with a close code, not an
    HTTPException."""
    if not token:
        return None
    if hmac.compare_digest(token, BRIDGE_TOKEN):
        return "master"
    with _PAIR_LOCK:
        dev = _DEVICE_TOKENS.get(token)
        if dev is not None:
            if not dev.get("apple_user_id") or _account_device_for_token(token) is not None:
                dev["last_seen"] = time.time()
                return dev.get("device_id") or _short_hash(token)
    acct_dev = _account_device_for_token(token)
    if acct_dev is not None:
        return acct_dev.get("device_id") or _short_hash(token)
    return None


class _TerminalSession:
    """One local PTY + login shell child process for one terminal WS."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self.master_fd: int | None = None
        self.proc: subprocess.Popen | None = None
        self._write_buf = bytearray()

    def start(self) -> None:
        shell = os.environ.get("SHELL") or "/bin/zsh"
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        home = os.path.expanduser("~")
        master_fd, slave_fd = pty.openpty()
        try:
            self.proc = subprocess.Popen(
                [shell, "-l"],
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                cwd=home, env=env,
                preexec_fn=os.setsid,   # new session/process group → clean signal targeting
                close_fds=True,
            )
        finally:
            os.close(slave_fd)   # child already dup'd it; parent only needs master
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self.master_fd = master_fd

    def resize(self, cols: int, rows: int) -> None:
        if self.master_fd is None:
            return
        try:
            packed = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, packed)
        except (OSError, ValueError, struct.error):
            pass

    def write(self, data: str) -> None:
        """Queue client keystrokes for the PTY. Non-blocking: the master fd is
        O_NONBLOCK, so a full PTY input buffer (e.g. a huge paste) falls back
        to an event-loop writer instead of blocking the whole bridge."""
        if self.master_fd is None or not data:
            return
        self._write_buf.extend(data.encode("utf-8", "replace"))
        self._flush_write()

    def _flush_write(self) -> None:
        if self.master_fd is None:
            self._write_buf.clear()
            return
        try:
            while self._write_buf:
                n = os.write(self.master_fd, bytes(self._write_buf))
                del self._write_buf[:n]
            try:
                self._loop.remove_writer(self.master_fd)
            except (ValueError, OSError):
                pass
        except BlockingIOError:
            self._loop.add_writer(self.master_fd, self._flush_write)
        except OSError:
            self._write_buf.clear()

    def read_nonblocking(self, size: int = 65536) -> bytes | None:
        """None means EOF (shell exited); b"" means nothing was ready (kept
        defensive — add_reader should only fire when data is available)."""
        try:
            data = os.read(self.master_fd, size)
        except BlockingIOError:
            return b""
        except OSError:
            return None
        return data if data else None

    def exit_code(self) -> int:
        """Blocking (bounded); always call via run_in_executor, never inline
        on the event loop."""
        if self.proc is None:
            return -1
        try:
            return self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return self.proc.returncode if self.proc.returncode is not None else -1

    def close(self) -> None:
        """Blocking (bounded); always call via run_in_executor. Kills the
        shell's whole process group and reaps it — no zombies, no fd leak."""
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGHUP)
            except (ProcessLookupError, OSError):
                pass
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    self.proc.wait(timeout=2)
                except Exception:  # noqa: BLE001
                    pass
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None


async def _terminal_recv_loop(websocket: WebSocket, session: "_TerminalSession") -> None:
    """Client → server: {"type":"input"} and {"type":"resize"} only (contract
    §訊息). Unknown message types/fields are ignored, not rejected, so the app
    can add fields later without a bridge redeploy."""
    while True:
        try:
            raw = await websocket.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            return
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(msg, dict):
            continue
        mtype = msg.get("type")
        if mtype == "input":
            data = msg.get("data")
            if isinstance(data, str) and data:
                session.write(data)
        elif mtype == "resize":
            try:
                cols = int(msg.get("cols") or 0)
                rows = int(msg.get("rows") or 0)
            except (TypeError, ValueError):
                continue
            if cols > 0 and rows > 0:
                session.resize(cols, rows)


@app.websocket("/app/v1/terminal")
async def terminal_ws(websocket: WebSocket) -> None:
    if not POCKET_TERMINAL_ENABLED:
        # Pre-accept reject. uvicorn's ASGI websocket implementation hardcodes
        # HTTP 403 for any pre-accept `websocket.close` regardless of the code
        # passed (it discards the numeric close code entirely for handshake
        # rejections) — that happens to be exactly the "端點回 403" the
        # contract asks for here, so no accept() round-trip is needed.
        await websocket.close(code=1013)
        return
    token = _terminal_token_from_ws(websocket)
    device_id = _terminal_device_id_for_token(token)
    if not device_id:
        # A pre-accept close would also flatten to plain HTTP 403 (see above),
        # which loses the "4401" the contract asks for. Accept first so the
        # rejection is a real WS close *frame*, whose code the client can read.
        await websocket.accept()
        try:
            await websocket.send_json({"type": "error", "message": "invalid or missing device token"})
        except Exception:  # noqa: BLE001
            pass
        await websocket.close(code=4401)
        return

    await websocket.accept()
    loop = asyncio.get_running_loop()
    session = _TerminalSession(loop)
    try:
        session.start()
    except Exception as e:  # noqa: BLE001 — PTY/shell spawn failed
        try:
            await websocket.send_json({"type": "error",
                                       "message": f"pty spawn failed: {type(e).__name__}"})
        except Exception:  # noqa: BLE001
            pass
        await websocket.close(code=1011)
        return

    master_fd = session.master_fd
    started_at = time.time()
    exited = asyncio.Event()
    output_q: asyncio.Queue = asyncio.Queue()
    _EOF = object()

    def _on_readable() -> None:
        chunk = session.read_nonblocking()
        if chunk is None:
            try:
                loop.remove_reader(master_fd)
            except (ValueError, OSError):
                pass
            exited.set()
            output_q.put_nowait(_EOF)
            return
        if chunk:
            output_q.put_nowait(chunk)

    loop.add_reader(master_fd, _on_readable)
    _log_event("terminal_open", device_id=device_id)  # never log keystrokes/output

    async def _writer_loop() -> None:
        while True:
            item = await output_q.get()
            if item is _EOF:
                return
            try:
                await websocket.send_json({"type": "output",
                                           "data": item.decode("utf-8", "replace")})
            except Exception:  # noqa: BLE001 — client gone; recv/exit path cleans up
                return

    recv_task = asyncio.create_task(_terminal_recv_loop(websocket, session))
    writer_task = asyncio.create_task(_writer_loop())
    exit_task = asyncio.create_task(exited.wait())
    try:
        await asyncio.wait({recv_task, exit_task}, return_when=asyncio.FIRST_COMPLETED)
        if exit_task.done():
            await writer_task  # drain any output queued before the EOF sentinel
            code = await loop.run_in_executor(None, session.exit_code)
            try:
                await websocket.send_json({"type": "exit", "code": code})
            except Exception:  # noqa: BLE001
                pass
    finally:
        for t in (recv_task, writer_task, exit_task):
            if not t.done():
                t.cancel()
        await asyncio.gather(recv_task, writer_task, exit_task, return_exceptions=True)
        try:
            loop.remove_reader(master_fd)
        except (ValueError, OSError):
            pass
        try:
            loop.remove_writer(master_fd)
        except (ValueError, OSError):
            pass
        await loop.run_in_executor(None, session.close)
        try:
            await websocket.close(code=1000)
        except Exception:  # noqa: BLE001
            pass
        _log_event("terminal_close", device_id=device_id,
                   duration_s=round(time.time() - started_at, 3))


@app.get("/capabilities")
async def capabilities(request: Request):
    _check_auth(request)
    return {"api": "app/v1",
            "features": ["canonical_messages", "reports", "notifications",
                         "approvals", "cc_sessions", "attachments", "vision",
                         "message_dry_run", "message_interrupt", "message_status",
                         "message_events", "apns_push", "accounts",
                         "apple_auth", "apple_web_auth", "account_pairing",
                         "delegations", "control_plane_v2", "attachment_uploads",
                         "interactive_push"] + (["terminal"] if POCKET_TERMINAL_ENABLED else []),
            "endpoints": ["/app/v1/sessions", "/app/v1/messages", "/reports",
                          "/app/v1/uploads",
                          "/app/v1/reactions", "/app/v1/pins",
                          "/app/v1/messages/{id}", "/app/v1/sessions/{id}/pin",
                          "/app/v1/messages/retract", "/app/v1/personas",
                          "/app/v1/messages/status", "/app/v1/messages/events",
                          "/app/v1/messages/interrupt",
                          "/cron/jobs", "/ccsessions", "/app/v1/approvals",
                          "/app/v1/devices", "/app/v1/push/test",
                          "/app/v1/auth/apple", "/app/v1/account",
                          "/app/v1/auth/apple/web/start",
                          "/app/v1/auth/apple/web/callback",
                          "/app/v1/auth/apple/web/status",
                          "/app/v1/pair/new", "/app/v1/pair/claim",
                          "/app/v1/devices/{id}/revoke",
                          "/app/v1/delegations", "/app/v2/sessions",
                          "/app/v2/sessions/{id}/approve", "/app/v1/terminal",
                          "/app/v1/usage"]}


@app.post("/app/v1/auth/apple")
async def app_auth_apple(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="bad json")
    apple_user_id = str(body.get("apple_user_id") or body.get("appleUserID") or "").strip()
    identity_token = str(body.get("identityToken") or body.get("identity_token") or "").strip()
    if not apple_user_id:
        raise HTTPException(status_code=400, detail="apple_user_id required")
    if not identity_token:
        raise HTTPException(status_code=400, detail="identityToken required")
    claims = await asyncio.to_thread(_apple_verify_identity_token, identity_token)
    if claims.get("sub") != apple_user_id:
        _log_event("apple_auth_subject_mismatch",
                   apple_user_hash=_short_hash(apple_user_id),
                   token_subject_hash=_short_hash(claims.get("sub")))
        raise HTTPException(status_code=401, detail="apple user id mismatch")

    display_name = body.get("display_name") or body.get("displayName") or body.get("name")
    if isinstance(display_name, dict):
        display_name = " ".join(
            str(display_name.get(k) or "").strip()
            for k in ("givenName", "familyName") if display_name.get(k)
        ).strip()
    display_name = str(display_name or "").strip() or None
    email = str(body.get("email") or claims.get("email") or "").strip() or None
    user = _account_upsert_user(apple_user_id, email=email, display_name=display_name)
    session_token, expires_at = _account_session_create(apple_user_id)
    _log_event("apple_auth_success",
               apple_user_hash=_short_hash(apple_user_id),
               audience=str(claims.get("aud") or ""))
    return {
        "ok": True,
        "user": _account_public_user(user),
        "session": {
            "type": "account",
            "token": session_token,
            "expires_at": expires_at,
        },
    }


@app.post("/app/v1/auth/apple/web/start")
async def app_auth_apple_web_start(request: Request):
    client_hash = _apple_web_check_start_rate(request)
    config_error = _apple_web_config_error()
    if config_error:
        _log_event("apple_web_auth_not_configured", reason=config_error)
        raise HTTPException(status_code=503, detail="web Apple sign-in is not configured")
    flow = _apple_web_new_flow()
    authorization_url = APPLE_WEB_AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "client_id": APPLE_WEB_CLIENT_ID,
        "redirect_uri": APPLE_WEB_REDIRECT_URI,
        "response_type": "code id_token",
        "response_mode": "form_post",
        "scope": "name email",
        "state": flow["state"],
        "nonce": flow["nonce"],
    })
    _log_event("apple_web_auth_started",
               flow_hash=_short_hash(flow["flow_id"]), client_hash=client_hash)
    return {
        "ok": True,
        "flow_id": flow["flow_id"],
        "poll_secret": flow["poll_secret"],
        "authorization_url": authorization_url,
        "expires_at": int(flow["expires_at"]),
        "poll_interval": 2,
    }


@app.post("/app/v1/auth/apple/web/status")
async def app_auth_apple_web_status(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="bad json")
    flow_id = str(body.get("flow_id") or "").strip()
    poll_secret = str(body.get("poll_secret") or "").strip()
    if not flow_id or not poll_secret:
        raise HTTPException(status_code=400, detail="flow_id and poll_secret required")
    with _APPLE_WEB_FLOW_LOCK:
        _apple_web_cleanup_locked()
        flow = _APPLE_WEB_FLOWS.get(flow_id)
        if not flow or not hmac.compare_digest(
                str(flow.get("poll_secret") or ""), poll_secret):
            raise HTTPException(status_code=404, detail="sign-in attempt not found")
        status = str(flow.get("status") or "pending")
        expires_at = int(flow.get("expires_at") or 0)
        if status == "complete":
            result = flow.get("result") or {}
            _APPLE_WEB_FLOWS.pop(flow_id, None)
        elif status in ("failed", "cancelled"):
            result = {"error": str(flow.get("error") or status)}
            _APPLE_WEB_FLOWS.pop(flow_id, None)
        else:
            result = None
    if status == "complete":
        return {"ok": True, "status": status, **result}
    if status in ("failed", "cancelled"):
        return {"ok": False, "status": status, **result}
    return {"ok": True, "status": status, "expires_at": expires_at}


@app.get("/app/v1/auth/apple/web/callback")
async def app_auth_apple_web_callback_get():
    return _apple_web_callback_page("failed")


@app.post("/app/v1/auth/apple/web/callback")
async def app_auth_apple_web_callback(request: Request):
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("application/x-www-form-urlencoded"):
        return _apple_web_callback_page("failed")
    raw_body = await request.body()
    if len(raw_body) > 16 * 1024:
        return _apple_web_callback_page("failed")
    try:
        form = urllib.parse.parse_qs(
            raw_body.decode("utf-8"),
            keep_blank_values=True,
            max_num_fields=16,
        )
    except (UnicodeDecodeError, ValueError):
        return _apple_web_callback_page("failed")

    def field(name: str) -> str:
        values = form.get(name) or []
        return str(values[0]).strip() if len(values) == 1 else ""

    state = field("state")
    if not state:
        return _apple_web_callback_page("failed")
    flow = _apple_web_claim_flow(state)
    if not flow:
        return _apple_web_callback_page("failed")

    flow_id = str(flow["flow_id"])
    apple_error = field("error")
    if apple_error:
        status = "cancelled" if apple_error == "user_cancelled_authorize" else "failed"
        error = "cancelled" if status == "cancelled" else "authorization_failed"
        _apple_web_finish_flow(flow_id, status, error=error)
        _log_event("apple_web_auth_cancelled" if status == "cancelled"
                   else "apple_web_auth_failed",
                   flow_hash=_short_hash(flow_id), apple_error=apple_error[:80])
        return _apple_web_callback_page(status)

    try:
        code = field("code")
        front_identity_token = field("id_token")
        if not code or not front_identity_token:
            raise ValueError("missing authorization response")
        front_claims = await asyncio.to_thread(
            _apple_verify_identity_token,
            front_identity_token,
            APPLE_WEB_CLIENT_ID,
        )
        expected_nonce = str(flow.get("nonce") or "")
        actual_nonce = str(front_claims.get("nonce") or "")
        if not expected_nonce or not hmac.compare_digest(expected_nonce, actual_nonce):
            raise ValueError("nonce mismatch")

        token_payload = await _apple_web_exchange_code(code)
        exchanged_claims = await asyncio.to_thread(
            _apple_verify_identity_token,
            str(token_payload["id_token"]),
            APPLE_WEB_CLIENT_ID,
        )
        if exchanged_claims.get("sub") != front_claims.get("sub"):
            raise ValueError("subject mismatch")
        exchanged_nonce = str(exchanged_claims.get("nonce") or "")
        if exchanged_nonce and not hmac.compare_digest(expected_nonce, exchanged_nonce):
            raise ValueError("exchanged nonce mismatch")

        user_payload = {}
        raw_user = field("user")
        if raw_user:
            parsed_user = json.loads(raw_user)
            if not isinstance(parsed_user, dict):
                raise ValueError("bad user payload")
            user_payload = parsed_user
        apple_user_id = str(exchanged_claims.get("sub") or "").strip()
        if not apple_user_id:
            raise ValueError("missing subject")
        email = str(
            exchanged_claims.get("email") or front_claims.get("email")
            or user_payload.get("email") or ""
        ).strip() or None
        display_name = _apple_web_display_name(user_payload)
        _apple_web_finish_flow(
            flow_id,
            "complete",
            result={
                "identity": {
                    "apple_user_id": apple_user_id,
                    "identity_token": str(token_payload["id_token"]),
                    "email": email,
                    "display_name": display_name,
                },
            },
        )
        _log_event(
            "apple_web_auth_success",
            flow_hash=_short_hash(flow_id),
            apple_user_hash=_short_hash(apple_user_id),
            audience=str(exchanged_claims.get("aud") or ""),
        )
        return _apple_web_callback_page("success")
    except Exception as e:  # noqa: BLE001
        _apple_web_finish_flow(flow_id, "failed", error="verification_failed")
        _log_event(
            "apple_web_auth_failed",
            flow_hash=_short_hash(flow_id),
            error=type(e).__name__,
        )
        return _apple_web_callback_page("failed")


# ── /app/v1/usage: Codex + Claude Code 本機用量(不打雲端 API)──────────
# 資料來源事實(僅取自 AIBar README 描述的檔案格式知識,程式碼為本專案重寫,
# 沒有看過/拷貝過它的 Swift 原始碼):
#   1. Codex:~/.codex/sessions/**/*.jsonl 裡 event_msg.token_count 的
#      payload.rate_limits(primary=5h window, secondary=7d/weekly window)。
#      剩餘額度 = 100 - used_percent。只挑「最近修改的幾個」session 檔、
#      每檔只讀尾部,避免整檔掃描動輒上百 MB 的 jsonl。
#   2. Claude 官方額度:~/.ai-usage/claude-status/*.json(Claude Code 官方
#      statusLine hook 寫入)。本機若沒裝這個 hook,目錄根本不存在 ——
#      必須優雅地回 available:false / official_synced:false,不能報錯。
#   3. Claude 本機備援:~/.claude/projects/**/*.jsonl 裡 assistant message
#      的 usage 欄位,用 message.id 去重(同一個 assistant turn 常因串流/
#      重試在 jsonl 裡留下多筆同 id 記錄)後加總 token 數。這只有 token
#      count,沒有官方配額百分比或重置時間。
#
# 信任邊界(AIBar README 明訂的設計原則,這裡照樣遵守):Claude 的官方額度
# 只能來自 claude-status/*.json 的 rate_limits;來源不存在、沒有
# rate_limits、或該視窗已過期,一律視為未同步 —— 絕對不能拿
# plan-usage-history.json / Desktop cache / IndexedDB 解析結果 / 本地
# token 加總去推算一個看起來像官方的百分比。
_USAGE_CACHE = {"ts": 0.0, "data": None}
_USAGE_CACHE_TTL = 10.0  # 秒;擋掉 app 端高頻輪詢造成的重複 jsonl 全掃

CODEX_SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")
CLAUDE_STATUS_DIR = os.path.expanduser("~/.ai-usage/claude-status")
CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

_USAGE_TAIL_BYTES = 200_000   # 每個 session 檔只讀最後 ~200KB 找 rate-limit
_USAGE_MAX_CODEX_FILES = 8    # 只挑最近修改的幾個 codex session 檔
_USAGE_MAX_CLAUDE_FILES = 40  # 備援統計只掃最近修改的幾個 claude jsonl


def _usage_iso_utc(epoch_seconds):
    """Unix epoch (int/float, seconds) -> ISO8601 UTC string, or None."""
    if epoch_seconds is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:  # noqa: BLE001
        return None


def _usage_now_iso_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _usage_normalize_iso_str(ts):
    """jsonl timestamps are already ISO8601 UTC (e.g. '...T16:00:50.586Z');
    just drop sub-second precision so every usage field matches the same
    'YYYY-MM-DDTHH:MM:SSZ' shape."""
    if not ts:
        return None
    try:
        s = str(ts).replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone(timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:  # noqa: BLE001
        return None


def _usage_newest_files(root, pattern="*.jsonl", limit=8):
    """Recently-modified files under root (recursive), newest first, capped
    at `limit` — this endpoint only ever needs the freshest session logs,
    never a full walk of a directory holding months of history."""
    try:
        paths = [str(p) for p in Path(root).rglob(pattern) if p.is_file()]
    except Exception:  # noqa: BLE001
        return []
    paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return paths[:limit]


def _usage_tail_text(path, max_bytes=_USAGE_TAIL_BYTES):
    """Last max_bytes of a file, decoded loosely. Session jsonl files can run
    into the hundreds of MB; the newest token_count/rate_limits event (or the
    newest assistant usage record) is always near the end, so seeking from
    EOF instead of parsing the whole file line-by-line keeps this endpoint
    fast even on old, huge logs."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _codex_latest_rate_limits():
    """Newest {timestamp, rate_limits} token_count event across the most
    recently modified codex session files, or None if nothing usable found."""
    best = None  # (timestamp_str, rate_limits_dict)
    for path in _usage_newest_files(CODEX_SESSIONS_DIR, "*.jsonl", _USAGE_MAX_CODEX_FILES):
        text = _usage_tail_text(path)
        if not text or '"token_count"' not in text:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or '"token_count"' not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            payload = rec.get("payload") or {}
            if payload.get("type") != "token_count":
                continue
            rl = payload.get("rate_limits")
            if not isinstance(rl, dict):
                continue
            ts = rec.get("timestamp") or ""
            if best is None or ts > best[0]:
                best = (ts, rl)
    if best is None:
        return None
    return {"timestamp": best[0], "rate_limits": best[1]}


def _codex_usage_snapshot():
    """codex wire block per /app/v1/usage contract. AIBar's rule: remaining
    quota is 100 - used_percent, headline percentage comes from the primary
    (5h) rate-limit window; resets_at comes straight from that same window."""
    found = _codex_latest_rate_limits()
    if not found:
        return {"available": False}
    primary = (found["rate_limits"].get("primary") or {})
    used_percent = primary.get("used_percent")
    if used_percent is None:
        return {"available": False}
    try:
        used_percent = float(used_percent)
    except Exception:  # noqa: BLE001
        return {"available": False}
    return {
        "available": True,
        "used_percent": round(used_percent, 2),
        "remaining_percent": round(100.0 - used_percent, 2),
        "reset_at": _usage_iso_utc(primary.get("resets_at")),
        "source": "codex_sessions_jsonl",
        "last_synced_at": _usage_now_iso_utc(),
    }


def _claude_official_snapshot():
    """~/.ai-usage/claude-status/*.json written by Claude Code's official
    statusLine hook. Trust boundary (per AIBar's own design note): the ONLY
    legitimate source for Claude's official quota percentage is this file's
    rate_limits field. No hook installed / no rate_limits / an expired reset
    window all mean "not synced" — never backfill a percentage from anywhere
    else (Desktop cache, IndexedDB dumps, local jsonl token counts, etc.)."""
    try:
        files = [str(p) for p in Path(CLAUDE_STATUS_DIR).glob("*.json") if p.is_file()]
    except Exception:  # noqa: BLE001
        return None
    if not files:
        return None
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    now = time.time()

    def _window(block):
        if not isinstance(block, dict):
            return None
        used = block.get("used_percentage", block.get("used_percent"))
        resets_at = block.get("resets_at")
        if used is None or resets_at is None:
            return None
        try:
            resets_at = float(resets_at)
            used = float(used)
        except Exception:  # noqa: BLE001
            return None
        if resets_at <= now:   # window 已過期 -> 視為未同步,不能沿用舊值
            return None
        return {
            "used_percent": round(used, 2),
            "remaining_percent": round(100.0 - used, 2),
            "reset_at": _usage_iso_utc(resets_at),
        }

    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                doc = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        rate_limits = doc.get("rate_limits")
        if not isinstance(rate_limits, dict):
            continue
        five_w = _window(rate_limits.get("five_hour"))
        seven_w = _window(rate_limits.get("seven_day"))
        if five_w is None and seven_w is None:
            continue  # 這份 status 檔沒有可用的 rate_limits -> 未同步
        return {
            "five_hour": five_w,
            "seven_day": seven_w,
            "mtime": os.path.getmtime(path),
            "account_label": doc.get("account_label") or doc.get("email") or None,
        }
    return None


def _claude_local_fallback_usage():
    """~/.claude/projects/**/*.jsonl assistant message usage, de-duplicated by
    (path, message.id) — a streamed/retried turn can appear multiple times in
    the log — and summed. Token counts ONLY, no percentage/reset time; exists
    purely so the app has *something* while official_synced is false, and
    must never be dressed up as an official quota number."""
    total_input = total_output = total_cache_read = total_cache_creation = 0
    seen_ids = set()
    latest_ts = None
    for path in _usage_newest_files(CLAUDE_PROJECTS_DIR, "*.jsonl", _USAGE_MAX_CLAUDE_FILES):
        text = _usage_tail_text(path)
        if not text or '"assistant"' not in text:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or '"assistant"' not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            mid = msg.get("id")
            dedupe_key = (path, mid) if mid else (path, line[:80])
            if dedupe_key in seen_ids:
                continue
            seen_ids.add(dedupe_key)
            total_input += int(usage.get("input_tokens") or 0)
            total_output += int(usage.get("output_tokens") or 0)
            total_cache_read += int(usage.get("cache_read_input_tokens") or 0)
            total_cache_creation += int(usage.get("cache_creation_input_tokens") or 0)
            ts = rec.get("timestamp")
            if ts and (latest_ts is None or ts > latest_ts):
                latest_ts = ts
    if not seen_ids:
        return None
    return {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cache_read_input_tokens": total_cache_read,
        "cache_creation_input_tokens": total_cache_creation,
        "latest_timestamp": latest_ts,
    }


def _claude_usage_snapshot():
    """claude wire block. official_synced only ever comes from the
    statusLine hook's rate_limits; the local jsonl fallback supplies raw
    token counts (not percent) when the hook isn't installed / hasn't
    produced a fresh window yet."""
    official = _claude_official_snapshot()
    if official is not None:
        return {
            "available": True,
            "official_synced": True,
            "five_hour": official["five_hour"],
            "seven_day": official["seven_day"],
            "source": "claude_statusline",
            "last_synced_at": _usage_iso_utc(official["mtime"]),
            "account_label": official.get("account_label"),
        }
    fallback = _claude_local_fallback_usage()
    if fallback is None:
        return {"available": False, "official_synced": False}
    return {
        "available": True,
        "official_synced": False,
        "five_hour": None,
        "seven_day": None,
        "source": "claude_projects_jsonl_fallback",
        "last_synced_at": _usage_normalize_iso_str(fallback["latest_timestamp"]) or _usage_now_iso_utc(),
        "account_label": None,
        "token_usage": {
            "input_tokens": fallback["input_tokens"],
            "output_tokens": fallback["output_tokens"],
            "cache_read_input_tokens": fallback["cache_read_input_tokens"],
            "cache_creation_input_tokens": fallback["cache_creation_input_tokens"],
        },
    }


@app.get("/app/v1/usage")
async def app_usage(request: Request):
    """Codex + Claude Code 本機用量,供 Pocket app 設定頁消費。純讀本機
    session/status 檔案,不打任何雲端用量 API(比照 AIBar 的做法)。
    10 秒快取,擋掉高頻輪詢造成的重複 jsonl 全掃。"""
    _check_auth(request)
    now = time.time()
    cached = _USAGE_CACHE["data"]
    if cached is not None and now - _USAGE_CACHE["ts"] < _USAGE_CACHE_TTL:
        return cached
    data = {
        "codex": _codex_usage_snapshot(),
        "claude": _claude_usage_snapshot(),
    }
    _USAGE_CACHE["data"] = data
    _USAGE_CACHE["ts"] = now
    return data


@app.get("/app/v1/account")
async def app_account(request: Request, include_revoked: bool = False):
    user = _account_user_from_request(request)
    devices = _account_devices_for_user(user["apple_user_id"], include_revoked=include_revoked)
    return {
        "user": _account_public_user(user),
        "devices": [_account_public_device(d) for d in devices],
    }


@app.post("/app/v1/devices/{device_id}/revoke")
async def app_revoke_account_device(device_id: str, request: Request):
    user = _account_user_from_request(request)
    device = _account_device_by_id(user["apple_user_id"], device_id)
    if not device:
        raise HTTPException(status_code=404, detail="unknown device")
    revoked = _account_device_revoke(user["apple_user_id"], device_id)
    token = device.get("device_token")
    if revoked and token:
        with _PAIR_LOCK:
            if token in _DEVICE_TOKENS:
                _DEVICE_TOKENS.pop(token, None)
                _save_device_tokens(_DEVICE_TOKENS)
    _log_event("account_device_revoked",
               apple_user_hash=_short_hash(user.get("apple_user_id")),
               device_id=device_id,
               token_hash=_short_hash(token))
    return {"revoked": revoked}


@app.get("/app/v1/sessions")
async def app_sessions(request: Request):
    return await list_sessions(request)


@app.get("/app/v1/delegations")
async def app_delegations(request: Request, parent_persona: str = "",
                          status: str = "", task_code: str = "", limit: int = 50):
    _check_auth(request)
    rows = _delegation_rows(limit=limit, parent_persona=parent_persona,
                             status=status, task_code=task_code)
    out = []
    for row in rows:
        out.append(_delegation_public(row, await _delegation_runtime_status(row)))
    return {"delegations": out}


@app.get("/app/v1/delegations/{delegation_id}")
async def app_delegation_get(delegation_id: str, request: Request):
    _check_auth(request)
    row = _delegation_get(delegation_id)
    if not row:
        raise HTTPException(status_code=404, detail="unknown delegation")
    return {"delegation": _delegation_public(row, await _delegation_runtime_status(row))}


@app.post("/app/v1/delegations/{delegation_id}/input")
async def app_delegation_input(delegation_id: str, request: Request):
    _check_auth(request)
    row = _delegation_get(delegation_id)
    if not row:
        raise HTTPException(status_code=404, detail="unknown delegation")
    d = dict(row)
    body = await request.json()
    text = (body.get("content") or body.get("text") or body.get("message") or "").strip()
    if not text and not body.get("attachments"):
        raise HTTPException(status_code=400, detail="content or attachments required")
    text = f"[工號 {d.get('work_order')}] {text}".strip()
    if d.get("provider") == "codex":
        thread_id = d.get("codex_thread_id") or d.get("provider_session_id") or ""
        if not thread_id:
            raise HTTPException(status_code=409, detail="delegation has no codex thread")
        input_items = await _codex_input_items(text, body.get("attachments") or [])
        try:
            res = await CODEX_APP.start_turn(thread_id, input_items,
                                             client_id=body.get("client_id"),
                                             cwd=d.get("cwd"))
        except Exception as e:  # noqa: BLE001
            _codex_http_error(e)
        _delegation_update(d["id"], status="running", updated_at=time.time(), last_error="")
        return {"ok": True, "delegation_id": d["id"], "work_order": d.get("work_order"),
                "provider": "codex", "thread_id": thread_id,
                "turn": (res or {}).get("turn")}
    if d.get("provider") == "claude_code":
        name = d.get("cc_session_name") or d.get("provider_session_id") or ""
        if not name:
            raise HTTPException(status_code=409, detail="delegation has no cc session")
        saved = []
        voice_lines = []
        for a in (body.get("attachments") or []):
            path = _save_attachment(a, a.get("filename") or "file")
            if not path:
                continue
            if a.get("kind") == "audio":
                t = await asyncio.to_thread(_transcribe, path)
                if t:
                    voice_lines.append(t)
            else:
                saved.append(path)
        if voice_lines:
            text += "\n\n[語音附件轉寫]\n" + " ".join(voice_lines)
        if saved:
            text += "\n\n[附件已存到本機,請用 Read 讀取/檢視]\n" + "\n".join(saved)
        await _cc_paste_text(name, text)
        _delegation_update(d["id"], status="running", updated_at=time.time(), last_error="")
        return {"ok": True, "delegation_id": d["id"], "work_order": d.get("work_order"),
                "provider": "claude_code", "session_name": name}
    raise HTTPException(status_code=400, detail="unsupported delegation provider")


@app.post("/app/v1/delegations/{delegation_id}/report")
async def app_delegation_report(delegation_id: str, request: Request):
    """子代理主動回報成果/里程碑(M1-3)。成果摘要由做事的人自己寫,品質最高、
    不靠 watcher 猜。body {summary, files?, verification?, status?}。id 或工號皆可。
    status=done/idle → 標完成並回流「已完成」;failed → 失敗;其餘 → 進度回報。"""
    _check_auth(request)
    row = _delegation_get(delegation_id)
    if not row:
        raise HTTPException(status_code=404, detail="unknown delegation")
    d = dict(row)
    body = await request.json()
    summary = str(body.get("summary") or body.get("content") or "").strip()
    if not summary:
        raise HTTPException(status_code=400, detail="summary required")
    status_in = str(body.get("status") or "").strip().lower()
    meta = _delegation_meta(d)
    meta["last_report"] = {"summary": summary[:4000], "ts": time.time(),
                           "files": body.get("files") or [],
                           "verification": str(body.get("verification") or "")[:2000]}
    fields = {"meta": meta, "updated_at": time.time()}
    if status_in in ("done", "idle"):
        fields["status"] = "idle"
    elif status_in in ("failed", "running"):
        fields["status"] = status_in
    _delegation_update(d["id"], **fields)
    d["meta"] = json.dumps(meta, ensure_ascii=False)
    if "status" in fields:
        d["status"] = fields["status"]
    event = ("done" if status_in in ("done", "idle")
             else "failed" if status_in == "failed" else "report")
    await _delegation_notify(d, event, summary=summary)
    return {"ok": True, "delegation_id": d["id"], "work_order": d.get("work_order"),
            "event": event}


@app.post("/app/v1/delegations")
async def app_delegation_create(request: Request):
    """Create a durable CC/Codex work-order session.

    This is the shared dispatch surface for every Hermes persona. It creates a
    provider-native session first (Codex app-server thread or ccsess Claude Code
    session), then stores the parent persona + work_order mapping so Pocket,
    Telegram, and official provider surfaces can all point to the same work.
    """
    _check_auth(request)
    body = await request.json()
    parent = (body.get("parent_persona") or body.get("parent") or "xcash").strip()
    if parent not in PERSONAS:
        raise HTTPException(status_code=400, detail="unknown parent_persona")
    provider = _normalise_provider(body.get("provider") or body.get("tool") or "codex")
    objective = (body.get("objective") or body.get("task") or body.get("text") or "").strip()
    if not objective:
        raise HTTPException(status_code=400, detail="objective required")
    title = (body.get("title") or objective.splitlines()[0]).strip()[:120]
    cwd = _normalise_workdir(body.get("cwd") or body.get("workdir") or HOME_ROOT,
                             create=(provider == "claude_code"))

    task_code_raw = (body.get("task_code") or body.get("task_id") or "").strip()
    subtask_code_raw = (body.get("subtask_code") or body.get("subtask_id") or "").strip()
    explicit_work_order = (body.get("work_order") or "").strip()
    if not explicit_work_order and not (task_code_raw and subtask_code_raw):
        raise HTTPException(
            status_code=400,
            detail="task_code and subtask_code are required (e.g. task_code=POCKETCONN, "
                   "subtask_code=APPLELOGIN) so work orders stay filterable by project/"
                   "subtask; pass an explicit work_order instead only for one-off cases")
    task_code = _work_order_segment(task_code_raw, fallback="GEN", max_len=16)
    subtask_code = _work_order_segment(subtask_code_raw, fallback="TASK", max_len=20)
    work_order = (explicit_work_order or
                  _new_work_order(parent, task_code, subtask_code)).strip().upper()
    if not re.match(r"^[A-Z0-9][A-Z0-9._-]{2,60}$", work_order):
        raise HTTPException(status_code=400, detail="unsupported work_order")
    # M2:CC↔CX 互調的呼叫鏈標記 + 防遞迴。parent_delegation = 父工號/父 id,
    # depth 隨鏈遞增,>2 擋(防互派炸鏈);同父併發 running 子任務 >3 擋。
    parent_delegation_ref = str(body.get("parent_delegation") or "").strip()
    parent_dlg_id = ""
    depth = 0
    if parent_delegation_ref:
        prow = _delegation_get(parent_delegation_ref)
        if not prow:
            raise HTTPException(status_code=400, detail="unknown parent_delegation")
        pd = dict(prow)
        parent_dlg_id = pd.get("id") or ""
        depth = int(_delegation_meta(pd).get("depth") or 0) + 1
        if depth > 2:
            raise HTTPException(status_code=400,
                                detail="delegation chain too deep (max depth 2)")
        running_children = sum(
            1 for r in _delegation_rows(limit=200)
            if _delegation_meta(dict(r)).get("parent_delegation") == parent_dlg_id
            and dict(r).get("status") == "running")
        if running_children >= 3:
            raise HTTPException(status_code=429,
                                detail="parent already has 3 running children")
    did = "dlg-" + uuid.uuid4().hex[:16]
    now = time.time()
    prompt = _delegation_prompt(work_order, parent, title, objective, cwd, body)
    provider_session_id = ""
    codex_thread_id = ""
    cc_session_name = ""
    status = "created"
    meta = {
        "parent_display": PERSONAS[parent][0],
        "created_by": "bridge",
        "created_via": body.get("created_via") or "bridge",
        "parent_delegation": parent_dlg_id,
        "depth": depth,
    }

    if provider == "codex":
        input_items = await _codex_input_items(prompt, body.get("attachments") or [])
        params = {"cwd": cwd, "ephemeral": False, "threadSource": "user"}
        if body.get("model"):
            params["model"] = body.get("model")
        try:
            res = await CODEX_APP.call("thread/start", params, timeout=30.0)
            thread = (res or {}).get("thread") or {}
            codex_thread_id = thread.get("id") or ""
            if not codex_thread_id:
                raise CodexAppServerError("thread/start returned no thread id")
            provider_session_id = codex_thread_id
            CODEX_APP.loaded_threads.add(codex_thread_id)
            try:
                await CODEX_APP.call("thread/name/set", {
                    "threadId": codex_thread_id,
                    "name": f"{work_order} - {title[:80]}",
                }, timeout=15.0)
            except Exception:  # noqa: BLE001
                pass
            await CODEX_APP.start_turn(codex_thread_id, input_items,
                                       client_id=f"delegation-{did}", cwd=cwd)
            status = "running"
        except Exception as e:  # noqa: BLE001
            _codex_http_error(e)
    else:
        requested = (body.get("session_name") or body.get("name") or "").strip()
        # work_order 本身已含 task/subtask 資訊，直接當 session 名稱即可，不再
        # 額外拼 title slug（新格式比 v1 長，拼上去容易變成過長又重複的 tmux
        # session 名稱）。
        cc_session_name = requested or work_order.lower()
        if any(ch in cc_session_name for ch in "/|:\n\r\t"):
            raise HTTPException(status_code=400, detail="unsupported session_name")
        _pretrust_claude_dir(cwd)
        # P0 派工分級(2026-07-10):正式派工端點也支援 model 參數(與 Codex
        # 分支的 params["model"] 對齊),企劃/大局思考類任務可指定旗艦模型。
        cc_model = (body.get("model") or "").strip()
        cc_new_args = ["new", cc_session_name, cwd] + ([cc_model] if cc_model else [])
        await _run_ccsess(*cc_new_args)
        ready = await _cc_wait_ready(cc_session_name)
        cc_prompt = prompt
        saved = []
        voice_lines = []
        for a in (body.get("attachments") or []):
            path = _save_attachment(a, a.get("filename") or "file")
            if not path:
                continue
            if a.get("kind") == "audio":
                t = await asyncio.to_thread(_transcribe, path)
                if t:
                    voice_lines.append(t)
            else:
                saved.append(path)
        if voice_lines:
            cc_prompt += "\n\n[語音附件轉寫]\n" + " ".join(voice_lines)
        if saved:
            cc_prompt += "\n\n[附件已存到本機,請用 Read 讀取/檢視]\n" + "\n".join(saved)
        await _cc_paste_text(cc_session_name, cc_prompt)
        provider_session_id = cc_session_name
        status = "running" if ready else "starting"

    row = {
        "id": did,
        "work_order": work_order,
        "parent_persona": parent,
        "parent_session": body.get("parent_session") or "",
        "created_via": body.get("created_via") or "bridge",
        "provider": provider,
        "title": title,
        "objective": objective,
        "cwd": cwd,
        "status": status,
        "provider_session_id": provider_session_id,
        "codex_thread_id": codex_thread_id,
        "cc_session_name": cc_session_name,
        "created_at": now,
        "updated_at": now,
        "last_error": "",
        "meta": meta,
        "task_code": task_code,
        "subtask_code": subtask_code,
    }
    try:
        _delegation_insert(row)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"delegation registry write failed: {e}")
    _log_event("delegation_created",
               work_order=work_order,
               parent_persona=parent,
               provider=provider,
               created_via=meta.get("created_via"),
               depth=depth,
               provider_session_hash=_short_hash(provider_session_id),
               objective_chars=len(objective),
               attachment_count=len(body.get("attachments") or []))
    # M1:建立即回流一張「已建立」卡進父人格對話(父是 delegation 則注回父 session)。
    try:
        await _delegation_notify(dict(row), "created")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "delegation": _delegation_public(row, status)}


@app.post("/app/v1/persona-report")
async def app_post_persona_report(request: Request):
    """外部內容線(FLiPER fed 的 today-pick / story 發佈)灌一則報告進某人格對話流。
    寫進 report_events(external_source 自訂,不會被 cron 同步蓋掉),再由
    _report_messages 併進 v1/v2 卡片流 → 出現在 Pocket 該人格聊天(卡片流 30s 保險絲
    週期補掃)。fed 端在發佈 today-pick / story 時 POST 這裡即可。"""
    _check_auth(request)
    body = await request.json()
    session = str(body.get("session") or "").strip()
    if session not in PERSONAS:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown persona session")
    content = str(body.get("content") or "").strip()
    if not content:
        raise http_err(400, "EMPTY_CONTENT", "content required")
    ts = float(body.get("ts") or time.time())
    report = {
        "label": str(body.get("label") or "今日精選"),
        "name": str(body.get("name") or "fed-today"),
        "content": content,
        "ts": ts,
        "external_source": str(body.get("external_source") or "fed"),
        "external_id": str(body.get("external_id") or "")
                       or _report_id(session, "fed-today", "", ts),
    }
    rid = _report_upsert(session, report)
    return {"ok": True, "id": rid}


@app.get("/app/v1/messages")
async def app_get_messages(session: str, request: Request, limit: int = 200):
    """Canonical history for a persona: app turns (bridge canonical store) merged
    with the Telegram history (Hermes state.db), ordered by time — so every
    device sees the same interleaved conversation."""
    _check_auth(request)
    if session not in PERSONAS:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown session")
    out = _canon_messages(session, limit)
    _, home = PERSONAS[session]
    # 同一則訊息會在兩個來源各留一份:
    # - assistant:app 回合寫 canonical(正文+〈🔧 執行步驟〉摺疊附錄、帶
    #   client_id)、Hermes state.db 另存乾淨正文(tg-* id、無 client_id)。
    # - user:TG 鏡像 ingest(/internal/v1/mirror/telegram-event)落 canonical
    #   (tgm-* id)後,state.db 掃描會再掃到同一句。
    # 兩份文字/ID 不同 → app 端按文字去重必然失敗,同一句畫面出現兩顆氣泡。
    # 在源頭壓掉 tg 側重複:同 role、剝附錄後正文相同、時間差 10 分鐘內,
    # 視為同一則。純 TG 對話(canonical 無副本)與相隔久遠的同文不受影響。
    def _steps_stripped(t: str) -> str:
        return re.sub(r"<details>.*?</details>", "", t or "", flags=re.S).strip()
    canon_recent = [((m.get("ts") or 0), m.get("role"),
                     _steps_stripped(m.get("content") or ""))
                    for m in out if m.get("role") in ("user", "assistant")]
    def _tg_dup(m) -> bool:
        body = _steps_stripped(m["content"])
        ts = m["ts"] or 0
        return bool(body) and any(r == m["role"] and c == body and abs(ts - cts) < 600
                                  for cts, r, c in canon_recent)
    for m in _persona_history(home, limit):
        if _tg_dup(m):
            continue
        out.append({"id": f"tg-{m['ts']}", "role": m["role"], "content": m["content"],
                    "attachments": m.get("attachments") or [], "ts": m["ts"],
                    "status": "done", "source": "telegram"})
    # Surface each persona's daily briefs (cron-delivered) IN its conversation,
    # like Telegram does — not only in the separate Reports tab. 袁方's 晨報/午報
    # etc. and 潘天晴's 編輯台晨報 (+ future 今日精選/限動) read from each persona's
    # OWN home, so the app thread matches what TG received this morning.
    _sync_persona_reports(session, 50)
    out.extend(_report_messages(session, limit))
    out.sort(key=lambda m: m.get("ts") or 0)
    out = out[-limit:]
    # Sync engine P1:app 每次輪詢這頁就是一次現成的三來源合併掃描,順手
    # 鏡射進 event_log(冪等、穩態零寫入;鏡射在 reaction overlay 疊加前,
    # payload 保持訊息本體的正典形狀)。
    _event_mirror_messages(session, out)
    # Reaction overlay (G2/#39) — one lookup for the whole page, ids as-is
    # (canonical mids and tg-<ts> alike), so reactions survive reinstall and
    # show identically on every device.
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        rows = con.execute("SELECT msg_id, reaction FROM reactions WHERE session=?",
                           (session,)).fetchall()
        overlay = {r[0]: r[1] for r in rows if r[1]}
        # Canonical multi-emoji reactions + pins (G2/#39 final contract). Both
        # fields are optional in the payload: omitted when there's no data.
        meta_rows = con.execute(
            "SELECT message_id, reactions, pinned, deleted FROM message_meta").fetchall()
        con.close()
        meta = {}
        for mid_, rx, pn, dl in meta_rows:
            try:
                lst = json.loads(rx) if rx else []
            except Exception:  # noqa: BLE001
                lst = []
            meta[mid_] = ([str(r) for r in lst if r] if isinstance(lst, list) else [],
                          bool(pn), bool(dl))
        for m in out:
            mid_ = str(m.get("id"))
            legacy = overlay.get(mid_)
            if legacy:
                m["reaction"] = legacy
            if mid_ in meta:
                reactions, pinned, deleted = meta[mid_]
                if reactions:
                    m["reactions"] = reactions
                if pinned:
                    m["pinned"] = True
                if deleted:
                    m["deleted"] = True    # G4 tombstone: row stays, flagged
            elif legacy:
                # Older builds wrote the single-reaction overlay only; surface
                # it in the new list field too so nothing disappears mid-migration.
                m["reactions"] = [legacy]
    except Exception as e:  # noqa: BLE001
        # Failing open (messages without reactions/pins) is right, but silent
        # failure made it undiagnosable (issue #7).
        _log_event("reaction_overlay_read_failed", session=session,
                   error=type(e).__name__, error_message=str(e)[:160])
    return {"messages": out}


@app.get("/app/v1/messages/status")
async def app_get_message_status(session: str, request: Request,
                                 client_id: str = ""):
    """Recovery status for a persona turn started by /app/v1/messages.

    Pocket polls this when a mobile upload/stream detaches. Returning an honest
    state here lets the app show delivered/running/done and avoids re-running
    image-heavy turns just because the phone lost its SSE connection.
    """
    _check_auth(request)
    if session not in PERSONAS:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown session")
    acp = await POOL.get(session, home_for(session))
    return _app_turn_status(session, client_id or None, acp_busy=acp.is_busy())


@app.get("/app/v1/messages/events")
async def app_get_message_events(session: str, request: Request,
                                 since: int = 0, follow: bool = True):
    """SSE feed for canonical persona messages.

    This is intentionally backed by the canonical store instead of an in-memory
    queue, so it survives bridge restarts and covers turns that completed after
    the client disconnected.
    """
    _check_auth(request)
    if session not in PERSONAS:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown session")

    async def gen():
        cursor = int(since or 0)
        deadline = time.monotonic() + (120.0 if follow else 0.0)
        last_ver = -1    # 首輪必掃(補客戶端斷線期間的積壓)
        last_scan = 0.0  # 保險絲:就算沒收到信號,至少每 30s 重掃一次
                         # (防未來新增的 canonical 寫入點忘了掛 _canon_notify)
        while True:
            sent = False
            ver = _CANON_VER.get(session, 0)
            if ver != last_ver or time.monotonic() - last_scan >= 30.0:
                last_scan = time.monotonic()
                # 真推送:只有 canonical 寫入過才重掃 DB。原本每 2 秒
                # 每 follower 讀 80 則+JSON parse 的空轉,是 bridge 閒置
                # 負載的大宗(N persona × M 裝置全天在跑)。
                last_ver = ver
                for msg in _canon_messages(session, 80):
                    seq = _app_message_seq(msg)
                    if seq <= cursor:
                        continue
                    event = _app_message_event(msg)
                    cursor = max(cursor, int(event["seq"]))
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    sent = True
            if not follow:
                yield "data: [DONE]\n\n"
                return
            if not sent:
                yield ": keepalive\n\n"
            if time.monotonic() >= deadline:
                yield "data: [DONE]\n\n"
                return
            try:
                await asyncio.wait_for(_canon_wait(session, last_ver),
                                       timeout=SSE_KEEPALIVE_SECS)
            except asyncio.TimeoutError:
                pass

    return StreamingResponse(gen(), media_type="text/event-stream")


# ─────────────── Sync engine P2:/app/v2 統一事件流 + 已讀游標 ───────────
_EVENTS_FOLLOW_MAX_SECS = 120.0   # 與 v1 messages/events 同款:app 週期重連


@app.get("/app/v2/events")
async def app_v2_events(request: Request, session: str | None = None,
                        since_seq: int = 0, follow: bool = True):
    """SYNC_ENGINE_REWRITE_PLAN §3.1 的統一訂閱端點:從 event_log 撈
    id > since_seq 的所有列,補洞 + 即時走同一條 SSE,三來源(App/TG/cron)
    與已讀游標不再各走各的加速通道。信封 {seq, ts, type, data} 與
    /app/v2/sessions/{id}/events 卡片流對齊;event_log 是持久表,沒有卡片
    ring buffer 的 410 SEQ_GONE 問題 — 任何裝置 since_seq=0 重放即可重建
    完整歷史(§3.3 / backlog B3)。

    session 可省略(P3 契約 #2):省略 = 全域訂閱,單一條 SSE 涵蓋全部
    hermes 人格 session(App 首頁列表+未讀靠這條,不用每 persona 開一條)。
    全域信封多帶 session 欄位;event_log.id 全域單調,since_seq 游標語意
    與 per-session 模式相同。"""
    _check_auth(request)
    if session is not None and session not in PERSONAS:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown session")

    async def gen():
        cursor = max(0, int(since_seq or 0))
        # 連上先主動拉一次 TG/cron(force 穿越節流):新訂閱者立刻看到外部
        # 來源的積壓,不用等下一個同步週期。
        await asyncio.to_thread(_event_sync_session, session, 200, True)
        deadline = time.monotonic() + (_EVENTS_FOLLOW_MAX_SECS if follow else 0.0)
        last_ver = -1     # 首輪必掃(補訂閱者斷線期間的積壓)
        last_sync = time.monotonic()
        sver = _STATEDB_VER.get(session, 0)   # watcher 版本基準(#tg-instant-sync)
        while True:
            sent = False
            cur_sver = _STATEDB_VER.get(session, 0)
            if cur_sver != sver:
                # state.db 剛被 TG/cron 寫入(stat watcher bump)→ 立刻拉進
                # event_log,不等下面的 10s 週期節流。先去抖:睡到距上次掃
                # 描 ≥0.5s 再掃,吸掉同一批寫入的連續 bump;之後用 0.4s 小
                # 節流 — 自己的時間線必然通過(剛睡滿 0.5s),只有別的訂閱
                # 者在我們去抖期間已經掃過(該掃必然晚於這次寫入,已涵蓋)
                # 才跳過。寫入絕不會被節流吞掉只剩 10s 兜底。
                sver = cur_sver
                gap = 0.5 - (time.monotonic() - _EVENT_SYNC_TS.get(session, 0.0))
                if gap > 0:
                    await asyncio.sleep(min(gap, 0.5))
                last_sync = time.monotonic()
                await asyncio.to_thread(_event_sync_session, session, 200,
                                        False, 0.4)
            ver = _EVENT_VER.get(session, 0)
            if ver != last_ver:
                last_ver = ver
                while True:
                    batch = await asyncio.to_thread(_event_since, session,
                                                    cursor, 500)
                    for ev in batch:
                        cursor = ev["seq"]
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                        sent = True
                    if len(batch) < 500:
                        break
            if not follow:
                yield "data: [DONE]\n\n"
                return
            if not sent:
                yield ": keepalive\n\n"
            if time.monotonic() >= deadline:
                yield "data: [DONE]\n\n"
                return
            if time.monotonic() - last_sync >= _EVENT_SYNC_MIN_SECS:
                # 週期主動拉當保險絲:statedb watcher(上面的 sver 檢查)是
                # 即時觸發主力,這條是 watcher 失效時的兜底 — v2 訂閱者的
                # TG 延遲上限仍 = _EVENT_SYNC_MIN_SECS,不依賴單一機制。
                last_sync = time.monotonic()
                await asyncio.to_thread(_event_sync_session, session, 200)
            try:
                await asyncio.wait_for(
                    _event_or_statedb_wait(session, last_ver, sver),
                    timeout=SSE_KEEPALIVE_SECS)
            except asyncio.TimeoutError:
                pass

    async def gen_all():
        # 全域訂閱:與 gen() 同構,差異只在 (a) 初連/兜底同步掃全部
        # persona (b) watcher 以 per-session snapshot 找出「誰剛被寫入」
        # 只拉那幾個 (c) 批次改走 _event_since_all、版本盯 _EVENT_VER_ALL。
        cursor = max(0, int(since_seq or 0))
        for s in list(PERSONAS):
            await asyncio.to_thread(_event_sync_session, s, 200, True)
        deadline = time.monotonic() + (_EVENTS_FOLLOW_MAX_SECS if follow else 0.0)
        last_ver = -1     # 首輪必掃
        last_sync = time.monotonic()
        svers = dict(_STATEDB_VER)   # per-session watcher 版本基準
        while True:
            sent = False
            # 先讀全域計數再算 changed:之後才 bump 的寫入會讓下面的
            # wait 立刻返回,喚醒不漏(順序反過來就有睡過頭的窗)。
            cur_sall = _STATEDB_VER_ALL
            changed = [s for s in list(PERSONAS)
                       if _STATEDB_VER.get(s, 0) != svers.get(s, 0)]
            if changed:
                # 去抖 + 小節流與 per-session 版同參數;gap 以 changed 中
                # 最近一次掃描起算(保守但上限 0.5s)。
                for s in changed:
                    svers[s] = _STATEDB_VER.get(s, 0)
                gap = 0.5 - (time.monotonic() - max(
                    _EVENT_SYNC_TS.get(s, 0.0) for s in changed))
                if gap > 0:
                    await asyncio.sleep(min(gap, 0.5))
                last_sync = time.monotonic()
                for s in changed:
                    await asyncio.to_thread(_event_sync_session, s, 200,
                                            False, 0.4)
            ver = _EVENT_VER_ALL
            if ver != last_ver:
                last_ver = ver
                while True:
                    batch = await asyncio.to_thread(_event_since_all,
                                                    cursor, 500)
                    for ev in batch:
                        cursor = ev["seq"]
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                        sent = True
                    if len(batch) < 500:
                        break
            if not follow:
                yield "data: [DONE]\n\n"
                return
            if not sent:
                yield ": keepalive\n\n"
            if time.monotonic() >= deadline:
                yield "data: [DONE]\n\n"
                return
            if time.monotonic() - last_sync >= _EVENT_SYNC_MIN_SECS:
                last_sync = time.monotonic()
                for s in list(PERSONAS):
                    await asyncio.to_thread(_event_sync_session, s, 200)
            try:
                await asyncio.wait_for(
                    _event_or_statedb_wait_all(last_ver, cur_sall),
                    timeout=SSE_KEEPALIVE_SECS)
            except asyncio.TimeoutError:
                pass

    return StreamingResponse(gen() if session is not None else gen_all(),
                             media_type="text/event-stream")


def _read_cursor_rows(session: str) -> list[dict]:
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
        rows = con.execute(
            "SELECT device_id,last_read_seq,last_read_ts,message_id,updated_at "
            "FROM read_cursors WHERE session=?", (session,)).fetchall()
        con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("read_cursor_read_failed", session=session,
                   error=type(e).__name__, error_message=str(e)[:160])
        return []
    return [{"device_id": r[0], "last_read_seq": r[1], "last_read_ts": r[2],
             "message_id": r[3], "updated_at": r[4]} for r in rows]


def _read_cursor_global(rows: list[dict]) -> dict:
    """已拍板語意(2026-07-11 善彰):「任一裝置讀過即全讀」= 全裝置 MAX。
    這裡做伺服器端聚合,App 端(P3)未讀數直接拿 global.last_read_seq 比
    event seq,不用自己算;cursors 仍按裝置分列保留原始資料(若未來要改
    per-device 語意,資料都在,schema 不用動)。"""
    return {
        "last_read_seq": max((int(r.get("last_read_seq") or 0) for r in rows),
                             default=0),
        "last_read_ts": max((float(r.get("last_read_ts") or 0.0) for r in rows),
                            default=0.0),
    }


@app.get("/app/v2/read")
async def app_v2_read_get(session: str, request: Request):
    """該 session 全部裝置的已讀游標(新裝置冷載算未讀用)。global 欄位
    是拍板語意「任一裝置讀過即全讀」的聚合(見 _read_cursor_global)。"""
    _check_auth(request)
    if session not in PERSONAS:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown session")
    rows = _read_cursor_rows(session)
    return {"session": session, "cursors": rows,
            "global": _read_cursor_global(rows)}


@app.post("/app/v2/read")
async def app_v2_read_post(request: Request):
    """回報已讀游標(SYNC_ENGINE_REWRITE_PLAN §3.1 read_cursor.update)。
    body: {session, device_id, last_read_seq 或 last_read_ts, message_id?}
    游標只進不退(多裝置/亂序回報取 max);真的前進才追加一筆
    read_cursor.update 事件,其他訂閱中的裝置從 /app/v2/events 收到就能
    同步已讀狀態 — 未讀從此有伺服器真相,不再是各裝置本地計數器瞎猜。"""
    import sqlite3
    _check_auth(request)
    body = await _json_body(request)
    session = (body.get("session") or "").strip()
    device_id = (body.get("device_id") or "").strip()
    if session not in PERSONAS:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown session")
    if not device_id:
        raise http_err(400, "DEVICE_ID_REQUIRED",
                       "device_id 必填(已讀游標按裝置分列)")
    try:
        req_seq = max(0, int(body.get("last_read_seq") or 0))
        req_ts = max(0.0, float(body.get("last_read_ts") or 0))
    except (TypeError, ValueError):
        raise http_err(400, "CURSOR_INVALID", "last_read_seq/last_read_ts 需為數字")
    message_id = str(body.get("message_id") or "").strip() or None
    if req_seq <= 0 and req_ts <= 0:
        raise http_err(400, "CURSOR_REQUIRED",
                       "last_read_seq 或 last_read_ts 至少要有一個")
    now = time.time()
    con = sqlite3.connect(CANON_DB, timeout=30)
    try:
        row = con.execute(
            "SELECT last_read_seq,last_read_ts,message_id,updated_at "
            "FROM read_cursors WHERE session=? AND device_id=?",
            (session, device_id)).fetchone()
        prev_seq, prev_ts = (row[0], row[1]) if row else (0, 0.0)
        new_seq, new_ts = max(prev_seq, req_seq), max(prev_ts, req_ts)
        moved = new_seq > prev_seq or new_ts > prev_ts
        if moved:
            message_id = message_id or (row[2] if row else None)
            con.execute(
                "INSERT OR REPLACE INTO read_cursors"
                "(session,device_id,last_read_seq,last_read_ts,message_id,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (session, device_id, new_seq, new_ts, message_id, now))
            con.commit()
        grow = con.execute(
            "SELECT MAX(last_read_seq),MAX(last_read_ts) FROM read_cursors "
            "WHERE session=?", (session,)).fetchone()
    finally:
        con.close()
    # 拍板語意「任一裝置讀過即全讀」的聚合,事件與回應都帶上 — 其他訂閱
    # 中的裝置收到 read_cursor.update 直接用 global 更新未讀,不用再 GET。
    gcur = {"last_read_seq": int(grow[0] or 0),
            "last_read_ts": float(grow[1] or 0.0)}
    if moved:
        cursor = {"device_id": device_id, "last_read_seq": new_seq,
                  "last_read_ts": new_ts, "message_id": message_id,
                  "updated_at": now}
        seq = _event_append(session, "read_cursor.update",
                            {"session": session, **cursor, "global": gcur})
    else:
        # 沒前進(重送/亂序)→ 冪等回現存游標,不追加事件
        cursor = {"device_id": device_id, "last_read_seq": prev_seq,
                  "last_read_ts": prev_ts,
                  "message_id": row[2] if row else None,
                  "updated_at": row[3] if row else None}
        seq = 0
    return {"ok": True, "moved": moved, "seq": seq, "cursor": cursor,
            "global": gcur}


@app.post("/app/v1/messages/{mid}/reaction")
async def app_set_reaction(mid: str, request: Request):
    """Set / clear one emoji reaction on a message (G2/#39). Works for both
    app-sent turns (canonical mid) and Telegram-side rows (tg-<ts> id) — the
    overlay table doesn't care where the message lives."""
    _check_auth(request)
    body = await request.json()
    session = (body.get("session") or "").strip()
    if session not in PERSONAS:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown session")
    reaction = (body.get("reaction") or "").strip()[:8]   # one emoji, not an essay
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=5)
        if reaction:
            con.execute("INSERT INTO reactions(msg_id, session, reaction, updated_at) "
                        "VALUES(?,?,?,?) ON CONFLICT(msg_id) DO UPDATE SET "
                        "reaction=excluded.reaction, updated_at=excluded.updated_at",
                        (mid, session, reaction, time.time()))
        else:
            con.execute("DELETE FROM reactions WHERE msg_id=?", (mid,))
        con.commit()
        con.close()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e)[:200])
    return {"ok": True, "reaction": reaction or None}


def _message_meta_load(con, message_id: str):
    """→ (reactions_list, pinned_int) for one message; ([], 0) when no row."""
    row = con.execute("SELECT reactions, pinned FROM message_meta WHERE message_id=?",
                      (message_id,)).fetchone()
    reactions: list = []
    if row and row[0]:
        try:
            parsed = json.loads(row[0])
            if isinstance(parsed, list):
                reactions = [str(r) for r in parsed if r]
        except Exception:  # noqa: BLE001
            reactions = []
    return reactions, (int(row[1] or 0) if row else 0)


def _message_session_of(con, message_id: str):
    """Session a message belongs to, from the canonical messages table.
    None for tg-<ts>/report ids — those live outside canonical (merged 流),
    the overlay row then keeps session NULL(讀取端用 join 補洞)。"""
    row = con.execute("SELECT session FROM messages WHERE id=?",
                      (message_id,)).fetchone()
    return row[0] if row else None


def _message_meta_upsert(con, message_id: str, reactions: list,
                         pinned: int, session=None):
    """One shared upsert for every message_meta writer (G2/#39). session 只在
    有值時覆蓋(COALESCE)— per-message 端點解析不出 tg id 的歸屬時,不把
    PUT /sessions/{id}/pin 已寫入的歸屬洗掉。"""
    con.execute(
        "INSERT INTO message_meta(message_id, reactions, pinned, session, updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET "
        "reactions=excluded.reactions, pinned=excluded.pinned, "
        "session=COALESCE(excluded.session, session), "
        "updated_at=excluded.updated_at",
        (message_id, json.dumps(reactions, ensure_ascii=False),
         pinned, session, time.time()))


@app.post("/app/v1/reactions")
async def app_reactions(request: Request):
    """Canonical reactions (G2/#39): add/remove one emoji on a message and
    return the message's full current emoji list. Works for canonical mids and
    tg-<ts> ids alike — the overlay doesn't care where the message lives."""
    _check_auth(request)
    body = await request.json()
    message_id = str(body.get("message_id") or "").strip()
    emoji = str(body.get("emoji") or "").strip()[:16]
    action = str(body.get("action") or "add").strip().lower()
    if not message_id or not emoji or action not in ("add", "remove"):
        raise HTTPException(status_code=400,
                            detail="message_id, emoji and action=add|remove required")
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
        reactions, pinned = _message_meta_load(con, message_id)
        if action == "add":
            if emoji not in reactions:
                reactions.append(emoji)
        else:
            reactions = [r for r in reactions if r != emoji]
        _message_meta_upsert(con, message_id, reactions, pinned,
                             session=_message_session_of(con, message_id))
        con.commit()
        con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("message_meta_write_failed", kind="reaction",
                   message_id=message_id, error=type(e).__name__)
        raise HTTPException(status_code=500, detail=str(e)[:200])
    return {"ok": True, "reactions": reactions}


@app.post("/app/v1/pins")
async def app_pins(request: Request):
    """Canonical per-message pin (G2/#39) — cross-device, survives reinstall."""
    _check_auth(request)
    body = await request.json()
    message_id = str(body.get("message_id") or "").strip()
    if not message_id:
        raise HTTPException(status_code=400, detail="message_id required")
    pinned = 1 if body.get("pinned") else 0
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
        reactions, _old = _message_meta_load(con, message_id)
        _message_meta_upsert(con, message_id, reactions, pinned,
                             session=_message_session_of(con, message_id))
        con.commit()
        con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("message_meta_write_failed", kind="pin",
                   message_id=message_id, error=type(e).__name__)
        raise HTTPException(status_code=500, detail=str(e)[:200])
    return {"ok": True}


@app.patch("/app/v1/messages/{mid}")
async def app_patch_message(mid: str, request: Request):
    """G2/#39 issue 合約收尾:單值 reaction 的 PATCH 形狀。body {"reaction":
    "👍" | null}(null/空字串=清除)。只認 canonical messages 表的 id —
    不存在回 404(合約要求存在性檢查;tg-<ts>/報告 id 不在 canonical,
    請走 id-agnostic 的 POST /app/v1/reactions,那條才蓋得到 TG 側訊息)。
    寫入同時落 legacy 單值 overlay 與 message_meta 清單(取代整串),
    GET /app/v1/messages 的 reaction/reactions 兩欄一起對齊。"""
    _check_auth(request)
    body = await _json_body(request)
    if "reaction" not in body:
        raise http_err(400, "BAD_REQUEST", "body must carry a 'reaction' key")
    raw = body.get("reaction")
    reaction = str(raw).strip()[:16] if raw is not None else ""
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
        session = _message_session_of(con, mid)
        if session is None:
            con.close()
            raise http_err(404, "MESSAGE_NOT_FOUND",
                           "no canonical message with this id",
                           "TG/cron-sourced ids: use POST /app/v1/reactions")
        _reactions_old, pinned = _message_meta_load(con, mid)
        if reaction:
            con.execute("INSERT INTO reactions(msg_id, session, reaction, updated_at) "
                        "VALUES(?,?,?,?) ON CONFLICT(msg_id) DO UPDATE SET "
                        "reaction=excluded.reaction, updated_at=excluded.updated_at",
                        (mid, session, reaction, time.time()))
            _message_meta_upsert(con, mid, [reaction], pinned, session=session)
        else:
            con.execute("DELETE FROM reactions WHERE msg_id=?", (mid,))
            _message_meta_upsert(con, mid, [], pinned, session=session)
        con.commit()
        con.close()
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        _log_event("message_meta_write_failed", kind="reaction_patch",
                   message_id=mid, error=type(e).__name__)
        raise HTTPException(status_code=500, detail=str(e)[:200])
    return {"ok": True, "id": mid, "reaction": reaction or None}


def _session_pinned_ids(con, session: str) -> list:
    """All pinned message ids belonging to a session, oldest-pin first.
    session 欄有值直接比;NULL(舊列/歸屬未知)用 canonical messages join
    補洞 — tg-<ts> 舊 pin 列兩邊都對不上時寧可漏,不跨 session 誤傷。"""
    rows = con.execute(
        "SELECT message_id FROM message_meta WHERE pinned=1 AND (session=? OR "
        "(session IS NULL AND message_id IN (SELECT id FROM messages WHERE session=?)))"
        " ORDER BY updated_at", (session, session)).fetchall()
    return [r[0] for r in rows]


@app.put("/app/v1/sessions/{sid}/pin")
async def app_put_session_pins(sid: str, request: Request):
    """G2/#39 issue 合約收尾:per-session 置頂全量替換。body
    {"pinned_message_ids": [...]}(空清單=全部解除)。id 收 GET
    /app/v1/messages 回的任何穩定 id(canonical mid / tg-<ts> / 報告 id)—
    寫入時直接掛 session 歸屬,tg id 從此也能按 session 讀回。解除只掃
    「歸屬得到本 session」的列,不動其他人格的置頂。"""
    _check_auth(request)
    if sid not in PERSONAS:
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    body = await _json_body(request)
    ids = body.get("pinned_message_ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) and i.strip() for i in ids):
        raise http_err(400, "BAD_REQUEST",
                       "pinned_message_ids must be a list of message ids")
    ids = [i.strip() for i in ids]
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
        for stale in _session_pinned_ids(con, sid):
            if stale in ids:
                continue
            reactions, _pin = _message_meta_load(con, stale)
            _message_meta_upsert(con, stale, reactions, 0, session=sid)
        for mid in ids:
            reactions, _pin = _message_meta_load(con, mid)
            _message_meta_upsert(con, mid, reactions, 1, session=sid)
        pinned_now = _session_pinned_ids(con, sid)
        con.commit()
        con.close()
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        _log_event("message_meta_write_failed", kind="session_pin",
                   session=sid, error=type(e).__name__)
        raise HTTPException(status_code=500, detail=str(e)[:200])
    return {"ok": True, "session": sid, "pinned_message_ids": pinned_now}


@app.get("/app/v1/sessions/{sid}/pin")
async def app_get_session_pins(sid: str, request: Request):
    """PUT 的讀回面(G2/#39):本 session 目前置頂的訊息 id 清單。
    (GET /app/v1/messages 的每則 pinned 旗標照舊,這條是 per-session 檢視。)"""
    _check_auth(request)
    if sid not in PERSONAS:
        raise http_err(404, "SESSION_NOT_FOUND", "unknown session")
    import sqlite3
    con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
    try:
        pinned = _session_pinned_ids(con, sid)
    finally:
        con.close()
    return {"session": sid, "pinned_message_ids": pinned}


@app.post("/app/v1/messages/retract")
async def app_message_retract(request: Request):
    """G4 tombstone: mark a message deleted. The row stays in
    GET /app/v1/messages with "deleted": true — every device renders the same
    tombstone instead of the messages silently diverging."""
    _check_auth(request)
    body = await request.json()
    message_id = str(body.get("message_id") or "").strip()
    if not message_id:
        raise HTTPException(status_code=400, detail="message_id required")
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB, timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
        reactions, pinned = _message_meta_load(con, message_id)
        con.execute("INSERT INTO message_meta(message_id, reactions, pinned, deleted, updated_at) "
                    "VALUES(?,?,?,1,?) ON CONFLICT(message_id) DO UPDATE SET "
                    "deleted=1, updated_at=excluded.updated_at",
                    (message_id, json.dumps(reactions, ensure_ascii=False),
                     pinned, time.time()))
        con.commit()
        con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("message_meta_write_failed", kind="retract",
                   message_id=message_id, error=type(e).__name__)
        raise HTTPException(status_code=500, detail=str(e)[:200])
    return {"ok": True}


# ───────────────────────── personas CRUD (G6, wave 2) ──────────────────────
_PERSONA_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def _persona_profile_of(home: str) -> str:
    """Human-facing profile name for a home path (main / profiles/<x> tail)."""
    if home == HOME_ROOT:
        return "main"
    prefix = f"{HOME_ROOT}/profiles/"
    return home[len(prefix):] if home.startswith(prefix) else os.path.basename(home or "")


def _persona_home_from_body(body: dict) -> str | None:
    """Resolve home from body {home} or {profile}; None when neither given."""
    home = str(body.get("home") or "").strip()
    profile = str(body.get("profile") or "").strip()
    if home:
        return os.path.realpath(os.path.expanduser(home))
    if profile:
        return HOME_ROOT if profile == "main" else f"{HOME_ROOT}/profiles/{profile}"
    return None


def _persona_public(pid: str, name: str, home: str, enabled: bool,
                    deleted: bool) -> dict:
    ent = _avatar_manifest().get(pid) or {}
    ap = _avatar_path(pid)
    return {"id": pid, "name": ent.get("name") or name,
            "profile": _persona_profile_of(home),
            "home": home, "enabled": enabled, "deleted": deleted,
            "builtin": pid in _PERSONAS_BUILTIN,
            # TG 同源身分:@username(manifest.tg,可後補)與頭像版本
            # (檔案 mtime;0=無圖,app 端以 rev 做快取失效)
            "username": ent.get("tg") or "",
            "avatar_rev": int(os.path.getmtime(ap)) if ap else 0}


def _persona_row_get(pid: str):
    import sqlite3
    con = sqlite3.connect(f"file:{CANON_DB}?mode=ro", uri=True, timeout=5)
    r = con.execute("SELECT id,name,home,enabled,deleted FROM personas WHERE id=?",
                    (pid,)).fetchone()
    con.close()
    return r


def _persona_row_upsert(pid: str, name: str, home: str, enabled: int, deleted: int):
    import sqlite3
    con = sqlite3.connect(CANON_DB, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("INSERT INTO personas(id,name,home,enabled,deleted,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "name=excluded.name, home=excluded.home, enabled=excluded.enabled, "
                "deleted=excluded.deleted, updated_at=excluded.updated_at",
                (pid, name, home, enabled, deleted, time.time(), time.time()))
    con.commit()
    con.close()


@app.get("/app/v1/personas")
async def personas_list(request: Request):
    """Full persona registry (builtins + custom), including disabled/deleted
    entries so the app can render a management list. What the conversation UI
    should offer is exactly the entries with enabled && !deleted (== the live
    PERSONAS routing table)."""
    _check_auth(request)
    rows = {r[0]: r for r in _personas_db_rows()}
    out = []
    for pid, (disp, home) in _PERSONAS_BUILTIN.items():
        r = rows.pop(pid, None)
        if r:
            out.append(_persona_public(pid, r[1] or disp, r[2] or home,
                                       bool(r[3]) and not r[4], bool(r[4])))
        else:
            out.append(_persona_public(pid, disp, home, True, False))
    for pid, r in rows.items():
        out.append(_persona_public(pid, r[1] or pid, r[2] or HOME_ROOT,
                                   bool(r[3]) and not r[4], bool(r[4])))
    return {"personas": out}


@app.get("/app/v1/personas/{pid}/avatar")
async def personas_avatar(pid: str, request: Request):
    """Persona 頭像 — TG 同源正典(HOME_ROOT/avatars)。無圖 404,app 退 glyph 圓盤。"""
    _check_auth(request)
    if not _PERSONA_ID_RE.match(pid):
        raise HTTPException(status_code=400, detail="bad persona id")
    p = _avatar_path(pid)
    if not p:
        raise HTTPException(status_code=404, detail="no avatar")
    return FileResponse(p)


@app.post("/app/v1/personas")
async def personas_create(request: Request):
    """Add a persona without touching bridge.py. The home (or profile) must
    already exist on disk — a Hermes profile is provisioned outside the bridge;
    this endpoint only registers it for routing."""
    _check_auth(request)
    body = await request.json()
    pid = str(body.get("id") or "").strip().lower()
    name = str(body.get("name") or "").strip()
    if not pid and name:
        pid = re.sub(r"[^a-z0-9_-]", "", name.lower())[:32]
    if not _PERSONA_ID_RE.match(pid or ""):
        raise HTTPException(status_code=400,
                            detail="id required: 1-32 chars of a-z 0-9 _ -")
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    home = _persona_home_from_body(body)
    if not home:
        raise HTTPException(status_code=400, detail="profile or home required")
    if not os.path.isdir(home):
        raise HTTPException(status_code=400,
                            detail=f"persona home not found: {home}")
    existing = _persona_row_get(pid)
    if pid in PERSONAS or (existing and not existing[4]):
        raise http_err(409, "PERSONA_EXISTS", "persona id already in use")
    _persona_row_upsert(pid, name, home, 1, 0)
    _personas_reload()
    _log_event("persona_created", id=pid, home=home)
    return _persona_public(pid, name, home, True, False)


@app.patch("/app/v1/personas/{pid}")
async def personas_patch(pid: str, request: Request):
    """Rename / re-home / enable / disable a persona (builtin or custom).
    enabled=true also un-deletes, so DELETE is reversible from the app."""
    _check_auth(request)
    body = await request.json()
    row = _persona_row_get(pid)
    builtin = _PERSONAS_BUILTIN.get(pid)
    if row is None and builtin is None:
        raise http_err(404, "PERSONA_NOT_FOUND", "unknown persona")
    cur_name = (row[1] if row else None) or (builtin[0] if builtin else pid)
    cur_home = (row[2] if row else None) or (builtin[1] if builtin else HOME_ROOT)
    cur_enabled = bool(row[3]) if row else True
    cur_deleted = bool(row[4]) if row else False
    name = str(body.get("name") or "").strip() or cur_name
    home = _persona_home_from_body(body) or cur_home
    if not os.path.isdir(home):
        raise HTTPException(status_code=400,
                            detail=f"persona home not found: {home}")
    if "enabled" in body:
        enabled = bool(body.get("enabled"))
        deleted = False if enabled else cur_deleted
    else:
        enabled, deleted = cur_enabled, cur_deleted
    _persona_row_upsert(pid, name, home, 1 if enabled else 0, 1 if deleted else 0)
    _personas_reload()
    _log_event("persona_patched", id=pid, enabled=enabled, deleted=deleted)
    return _persona_public(pid, name, home, enabled and not deleted, deleted)


@app.delete("/app/v1/personas/{pid}")
async def personas_delete(pid: str, request: Request):
    """Soft delete: the row is kept (deleted=1, enabled=0) and the persona
    drops out of routing; PATCH {"enabled": true} restores it."""
    _check_auth(request)
    row = _persona_row_get(pid)
    builtin = _PERSONAS_BUILTIN.get(pid)
    if row is None and builtin is None:
        raise http_err(404, "PERSONA_NOT_FOUND", "unknown persona")
    name = (row[1] if row else None) or (builtin[0] if builtin else pid)
    home = (row[2] if row else None) or (builtin[1] if builtin else HOME_ROOT)
    _persona_row_upsert(pid, name, home, 0, 1)
    _personas_reload()
    _log_event("persona_deleted", id=pid)
    return {"ok": True}


async def _persona_prepare_turn(session: str, content: str, attachments: list,
                                stt_lang: str = ""):
    """persona 回合前置(附件轉存/語音轉寫/多模態 parts/prompt 組裝)→
    (content, att_meta, prompt)。v1 POST /app/v1/messages 與 v2 統一路由
    input 共用。"""
    _att_guard(attachments)   # 修復單「附件限制」:直送口件數閥
    normalized_attachments = []
    for a in attachments:
        if not isinstance(a, dict):
            continue
        na = dict(a)
        path = _save_attachment(na, na.get("filename") or "file")
        if path:
            na["path"] = path
            na.pop("data", None)       # keep the persona turn body lightweight
            na.pop("data_uri", None)
        normalized_attachments.append(na)
    attachments = normalized_attachments

    # Voice messages: transcribe any audio attachment and fold the transcript
    # into the turn text. The audio still rides along as an attachment so the
    # conversation shows the voice bubble; the model gets the words.
    voice_text = await _transcribe_attachments(attachments, stt_lang)
    if voice_text:
        content = (content + "\n" + voice_text).strip() if content else voice_text

    parts = []
    if content:
        parts.append({"type": "text", "text": content})
    for a in attachments:
        if a.get("kind") == "image":
            path = _upload_ref_path(a.get("path")) or _save_attachment(a, a.get("filename") or "image.jpg")
            if path:
                parts.append({"type": "image_url", "image_url": {"url": path}})
        elif a.get("kind") == "audio":
            continue                       # transcript already in `content`
        else:
            path = _upload_ref_path(a.get("path")) or _save_attachment(a, a.get("filename") or "file")
            if not path:
                continue
            parts.append({"type": "file", "file": {"filename": a.get("filename"),
                          "mime_type": a.get("mime"), "file_data": path}})
    prompt = await _resolve_persona_prompt([{"role": "user", "content": parts or content}])
    report_context = _report_context_for_prompt(session, content)
    if report_context:
        prompt = f"{report_context}\n\n---\n【使用者現在的訊息】\n{prompt}"

    att_meta = [{"kind": a.get("kind"), "filename": a.get("filename"),
                 "mime": a.get("mime"), "path": _upload_ref_path(a.get("path"))}
                for a in attachments]
    return content, att_meta, prompt


def _persona_launch_turn(session: str, prompt: str, client_id, common_log: dict,
                         turn_started: float, canonical_user_ok, cid: str):
    """建 queue/state、把 persona 回合掛成獨立背景任務,回 (task, state, q)。

    v1 POST /app/v1/messages 串流消費 q;v2 統一路由 input 不消費 q(回覆走
    S3 卡片事件流)。回合獨立於 client 連線:斷網不斷回合,收尾一定落
    canonical。S3 digest 掛鉤都在這裡——delta/status/收尾,單一實作兩邊共用。
    """
    q: asyncio.Queue = asyncio.Queue()
    state = {"acc": "", "usage": None, "content_chunks": 0, "keepalives": 0,
             "first_content_ms": None, "first_status_ms": None, "status_updates": 0,
             "runner_error": "", "stream_error": "", "canonical_reply_ok": None,
             "done_sent": False}

    async def run_turn():
        # Drains the persona turn to completion INDEPENDENTLY of the client
        # connection. If the app's network drops mid-stream this task keeps
        # going and records the reply, so the canonical store always reflects
        # what actually happened server-side (the tool ran, the calendar was
        # created) — a reload then shows the real reply instead of losing it.
        digest = _hp_digest_maybe(session)
        try:
            async for k, v in _persona_content_stream(session, prompt):
                if k == "content":
                    state["acc"] += v
                    state["content_chunks"] += 1
                    if state["first_content_ms"] is None:
                        state["first_content_ms"] = int(
                            (time.monotonic() - turn_started) * 1000
                        )
                    state["step_label"] = ""     # 正文恢復 → 步驟 label 讓位
                elif k == "usage":
                    state["usage"] = v
                elif k == "status":
                    # 步驟進度(執行步驟 N:工具)— 讓輪詢的 /messages/status
                    # 也能給 working bar 同一句人話。
                    state["step_label"] = (v or {}).get("label") or ""
                    state["status_updates"] += 1
                    if state["first_status_ms"] is None:
                        state["first_status_ms"] = int(
                            (time.monotonic() - turn_started) * 1000
                        )
                if digest is not None:
                    try:
                        if k == "content":
                            digest.turn_delta(cid, v)
                        elif k == "status":
                            digest.turn_status((v or {}).get("label") or "")
                    except Exception as e:  # noqa: BLE001
                        _log_event("hp_card_turn_error", session=session,
                                   error=str(e)[:160])
                await q.put((k, v))
        except Exception as e:  # noqa: BLE001
            state["runner_error"] = f"{type(e).__name__}: {str(e)[:180]}"
            await q.put(("error", str(e)))
        finally:
            reply_mid = ""
            if state["acc"]:
                reply_mid, reply_ok = _canon_add_retry(session, "assistant", state["acc"],
                                                       client_id=client_id)
                state["canonical_reply_ok"] = reply_ok
            if digest is not None:
                try:
                    digest.turn_end(cid, state["acc"], reply_mid=reply_mid or "",
                                    error=state["runner_error"])
                except Exception as e:  # noqa: BLE001
                    _log_event("hp_card_turn_error", session=session,
                               error=str(e)[:160])
            _log_event("app_turn_background_done", **common_log,
                       output_chars=len(state["acc"]),
                       content_chunks=state["content_chunks"],
                       first_content_ms=state["first_content_ms"],
                       first_status_ms=state["first_status_ms"],
                       status_updates=state["status_updates"],
                       usage_used=(state["usage"] or {}).get("used"),
                       usage_size=(state["usage"] or {}).get("size"),
                       canonical_user_ok=canonical_user_ok,
                       canonical_reply_ok=state["canonical_reply_ok"],
                       runner_error=state["runner_error"] or None,
                       duration_ms=int((time.monotonic() - turn_started) * 1000))
            await q.put((None, None))

    _log_event("app_turn_model_start", **common_log,
               prompt_chars=len(prompt), canonical_user_ok=canonical_user_ok)
    # The turn runs as a handler-scope task (not inside the generator) so a
    # duplicate POST can attach to it via _APP_TURN_INFLIGHT before the client
    # even starts reading this response.
    task = asyncio.create_task(run_turn())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task, state, q


@app.post("/app/v1/messages")
async def app_post_message(request: Request):
    """Send a turn: record the user message canonically, run the persona turn,
    stream the reply (OpenAI-style SSE), and record the reply canonically too."""
    _check_auth(request)
    body = await request.json()
    session = body.get("session") or "xcash"
    if session not in PERSONAS:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown session")
    content = (body.get("content") or "").strip()
    attachments = body.get("attachments") or []   # [{kind,filename,mime,data(dataURI)|path}]
    dry_run = bool(body.get("dry_run"))

    client_id = body.get("client_id")    # stable across retries; enables idempotency
    cid = "appmsg-" + uuid.uuid4().hex[:20]
    created = int(time.time())
    turn_started = time.monotonic()
    common_log = {
        "cid": cid,
        "session": session,
        "client_id_hash": _short_hash(client_id),
        "client": _client_host(request),
        "dry_run": dry_run,
        "input_chars": len(content),
        **_attachment_stats(attachments),
    }
    _log_event("app_turn_received", **common_log)

    def chunk(delta, finish=None):
        payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                   "model": session, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def status_chunk(state: str, label: str):
        payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                   "model": session,
                   "status": {"state": state, "label": label},
                   "choices": [{"index": 0, "delta": {}, "finish_reason": None}]}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def stream_response(events):
        # Explicit anti-buffering headers protect token deltas when this route
        # sits behind nginx/CDN; uvicorn also flushes each yielded SSE frame.
        return StreamingResponse(
            events,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    # Retry idempotency: if this exact logical send already produced a recorded
    # reply (first attempt completed server-side but the app's network dropped
    # before it saw the reply), replay that reply — do NOT re-run the turn or
    # repeat its side effects (e.g. creating the calendar event twice).
    if not dry_run and client_id:
        prior = _canon_reply_for_client(session, client_id)
        if prior is not None:
            async def replay_agen():
                done_sent = False
                try:
                    yield chunk({"role": "assistant", "content": ""})
                    yield status_chunk("replayed", "已找到上一輪完成回覆，正在重播。")
                    yield chunk({"content": prior})
                    payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                               "model": session, "replayed": True,
                               "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    done_sent = True
                finally:
                    _log_event("app_turn_stream_done", **common_log,
                               replayed=True, output_chars=len(prior),
                               done_sent=done_sent,
                               duration_ms=int((time.monotonic() - turn_started) * 1000),
                               canonical_user_ok=None, canonical_reply_ok=True)
            return stream_response(replay_agen())

    # In-flight idempotency (issue #9): the canonical replay above only covers
    # turns that already FINISHED. A duplicate POST while the first run is still
    # going must not start a second run — it attaches to the in-flight one.
    inflight_key = (session, client_id) if client_id else None
    inflight_entry = None
    attached = None
    if not dry_run and inflight_key:
        async with _APP_TURN_INFLIGHT_LOCK:
            _now = time.monotonic()
            for k in [k for k, e in _APP_TURN_INFLIGHT.items()
                      if _now - e["ts"] > _APP_TURN_INFLIGHT_TTL]:
                _APP_TURN_INFLIGHT.pop(k, None)   # TTL cleanup on each access
            attached = _APP_TURN_INFLIGHT.get(inflight_key)
            if attached is None:
                inflight_entry = {"ts": _now, "task": None, "state": None}
                _APP_TURN_INFLIGHT[inflight_key] = inflight_entry

    if attached is not None:
        _log_event("app_turn_attach", **common_log)

        async def attach_agen():
            done_sent = False
            acc = ""
            sent_chars = 0
            last_label = None
            last_emit = time.monotonic()
            try:
                yield chunk({"role": "assistant", "content": ""})
                yield status_chunk("attached", "同一則訊息已在處理中，附掛原回合等待結果。")
                t0 = time.monotonic()
                while True:
                    _task = attached.get("task")
                    st = attached.get("state") or {}
                    current = st.get("acc") or ""
                    if len(current) > sent_chars:
                        yield chunk({"content": current[sent_chars:]})
                        sent_chars = len(current)
                        last_emit = time.monotonic()
                    label = st.get("step_label") or ""
                    if label and label != last_label:
                        yield status_chunk("running", label)
                        last_label = label
                        last_emit = time.monotonic()
                    if _task is not None and _task.done():
                        break
                    if _task is None and time.monotonic() - t0 > 30:
                        break   # original request died before starting its turn
                    if time.monotonic() - t0 > _APP_TURN_INFLIGHT_TTL:
                        break
                    if time.monotonic() - last_emit >= SSE_KEEPALIVE_SECS:
                        yield ": keepalive\n\n"
                        last_emit = time.monotonic()
                    await asyncio.sleep(0.1)
                st = attached.get("state") or {}
                acc = st.get("acc") or ""
                if len(acc) > sent_chars:
                    yield chunk({"content": acc[sent_chars:]})
                    sent_chars = len(acc)
                elif not acc:
                    yield chunk({"content": "(原回合沒有產出回覆)"})
                payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                           "model": session, "replayed": True,
                           "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                done_sent = True
            finally:
                _log_event("app_turn_stream_done", **common_log,
                           replayed=True, attached=True, output_chars=len(acc),
                           done_sent=done_sent,
                           duration_ms=int((time.monotonic() - turn_started) * 1000),
                           canonical_user_ok=None, canonical_reply_ok=None)
        return stream_response(attach_agen())

    if dry_run:
        async def dry_agen():
            done_sent = False
            text = f"✅ dry-run ok: {session} message path is reachable; nothing was persisted."
            try:
                yield chunk({"role": "assistant", "content": ""})
                yield status_chunk("accepted", "dry-run 已送達 bridge。")
                yield chunk({"content": text})
                payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                           "model": session,
                           "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                done_sent = True
            finally:
                _log_event("app_turn_stream_done", **common_log,
                           replayed=False, output_chars=len(text),
                           done_sent=done_sent,
                           duration_ms=int((time.monotonic() - turn_started) * 1000),
                           canonical_user_ok=None, canonical_reply_ok=None)
        return stream_response(dry_agen())

    content, att_meta, prompt = await _persona_prepare_turn(
        session, content, attachments, stt_lang=str(body.get("stt_lang") or ""))
    acp_session = await POOL.get(session, home_for(session))
    queued_at_accept = acp_session.is_busy()

    # Record the transcript as the canonical text (so other devices see what was
    # said even without the audio bytes), tagged so the app can show 🎤.
    _user_mid, canonical_user_ok = _canon_add_retry(session, "user", content, att_meta,
                                                    client_id=client_id)
    _hp_cards_turn_start(session, cid, _user_mid, content, att_meta)

    task, state, q = _persona_launch_turn(session, prompt, client_id, common_log,
                                          turn_started, canonical_user_ok, cid)
    if inflight_entry is not None:
        inflight_entry["task"] = task
        inflight_entry["state"] = state

    async def agen():
        try:
            yield chunk({"role": "assistant", "content": ""})
            if queued_at_accept:
                yield status_chunk("queued", "已收到 · 上一輪還在跑，這則會排隊處理。")
            else:
                yield status_chunk("accepted", "已送達 Hermes，等待回覆。")
            while True:
                k, v = await q.get()
                if k is None:
                    break
                if k == "content":
                    yield chunk({"content": v})
                elif k == "keepalive":
                    state["keepalives"] += 1
                    yield ": keepalive\n\n"
                elif k == "status":
                    if isinstance(v, dict):
                        yield status_chunk(v.get("state") or "running",
                                           v.get("label") or "Hermes 開始處理")
                elif k == "error":
                    state["stream_error"] = str(v)[:180]
            final = {"index": 0, "delta": {}, "finish_reason": "stop"}
            payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                       "model": session, "choices": [final]}
            if state["usage"] and state["usage"].get("size"):
                payload["usage"] = {"context_used": state["usage"].get("used"),
                                    "context_size": state["usage"].get("size")}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
            state["done_sent"] = True
        finally:
            _log_event("app_turn_stream_done", **common_log,
                       replayed=False,
                       output_chars=len(state["acc"]),
                       content_chunks=state["content_chunks"],
                       first_content_ms=state["first_content_ms"],
                       first_status_ms=state["first_status_ms"],
                       status_updates=state["status_updates"],
                       keepalives=state["keepalives"],
                       done_sent=state["done_sent"],
                       canonical_user_ok=canonical_user_ok,
                       canonical_reply_ok=state["canonical_reply_ok"],
                       stream_error=state["stream_error"] or None,
                       duration_ms=int((time.monotonic() - turn_started) * 1000))

    return stream_response(agen())


@app.post("/app/v1/messages/interrupt")
async def app_message_interrupt(request: Request):
    _check_auth(request)
    body = await request.json()
    session = body.get("session") or "xcash"
    if session not in PERSONAS:
        raise http_err(400, "SESSION_NOT_FOUND", "unknown session")
    return await _persona_interrupt_core(session)


async def _persona_interrupt_core(session: str) -> dict:
    """persona 中斷核心 — v1 與 v2 統一路由共用。
    Same verify-and-retry contract as /ccsessions/{name}/interrupt: don't
    report ok on a cancel that didn't land — check busy and retry up to 3×."""
    acp_session = await POOL.get(session, home_for(session))
    if not acp_session.is_busy():
        raise HTTPException(status_code=409, detail="no active turn")
    attempts = 0
    interrupted = False
    for _ in range(3):
        attempts += 1
        await acp_session.cancel()
        await asyncio.sleep(0.7)
        if not acp_session.is_busy():
            interrupted = True
            break
    _log_event("persona_interrupt", session=session,
               interrupted=interrupted, attempts=attempts)
    return {"ok": True, "session": session,
            "interrupted": interrupted, "attempts": attempts}


# ───────────────────────── Approval Center (M21) ───────────────────────────
# Hermes skills (post / email / story / backup cleanup / risky tasks) POST an
# approval here; the app shows a native approve/reject card with TTL + risk; the
# skill polls the decision. Bridge owns the store (no Hermes internals exposed).

# A1:讀取端共用的欄位序 — SELECT 一律用這一串,tuple 索引不漂移。
_APPROVAL_COLS = ("id,title,source,risk,detail,created_at,expires_at,status,"
                  "decided_at,result,session_id,provider,kind,options")
_APPROVAL_KINDS = ("permission", "question", "notice")


def _approval_default_options(kind: str) -> list:
    """options 未宣告時的預設鍵(APPROVAL_HUB_SPEC §1/§2)。"""
    if kind == "notice":
        return [{"key": "ack", "label": "知道了", "style": "primary"}]
    return [{"key": "approve", "label": "允許", "style": "primary"},
            {"key": "deny", "label": "拒絕", "style": "danger"}]


def _approval_provider_of(source: str) -> str:
    if source.startswith("claude_code:"):
        return "claude_code"
    if source.startswith("codex"):
        return "codex"
    return "hermes"


def _approval_row(r):
    """DB tuple(_APPROVAL_COLS 序)→ 統一 approval 物件(spec §1)。
    舊欄位全保留(相容期);新欄位缺值時由 source 推導 — 遷移前的舊列與
    新列走同一條序列化,wire 形狀只有這一份。"""
    src = str(r[2] or "")
    kind = r[12] if r[12] in _APPROVAL_KINDS else "permission"
    options = None
    if r[13]:
        try:
            options = json.loads(r[13])
        except (TypeError, ValueError):
            options = None
    return {"id": r[0], "title": r[1], "source": r[2], "risk": r[3], "detail": r[4],
            "created_at": r[5], "expires_at": r[6], "status": r[7],
            "decided_at": r[8], "result": r[9],
            "session_id": r[10] or (src if src.startswith(("claude_code:", "codex:")) else ""),
            "provider": r[11] or _approval_provider_of(src),
            "kind": kind,
            "options": options or _approval_default_options(kind)}


def _approval_get_row(aid: str):
    """單筆統一物件(v2 meta.approval / 決定路由用);不存在回 None。"""
    import sqlite3
    con = sqlite3.connect(CANON_DB)
    r = con.execute(f"SELECT {_APPROVAL_COLS} FROM approvals WHERE id=?",
                    (aid,)).fetchone()
    con.close()
    return _approval_row(r) if r else None


def _hermes_pending_by_session() -> dict:
    """hermes persona 的 pending 待審(session_id='hermes:{mid}')→ 統一物件,
    每 persona 取最早一筆。v2 sessions 補 waiting_approval 用(spec §7-5)。"""
    import sqlite3
    out = {}
    try:
        con = sqlite3.connect(CANON_DB)
        _approvals_expire(con)
        con.commit()
        rows = con.execute(
            f"SELECT {_APPROVAL_COLS} FROM approvals WHERE status='pending'"
            " AND session_id LIKE 'hermes:%' ORDER BY created_at ASC").fetchall()
        con.close()
        for r in rows:
            d = _approval_row(r)
            out.setdefault(d["session_id"], d)
    except Exception as e:  # noqa: BLE001
        _log_event("hermes_pending_scan_failed", error=str(e)[:160])
    return out


def _approvals_expire(con):
    now = time.time()
    # A3:過期不只翻 DB 狀態,存在中的卡片流也要同卡收尾(不然 pending 卡
    # 掛著可點,點了才吃 409)。先撈再改;卡片收尾是記憶體操作、冪等。
    try:
        stale = con.execute(
            "SELECT id, title, session_id FROM approvals WHERE status='pending' "
            "AND expires_at IS NOT NULL AND expires_at < ?", (now,)).fetchall()
    except Exception:  # noqa: BLE001
        stale = []
    con.execute("UPDATE approvals SET status='expired' WHERE status='pending' "
                "AND expires_at IS NOT NULL AND expires_at < ?", (now,))
    for aid, title, sid in stale:
        rec = {"id": aid, "title": title}
        sid = str(sid or "")
        try:
            if sid.startswith("hermes:"):
                _hp_cards_feed_approval(sid, rec, resolved="expired")
            elif sid.startswith("claude_code:"):
                _cc_cards_feed_approval(sid.split(":", 1)[1], rec,
                                        resolved="expired")
        except Exception as e:  # noqa: BLE001
            _log_event("approval_expire_feed_error", id=aid, error=str(e)[:160])


# B4 (issue #9): an approval that never expires pends forever if the phone
# misses the push — default to 1h, clamp to [30s, 7d] so a typo'd ttl can't
# create an immortal (or instantly-dead) row.
_APPROVAL_TTL_DEFAULT = 3600.0
_APPROVAL_TTL_MIN, _APPROVAL_TTL_MAX = 30.0, 7 * 86400.0


async def _approval_fire_callback(aid: str, callback: str, status: str, result,
                                  key: str = ""):
    """POST the decision to the creator's callback URL (fire-and-forget).
    A3:callback=="persona-relay:" 時不走 HTTP —— 把選中選項的 send 文字
    (缺席退 label)注入該 persona 對話,由人格接手執行(FED 審稿等
    「決定即指令」流)。"""
    if callback == "persona-relay:":
        try:
            await _approval_persona_relay(aid, status, key or str(result or ""))
        except Exception as e:  # noqa: BLE001
            _log_event("approval_relay_failed", id=aid, status=status,
                       error=type(e).__name__, error_message=str(e)[:160])
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(callback, json={"id": aid, "status": status,
                                                  "result": result, "key": key})
        _log_event("approval_callback_sent", id=aid, status=status,
                   http_status=r.status_code)
    except Exception as e:  # noqa: BLE001
        _log_event("approval_callback_failed", id=aid, status=status,
                   error=type(e).__name__, error_message=str(e)[:160])


async def _approval_persona_relay(aid: str, status: str, key: str):
    """決定 → persona 指令注入。expired/逾時不注入(沒人做決定就不該有動作)。"""
    if status == "expired" or not key:
        return
    d = _approval_get_row(aid)
    sid = str((d or {}).get("session_id") or "")
    if not sid.startswith("hermes:"):
        return
    session = sid.split(":", 1)[1]
    if session not in PERSONAS:
        _log_event("approval_relay_skipped", id=aid, reason="unknown persona",
                   session=session)
        return
    opt = next((o for o in (d.get("options") or [])
                if str(o.get("key") or "") == key), None)
    text = str((opt or {}).get("send") or (opt or {}).get("label") or "").strip()
    if not text:
        return
    _log_event("approval_relay_inject", id=aid, session=session, key=key,
               chars=len(text))
    await _persona_inject_turn(
        session,
        f"【審核決定 · {d.get('title') or aid}】{text}",
        via="approval_relay")


async def _persona_inject_turn(session: str, content: str, via: str):
    """內部發起的 persona 回合(approval persona-relay 等):與 v1/v2 input
    同一套前置/canonical/卡片掛鉤,fire-and-forget —— 回覆走 S3 卡片事件流
    與 canonical,不佔任何 client 連線。"""
    cid = "appmsg-" + uuid.uuid4().hex[:20]
    turn_started = time.monotonic()
    common_log = {"cid": cid, "session": session, "client_id_hash": None,
                  "client": "internal", "dry_run": False,
                  "input_chars": len(content), "via": via}
    _log_event("app_turn_received", **common_log)
    content, att_meta, prompt = await _persona_prepare_turn(session, content, [])
    user_mid, canonical_user_ok = _canon_add_retry(session, "user", content,
                                                   att_meta)
    _hp_cards_turn_start(session, cid, user_mid, content, att_meta)
    _persona_launch_turn(session, prompt, None, common_log, turn_started,
                         canonical_user_ok, cid)


@app.post("/app/v1/approvals")
async def approval_create(request: Request):
    """Create a pending approval (called by Hermes / a skill).
    A1(spec §3.4):`source` 升級為 `session_id`(舊名相容照收);新增
    `kind`(permission|question|notice,預設 permission)與 `options`
    (建立方宣告的鍵,bridge 驗形狀、收斂 style 字彙;缺席由讀取端給預設)。"""
    _check_auth(request)
    import sqlite3
    b = await request.json()
    aid = b.get("id") or uuid.uuid4().hex
    try:
        ttl = float(b.get("ttl_seconds") or _APPROVAL_TTL_DEFAULT)
    except (TypeError, ValueError):
        ttl = _APPROVAL_TTL_DEFAULT
    ttl = max(_APPROVAL_TTL_MIN, min(ttl, _APPROVAL_TTL_MAX))
    callback = (str(b.get("callback_url") or "").strip() or None)
    # A3:persona-relay: 為內部 callback 傳輸 —— 決定後把選中選項的 send
    # 文字注入該 persona 對話(FED 審稿等「決定即指令」流),不走 HTTP。
    if callback and callback != "persona-relay:" \
            and not callback.startswith(("http://", "https://")):
        raise HTTPException(status_code=400,
                            detail="callback_url must be http(s) or persona-relay:")
    session_id = str(b.get("session_id") or b.get("source") or "").strip()
    if callback == "persona-relay:" and not (
            session_id.startswith("hermes:")
            and session_id.split(":", 1)[1] in PERSONAS):
        raise http_err(400, "INVALID_CALLBACK",
                       "persona-relay: 需要 session_id=hermes:{persona}(已註冊人格)")
    kind = str(b.get("kind") or "permission").strip()
    if kind not in _APPROVAL_KINDS:
        raise http_err(400, "INVALID_KIND", f"kind 必須是 {'|'.join(_APPROVAL_KINDS)}")
    options = b.get("options")
    if options is not None:
        if (not isinstance(options, list) or not options
                or not all(isinstance(o, dict) and str(o.get("key") or "").strip()
                           and str(o.get("label") or "").strip() for o in options)):
            raise http_err(400, "INVALID_OPTIONS",
                           "options 需為 [{key,label[,style]}…] 且 key/label 非空")
        norm = []
        for o in options[:6]:
            ent = {"key": str(o["key"]).strip()[:40], "label": str(o["label"]).strip()[:80]}
            style = str(o.get("style") or "").strip()
            if style == "deny":                     # 舊字彙收斂(spec §1 用 danger)
                style = "danger"
            if style in ("primary", "secondary", "danger"):
                ent["style"] = style
            # A3:send = 建立方宣告「這鍵決定後要對 persona 說的話」
            # (persona-relay 消費;app 不認得就忽略,fallback 原則)。
            send = str(o.get("send") or "").strip()
            if send:
                ent["send"] = send[:200]
            norm.append(ent)
        options = norm
    now = time.time()
    con = sqlite3.connect(CANON_DB)
    con.execute("INSERT OR REPLACE INTO approvals"
                "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result,callback,"
                "session_id,provider,kind,options) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (aid, b.get("title") or "需要核准", session_id, b.get("risk") or "",
                 b.get("detail") or "", now, now + ttl, "pending", None, None, callback,
                 session_id or None, _approval_provider_of(session_id), kind,
                 json.dumps(options, ensure_ascii=False) if options else None))
    con.commit()
    con.close()
    title = b.get("title") or "需要核准"
    try:
        # A3:hermes create 流程補齊卡片流 — pending → approval 卡(與
        # cc/codex 同一組 wire shape,見 carddigest.ApprovalCardMixin)。
        _hp_cards_feed_approval(session_id, _approval_get_row(aid) or {})
    except Exception as e:  # noqa: BLE001
        _log_event("hp_cards_feed_error", error=str(e)[:160])
    if b.get("push") is False:
        # A3:建立方已用自己的通道通知過(例:cron 報告本體已推)→ 不疊
        # 推播;待審列/卡片照常存在。
        return {"id": aid, "status": "pending", "expires_at": now + ttl,
                "kind": kind, "session_id": session_id}
    body = (b.get("detail") or session_id or "點開查看並決定")[:120]
    _approval_push(aid, title, body, session_id)
    return {"id": aid, "status": "pending", "expires_at": now + ttl,
            "kind": kind, "session_id": session_id}


@app.post("/app/v1/devices")
async def register_device(request: Request):
    """App registers its APNs device token here on launch / token refresh."""
    _check_auth(request)
    b = await request.json()
    token = (b.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="missing token")
    _device_add(token, b.get("platform") or "ios")
    return {"ok": True, "devices": len(_devices())}


@app.get("/app/v1/devices")
async def list_devices(request: Request):
    _check_auth(request)
    return {"count": len(_devices())}


@app.post("/app/v1/push/test")
async def push_test(request: Request):
    """Send a test push to every registered device — verifies APNs auth end-to-end."""
    _check_auth(request)
    b = await request.json() if await request.body() else {}
    res = await push_notify(b.get("title") or "Pocket Agent",
                            b.get("body") or "測試推播 ✅ M23 已接上",
                            {"kind": "test"})
    # 回傳真實 APNs 結果(topic、每台裝置的 code/detail)—— 以前一律回 200 讓人盲測。
    return {"sent": res["sent"], "devices": res["total"],
            "apns_topic": APNS_BUNDLE_ID, "failures": res["failures"]}


@app.get("/app/v1/approvals")
async def approval_list(request: Request, status: str = "", limit: int = 50,
                        offset: int = 0):
    """List approvals. B4 (issue #9): paginated — limit is clamped, `offset`
    pages back, `total` lets the app render 'N more'."""
    _check_auth(request)
    import sqlite3
    lim = max(1, min(int(limit or 50), 200))
    off = max(0, int(offset or 0))
    con = sqlite3.connect(CANON_DB)
    _approvals_expire(con)
    con.commit()
    if status:
        total = con.execute("SELECT COUNT(*) FROM approvals WHERE status=?",
                            (status,)).fetchone()[0]
        rows = con.execute(f"SELECT {_APPROVAL_COLS} "
                           "FROM approvals WHERE status=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                           (status, lim, off)).fetchall()
    else:
        total = con.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
        rows = con.execute(f"SELECT {_APPROVAL_COLS} "
                           "FROM approvals ORDER BY created_at DESC LIMIT ? OFFSET ?",
                           (lim, off)).fetchall()
    con.close()
    out = [_approval_row(r) for r in rows]
    return {"approvals": out, "total": total,
            "next_offset": (off + lim) if off + lim < total else None}


@app.get("/app/v1/approvals/{aid}")
async def approval_get(aid: str, request: Request):
    """Poll a decision (called by the requesting skill)."""
    _check_auth(request)
    import sqlite3
    con = sqlite3.connect(CANON_DB)
    _approvals_expire(con)
    con.commit()
    r = con.execute(f"SELECT {_APPROVAL_COLS} "
                    "FROM approvals WHERE id=?", (aid,)).fetchone()
    con.close()
    if not r:
        raise http_err(404, "APPROVAL_NOT_FOUND", "unknown approval")
    return _approval_row(r)


@app.post("/app/v1/approvals/{aid}/decision")
async def approval_decide(aid: str, request: Request):
    """Approve / reject (from the app)."""
    _check_auth(request)
    b = await request.json()
    return await _approval_decide_core(aid, b)


async def _approval_decide_core(aid: str, b: dict) -> dict:
    """Approval Center 決議核心 — v1 與 v2 統一路由 approve 共用。
    A1(spec §3.2):`{key}` 為決定的第一公民;`{approve: bool}` 保留為相容糖
    (approve→第一個 primary、deny→第一個 danger)。status 依 kind 落:
    permission→approved|denied、question→answered(result=key)、
    notice→acknowledged。新決議寫 `denied`(拍板);歷史列的 `rejected`
    讀取端一律視為等價,A4 收斂。"""
    import sqlite3
    key = str(b.get("key") or "").strip()
    d = _approval_get_row(aid)
    src = str((d or {}).get("source") or "")
    if d and src.startswith("claude_code:"):
        # 批次 3 斷點③:CC 審核決議 → 回流 TUI 鍵。以「當下 pane 的 prompt」
        # 為準(推播到點按之間 prompt 可能已被回掉——過時就 409,不盲送鍵)。
        name = src.split(":", 1)[1]
        active = _CC_APPROVAL_ACTIVE.get(name)
        st = await _cc_status_core(name)
        prompt = st.get("prompt")
        if not prompt or not active or active.get("aid") != aid:
            _cc_approval_set_status(aid, "expired")
            raise HTTPException(status_code=409, detail="already decided or expired")
        key = key or _cc_choice_key(prompt, bool(b.get("approve")))
        # 決議語意:帶 approve bool 用 bool;只給 {key} 時由該鍵的 style 判斷
        # (danger/esc=否決)— 之前 {key} 決定一律被記成 rejected 是誤標。
        if "approve" in b:
            decision = "approved" if b.get("approve") else "denied"
        else:
            styles = {str(o.get("key") or ""): o.get("style")
                      for o in (d.get("options") or [])}
            decision = "denied" if (key == "esc" or styles.get(key) == "danger") \
                else "approved"
        await _cc_key_core(name, key)
        _cc_approval_set_status(aid, decision)
        _CC_APPROVAL_ACTIVE.pop(name, None)
        try:
            # A3:決定發生時也要收尾卡片流(同一決定路徑,三 provider 一致)。
            _cc_cards_feed_approval(name, d, resolved=decision)
        except Exception as e:  # noqa: BLE001
            _log_event("cc_cards_feed_error", error=str(e)[:160])
        _log_event("cc_approval_decision", session=name, approval_id=aid,
                   status=decision, key=key)
        return {"id": aid, "status": decision, "key": key}
    if d and src.startswith("codex"):
        # {key} → app-server 決議參數;approve_for_session 映射 Codex 原生
        # acceptForSession(_approval_response_result 既有機制)。codex 線的
        # 狀態字彙(approved/rejected)相容期不動 — 卡片流/記憶體 record 同源。
        if key:
            approved = key != "deny"
            for_session = key == "approve_for_session"
        else:
            approved = bool(b.get("approve"))
            for_session = bool(b.get("for_session") or b.get("approve_for_session") or
                               b.get("remember"))
        try:
            result = await CODEX_APP.decide_approval(aid, approved,
                                                     for_session=for_session)
            return {"id": aid, "status": result["status"], "result": result["result"]}
        except CodexAppServerError as e:
            if e.code == 404:
                raise http_err(409, "APPROVAL_NOT_PENDING",
                               "Codex approval is no longer live")
            _codex_http_error(e)
    # hermes / 本地列(permission|question|notice):key 或相容糖決議
    kind = (d or {}).get("kind") or "permission"
    options = (d or {}).get("options") or _approval_default_options(kind)
    okeys = [str(o.get("key") or "") for o in options]
    if d and key and key not in okeys:
        raise http_err(400, "UNKNOWN_KEY", f"key 必須是 {okeys} 之一")
    if not key:
        want = "primary" if b.get("approve") else "danger"
        key = next((str(o.get("key")) for o in options if o.get("style") == want),
                   "approve" if b.get("approve") else "deny")
    if kind == "notice":
        status = "acknowledged"
    elif kind == "question":
        status = "answered"
    else:
        styles = {str(o.get("key") or ""): o.get("style") for o in options}
        status = "denied" if styles.get(key) == "danger" else "approved"
    # result:question/notice 的答案就是 key(spec §2);permission 維持舊預設
    # (建立方自帶 result 優先,否則空字串)以免驚動既有 callback 消費者。
    result_val = str(b.get("result") or "") or (key if kind != "permission" else "")
    con = sqlite3.connect(CANON_DB)
    cur = con.execute("UPDATE approvals SET status=?, decided_at=?, result=? "
                      "WHERE id=? AND status='pending'",
                      (status, time.time(), result_val, aid))
    con.commit()
    changed = cur.rowcount
    cb_row = con.execute("SELECT callback FROM approvals WHERE id=?", (aid,)).fetchone()
    con.close()
    if not changed:
        raise HTTPException(status_code=409, detail="already decided or expired")
    try:
        # A3:hermes 決定發生時收尾卡片流(同一決定路徑,三 provider 一致)。
        # _hp_cards_feed_approval 內部會篩 session_id 前綴,非 hermes: 的列
        # (例如舊資料 session_id 空缺)在這裡是安全 no-op。
        _hp_cards_feed_approval(str((d or {}).get("session_id") or ""),
                               d or {}, resolved=status)
    except Exception as e:  # noqa: BLE001
        _log_event("hp_cards_feed_error", error=str(e)[:160])
    # B4 (issue #9): push the decision back to the creator (Hermes skill / TG
    # flow) so it doesn't have to poll GET /app/v1/approvals/{id}.
    if cb_row and cb_row[0]:
        asyncio.create_task(_approval_fire_callback(
            aid, cb_row[0], status, result_val, key=key))
    # 2b:人格 choices 審核決議 → 把選項的 send 文字當人格回合送回(如 FLiPER
    # 「解除待檢討」→ 送 "resume 386563" 給潘天晴,與聊天視窗點按鈕等效)。
    if src.startswith("hermes:") and status == "answered":
        persona = src.split(":", 1)[1]
        chosen = next((o for o in options if str(o.get("key")) == key), None)
        send_text = (chosen or {}).get("send")
        if persona in PERSONAS and send_text:
            asyncio.create_task(
                _persona_inject_turn(persona, str(send_text), "approval-choice"))
            _log_event("hp_choices_decision_relayed", session=persona,
                       approval_id=aid, key=key)
    return {"id": aid, "status": status, "key": key}


@app.post("/dispatch")
async def dispatch(request: Request):
    """Hermes (or a tool) asks the bridge to spawn a CC/Codex sub-agent.
    Returns a session id that shows up in GET /sessions and streams like a chat."""
    _check_auth(request)
    body = await request.json()
    tool = body.get("tool", "claude-code")
    task = (body.get("task") or "").strip()
    cwd = os.path.expanduser(body.get("cwd") or HOME_ROOT)
    parent = body.get("parent", "yuanfang")
    isolate = bool(body.get("isolate"))
    if not task:
        raise http_err(400, "TASK_REQUIRED", "task required")
    sid = "sub-" + uuid.uuid4().hex[:16]
    SUBSESSIONS[sid] = {"name": task[:40], "parent": parent, "tool": tool,
                        "status": "running", "lastAt": time.time(), "cwd": cwd,
                        "proc": None, "output": [("text", f"**任務:** {task}\n\n")]}
    _subsession_persist(sid)   # issue #5: registered rows survive a restart
    asyncio.create_task(_run_dispatch(sid, tool, task, cwd, isolate))
    return {"session_id": sid, "type": "subprocess", "tool": tool, "parent": parent}


async def _make_worktree(base: str, sid: str):
    """Isolate a worker in its own git worktree (like a branch) so parallel
    dispatches don't clobber each other's edits. Returns the worktree path, or
    the original base if it isn't a git repo / the command fails."""
    try:
        # _git_out gives both calls a kill-on-timeout guard (issue #7): a git
        # hung on a dead network mount used to hang the dispatch handler.
        rc, out = await _git_out("-C", base, "rev-parse", "--show-toplevel")
        if rc != 0:
            return base
        top = out.strip() or base
        wt = os.path.expanduser(f"~/.pocket/worktrees/{sid}")
        os.makedirs(os.path.dirname(wt), exist_ok=True)
        rc, _ = await _git_out("-C", top, "worktree", "add",
                               "-b", f"pocket/{sid}", wt, "HEAD", timeout=60)
        return wt if rc == 0 and os.path.isdir(wt) else base
    except Exception as e:  # noqa: BLE001
        _log_event("make_worktree_failed", sid=sid, base=base,
                   error=type(e).__name__, error_message=str(e)[:160])
        return base


async def _git_out(*args, timeout: float = 15.0):
    """Run git, return (rc, stdout_str). Kill-on-timeout like _tmux_run."""
    p = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            p.kill()
        except ProcessLookupError:
            pass
        return 124, ""
    return p.returncode, (out or b"").decode("utf-8", "replace")


async def _cleanup_worktree(sid: str, sub: dict):
    """After an isolated sub finishes: remove its worktree IF it's clean
    (`git status --porcelain` empty). Dirty trees are kept — someone's
    uncommitted work lives there — and logged. ~/.pocket/worktrees no longer
    grows without bound (issue #7)."""
    wt = sub.get("worktree")
    if not wt or not os.path.isdir(wt):
        return
    try:
        rc, dirty = await _git_out("-C", wt, "status", "--porcelain")
        if rc != 0 or dirty.strip():
            _log_event("worktree_kept", sid=sid, worktree=wt,
                       reason="status-failed" if rc != 0 else "dirty")
            return
        # `worktree remove` must run from the MAIN repo (git refuses to remove
        # the tree it's currently -C'd into), so resolve the common dir first.
        rc, common = await _git_out("-C", wt, "rev-parse", "--git-common-dir")
        common = common.strip()
        if rc != 0 or not common:
            _log_event("worktree_kept", sid=sid, worktree=wt, reason="no-common-dir")
            return
        if not os.path.isabs(common):
            common = os.path.abspath(os.path.join(wt, common))
        main_root = os.path.dirname(common)
        rc, _ = await _git_out("-C", main_root, "worktree", "remove", wt, timeout=30)
        if rc == 0:
            sub["worktree"] = None
            if sub.get("base_cwd"):
                sub["cwd"] = sub["base_cwd"]   # follow-ups run in the main tree
            _log_event("worktree_removed", sid=sid, worktree=wt)
        else:
            _log_event("worktree_remove_failed", sid=sid, worktree=wt, rc=rc)
    except Exception as e:  # noqa: BLE001
        _log_event("worktree_cleanup_error", sid=sid, worktree=wt,
                   error=type(e).__name__, error_message=str(e)[:160])


def _fmt_item(kind, val):
    """Format one transcript item (text/tool/result/perm) → SSE content string."""
    if kind == "text":
        return val
    if kind == "tool_start":
        name = val.get("name", "tool")
        cmd = (val.get("cmd") or "").strip().splitlines()
        cmd1 = (cmd[0] if cmd else "")[:TOOL_CMD_MAX]
        return f"\n› 🔧 **{name}**" + (f" `{cmd1}`" if cmd1 else "") + "\n"
    if kind == "tool_result":
        res = (val.get("text") or "").strip()
        if not res:
            return None
        short = res[:900]
        more = "\n…(截斷)" if len(res) > 900 else ""
        return f"<details><summary>↳ 結果</summary>\n\n```\n{short}{more}\n```\n\n</details>\n"
    if kind == "perm":
        return f"\n› 🔐 自動允許 **{val}**\n"
    return None


@app.get("/health")
async def health():
    return {"ok": True, "personas": list(PERSONAS),
            "subsessions": len(SUBSESSIONS),
            "bg_tasks": len(_BG_TASKS)}


# ───────────────────────── log rotation (issue #7 item 6) ──────────────────
# launchd redirects stdout/stderr to bridge.out.log / bridge.err.log and never
# rotates them, so a long-running bridge grows them toward GBs. launchd keeps
# the fd open, so rename-rotation would keep writing into the renamed file;
# copy-then-truncate is safe here because launchd opens the logs with O_APPEND
# (verified on the live process) — every write seeks to the new EOF.
_LOG_ROTATE_MAX_BYTES = int(os.environ.get("BRIDGE_LOG_MAX_BYTES", 64 * 1024 * 1024))
_LOG_ROTATE_CHECK_SECS = 900.0


def _rotate_log_file(path: str) -> None:
    try:
        if os.path.getsize(path) < _LOG_ROTATE_MAX_BYTES:
            return
    except OSError:
        return
    try:
        import shutil
        shutil.copyfile(path, path + ".1")   # keep exactly one old generation
        os.truncate(path, 0)
        _log_event("log_rotated", path=path)
    except Exception as e:  # noqa: BLE001
        _log_event("log_rotate_failed", path=path,
                   error=type(e).__name__, error_message=str(e)[:160])


async def _log_rotation_loop():
    base = os.path.dirname(os.path.abspath(__file__))
    logs = [os.path.join(base, "bridge.out.log"),
            os.path.join(base, "bridge.err.log")]
    extra = os.environ.get("BRIDGE_LOG_ROTATE_PATHS", "")
    logs.extend(p for p in (s.strip() for s in extra.split(":")) if p)
    while True:
        for p in logs:
            if os.path.exists(p):
                _rotate_log_file(p)
        await asyncio.sleep(_LOG_ROTATE_CHECK_SECS)


@app.on_event("startup")
async def _start_log_rotation():
    global _MAIN_LOOP
    _MAIN_LOOP = asyncio.get_running_loop()
    task = asyncio.create_task(_log_rotation_loop())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


@app.on_event("startup")
async def _reseed_cc_resume_pins():
    # 重啟盲窗修復:把 hook 落地的 resume-pin 重載回記憶體,避免重啟後
    # cmdline 解到凍結舊 sid(見 _cc_reseed_pins_from_files 註解)。
    n = _cc_reseed_pins_from_files()
    _log_event("cc_resume_pins_reseeded", count=n)
    # 同源盲窗:`_CC_APPROVAL_ACTIVE` 也是行程內狀態,重啟即清空 → watcher
    # 首巡前的空窗會把 App 手上舊 aid 的 CC 審核決議打成 409。先從 DB 灌回。
    _cc_reseed_approvals_from_db()


@app.on_event("startup")
async def _start_cc_approval_watcher():
    # 批次 3 斷點③:CC waiting_approval → approval feed + 推播(常駐)
    task = asyncio.create_task(_cc_approval_watcher())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    # M1:CC 委派完成偵測(15s;codex 走 turn/completed 事件不用巡)
    dtask = asyncio.create_task(_delegation_cc_watcher())
    _BG_TASKS.add(dtask)
    dtask.add_done_callback(_BG_TASKS.discard)
    # 2b:人格 choices 卡 → 審核中心(30s 巡 report_events)
    htask = asyncio.create_task(_hp_choices_watcher())
    _BG_TASKS.add(htask)
    htask.add_done_callback(_BG_TASKS.discard)


@app.on_event("startup")
async def _start_state_db_watcher():
    # #tg-instant-sync:TG/cron 寫進各 persona home 的 state.db,唯讀 stat
    # 輪詢偵測寫入 → 立刻喚醒 _hp_canon_follower(見該函式與
    # _state_db_watcher_loop 上方註解)。只讀檔案 mtime/size,不碰
    # hermes_cli 內核、不寫 state.db,常駐到 process 生命週期結束。
    stask = asyncio.create_task(_state_db_watcher_loop())
    _BG_TASKS.add(stask)
    stask.add_done_callback(_BG_TASKS.discard)
