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
import collections
import glob
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse

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
_PAIR_CODE_TTL = 300.0          # a pairing code is valid for 5 minutes
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
    except Exception:
        return {}


def _save_device_tokens(d: dict) -> None:
    os.makedirs(_POCKET_DIR, exist_ok=True)
    tmp = _DEVICE_TOKENS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _DEVICE_TOKENS_PATH)


_DEVICE_TOKENS: dict = _load_device_tokens()

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
    raise HTTPException(status_code=401, detail="invalid bridge token")

HERMES_BIN = "/Users/xcash/apps/hermes-agent/runtime/venv/bin/hermes"
HOME_ROOT = "/Users/xcash/apps/hermes-agent/home"

# model id -> (display name, HERMES_HOME). id stays ascii for client URLs.
PERSONAS = {
    "yuanfang":    ("袁方 (幕僚長/main)", HOME_ROOT),
    "pantianqing": ("潘天晴 (FLiPER)",    f"{HOME_ROOT}/profiles/fliper"),
    "xcash":       ("XCash (PocketAgent 協調)", f"{HOME_ROOT}/profiles/xcash"),
    "shuijing":    ("水鏡 (shuijing)",    f"{HOME_ROOT}/profiles/shuijing"),
}

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
    except Exception:  # noqa: BLE001
        return None
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\-]", "_", os.path.basename(filename or "")) or "file"
    if "." not in safe:
        safe += _MIME_EXT.get(mime, "")
    path = UPLOAD_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}-{safe}"
    try:
        path.write_bytes(raw)
    except Exception:  # noqa: BLE001
        return None
    return str(path)


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


def _transcribe(path: str) -> str:
    """Audio file path → transcript (best-effort; '' on failure)."""
    try:
        with open(path, "rb") as f:
            r = _openai_client().audio.transcriptions.create(model="whisper-1", file=f)
        return (r.text or "").strip()
    except Exception as e:  # noqa: BLE001
        print(f"[voice] transcription failed: {e}", flush=True)
        return ""


async def _transcribe_attachments(attachments: list) -> str:
    """Save + transcribe every audio attachment; return the joined transcript.
    Runs the blocking whisper call off the event loop."""
    texts = []
    for a in (attachments or []):
        if a.get("kind") != "audio":
            continue
        path = _save_data_uri(a.get("data", ""), a.get("filename", "voice.m4a"))
        if not path:
            continue
        t = await asyncio.to_thread(_transcribe, path)
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
                    path = _save_data_uri((p.get("image_url") or {}).get("url", ""), "image.jpg")
                    if path:
                        images.append(path)
                elif t == "file":
                    f = p.get("file") or {}
                    path = _save_data_uri(f.get("file_data", ""), f.get("filename", "file"))
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
    except Exception:  # noqa: BLE001
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
    con = sqlite3.connect(CANON_DB)
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
    con.execute("""CREATE TABLE IF NOT EXISTS approvals(
        id TEXT PRIMARY KEY, title TEXT, source TEXT, risk TEXT, detail TEXT,
        created_at REAL, expires_at REAL, status TEXT, decided_at REAL, result TEXT)""")
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
    con = sqlite3.connect(ACCOUNTS_DB, timeout=10)
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
    con = sqlite3.connect(ACCOUNTS_DB, timeout=10)
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
    con = sqlite3.connect(ACCOUNTS_DB, timeout=10)
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
    con = sqlite3.connect(ACCOUNTS_DB, timeout=10)
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
        con = sqlite3.connect(ACCOUNTS_DB, timeout=10)
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
    con = sqlite3.connect(ACCOUNTS_DB, timeout=10)
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
        return mid, True
    except Exception as e:  # noqa: BLE001
        _log_event("canonical_write_failed",
                   session=session, role=role, status=status,
                   client_id_hash=_short_hash(client_id),
                   content_chars=len(content or ""),
                   attachment_count=len(attachments or []),
                   error=type(e).__name__, error_message=str(e)[:160])
    return mid, False


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
        rows = con.execute("SELECT id,role,content,attachments,created_at,status FROM messages "
                           "WHERE session=? ORDER BY created_at DESC LIMIT ?", (session, limit)).fetchall()
        con.close()
    except Exception:  # noqa: BLE001
        return []
    rows.reverse()
    return [{"id": r[0], "role": r[1], "content": r[2],
             "attachments": json.loads(r[3] or "[]"), "ts": r[4],
             "status": r[5], "source": "app"} for r in rows]


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
    con = sqlite3.connect(CANON_DB, timeout=10)
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
    con = sqlite3.connect(CANON_DB, timeout=10)
    con.execute(f"UPDATE delegations SET {', '.join(sets)} WHERE id=?", args)
    con.commit()
    con.close()


