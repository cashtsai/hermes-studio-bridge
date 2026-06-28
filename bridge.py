#!/usr/bin/env python3
"""OpenAI-compatible bridge → Hermes agent (per-persona, shared memory).

Open WebUI (or any OpenAI-compatible client) points its API base at this
server. Each Hermes persona is exposed as a "model"; a chat completion runs
`hermes -z <last user msg> --continue <persona-session>` with the persona's
HERMES_HOME, so the reply comes from that persona WITH its shared long-term
memory (the same MEMORY.md / state.db the Telegram gateway uses).

Run:  uvicorn bridge:app --host 0.0.0.0 --port 8081
"""
import asyncio
import base64
import glob
import json
import os
import re
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from acp_client import ACPPool

# Persistent warm ACP process per persona — removes the ~5s `hermes -z`
# cold start per message and streams output live. Cold `hermes -z` stays as a
# fallback if ACP ever fails.
POOL = ACPPool()

# M2/M3 — registry of dispatched CC/Codex sub-sessions, surfaced in GET /sessions
# and continuable like a persona. Keyed by an opaque session id.
SUBSESSIONS: dict = {}

# Bearer token gate. The bridge fronts a tool-executing agent, so it must not
# be an open control surface even on the tailnet. Open WebUI sends this as its
# OpenAI API key. Override via the BRIDGE_TOKEN env var.
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "CHANGE-ME")  # real value injected via LaunchAgent env


