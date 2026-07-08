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
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
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

# One SSE keepalive cadence for every streaming endpoint (issue #8: it was
# 2s / 4s / 10s across chat, ccsessions and codexsessions for no reason).
SSE_KEEPALIVE_SECS = 2.0

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
APPLE_ID_AUDIENCES = tuple(
    a.strip() for a in os.environ.get("APPLE_ID_AUDIENCES", "com.pocketagent.ios").split(",")
    if a.strip()
)
ACCOUNT_SESSION_PREFIX = "paacct."
ACCOUNT_SESSION_TTL = 60 * 60 * 24 * 90
_APPLE_JWK_CLIENT = None


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


def _save_data_uri(data_uri: str, filename: str = "") -> str | None:
    """Decode a `data:<mime>;base64,<...>` URI to UPLOAD_DIR; return the path."""
    m = re.match(r"data:([^;]+);base64,(.*)$", data_uri or "", re.DOTALL)
    if not m:
        return None
    mime, b64 = m.group(1), m.group(2)
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
CANON_DB = os.path.expanduser("~/.local/share/pocket-agent/canonical.db")
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
    con.execute("""CREATE TABLE IF NOT EXISTS devices(
        token TEXT PRIMARY KEY, platform TEXT, created_at REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS report_events(
        id TEXT PRIMARY KEY, session TEXT NOT NULL, label TEXT, name TEXT,
        content TEXT NOT NULL, ts REAL NOT NULL,
        external_source TEXT, external_id TEXT UNIQUE, ingested_at REAL NOT NULL)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_report_session_time ON report_events(session, ts)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_report_external ON report_events(external_source, external_id)")
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


def _apple_verify_identity_token(identity_token: str):
    import jwt as pyjwt
    if not APPLE_ID_AUDIENCES:
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
            audience=list(APPLE_ID_AUDIENCES),
            issuer=APPLE_ID_ISSUER,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        _log_event("apple_auth_invalid_token", error=type(e).__name__)
        raise HTTPException(status_code=401, detail="invalid apple identity token")


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


def _canon_add(session: str, role: str, content: str, attachments=None,
               mid: str | None = None, status: str = "done",
               client_id: str | None = None) -> tuple[str, bool]:
    import sqlite3
    mid = mid or uuid.uuid4().hex
    try:
        con = sqlite3.connect(CANON_DB)
        con.execute("INSERT OR REPLACE INTO messages"
                    "(id,session,role,content,attachments,created_at,status,client_id) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (mid, session, role, content, json.dumps(attachments or [], ensure_ascii=False),
                     time.time(), status, client_id))
        con.commit()
        con.close()
        _canon_notify(session)
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
    in_flight = bool(task is not None and not task.done())
    if canonical_reply:
        turn_state, label = "done", "已同步"
    elif in_flight:
        turn_state = "streaming" if acc else ("queued" if acp_busy else "running")
        label = (state or {}).get("step_label") or ("思考中" if acc else "處理中")
    elif task is not None and task.done():
        turn_state, label = ("done", "已同步") if acc else ("stream_detached", "處理中")
    elif acp_busy:
        turn_state, label = "running", "處理中"
    else:
        turn_state, label = "idle", "閒置"
    elapsed = int(now - entry["ts"]) if entry and entry.get("ts") else None
    return {"session": session, "state": turn_state, "label": label,
            "in_flight": in_flight, "acp_busy": acp_busy,
            "elapsed_seconds": elapsed, "stale_seconds": elapsed,
            "output_chars": len(acc), "canonical_reply": bool(canonical_reply),
            "canonical_reply_chars": len(canonical_reply or ""),
            "error": runner_error or None}


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


def _report_messages(session: str, limit: int = 100):
    return [{
        "id": f"rep-{r['id']}", "role": "assistant",
        "content": f"📰 **{r['label']}**\n\n{r['content']}",
        "attachments": [], "ts": r["ts"], "status": "done", "source": "report",
    } for r in _report_events(session, limit)]


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
APNS_BUNDLE_ID = "com.pocketagent.ios"
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
                     category: str | None = None, thread_id: str | None = None):
    import httpx
    headers = {"authorization": f"bearer {_apns_jwt()}",
               "apns-topic": APNS_BUNDLE_ID,
               "apns-push-type": "alert", "apns-priority": "10"}
    aps = {"alert": {"title": title, "body": body}, "sound": "default"}
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
                      thread_id: str | None = None) -> int:
    """Fan a push to every registered device; prune dead tokens (410/BadToken)."""
    sent = 0
    for tok in _devices():
        try:
            code, text = await _apns_send(tok, title, body, data,
                                          category=category, thread_id=thread_id)
            if code == 200:
                sent += 1
            elif code == 410 or "BadDeviceToken" in text or "Unregistered" in text:
                _device_remove(tok)
        except Exception:  # noqa: BLE001
            pass
    return sent


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

    asyncio.create_task(pump())
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
    STALL_LIMIT = 300
    try:
        while True:
            try:
                kind, val = await asyncio.wait_for(q.get(), timeout=SSE_KEEPALIVE_SECS)
                last_event = _t.monotonic()
            except asyncio.TimeoutError:
                if _t.monotonic() - last_event > STALL_LIMIT:
                    asyncio.create_task(session.cancel())
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
            asyncio.create_task(session.cancel())


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
CODEX_BIN = "/Users/xcash/.local/bin/codex"


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
        self.proc = await asyncio.create_subprocess_exec(
            CODEX_BIN, "app-server", "--stdio",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
            cwd=HOME_ROOT,
            limit=8 * 1024 * 1024,
        )
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
                   codex_home=(init or {}).get("codexHome", ""))

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
        con.execute("INSERT OR REPLACE INTO approvals"
                    "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (record["id"], record["title"], record["source"], record["risk"],
                     record["detail"], now, now + 3600, "pending", None, None))
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


async def _codex_warm_threads(thread_ids: list) -> None:
    """B3 light warmup: pre-run thread/resume for the sessions the user is most
    likely to tap next, so entering one skips the cold load. Strictly
    sequential and skip-if-loaded, so it never amplifies app-server queueing —
    at most one warm call is in the single _lock queue at a time."""
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
    for method, params in (
        ("thread/archive/set", {"threadId": thread_id, "archived": archived}),
        ("thread/setArchived", {"threadId": thread_id, "archived": archived}),
        ("thread/archive", {"threadId": thread_id}),
    ):
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

    async def _run(*args, cwd=None, timeout: float = 20.0):
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            # git on a wedged repo/mount must not hang the handler (issue #7).
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            _log_event("filediff_git_timeout", args=" ".join(args[:4]),
                       timeout_s=timeout)
            return 124, ""
        return proc.returncode, (out or b"").decode("utf-8", "replace")

    if os.path.isdir(p):
        rc, top = await _run("git", "-C", p, "rev-parse", "--show-toplevel")
        if rc != 0 or not top.strip():
            raise HTTPException(status_code=404, detail="目錄不在 git repo 裡，沒有 diff 可看")
        top = top.strip()
        rc2, out = await _run("git", "-C", top, "diff", "HEAD", "--", p)
        diff = out if rc2 == 0 else ""
        if not diff:
            raise HTTPException(status_code=404, detail="目錄內沒有待提交的變更")
        files = []
        rc3, names = await _run("git", "-C", top, "diff", "HEAD",
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
    rc, top = await _run("git", "-C", d, "rev-parse", "--show-toplevel")
    diff = ""
    if rc == 0 and top.strip():
        # HEAD..worktree for this file — covers staged + unstaged edits.
        rc2, out = await _run("git", "-C", top.strip(), "diff", "HEAD", "--", p)
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
        try:
            await CODEX_APP.ensure_thread_loaded(thread_id)
        except Exception as e:  # noqa: BLE001
            yield chunk({"content": f"\n⚠️ thread load failed: {e}\n"})
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
        idle = 0
        idle_limit = 120 if follow else 0
        while True:
            if await request.is_disconnected():
                break
            events = CODEX_APP.events_for(thread_id)
            while idx < len(events):
                kind, val = events[idx]
                idx += 1
                c = _fmt_item(kind, val)
                if c:
                    yield chunk({"content": c})
            if not CODEX_APP.is_active(thread_id) and idx >= len(events) and not follow:
                break
            await asyncio.sleep(0.5)
            idle += 1
            if idle >= max(1, int(SSE_KEEPALIVE_SECS / 0.5)):
                idle = 0
                yield ": keepalive\n\n"
            if follow and idle_limit > 0 and not CODEX_APP.is_active(thread_id):
                idle_limit -= 1
                if idle_limit <= 0:
                    break
            elif follow and CODEX_APP.is_active(thread_id):
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
        argv = [CODEX_BIN, "exec", "--json", task]
    else:
        argv = _claude_argv(sub.get("parent", "yuanfang"), task)
    await _stream_agent(sid, argv, run_cwd, "dispatch 失敗")


async def _run_resume(sid: str, prompt: str):
    """Follow-up turn into an existing sub-session — resumes the CC session so
    the sub-agent keeps its full prior context."""
    sub = SUBSESSIONS[sid]
    cwd = sub.get("cwd") or HOME_ROOT
    if sub.get("tool") == "codex":
        argv = [CODEX_BIN, "exec", "--json", prompt]   # codex: new exec in same cwd
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
        text, ts = _persona_preview(home, session=mid)
        out.append({"id": mid, "type": "persona", "name": disp,
                    "preview": text, "lastAt": ts, "status": "idle"})
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
_CC_HOOK_STATE: dict[str, dict] = {}
_CC_HOOK_TTL = 600.0

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


# App-owned CC sessions registry. CCSESS_CONF is shared with the ccsess CLI
# (daemon sessions like "Culture Supply"/"Ops"/"FLiPER" live there too), and its
# `name|workdir|enabled` format is read by many 3-tuple callers — so instead of
# adding a 4th field we keep a SEPARATE bridge-managed list of the CC sessions
# THIS app created (via POST /ccsessions). The approval watcher only pushes for
# these, so a foreign ccsess session's TUI prompt never reaches the app's審核中心
# / push. One name per line.
APP_OWNED_CC = os.path.join(os.path.dirname(CCSESS_CONF), "app-owned.txt")


def _cc_app_owned_names() -> set:
    try:
        with open(APP_OWNED_CC) as f:
            return {ln.strip() for ln in f if ln.strip() and not ln.startswith("#")}
    except Exception:  # noqa: BLE001
        return set()


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


def _cc_last_activity(workdir: str):
    """Transcript mtime for the home recency sort. Just os.path.getmtime — no file
    read/parse (the app shows YOUR last sent command from its local SentLog, so the
    server needn't extract a preview). Cheap enough to run per-poll."""
    jsonl = _cc_latest_jsonl(workdir)
    if not jsonl:
        return (0.0, "")
    try:
        return (os.path.getmtime(jsonl), "")
    except Exception:  # noqa: BLE001
        return (0.0, "")


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
        mtime, preview = _cc_last_activity(workdir)
        out.append({"name": name, "workdir": workdir,
                    "status": "running" if alive else "down", "busy": busy,
                    "awaiting": awaiting, "updatedAt": mtime, "preview": preview})
    return out


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
_CC_JSONL_SCAN_CACHE: dict = {}   # workdir -> (jsonl, mtime, usage, plan)


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


def _cc_scan_jsonl(workdir: str):
    """→ (usage_dict_or_None, latest_plan_or_None) for the session's live jsonl."""
    jsonl = _cc_latest_jsonl(workdir)
    if not jsonl:
        return (None, None)
    try:
        mt = os.path.getmtime(jsonl)
    except OSError:
        return (None, None)
    hit = _CC_JSONL_SCAN_CACHE.get(workdir)
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
        _log_event("cc_jsonl_scan_failed", workdir=workdir,
                   error=type(e).__name__, error_message=str(e)[:120])
    _CC_JSONL_SCAN_CACHE[workdir] = (jsonl, mt, usage, plan)
    return (usage, plan)


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
    await _run_ccsess("new", name, wd)
    _cc_mark_app_owned(name)   # 這條是 app 開的 → 只有它的審核會進 app(見 _cc_approval_watcher)
    ready = await _cc_wait_ready(name)
    return {"ok": True, "session": {"name": name, "workdir": wd,
                                    "status": "running" if ready else "starting"}}


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
        jsonl = _cc_latest_jsonl(workdir)
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
            cur = _cc_latest_jsonl(workdir)
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
    jsonl = _cc_latest_jsonl(row[1])
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
    rc, _, err = await _tmux_run("new-session", "-d", "-s", name, "-c", cwd,
                                 CLAUDE_BIN, "--resume", sid)
    if rc != 0:
        raise http_err(502, "TMUX_FAILED", "tmux new-session failed",
                       (err or "tmux new-session failed")[:200])
    try:
        with open(CCSESS_CONF, "a") as f:
            f.write(f"{name}|{cwd}|1\n")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"registered tmux but conf write failed: {e}")
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
    """cc 中斷核心(Esc + 驗證重試 3 次)— v1 與 v2 統一路由共用。"""
    if not await _tmux_alive(name):
        raise http_err(409, "SESSION_NOT_RUNNING", "session not running")
    attempts = 0
    interrupted = False
    for _ in range(3):
        attempts += 1
        rc, _, err = await _tmux_run("send-keys", "-t", name, "Escape")
        if rc:
            raise http_err(502, "TMUX_FAILED", "tmux send-keys failed",
                           err[:200] or "interrupt failed")
        _PANE_CACHE.pop(name, None)              # the cached pane is now stale
        await asyncio.sleep(0.7)                 # let the TUI react before checking
        pane = await _cc_capture_pane_fresh(name)
        if not _cc_pane_busy(pane):
            interrupted = True
            break
    _log_event("cc_interrupt", session=name, interrupted=interrupted, attempts=attempts)
    return {"ok": True, "interrupted": interrupted, "attempts": attempts}


# Claude Code's TUI shows a working spinner like "· Fermenting… (1m 51s · ↓ 6.5k
# tokens)" while a turn runs — capture the pane and look for it. Covers long,
# silent commands (the spinner stays up), which a stream-silence heuristic misses.
_CC_BUSY_RE = re.compile(r"\((?:\d+m\s*)?\d+(?:\.\d+)?s\s*·.*tokens", re.IGNORECASE)
_CC_OPT_NUM_RE = re.compile(r"^(\d+)[.)]\s+(.{1,60})$")
_CC_OPT_LABEL_RE = re.compile(r"^(allow once|always allow|don.t allow|allow|deny|yes,|yes\b|no,|no\b)", re.IGNORECASE)


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
            return {"kind": "menu", "title": title, "options": opts[:6]}
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
            return {"kind": "menu", "title": title, "options": opts[:5]}
    if re.search(r"\(y/n\)|press y\b|y to (confirm|continue|proceed)", tail_low):
        return {"kind": "yesno", "title": "",
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
    name = _cc_name_for_cwd(body.get("cwd"))
    if not name:
        return {"ok": True, "ignored": True}
    now = time.time()
    state = {"busy": event == "UserPromptSubmit", "updated_at": now, "source": "hook"}
    if event == "Stop":
        state["last_assistant_message"] = body.get("last_assistant_message")
    _CC_HOOK_STATE[name] = state
    _log_event("cc_hook_state",
               name=name,
               hook_event_name=event,
               busy=state["busy"],
               cwd_hash=_short_hash(str(body.get("cwd") or "")),
               last_assistant_message_chars=len(str(body.get("last_assistant_message") or "")))
    return {"ok": True, "session": name, "busy": state["busy"], "source": "hook"}


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
    st = {"busy": busy, "running": True, "mode": mode, "prompt": prompt}
    # wave 2: usage meter + full plan text from the transcript jsonl.
    row = next((r for r in _cc_conf_rows() if r[0] == name), None)
    if row:
        usage, plan = _cc_scan_jsonl(row[1])
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
    if mapped:
        args.append(mapped)                  # named control key
    elif len(raw) == 1 and raw.isprintable():
        args += ["-l", raw]                  # literal single char (y / n / 1-3)
    else:
        raise HTTPException(status_code=400, detail="unsupported key")
    rc, _, err = await _tmux_run(*args)
    if rc:
        raise http_err(502, "TMUX_FAILED", "tmux send-keys failed",
                       err[:200] or "send-keys failed")
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
_CC_APPROVAL_POLL_SECS = 4.0
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
    opts = " / ".join(str(o.get("label") or "")[:30]
                      for o in (prompt.get("options") or [])[:4])
    detail = f"session: {name}\n{title}" + (f"\n選項: {opts}" if opts else "")
    now = time.time()
    con = sqlite3.connect(CANON_DB, timeout=30)
    con.execute("INSERT OR REPLACE INTO approvals"
                "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result,callback) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (aid, title, f"claude_code:{name}", "high", detail,
                 now, now + _CC_APPROVAL_TTL, "pending", None, None, None))
    con.commit()
    con.close()
    return aid


async def _cc_approval_watcher():
    """常駐:每 4s 巡一輪 enabled CC sessions(pane 走快取,成本低)。"""
    while True:
        await asyncio.sleep(_CC_APPROVAL_POLL_SECS)
        owned = _cc_app_owned_names()   # 只推 app 自己開的 CC session 的審核
        for name, _workdir, enabled in _cc_conf_rows():
            if enabled != "1":
                continue
            # daemon / 別處開的 ccsess(Culture Supply、Ops、FLiPER…)不進 app 審核中心、
            # 不發推播。它們照常在 ccsess 跑,只是審核通知不外漏到這台 app。
            if name not in owned:
                continue
            try:
                st = await _cc_status_core(name)
                prompt = st.get("prompt")
                active = _CC_APPROVAL_ACTIVE.get(name)
                if prompt:
                    sig = _cc_prompt_sig(prompt)
                    if active and active["sig"] == sig:
                        continue                     # 同一個 prompt,已建過
                    if active:
                        _cc_approval_set_status(active["aid"], "expired")
                    aid = _cc_approval_create(name, prompt)
                    _CC_APPROVAL_ACTIVE[name] = {"aid": aid, "sig": sig}
                    opts = " / ".join(str(o.get("label") or "")[:20]
                                      for o in (prompt.get("options") or [])[:3])
                    _approval_push(aid, prompt.get("title") or f"{name} 等待核准",
                                   f"{name}" + (f" · {opts}" if opts else ""),
                                   f"claude_code:{name}")
                    _log_event("cc_approval_created", session=name,
                               approval_id=aid)
                elif active:
                    # prompt 消失(TUI 上被回掉/回合結束)→ 記錄過期,feed 不留殭屍
                    _cc_approval_set_status(active["aid"], "expired")
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
    out.extend(await _delegation_v2_sessions())
    for name, workdir, enabled in _cc_conf_rows():
        if enabled != "1":
            continue
        st, prompt = await _v2_cc_state(name)
        caps = ["input", "interrupt", "keys", "attachments", "replay", "follow"]
        if prompt:
            caps.append("approve")
        out.append({"id": f"claude_code:{name}", "provider": "claude_code", "title": name,
                    "subtitle": workdir, "status": st, "last_event_at": None,
                    "capabilities": caps, "meta": ({"prompt": prompt} if prompt else {})})
    for mid, (disp, _home) in PERSONAS.items():
        out.append({"id": f"hermes:{mid}", "provider": "hermes", "title": disp,
                    "subtitle": None, "status": "idle", "last_event_at": None,
                    "capabilities": ["input", "attachments", "replay", "follow", "approve"], "meta": {}})
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
            out.append({"id": f"codex:{thread_id}", "provider": "codex",
                        "title": s.get("name") or "codex", "subtitle": s.get("workdir"),
                        "status": "waiting_approval" if approval else ("running" if active else "idle"),
                        "last_event_at": s.get("lastEventAt"),
                        "capabilities": caps,
                        "meta": {"approval": CODEX_APP._approval_public(approval) if approval else None}})
    except Exception:  # noqa: BLE001
        pass
    if provider:
        out = [s for s in out if s["provider"] == provider]
    if status:
        out = [s for s in out if s["status"] == status]
    return {"sessions": out}


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
    jsonl = _cc_latest_jsonl(workdir)
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


async def _cc_card_follower(name: str, workdir: str):
    """每秒 tail 該 session 的 jsonl → digest 進卡片庫;有訂閱者時再巡
    busy/mode/prompt(tmux capture 有成本)發 session.status / turn 事件。"""
    store = _cc_card_store(name)
    await _cc_card_seed(store, name, workdir)
    prev_busy = None
    while True:
        await asyncio.sleep(1.0)
        try:
            cur = _cc_latest_jsonl(workdir)
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
            await CODEX_APP.ensure_thread_loaded(thread_id)
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


async def _hp_canon_follower(session: str):
    """canonical 寫入版本喚醒(#28 的 _canon_wait)→ 補掃出卡。known_mids
    去重;30s 保險絲重掃與 v1 messages/events 同款。"""
    d = _HP_CARD_DIGESTS[session]
    ver = _CANON_VER.get(session, 0)
    while True:
        try:
            await asyncio.wait_for(_canon_wait(session, ver), timeout=30.0)
        except asyncio.TimeoutError:
            pass
        except Exception as e:  # noqa: BLE001
            _log_event("hp_card_follower_error", session=session,
                       error=str(e)[:200])
            await asyncio.sleep(2.0)
        ver = _CANON_VER.get(session, 0)
        try:
            d.seed_messages(_canon_messages(session, 80))
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
            msgs = await asyncio.to_thread(_canon_messages, session,
                                           _HP_CARD_SEED_MSGS)
            d.seed_messages(msgs)
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
    if len(attachments) > 12:
        raise HTTPException(status_code=413, detail="too many attachments")
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

@app.get("/capabilities")
async def capabilities(request: Request):
    _check_auth(request)
    return {"api": "app/v1",
            "features": ["canonical_messages", "reports", "notifications",
                         "approvals", "cc_sessions", "attachments", "vision",
                         "message_dry_run", "message_interrupt", "message_status",
                         "message_events", "apns_push", "accounts",
                         "apple_auth", "account_pairing",
                         "delegations", "control_plane_v2", "attachment_uploads",
                         "interactive_push"],
            "endpoints": ["/app/v1/sessions", "/app/v1/messages", "/reports",
                          "/app/v1/uploads",
                          "/app/v1/reactions", "/app/v1/pins",
                          "/app/v1/messages/retract", "/app/v1/personas",
                          "/app/v1/messages/status", "/app/v1/messages/events",
                          "/app/v1/messages/interrupt",
                          "/cron/jobs", "/ccsessions", "/app/v1/approvals",
                          "/app/v1/devices", "/app/v1/push/test",
                          "/app/v1/auth/apple", "/app/v1/account",
                          "/app/v1/pair/new", "/app/v1/pair/claim",
                          "/app/v1/devices/{id}/revoke",
                          "/app/v1/delegations", "/app/v2/sessions",
                          "/app/v2/sessions/{id}/approve"]}


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
        await _run_ccsess("new", cc_session_name, cwd)
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
               provider_session_hash=_short_hash(provider_session_id),
               objective_chars=len(objective),
               attachment_count=len(body.get("attachments") or []))
    return {"ok": True, "delegation": _delegation_public(row, status)}


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
    # app 回合的 assistant 回覆會在兩個來源各留一份:canonical store(正文+
    # 〈🔧 執行步驟〉摺疊附錄、帶 client_id)與 Hermes state.db(乾淨正文、
    # tg-* id、無 client_id)。兩份文字不同 → app 端按文字去重必然失敗,同
    # 一回覆畫面出現兩顆氣泡。在源頭壓掉 tg 側重複:剝附錄後正文相同、且
    # 時間差 10 分鐘內,視為同一回合。純 TG 對話(canonical 無該回合)與
    # 相隔久遠的同文回覆不受影響。
    def _steps_stripped(t: str) -> str:
        return re.sub(r"<details>.*?</details>", "", t or "", flags=re.S).strip()
    canon_assist = [((m.get("ts") or 0), _steps_stripped(m.get("content") or ""))
                    for m in out if m.get("role") == "assistant"]
    def _tg_dup(m) -> bool:
        if m["role"] != "assistant":
            return False
        body = _steps_stripped(m["content"])
        ts = m["ts"] or 0
        return bool(body) and any(c == body and abs(ts - cts) < 600
                                  for cts, c in canon_assist)
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
        con.execute("INSERT INTO message_meta(message_id, reactions, pinned, updated_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET "
                    "reactions=excluded.reactions, updated_at=excluded.updated_at",
                    (message_id, json.dumps(reactions, ensure_ascii=False),
                     pinned, time.time()))
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
        con.execute("INSERT INTO message_meta(message_id, reactions, pinned, updated_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET "
                    "pinned=excluded.pinned, updated_at=excluded.updated_at",
                    (message_id, json.dumps(reactions, ensure_ascii=False),
                     pinned, time.time()))
        con.commit()
        con.close()
    except Exception as e:  # noqa: BLE001
        _log_event("message_meta_write_failed", kind="pin",
                   message_id=message_id, error=type(e).__name__)
        raise HTTPException(status_code=500, detail=str(e)[:200])
    return {"ok": True}


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
                    state["step_label"] = ""     # 正文恢復 → 步驟 label 讓位
                elif k == "usage":
                    state["usage"] = v
                elif k == "status":
                    # 步驟進度(執行步驟 N:工具)— 讓輪詢的 /messages/status
                    # 也能給 working bar 同一句人話。
                    state["step_label"] = (v or {}).get("label") or ""
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
            return StreamingResponse(replay_agen(), media_type="text/event-stream")

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
            try:
                yield chunk({"role": "assistant", "content": ""})
                yield status_chunk("attached", "同一則訊息已在處理中，附掛原回合等待結果。")
                t0 = time.monotonic()
                while True:
                    _task = attached.get("task")
                    if _task is not None and _task.done():
                        break
                    if _task is None and time.monotonic() - t0 > 30:
                        break   # original request died before starting its turn
                    if time.monotonic() - t0 > _APP_TURN_INFLIGHT_TTL:
                        break
                    await asyncio.sleep(SSE_KEEPALIVE_SECS)
                    yield ": keepalive\n\n"
                st = attached.get("state") or {}
                acc = st.get("acc") or ""
                yield chunk({"content": acc or "(原回合沒有產出回覆)"})
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
        return StreamingResponse(attach_agen(), media_type="text/event-stream")

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
        return StreamingResponse(dry_agen(), media_type="text/event-stream")

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
                       keepalives=state["keepalives"],
                       done_sent=state["done_sent"],
                       canonical_user_ok=canonical_user_ok,
                       canonical_reply_ok=state["canonical_reply_ok"],
                       stream_error=state["stream_error"] or None,
                       duration_ms=int((time.monotonic() - turn_started) * 1000))

    return StreamingResponse(agen(), media_type="text/event-stream")


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

