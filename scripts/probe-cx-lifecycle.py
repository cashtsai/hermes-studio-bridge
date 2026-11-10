#!/usr/bin/env python3
"""Record a real isolated Codex test turn's lifecycle on stdout.

No account, live bridge, model server, or tool execution is needed. Stdout is
sanitized JSONL; diagnostics and non-fixture RPC metadata never enter it.
"""
import argparse
import asyncio
from functools import partial
import json
from pathlib import Path
import shutil
import sys
import tempfile


async def fixture_response(reader, writer, *, approval=False):
    """Loopback-only Responses stub; no request contents go into fixture output."""
    try:
        header = await reader.readuntil(b"\r\n\r\n")
        if not header.startswith(b"POST /v1/responses "):
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            return
        body = {}
        for line in header.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                body = json.loads(await reader.readexactly(int(line.split(b":", 1)[1])))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
        item = {"id": "msg_probe", "type": "message", "role": "assistant",
                "status": "in_progress", "content": []}
        response = {"id": "resp_probe", "object": "response", "output": [],
                    "status": "in_progress"}

        def emit(kind, **data):
            packet = json.dumps(dict(type=kind, **data)).encode()
            writer.write(b"event: " + kind.encode() + b"\ndata: " + packet + b"\n\n")

        emit("response.created", response=response)
        if approval and not any(x.get("type") == "function_call_output"
                                for x in body.get("input", [])):
            call = {"type": "function_call", "id": "fc_probe", "call_id": "call_probe",
                    "name": "exec_command", "arguments": json.dumps({
                        "cmd": "true", "sandbox_permissions": "require_escalated",
                        "justification": "Allow the fixture no-op?"})}
            emit("response.output_item.done", output_index=0, item=call)
            response.update(status="completed", output=[call])
            emit("response.completed", response=response)
            await writer.drain()
            return
        emit("response.output_item.added", output_index=0, item=item)
        for delta in ("FIRST", "SECOND"):
            emit("response.output_text.delta", item_id=item["id"],
                 output_index=0, content_index=0, delta=delta)
        item.update(status="completed", content=[{"type": "output_text", "text": "FIRSTSECOND"}])
        emit("response.output_item.done", output_index=0, item=item)
        response.update(status="completed", output=[item])
        emit("response.completed", response=response)
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def record(binary, home, scenario, model_url):
    codex_home = home / "codex"
    codex_home.mkdir(mode=0o700)
    # Only loopback is reachable via the configured model/proxy endpoints.
    sink = "http://127.0.0.1:1"
    env = {"HOME": str(home), "CODEX_HOME": str(codex_home), "PATH": "/usr/bin:/bin",
           "HTTPS_PROXY": sink, "HTTP_PROXY": sink, "ALL_PROXY": sink,
           "NO_PROXY": "127.0.0.1", "TMPDIR": str(home)}
    options = ['model_provider="probe"', 'model_providers.probe.name="probe"',
               f'model_providers.probe.base_url="{model_url}/v1"',
               'model_providers.probe.wire_api="responses"',
               'model_providers.probe.supports_websockets=false',
               'cli_auth_credentials_store="file"', 'analytics.enabled=false']
    proc = await asyncio.create_subprocess_exec(
        binary, "app-server", "--listen", "stdio://",
        *(part for opt in options for part in ("-c", opt)),
        env=env, cwd=home, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        limit=4 * 1024 * 1024)

    async def send(method, params, req_id=None):
        msg = {"method": method, "params": params}
        if req_id is not None:
            msg["id"] = req_id
        proc.stdin.write(json.dumps(msg).encode() + b"\n")
        await proc.stdin.drain()

    async def read():
        line = await proc.stdout.readline()
        if not line:
            raise RuntimeError("isolated app-server exited before the probe finished")
        msg = json.loads(line)
        if "error" in msg:
            raise RuntimeError(f"RPC {msg.get('id')} failed: {msg['error'].get('code')}")
        return msg

    async def result(idx):
        while True:
            msg = await read()
            if msg.get("id") == idx:
                return msg["result"]

    try:
        await send("initialize", {"clientInfo": {"name": "pocket_replay",
                   "title": "Pocket replay probe", "version": "1"}}, 1)
        await result(1)
        await send("initialized", {})
        await send("thread/start", {"cwd": str(home), "model": "probe-model",
                                    "approvalPolicy": "on-request" if scenario == "approval" else "never",
                                    "sandbox": "read-only"}, 2)
        thread = (await result(2))["thread"]["id"]
        await send("turn/start", {"threadId": thread, "input": [
            {"type": "text", "text": "Fixture only.", "text_elements": []}]}, 3)
        frames = []
        started = None
        acknowledged = False
        ended = False
        item_id = None
        deltas = []
        request_id = None
        resolved = False
        waiting = False
        while not ended or (scenario == "interrupt" and not acknowledged):
            msg = await read()
            if msg.get("id") == 4 and "result" in msg:
                acknowledged = True
            method = msg.get("method")
            params = msg.get("params") or {}
            if params.get("threadId") != thread:
                continue
            if scenario == "approval" and method == "thread/status/changed":
                status = params["status"]
                flags = status.get("activeFlags") or []
                if status.get("type") == "active" and (flags == ["waitingOnApproval"]
                                                        or (waiting and not flags)):
                    waiting = bool(flags)
                    frames.append({"method": method, "params": {"threadId": "fixture-thread",
                                  "status": {"type": "active", "activeFlags": flags}}})
                continue
            if scenario == "approval" and method == "item/commandExecution/requestApproval":
                if (request_id is not None or not started or params.get("turnId") != started
                        or msg.get("id") is None or not params.get("itemId")):
                    raise RuntimeError("unexpected approval request")
                request_id = msg["id"]
                offered = params.get("availableDecisions")
                amendment = {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["true"]}}
                if (offered != ["accept", amendment, "cancel"]
                        or params.get("command") not in ("true", "/bin/zsh -lc true", "/bin/bash -lc true")
                        or params.get("reason") != "Allow the fixture no-op?"):
                    raise RuntimeError("changed approval contract; review before recording")
                frames.append({"id": 101, "method": method, "params": {
                    "threadId": "fixture-thread", "turnId": "fixture-turn", "itemId": "fixture-item",
                    "availableDecisions": offered, "command": "true", "reason": params["reason"]}})
                # Never approve or run the command, even in this disposable home.
                proc.stdin.write(json.dumps({"id": request_id, "result": {
                    "decision": "cancel"}}).encode() + b"\n")
                await proc.stdin.drain()
                continue
            if scenario == "approval" and method == "serverRequest/resolved":
                if request_id is None or params.get("requestId") != request_id or resolved:
                    raise RuntimeError("unexpected resolution")
                resolved = True
                frames.append({"method": method, "params": {
                    "threadId": "fixture-thread", "requestId": 101}})
                continue
            if scenario == "stream" and method == "item/agentMessage/delta":
                delta = params["delta"]
                if (delta not in ("FIRST", "SECOND") or params.get("turnId") != started
                        or not params.get("itemId") or item_id not in (None, params["itemId"])):
                    raise RuntimeError("unexpected response from isolated model fixture")
                item_id = params["itemId"]
                deltas.append(delta)
                frames.append({"method": method, "params": {"threadId": "fixture-thread",
                              "turnId": "fixture-turn", "itemId": "fixture-item", "delta": delta}})
                continue
            if scenario == "stream" and method == "item/completed" and item_id:
                item = params["item"]
                if item.get("id") == item_id and item.get("type") == "agentMessage":
                    if item.get("text") != "FIRSTSECOND":
                        raise RuntimeError("unexpected final text")
                    frames.append({"method": method, "params": {"threadId": "fixture-thread",
                                  "turnId": "fixture-turn", "item": {"type": "agentMessage",
                                  "id": "fixture-item", "text": item["text"]}}})
                continue
            if method not in ("turn/started", "turn/completed"):
                continue
            turn = params["turn"]
            if method == "turn/started" and started is None and turn["status"] == "inProgress":
                started = turn["id"]
                if scenario == "interrupt":
                    await send("turn/interrupt", {"threadId": thread, "turnId": started}, 4)
            elif (method == "turn/completed" and started and turn["id"] == started
                  and turn["status"] in (("interrupted", "completed") if scenario == "approval"
                                         else ("interrupted" if scenario == "interrupt" else "completed",))
                  and turn.get("error") is None):
                ended = True
            else:
                raise RuntimeError("unexpected turn lifecycle")
            frames.append({"method": method, "params": {"threadId": "fixture-thread",
                          "turn": {"id": "fixture-turn", "status": turn["status"]}}})
        if scenario == "stream" and (deltas != ["FIRST", "SECOND"] or
                not any(f["method"] == "item/completed" for f in frames)):
            raise RuntimeError("incomplete stream fixture")
        if scenario == "approval" and not resolved:
            raise RuntimeError("provider did not confirm resolution of the cancelled approval")
        return frames
    finally:
        if proc.returncode is None:
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