def _check_auth(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if token != BRIDGE_TOKEN:
        raise HTTPException(status_code=401, detail="invalid bridge token")

HERMES_BIN = "/Users/xcash/apps/hermes-agent/runtime/venv/bin/hermes"
HOME_ROOT = "/Users/xcash/apps/hermes-agent/home"

# model id -> (display name, HERMES_HOME). id stays ascii for client URLs.
PERSONAS = {
    "yuanfang":    ("袁方 (幕僚長/main)", HOME_ROOT),
    "pantianqing": ("潘天晴 (FLiPER)",    f"{HOME_ROOT}/profiles/fliper"),
    "xcash":       ("XCash (善彰)",       f"{HOME_ROOT}/profiles/xcash"),
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
    proc = await asyncio.create_subprocess_exec(
        HERMES_BIN, "-z", prompt, "--continue", session_name(model),
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
             "image/webp": ".webp", "image/heic": ".heic", "application/pdf": ".pdf"}


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


def _canon_init():
    import sqlite3
    os.makedirs(os.path.dirname(CANON_DB), exist_ok=True)
    con = sqlite3.connect(CANON_DB)
    con.execute("""CREATE TABLE IF NOT EXISTS messages(
        id TEXT PRIMARY KEY, session TEXT NOT NULL, role TEXT NOT NULL,
        content TEXT, attachments TEXT, created_at REAL NOT NULL, status TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_msg_session_time ON messages(session, created_at)")
    con.execute("""CREATE TABLE IF NOT EXISTS approvals(
        id TEXT PRIMARY KEY, title TEXT, source TEXT, risk TEXT, detail TEXT,
        created_at REAL, expires_at REAL, status TEXT, decided_at REAL, result TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS devices(
        token TEXT PRIMARY KEY, platform TEXT, created_at REAL)""")
    con.commit()
    con.close()


def _canon_add(session: str, role: str, content: str, attachments=None,
               mid: str | None = None, status: str = "done") -> str:
    import sqlite3
    mid = mid or uuid.uuid4().hex
    try:
        con = sqlite3.connect(CANON_DB)
        con.execute("INSERT OR REPLACE INTO messages"
                    "(id,session,role,content,attachments,created_at,status) VALUES(?,?,?,?,?,?,?)",
                    (mid, session, role, content, json.dumps(attachments or [], ensure_ascii=False),
                     time.time(), status))
        con.commit()
        con.close()
    except Exception:  # noqa: BLE001
        pass
    return mid


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


# ───────────────────────── APNs push (M23) ─────────────────────────────────
# Token-based (.p8) auth. The key lives UNDER Hermes management:
#   ~/apps/hermes-agent/home/credentials/AuthKey_86FF9D976T.p8  (chmod 600)
# See docs/HANDOFF_CREDENTIALS.md for the rotation procedure / inventory.
APNS_KEY_PATH = os.path.expanduser(
    "~/apps/hermes-agent/home/credentials/AuthKey_86FF9D976T.p8")
APNS_KEY_ID = "86FF9D976T"
APNS_TEAM_ID = "4F8B93R3SH"
APNS_BUNDLE_ID = "com.scarfgo.app.4F8B93R3SH"
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


async def _run_dispatch(sid: str, tool: str, task: str, cwd: str):
    """Spawn a headless Claude Code / Codex sub-agent for the initial task."""
    sub = SUBSESSIONS[sid]
    if tool == "codex":
        argv = [CODEX_BIN, "exec", "--json", task]
    else:
        argv = _claude_argv(sub.get("parent", "yuanfang"), task)
    await _stream_agent(sid, argv, cwd, "dispatch 失敗")


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
            TMUX_BIN, "has-session", "-t", "=" + name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return (await p.wait()) == 0
    except Exception:  # noqa: BLE001
        return False


async def _cc_sessions():
    out = []
    for name, workdir, enabled in _cc_conf_rows():
        if enabled != "1":
            continue
        out.append({"name": name, "workdir": workdir,
                    "status": "running" if await _tmux_alive(name) else "down"})
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
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().strftime("%m/%d %H:%M")
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
async def cc_list(request: Request):
    _check_auth(request)
    return {"sessions": await _cc_sessions()}


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
            for line in lines[-replay:]:
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
            if idle >= 15:                        # ~15s quiet → keepalive comment
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
    saved = []
    for a in (body.get("attachments") or []):
        path = _save_data_uri(a.get("data", ""), a.get("filename", "file"))
        if path:
            saved.append(path)
    if saved:
        text = (text + "\n\n[附件已存到本機,請用 Read 讀取]\n"
                + "\n".join(f"- {p}" for p in saved)).strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty")
    if not await _tmux_alive(name):
        raise HTTPException(status_code=409, detail="session not running")
    target = "=" + name

    async def _sk(*args):
        p = await asyncio.create_subprocess_exec(
            TMUX_BIN, "send-keys", "-t", target, *args,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await p.communicate()
        return p.returncode, (err or b"").decode("utf-8", "replace").strip()

    try:
        await _sk("C-u")                                   # clear any residual input
        rc1, err1 = await _sk("-l", text)                  # type the line literally
        await asyncio.sleep(0.2)
        rc2, err2 = await _sk("Enter")                     # submit
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))
    if rc1 != 0 or rc2 != 0:                               # don't false-report success
        raise HTTPException(status_code=502, detail=(err1 or err2 or "send-keys failed")[:200])
    return {"ok": True}


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


def _clean_report(s: str) -> str:
    """Trim a leading English working-note preamble some cron runs leak before
    the actual brief (the SKILL says not to emit it, but it sneaks in)."""
    m = _REPORT_START.search(s)
    if m and 0 < m.start() < 600:
        return s[m.start():].strip()
    # Fallback (e.g. 午報): drop leading non-CJK working-note lines before the
    # first line that actually contains Chinese.
    lines = s.split("\n")
    for i, line in enumerate(lines):
        if any("一" <= c <= "鿿" for c in line):
            return "\n".join(lines[i:]).strip() if i > 0 else s.strip()
    return s.strip()


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
                         "message_dry_run", "apns_push"],
            "endpoints": ["/app/v1/sessions", "/app/v1/messages", "/reports",
                          "/cron/jobs", "/ccsessions", "/app/v1/approvals",
                          "/app/v1/devices", "/app/v1/push/test"]}


@app.get("/app/v1/sessions")
async def app_sessions(request: Request):
    return await list_sessions(request)


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
    # 袁方 owns the daily briefs (cron) — surface them IN 袁方's conversation, like
    # Telegram does, not only in the separate Reports tab.
    if session == "yuanfang":
        for r in _reports(20):
            out.append({"id": f"rep-{r['ts']}", "role": "assistant",
                        "content": f"📰 **{r['label']}**\n\n{r['content']}",
                        "attachments": [], "ts": r["ts"], "status": "done", "source": "report"})
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

    cid = "appmsg-" + uuid.uuid4().hex[:20]
    created = int(time.time())

    def chunk(delta, finish=None):
        payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                   "model": session, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    if dry_run:
        async def dry_agen():
            yield chunk({"role": "assistant", "content": ""})
            text = f"✅ dry-run ok: {session} message path is reachable; nothing was persisted."
            yield chunk({"content": text})
            payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                       "model": session,
                       "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(dry_agen(), media_type="text/event-stream")

    parts = []
    if content:
        parts.append({"type": "text", "text": content})
    for a in attachments:
        if a.get("kind") == "image":
            parts.append({"type": "image_url", "image_url": {"url": a.get("data")}})
        else:
            parts.append({"type": "file", "file": {"filename": a.get("filename"),
                          "mime_type": a.get("mime"), "file_data": a.get("data")}})
    prompt = await _resolve_persona_prompt([{"role": "user", "content": parts or content}])

    att_meta = [{"kind": a.get("kind"), "filename": a.get("filename"), "mime": a.get("mime")}
                for a in attachments]
    _canon_add(session, "user", content, att_meta)

    async def agen():
        yield chunk({"role": "assistant", "content": ""})
        acc = ""
        last_usage = None
        async for k, v in _persona_content_stream(session, prompt):
            if k == "content":
                acc += v
                yield chunk({"content": v})
            elif k == "keepalive":
                yield ": keepalive\n\n"
            elif k == "usage":
                last_usage = v
        _canon_add(session, "assistant", acc)
        final = {"index": 0, "delta": {}, "finish_reason": "stop"}
        payload = {"id": cid, "object": "chat.completion.chunk", "created": created,
                   "model": session, "choices": [final]}
        if last_usage and last_usage.get("size"):
            payload["usage"] = {"context_used": last_usage.get("used"),
                                "context_size": last_usage.get("size")}
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(agen(), media_type="text/event-stream")


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
    cwd = body.get("cwd") or HOME_ROOT
    parent = body.get("parent", "yuanfang")
    if not task:
        raise HTTPException(status_code=400, detail="task required")
    sid = "sub-" + uuid.uuid4().hex[:16]
    SUBSESSIONS[sid] = {"name": task[:40], "parent": parent, "tool": tool,
                        "status": "running", "lastAt": time.time(), "cwd": cwd,
                        "proc": None, "output": [("text", f"**任務:** {task}\n\n")]}
    asyncio.create_task(_run_dispatch(sid, tool, task, cwd))
    return {"session_id": sid, "type": "subprocess", "tool": tool, "parent": parent}


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