def _approval_row(r):
    return {"id": r[0], "title": r[1], "source": r[2], "risk": r[3], "detail": r[4],
            "created_at": r[5], "expires_at": r[6], "status": r[7],
            "decided_at": r[8], "result": r[9]}


def _approvals_expire(con):
    con.execute("UPDATE approvals SET status='expired' WHERE status='pending' "
                "AND expires_at IS NOT NULL AND expires_at < ?", (time.time(),))


# B4 (issue #9): an approval that never expires pends forever if the phone
# misses the push — default to 1h, clamp to [30s, 7d] so a typo'd ttl can't
# create an immortal (or instantly-dead) row.
_APPROVAL_TTL_DEFAULT = 3600.0
_APPROVAL_TTL_MIN, _APPROVAL_TTL_MAX = 30.0, 7 * 86400.0


async def _approval_fire_callback(aid: str, callback: str, status: str, result):
    """POST the decision to the creator's callback URL (fire-and-forget)."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(callback, json={"id": aid, "status": status,
                                                  "result": result})
        _log_event("approval_callback_sent", id=aid, status=status,
                   http_status=r.status_code)
    except Exception as e:  # noqa: BLE001
        _log_event("approval_callback_failed", id=aid, status=status,
                   error=type(e).__name__, error_message=str(e)[:160])


@app.post("/app/v1/approvals")
async def approval_create(request: Request):
    """Create a pending approval (called by Hermes / a skill)."""
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
    if callback and not callback.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="callback_url must be http(s)")
    now = time.time()
    con = sqlite3.connect(CANON_DB)
    con.execute("INSERT OR REPLACE INTO approvals"
                "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result,callback) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (aid, b.get("title") or "需要核准", b.get("source") or "", b.get("risk") or "",
                 b.get("detail") or "", now, now + ttl, "pending", None, None, callback))
    con.commit()
    con.close()
    title = b.get("title") or "需要核准"
    body = (b.get("detail") or b.get("source") or "點開查看並決定")[:120]
    _approval_push(aid, title, body, str(b.get("source") or ""))
    return {"id": aid, "status": "pending", "expires_at": now + ttl}


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
    n = await push_notify(b.get("title") or "Pocket Agent",
                          b.get("body") or "測試推播 ✅ M23 已接上",
                          {"kind": "test"})
    return {"sent": n, "devices": len(_devices())}


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
        rows = con.execute("SELECT id,title,source,risk,detail,created_at,expires_at,status,decided_at,result "
                           "FROM approvals WHERE status=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                           (status, lim, off)).fetchall()
    else:
        total = con.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
        rows = con.execute("SELECT id,title,source,risk,detail,created_at,expires_at,status,decided_at,result "
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
    r = con.execute("SELECT id,title,source,risk,detail,created_at,expires_at,status,decided_at,result "
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
    """Approval Center 決議核心 — v1 與 v2 統一路由 approve 共用。"""
    import sqlite3
    decision = "approved" if b.get("approve") else "rejected"
    con = sqlite3.connect(CANON_DB)
    row = con.execute("SELECT source FROM approvals WHERE id=?", (aid,)).fetchone()
    if row and str(row[0] or "").startswith("claude_code:"):
        # 批次 3 斷點③:CC 審核決議 → 回流 TUI 鍵。以「當下 pane 的 prompt」
        # 為準(推播到點按之間 prompt 可能已被回掉——過時就 409,不盲送鍵)。
        con.close()
        name = str(row[0]).split(":", 1)[1]
        active = _CC_APPROVAL_ACTIVE.get(name)
        st = await _cc_status_core(name)
        prompt = st.get("prompt")
        if not prompt or not active or active.get("aid") != aid:
            _cc_approval_set_status(aid, "expired")
            raise HTTPException(status_code=409, detail="already decided or expired")
        key = str(b.get("key") or "").strip() or _cc_choice_key(prompt, bool(b.get("approve")))
        await _cc_key_core(name, key)
        _cc_approval_set_status(aid, decision)
        _CC_APPROVAL_ACTIVE.pop(name, None)
        _log_event("cc_approval_decision", session=name, approval_id=aid,
                   status=decision, key=key)
        return {"id": aid, "status": decision, "key": key}
    if row and str(row[0] or "").startswith("codex"):
        con.close()
        try:
            result = await CODEX_APP.decide_approval(
                aid,
                decision == "approved",
                for_session=bool(b.get("for_session") or b.get("approve_for_session") or
                                 b.get("remember")),
            )
            return {"id": aid, "status": result["status"], "result": result["result"]}
        except CodexAppServerError as e:
            if e.code == 404:
                raise http_err(409, "APPROVAL_NOT_PENDING",
                               "Codex approval is no longer live")
            _codex_http_error(e)
    cur = con.execute("UPDATE approvals SET status=?, decided_at=?, result=? "
                      "WHERE id=? AND status='pending'",
                      (decision, time.time(), b.get("result") or "", aid))
    con.commit()
    changed = cur.rowcount
    cb_row = con.execute("SELECT callback FROM approvals WHERE id=?", (aid,)).fetchone()
    con.close()
    if not changed:
        raise HTTPException(status_code=409, detail="already decided or expired")
    # B4 (issue #9): push the decision back to the creator (Hermes skill / TG
    # flow) so it doesn't have to poll GET /app/v1/approvals/{id}.
    if cb_row and cb_row[0]:
        asyncio.create_task(_approval_fire_callback(
            aid, cb_row[0], decision, b.get("result") or ""))
    return {"id": aid, "status": decision}


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
    task = asyncio.create_task(_log_rotation_loop())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


@app.on_event("startup")
async def _start_cc_approval_watcher():
    # 批次 3 斷點③:CC waiting_approval → approval feed + 推播(常駐)
    task = asyncio.create_task(_cc_approval_watcher())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