async def probe(binary, home, scenario):
    if scenario == "interrupt":
        return await record(binary, home, scenario, "http://127.0.0.1:1")
    handler = partial(fixture_response, approval=scenario == "approval")
    async with await asyncio.start_server(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        return await record(binary, home, scenario, f"http://127.0.0.1:{port}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default="codex", help="local Codex CLI executable")
    parser.add_argument("scenario", choices=("interrupt", "stream", "approval"))
    parser.add_argument("--check", action="store_true",
                        help="compare the fresh capture to its committed fixture")
    args = parser.parse_args()
    binary = shutil.which(args.codex)
    if not binary:
        parser.error("Codex CLI not found")
    with tempfile.TemporaryDirectory(prefix="pocket-cx-probe-") as tmp:
        frames = asyncio.run(asyncio.wait_for(probe(binary, Path(tmp), args.scenario), timeout=30))
    recorded = "".join(json.dumps(frame, sort_keys=True, separators=(",", ":")) + "\n"
                       for frame in frames)
    if args.check:
        fixtures = Path(__file__).resolve().parents[1] / "tests/fixtures/replay"
        filename = {"stream": "cx-stream-resume.jsonl", "interrupt": "cx-interrupt.jsonl",
                    "approval": "cx-approval.jsonl"}[args.scenario]
        if recorded.encode() != (fixtures / filename).read_bytes():
            sys.exit(f"{filename}: captured protocol differs from reviewed fixture")
        print(f"{filename}: matched {len(frames)} frames")
    else:
        sys.stdout.write(recorded)


if __name__ == "__main__":
    main()
