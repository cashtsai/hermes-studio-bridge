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
import json
import os
import time
import uuid

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from acp_client import ACPPool

# Persistent warm ACP process per persona — removes the ~5s `hermes -z`
# cold start per message and streams output live. Cold `hermes -z` stays as a
# fallback if ACP ever fails.
POOL = ACPPool()

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


def _last_user_message(messages: list) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):  # OpenAI vision-style content parts
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return (c or "").strip()
    return ""


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    _check_auth(request)
    body = await request.json()
    model = body.get("model", "xcash")
    if model not in PERSONAS:
        model = "xcash"
    prompt = _last_user_message(body.get("messages", []))
    stream = bool(body.get("stream", False))
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())

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
            if not prompt:
                yield chunk({"content": "(沒有收到訊息)"})
                yield chunk({}, finish="stop")
                yield "data: [DONE]\n\n"
                return

            q: asyncio.Queue = asyncio.Queue()

            async def pump():
                try:
                    session = await POOL.get(model, home_for(model))
                    async for kind, val in session.prompt_stream(prompt):
                        await q.put((kind, val))            # ("text", …) / ("tool", name)
                except Exception as e:                      # noqa: BLE001
                    await q.put(("error", str(e)))
                finally:
                    await q.put(("end", None))

            asyncio.create_task(pump())
            got_text = False
            thought_buf: list[str] = []

            def flush_thought():
                if thought_buf:
                    t = "".join(thought_buf).strip()
                    thought_buf.clear()
                    if t:
                        return f"\n<details><summary>💭 思考</summary>\n\n{t}\n\n</details>\n\n"
                return None

            while True:
                try:
                    kind, val = await asyncio.wait_for(q.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if kind == "text":
                    if not got_text:                         # surface buffered thinking first
                        ft = flush_thought()
                        if ft:
                            yield chunk({"content": ft})
                    got_text = True
                    yield chunk({"content": val})
                elif kind == "thought":
                    thought_buf.append(val)                   # buffered, shown before the answer
                elif kind == "tool_start":
                    name = val.get("name", "tool")
                    cmd = (val.get("cmd") or "").strip().splitlines()
                    cmd1 = (cmd[0] if cmd else "")[:140]
                    line = f"\n› 🔧 **{name}**" + (f" `{cmd1}`" if cmd1 else "") + "\n"
                    yield chunk({"content": line})
                elif kind == "tool_result":
                    res = (val.get("text") or "").strip()
                    if res:
                        short = res[:900]
                        more = "\n…(截斷)" if len(res) > 900 else ""
                        yield chunk({"content":
                            f"<details><summary>↳ 結果</summary>\n\n```\n{short}{more}\n```\n\n</details>\n"})
                elif kind == "usage":
                    pass                                     # Phase 2: status line
                elif kind == "error":
                    if not got_text:                         # ACP failed cold → fall back
                        try:
                            yield chunk({"content": await run_hermes(model, prompt)})
                        except Exception as e2:              # noqa: BLE001
                            yield chunk({"content": f"⚠️ {e2}"})
                    else:
                        yield chunk({"content": f"\n\n⚠️ 串流中斷:{val}"})
                else:  # end
                    break
            ft = flush_thought()                             # thinking but no answer text
            if ft:
                yield chunk({"content": ft})
            yield chunk({}, finish="stop")
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


@app.get("/health")
async def health():
    return {"ok": True, "personas": list(PERSONAS)}
