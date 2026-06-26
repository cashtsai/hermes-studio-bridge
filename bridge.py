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
        # Heartbeat streaming: Hermes turns are agentic and can run 60–90s.
        # A plain blocking response with no bytes for that long gets the
        # client connection dropped ("network connection lost"). We run the
        # turn in the background and emit SSE keepalive comments every ~2s so
        # the connection stays alive, then push the full reply. (Real
        # token-by-token streaming + tool progress = ACP, a later upgrade.)
        async def gen():
            def chunk(delta, finish=None):
                payload = {"id": cid, "object": "chat.completion.chunk",
                           "created": created, "model": model,
                           "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

            yield chunk({"role": "assistant", "content": ""})  # open the bubble
            if not prompt:
                yield chunk({"content": "(沒有收到訊息)"})
            else:
                task = asyncio.create_task(run_hermes(model, prompt))
                while not task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"   # SSE comment — keeps the socket warm
                yield chunk({"content": task.result()})
            yield chunk({}, finish="stop")
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    content = "(沒有收到訊息)" if not prompt else await run_hermes(model, prompt)
    return JSONResponse({
        "id": cid, "object": "chat.completion", "created": created, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })


@app.get("/health")
async def health():
    return {"ok": True, "personas": list(PERSONAS)}