async def _delegation_runtime_status(row) -> str:
    d = dict(row)
    provider = d.get("provider") or ""
    if provider == "codex":
        tid = d.get("codex_thread_id") or d.get("provider_session_id") or ""
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
        if d.get("provider") == "claude_code" and st == "waiting_approval":
            caps.append("approve")
        out.append({
            "id": f"delegation:{d['id']}",
            "provider": d.get("provider"),
            "title": d["display_title"],
            "subtitle": f"{d.get('parent_persona')} · {d.get('cwd')}",
            "status": d.get("status"),
            "last_event_at": d.get("updated_at"),
            "capabilities": caps,
            "meta": {"delegation": d, "work_order": d.get("work_order"),
                     "takeover": d.get("takeover")},
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
    con = sqlite3.connect(CANON_DB, timeout=10)
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
    except Exception:  # noqa: BLE001
        return []


def _device_add(token: str, platform: str = "ios") -> None:
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB)
        con.execute("INSERT OR REPLACE INTO devices(token,platform,created_at) "
                    "VALUES(?,?,?)", (token, platform, time.time()))
        con.commit()
        con.close()
    except Exception:  # noqa: BLE001
        pass


def _device_remove(token: str) -> None:
    import sqlite3
    try:
        con = sqlite3.connect(CANON_DB)
        con.execute("DELETE FROM devices WHERE token=?", (token,))
        con.commit()
        con.close()
    except Exception:  # noqa: BLE001
        pass


async def _apns_send(token: str, title: str, body: str, data: dict | None = None):
    import httpx
    headers = {"authorization": f"bearer {_apns_jwt()}",
               "apns-topic": APNS_BUNDLE_ID,
               "apns-push-type": "alert", "apns-priority": "10"}
    payload = {"aps": {"alert": {"title": title, "body": body}, "sound": "default"}}
    if data:
        payload.update(data)
    async with httpx.AsyncClient(http2=True, timeout=10) as client:
        r = await client.post(f"{APNS_HOST}/3/device/{token}",
                              headers=headers, json=payload)
        return r.status_code, r.text


async def push_notify(title: str, body: str, data: dict | None = None) -> int:
    """Fan a push to every registered device; prune dead tokens (410/BadToken)."""
    sent = 0
    for tok in _devices():
        try:
            code, text = await _apns_send(tok, title, body, data)
            if code == 200:
                sent += 1
            elif code == 410 or "BadDeviceToken" in text or "Unregistered" in text:
                _device_remove(tok)
        except Exception:  # noqa: BLE001
            pass
    return sent


_canon_init()
_accounts_init()


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

    def flush_thought():
        if thought_buf:
            t = "".join(thought_buf).strip()
            thought_buf.clear()
            if t:
                return f"\n<details><summary>💭 思考</summary>\n\n{t}\n\n</details>\n\n"
        return None

    import time as _t
    last_event = _t.monotonic()
    STALL_LIMIT = 300
    try:
        while True:
            try:
                kind, val = await asyncio.wait_for(q.get(), timeout=2.0)
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
                name = val.get("name", "tool")
                cmd = (val.get("cmd") or "").strip().splitlines()
                cmd1 = (cmd[0] if cmd else "")[:140]
                yield ("content", f"\n› 🔧 **{name}**" + (f" `{cmd1}`" if cmd1 else "") + "\n")
            elif kind == "tool_result":
                res = (val.get("text") or "").strip()
                if res:
                    short = res[:900]
                    more = "\n…(截斷)" if len(res) > 900 else ""
                    yield ("content", f"<details><summary>↳ 結果</summary>\n\n```\n{short}{more}\n```\n\n</details>\n")
            elif kind == "perm":
                yield ("content", f"\n› 🔐 自動允許 **{val}**\n")
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
            while True:
                while idx < len(sub["output"]):
                    kind, val = sub["output"][idx]
                    idx += 1
                    c = _fmt_item(kind, val)
                    if c:
                        yield schunk({"content": c})
                if sub.get("status") == "done" and idx >= len(sub["output"]):
                    break
                await asyncio.sleep(0.4)
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
                except Exception:  # noqa: BLE001
                    continue
                if "id" in msg:
                    fut = self._pending.pop(msg.get("id"), None)
                    if not fut or fut.done():
                        continue
                    if "error" in msg:
                        err = msg.get("error") or {}
                        fut.set_exception(CodexAppServerError(
                            err.get("message") or "codex app-server error",
                            err.get("code")))
                    else:
                        fut.set_result(msg.get("result"))
                else:
                    self._handle_notification(msg)
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

    def _handle_notification(self, msg: dict):
        method = msg.get("method")
        params = msg.get("params") or {}
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
        if method == "turn/started" and tid:
            turn = params.get("turn") or {}
            self.active_turns[tid] = turn.get("id") or True
            self.last_event_at[tid] = time.time()
            self.thread_errors.pop(tid, None)
            return
        if method == "turn/completed" and tid:
            self.active_turns.pop(tid, None)
            self.last_event_at[tid] = time.time()
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
        if not tid:
            return
        if method == "item/agentMessage/delta":
            item_id = params.get("itemId")
            if item_id:
                self._streamed_item_ids.add(item_id)
            self._append(tid, ("text", params.get("delta") or ""))
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


def _codex_enrich_summary(summary: dict) -> dict:
    tid = summary.get("thread_id") or summary.get("id") or ""
    summary["activeTurn"] = CODEX_APP.is_active(tid)
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
    return {
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
        return text if text else ""
    if t == "plan":
        text = item.get("text") or ""
        return f"\n<details><summary>Plan</summary>\n\n{text}\n\n</details>\n" if text else ""
    if t == "reasoning":
        summary = "\n".join(item.get("summary") or []).strip()
        return f"\n<details><summary>Reasoning</summary>\n\n{summary}\n\n</details>\n" if summary else ""
    if t == "commandExecution":
        cmd = (item.get("command") or "").strip().splitlines()
        cmd1 = (cmd[0] if cmd else "")[:160]
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
        path = _save_data_uri(a.get("data", ""), a.get("filename", "file"))
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


@app.get("/codexsessions")
async def codex_sessions(request: Request, limit: int = 40, cwd: str | None = None,
                         archived: bool = False):
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
    try:
        res = await CODEX_APP.call("thread/list", params, timeout=45.0)
        return {
            "sessions": [_codex_enrich_summary(_codex_session_summary(t))
                         for t in (res or {}).get("data", [])],
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
    policy as /file."""
    _check_auth(request)
    p = os.path.realpath(os.path.expanduser(path))
    roots = [os.path.realpath(os.path.expanduser("~"))]
    for t in ("/tmp", "/private/tmp", "/var/folders"):
        rt = os.path.realpath(t)
        if rt not in roots:
            roots.append(rt)
    if not any(p == r or p.startswith(r + os.sep) for r in roots) or not os.path.isfile(p):
        raise HTTPException(status_code=404, detail="not found")

    async def _run(*args, cwd=None):
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        return proc.returncode, (out or b"").decode("utf-8", "replace")

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
    body = await request.json()
    input_items = await _codex_input_items((body.get("text") or "").strip(),
                                           body.get("attachments") or [])
    if not input_items:
        raise HTTPException(status_code=400, detail="empty")
    try:
        res = await CODEX_APP.start_turn(thread_id, input_items,
                                         client_id=body.get("client_id"),
                                         cwd=body.get("cwd"))
        return {"ok": True, "thread_id": thread_id, "turn": (res or {}).get("turn")}
    except Exception as e:  # noqa: BLE001
        _codex_http_error(e)


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
    try:
        params = {
            "threadId": thread_id,
            "limit": max(1, min(limit, 100)),
            "itemsView": "full",
            "sortDirection": "desc",
        }
        if cursor:
            params["cursor"] = cursor
        res = await CODEX_APP.call("thread/turns/list", params, timeout=45.0)
        turns = list((res or {}).get("data", []))
        turns.reverse()
        return {"text": _codex_format_turns(turns),
                "more": bool((res or {}).get("nextCursor")),
                "nextCursor": (res or {}).get("nextCursor")}
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
            if idle >= 20:
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


async def _stream_agent(sid: str, argv: list, cwd: str, fail_label: str):
    """Run a sub-agent subprocess, append its transcript to the sub's output
    buffer, capture the Claude Code session id (for later --resume), and mark
    the sub done when it exits."""
    sub = SUBSESSIONS[sid]
    out = sub["output"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        sub["proc"] = proc
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
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
    finally:
        sub["status"] = "done"
        sub["lastAt"] = time.time()
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


def _persona_preview(home: str):
    """Latest message of the persona's canonical Telegram session → (text, ts)."""
    import sqlite3
    db = os.path.join(home, "state.db")
    if not os.path.exists(db):
        return (None, None)
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        cur = con.execute(
            "SELECT m.content, m.timestamp FROM messages m "
            "JOIN sessions s ON s.id = m.session_id "
            "WHERE s.source='telegram' AND m.role IN ('user','assistant') "
            "AND m.content IS NOT NULL AND m.content != '' "
            "ORDER BY m.timestamp DESC LIMIT 1")
        row = cur.fetchone()
        con.close()
        if row:
            return (str(row[0])[:80], row[1])
    except Exception:
        pass
    return (None, None)


def _persona_history(home: str, limit: int = 100):
    """Full recent transcript of the persona's canonical Telegram session, so a
    fresh app install / new device can render the conversation instead of a
    blank thread. Returns oldest→newest [{role, content, ts}]."""
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
        return [{"role": r[0], "content": r[1], "ts": r[2]} for r in rows]
    except Exception:  # noqa: BLE001
        return []


@app.get("/sessions")
async def list_sessions(request: Request):
    """Unified conversation list: personas (pinned) + dispatched sub-sessions."""
    _check_auth(request)
    out = []
    for mid, (disp, home) in PERSONAS.items():
        text, ts = _persona_preview(home)
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

CCSESS_CONF = os.path.expanduser("~/.config/ccsess/sessions.conf")
TMUX_BIN = "/opt/homebrew/bin/tmux" if os.path.exists("/opt/homebrew/bin/tmux") else "tmux"


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


async def _tmux_alive(name: str) -> bool:
    try:
        p = await asyncio.create_subprocess_exec(
            TMUX_BIN, "has-session", "-t", name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return (await p.wait()) == 0
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
            # Mid-turn? Capture the pane and look for the working spinner — so the
            # home list can animate a running CC session (parity with Codex).
            try:
                p = await asyncio.create_subprocess_exec(
                    TMUX_BIN, "capture-pane", "-p", "-t", name,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                paneb, _ = await p.communicate()
                pane = (paneb or b"").decode("utf-8", "replace")
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
                cmd = (inp.get("command") or inp.get("file_path") or inp.get("path")
                       or inp.get("pattern") or "")
                if not cmd and isinstance(inp, dict):
                    cmd = next((str(v) for v in inp.values() if isinstance(v, (str, int))), "")
                cmd = str(cmd).splitlines()[0][:140] if cmd else ""
                out.append(f"\n› 🔧 **{name}**" + (f" `{cmd}`" if cmd else "") + "\n")
        return "\n".join(out)
    return ""


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
        raise HTTPException(status_code=404, detail="unknown session")
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
    out, err = await p.communicate()
    if p.returncode != 0:
        detail = (err or out or b"ccsess failed").decode("utf-8", "replace")[:300]
        raise HTTPException(status_code=502, detail=detail)
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
            p = await asyncio.create_subprocess_exec(
                TMUX_BIN, "capture-pane", "-p", "-t", name,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await p.communicate()
            pane = (out or b"").decode("utf-8", "replace").lower()
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
        raise HTTPException(status_code=404, detail="unknown session")
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
        while True:
            if await request.is_disconnected():
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
                    idle = 0
            idle += 1
            if idle >= 4:                         # ~4s quiet → keepalive comment.
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
        raise HTTPException(status_code=404, detail="unknown session")
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
    p = await asyncio.create_subprocess_exec(
        TMUX_BIN, "new-session", "-d", "-s", name, "-c", cwd,
        CLAUDE_BIN, "--resume", sid,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await p.communicate()
    if p.returncode != 0:
        raise HTTPException(status_code=502,
                            detail=(err or b"tmux new-session failed").decode("utf-8", "replace")[:200])
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
        raise HTTPException(status_code=404, detail="unknown session")
    body = await request.json()
    text = (body.get("text") or "").strip()
    # Relay layer (like the persona attachment path): persist any attachments and
    # inject their on-disk paths into the typed line. Claude Code can Read files
    # (and sees images natively), so a bare path is enough — no vision pre-pass.
    # Audio attachments are transcribed (voice message → typed command).
    saved = []
    voice_lines = []
    for a in (body.get("attachments") or []):
        path = _save_data_uri(a.get("data", ""), a.get("filename", "file"))
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
    if not await _tmux_alive(name):
        raise HTTPException(status_code=409, detail="session not running")
    target = name

    async def _tmux(*args):
        p = await asyncio.create_subprocess_exec(
            TMUX_BIN, *args,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await p.communicate()
        return p.returncode, (err or b"").decode("utf-8", "replace").strip()

    # Deliver via tmux bracketed paste (set-buffer → paste-buffer -p) instead of
    # `send-keys -l`. This is how Claude Code receives a pasted prompt: the whole
    # block (incl. multi-line text + an image path) lands as ONE input, so a
    # newline no longer submits the command half-typed — the old 502 cause.
    buf = "pa-" + uuid.uuid4().hex[:8]
    try:
        await _tmux("send-keys", "-t", target, "C-u")          # clear residual input
        rc_set, e_set = await _tmux("set-buffer", "-b", buf, text)
        rc_paste, e_paste = await _tmux("paste-buffer", "-t", target, "-b", buf, "-p", "-d")
        await asyncio.sleep(0.25)                               # let the editor settle
        rc_enter, e_enter = await _tmux("send-keys", "-t", target, "Enter")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))
    if rc_set or rc_paste or rc_enter:                         # don't false-report success
        detail = (e_set or e_paste or e_enter or "tmux paste failed")[:200]
        raise HTTPException(status_code=502, detail=detail)
    return {"ok": True}


async def _cc_paste_text(name: str, text: str) -> None:
    if not await _tmux_alive(name):
        raise HTTPException(status_code=409, detail="session not running")

    async def _tmux(*args):
        p = await asyncio.create_subprocess_exec(
            TMUX_BIN, *args,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await p.communicate()
        return p.returncode, (err or b"").decode("utf-8", "replace").strip()

    buf = "pa-" + uuid.uuid4().hex[:8]
    await _tmux("send-keys", "-t", name, "C-u")
    rc_set, e_set = await _tmux("set-buffer", "-b", buf, text)
    rc_paste, e_paste = await _tmux("paste-buffer", "-t", name, "-b", buf, "-p", "-d")
    await asyncio.sleep(0.25)
    rc_enter, e_enter = await _tmux("send-keys", "-t", name, "Enter")
    if rc_set or rc_paste or rc_enter:
        detail = (e_set or e_paste or e_enter or "tmux paste failed")[:200]
        raise HTTPException(status_code=502, detail=detail)


# CC interrupt + busy status (parity with Codex's stop/active). The app uses
# these to offer a stop button and to detect a running turn reliably instead of
# guessing from stream silence (which mis-fires on long, quiet commands).
@app.post("/ccsessions/{name}/interrupt")
async def cc_session_interrupt(name: str, request: Request):
    """Send Escape to the live TUI — same as pressing Esc to interrupt."""
    _check_auth(request)
    if not any(r[0] == name for r in _cc_conf_rows()):
        raise HTTPException(status_code=404, detail="unknown session")
    if not await _tmux_alive(name):
        raise HTTPException(status_code=409, detail="session not running")
    p = await asyncio.create_subprocess_exec(
        TMUX_BIN, "send-keys", "-t", name, "Escape",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await p.communicate()
    if p.returncode:
        raise HTTPException(status_code=502,
            detail=(err or b"").decode("utf-8", "replace")[:200] or "interrupt failed")
    return {"ok": True}


# Claude Code's TUI shows a working spinner like "· Fermenting… (1m 51s · ↓ 6.5k
# tokens)" while a turn runs — capture the pane and look for it. Covers long,
# silent commands (the spinner stays up), which a stream-silence heuristic misses.
_CC_BUSY_RE = re.compile(r"\((?:\d+m\s*)?\d+(?:\.\d+)?s\s*·.*tokens", re.IGNORECASE)
_CC_OPT_NUM_RE = re.compile(r"^(\d+)[.)]\s+(.{1,60})$")
_CC_OPT_LABEL_RE = re.compile(r"^(allow once|always allow|don.t allow|allow|deny|yes,|yes\b|no,|no\b)", re.IGNORECASE)


def _cc_prompt(pane: str):
    """Detect a Claude Code interactive choice prompt (permission / yes-no) so the
    app can render real buttons. Returns {kind,title,options:[{key,label}]} or None.
    STRICT: only the bottom of the pane (the active prompt box) AND a permission
    context (wants to / do you want / proceed) — so transcript numbered lists never
    false-trigger. Never when working."""
    low = pane.lower()
    if "esc to interrupt" in low or _CC_BUSY_RE.search(pane):
        return None
    tail = pane.splitlines()[-16:]                  # the prompt always sits at the bottom
    tail_low = "\n".join(tail).lower()
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


@app.get("/ccsessions/{name}/status")
async def cc_session_status(name: str, request: Request):
    _check_auth(request)
    if not any(r[0] == name for r in _cc_conf_rows()):
        raise HTTPException(status_code=404, detail="unknown session")
    if not await _tmux_alive(name):
        return {"busy": False, "running": False}
    p = await asyncio.create_subprocess_exec(
        TMUX_BIN, "capture-pane", "-p", "-t", name,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await p.communicate()
    pane = (out or b"").decode("utf-8", "replace")
    busy = bool(_CC_BUSY_RE.search(pane)) or ("esc to interrupt" in pane.lower())
    low = pane.lower()
    if "plan mode on" in low:
        mode = "plan"
    elif "auto mode on" in low or "accept edits on" in low or "bypass" in low:
        mode = "auto"
    else:
        mode = "normal"
    return {"busy": busy, "running": True, "mode": mode, "prompt": _cc_prompt(pane)}


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
        raise HTTPException(status_code=404, detail="unknown session")
    if not await _tmux_alive(name):
        raise HTTPException(status_code=409, detail="session not running")
    body = await request.json()
    raw = str(body.get("key") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="key required")
    args = ["send-keys", "-t", name]
    mapped = _CC_KEYS.get(raw.lower())
    if mapped:
        args.append(mapped)                  # named control key
    elif len(raw) == 1 and raw.isprintable():
        args += ["-l", raw]                  # literal single char (y / n / 1-3)
    else:
        raise HTTPException(status_code=400, detail="unsupported key")
    p = await asyncio.create_subprocess_exec(
        TMUX_BIN, *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await p.communicate()
    if p.returncode:
        raise HTTPException(status_code=502,
            detail=(err or b"").decode("utf-8", "replace")[:200] or "send-keys failed")
    return {"ok": True}


# ───────────────────────── /app/v2 control-plane facade ─────────────────────
# Additive: aggregates claude_code / codex / hermes into one Session shape
# (docs/CONTROL_PLANE_V2.md). CC sessions awaiting a permission prompt surface as
# status=waiting_approval so the app can list them. v1/ccsessions/codexsessions
# stay untouched.

async def _v2_cc_state(name: str):
    if not await _tmux_alive(name):
        return ("failed", None)
    p = await asyncio.create_subprocess_exec(
        TMUX_BIN, "capture-pane", "-p", "-t", name,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await p.communicate()
    pane = (out or b"").decode("utf-8", "replace")
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
            active = bool(s.get("activeTurn")) or s.get("status") in ("active", "running")
            out.append({"id": f"codex:{s.get('thread_id') or s.get('id')}", "provider": "codex",
                        "title": s.get("name") or "codex", "subtitle": s.get("workdir"),
                        "status": "running" if active else "idle", "last_event_at": None,
                        "capabilities": ["input", "interrupt", "attachments", "replay", "follow"], "meta": {}})
    except Exception:  # noqa: BLE001
        pass
    if provider:
        out = [s for s in out if s["provider"] == provider]
    if status:
        out = [s for s in out if s["status"] == status]
    return {"sessions": out}


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

@app.get("/capabilities")
async def capabilities(request: Request):
    _check_auth(request)
    return {"api": "app/v1",
            "features": ["canonical_messages", "reports", "notifications",
                         "approvals", "cc_sessions", "attachments", "vision",
                         "message_dry_run", "message_interrupt", "apns_push", "accounts",
                         "apple_auth", "account_pairing",
                         "delegations", "control_plane_v2"],
            "endpoints": ["/app/v1/sessions", "/app/v1/messages", "/reports",
                          "/app/v1/messages/interrupt",
                          "/cron/jobs", "/ccsessions", "/app/v1/approvals",
                          "/app/v1/devices", "/app/v1/push/test",
                          "/app/v1/auth/apple", "/app/v1/account",
                          "/app/v1/pair/new", "/app/v1/pair/claim",
                          "/app/v1/devices/{id}/revoke",
                          "/app/v1/delegations", "/app/v2/sessions"]}


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
            path = _save_data_uri(a.get("data", ""), a.get("filename", "file"))
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
            path = _save_data_uri(a.get("data", ""), a.get("filename", "file"))
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
        raise HTTPException(status_code=400, detail="unknown session")
    out = _canon_messages(session, limit)
    _, home = PERSONAS[session]
    for m in _persona_history(home, limit):
        out.append({"id": f"tg-{m['ts']}", "role": m["role"], "content": m["content"],
                    "attachments": [], "ts": m["ts"], "status": "done", "source": "telegram"})
    # Surface each persona's daily briefs (cron-delivered) IN its conversation,
    # like Telegram does — not only in the separate Reports tab. 袁方's 晨報/午報
    # etc. and 潘天晴's 編輯台晨報 (+ future 今日精選/限動) read from each persona's
    # OWN home, so the app thread matches what TG received this morning.
    _sync_persona_reports(session, 50)
    out.extend(_report_messages(session, limit))
    out.sort(key=lambda m: m.get("ts") or 0)
    return {"messages": out[-limit:]}


@app.post("/app/v1/messages")
async def app_post_message(request: Request):
    """Send a turn: record the user message canonically, run the persona turn,
    stream the reply (OpenAI-style SSE), and record the reply canonically too."""
    _check_auth(request)
    body = await request.json()
    session = body.get("session") or "xcash"
    if session not in PERSONAS:
        raise HTTPException(status_code=400, detail="unknown session")
    content = (body.get("content") or "").strip()
    attachments = body.get("attachments") or []   # [{kind,filename,mime,data(dataURI)}]
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

    # Voice messages: transcribe any audio attachment and fold the transcript
    # into the turn text. The audio still rides along as an attachment so the
    # conversation shows the voice bubble; the model gets the words.
    voice_text = await _transcribe_attachments(attachments)
    if voice_text:
        content = (content + "\n" + voice_text).strip() if content else voice_text

    parts = []
    if content:
        parts.append({"type": "text", "text": content})
    for a in attachments:
        if a.get("kind") == "image":
            parts.append({"type": "image_url", "image_url": {"url": a.get("data")}})
        elif a.get("kind") == "audio":
            continue                       # transcript already in `content`
        else:
            parts.append({"type": "file", "file": {"filename": a.get("filename"),
                          "mime_type": a.get("mime"), "file_data": a.get("data")}})
    prompt = await _resolve_persona_prompt([{"role": "user", "content": parts or content}])
    report_context = _report_context_for_prompt(session, content)
    if report_context:
        prompt = f"{report_context}\n\n---\n【使用者現在的訊息】\n{prompt}"

    acp_session = await POOL.get(session, home_for(session))
    queued_at_accept = acp_session.is_busy()

    att_meta = [{"kind": a.get("kind"), "filename": a.get("filename"), "mime": a.get("mime")}
                for a in attachments]
    # Record the transcript as the canonical text (so other devices see what was
    # said even without the audio bytes), tagged so the app can show 🎤.
    _user_mid, canonical_user_ok = _canon_add(session, "user", content, att_meta, client_id=client_id)

    async def agen():
        _log_event("app_turn_model_start", **common_log,
                   prompt_chars=len(prompt), canonical_user_ok=canonical_user_ok)
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
            try:
                async for k, v in _persona_content_stream(session, prompt):
                    if k == "content":
                        state["acc"] += v
                        state["content_chunks"] += 1
                    elif k == "usage":
                        state["usage"] = v
                    elif k == "status":
                        pass
                    await q.put((k, v))
            except Exception as e:  # noqa: BLE001
                state["runner_error"] = f"{type(e).__name__}: {str(e)[:180]}"
                await q.put(("error", str(e)))
            finally:
                if state["acc"]:
                    _reply_mid, reply_ok = _canon_add(session, "assistant", state["acc"],
                                                      client_id=client_id)
                    state["canonical_reply_ok"] = reply_ok
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

        task = asyncio.create_task(run_turn())
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)

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
        raise HTTPException(status_code=400, detail="unknown session")
    acp_session = await POOL.get(session, home_for(session))
    if not acp_session.is_busy():
        raise HTTPException(status_code=409, detail="no active turn")
    await acp_session.cancel()
    return {"ok": True, "session": session}


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


@app.post("/app/v1/approvals")
async def approval_create(request: Request):
    """Create a pending approval (called by Hermes / a skill)."""
    _check_auth(request)
    import sqlite3
    b = await request.json()
    aid = b.get("id") or uuid.uuid4().hex
    ttl = b.get("ttl_seconds")
    now = time.time()
    con = sqlite3.connect(CANON_DB)
    con.execute("INSERT OR REPLACE INTO approvals"
                "(id,title,source,risk,detail,created_at,expires_at,status,decided_at,result) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (aid, b.get("title") or "需要核准", b.get("source") or "", b.get("risk") or "",
                 b.get("detail") or "", now, (now + ttl) if ttl else None, "pending", None, None))
    con.commit()
    con.close()
    title = b.get("title") or "需要核准"
    body = (b.get("detail") or b.get("source") or "點開查看並決定")[:120]
    asyncio.create_task(push_notify(f"🔐 {title}", body,
                                    {"kind": "approval", "id": aid}))
    return {"id": aid, "status": "pending"}


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
async def approval_list(request: Request, status: str = "", limit: int = 50):
    _check_auth(request)
    import sqlite3
    con = sqlite3.connect(CANON_DB)
    _approvals_expire(con)
    con.commit()
    if status:
        rows = con.execute("SELECT id,title,source,risk,detail,created_at,expires_at,status,decided_at,result "
                           "FROM approvals WHERE status=? ORDER BY created_at DESC LIMIT ?",
                           (status, limit)).fetchall()
    else:
        rows = con.execute("SELECT id,title,source,risk,detail,created_at,expires_at,status,decided_at,result "
                           "FROM approvals ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return {"approvals": [_approval_row(r) for r in rows]}


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
        raise HTTPException(status_code=404, detail="unknown approval")
    return _approval_row(r)


@app.post("/app/v1/approvals/{aid}/decision")
async def approval_decide(aid: str, request: Request):
    """Approve / reject (from the app)."""
    _check_auth(request)
    import sqlite3
    b = await request.json()
    decision = "approved" if b.get("approve") else "rejected"
    con = sqlite3.connect(CANON_DB)
    cur = con.execute("UPDATE approvals SET status=?, decided_at=?, result=? "
                      "WHERE id=? AND status='pending'",
                      (decision, time.time(), b.get("result") or "", aid))
    con.commit()
    changed = cur.rowcount
    con.close()
    if not changed:
        raise HTTPException(status_code=409, detail="already decided or expired")
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
        raise HTTPException(status_code=400, detail="task required")
    sid = "sub-" + uuid.uuid4().hex[:16]
    SUBSESSIONS[sid] = {"name": task[:40], "parent": parent, "tool": tool,
                        "status": "running", "lastAt": time.time(), "cwd": cwd,
                        "proc": None, "output": [("text", f"**任務:** {task}\n\n")]}
    asyncio.create_task(_run_dispatch(sid, tool, task, cwd, isolate))
    return {"session_id": sid, "type": "subprocess", "tool": tool, "parent": parent}


async def _make_worktree(base: str, sid: str):
    """Isolate a worker in its own git worktree (like a branch) so parallel
    dispatches don't clobber each other's edits. Returns the worktree path, or
    the original base if it isn't a git repo / the command fails."""
    try:
        chk = await asyncio.create_subprocess_exec(
            "git", "-C", base, "rev-parse", "--show-toplevel",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await chk.communicate()
        if chk.returncode != 0:
            return base
        top = out.decode().strip() or base
        wt = os.path.expanduser(f"~/.pocket/worktrees/{sid}")
        os.makedirs(os.path.dirname(wt), exist_ok=True)
        add = await asyncio.create_subprocess_exec(
            "git", "-C", top, "worktree", "add", "-b", f"pocket/{sid}", wt, "HEAD",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await add.communicate()
        return wt if add.returncode == 0 and os.path.isdir(wt) else base
    except Exception:  # noqa: BLE001
        return base


def _fmt_item(kind, val):
    """Format one transcript item (text/tool/result/perm) → SSE content string."""
    if kind == "text":
        return val
    if kind == "tool_start":
        name = val.get("name", "tool")
        cmd = (val.get("cmd") or "").strip().splitlines()
        cmd1 = (cmd[0] if cmd else "")[:140]
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
    return {"ok": True, "personas": list(PERSONAS), "subsessions": len(SUBSESSIONS)}
