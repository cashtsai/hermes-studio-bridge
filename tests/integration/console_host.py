"""Private loopback fixture for console-roundtrip.cjs. No production startup or provider discovery."""
import asyncio
import os
import socket
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


def main():
    root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(root / "tests"), str(root)]
    with tempfile.TemporaryDirectory(prefix="console-host-") as home, ExitStack() as st:
        token = os.environ["CONSOLE_QA_OWNER"]
        live_source = os.environ.get("CONSOLE_QA_CODEX_SOURCE")
        binary = os.environ.get("CONSOLE_QA_CODEX_BIN")
        os.environ.clear()
        os.environ.update(HOME=home, PATH=os.defpath, BRIDGE_TOKEN=token,
                          POCKET_CONSOLE_ENABLED="1", POCKET_CONSOLE_QR_LOGIN="1")
        import _isolation  # noqa: F401
        if live_source:
            if not binary or not os.path.isabs(binary) or not os.path.isabs(live_source):
                raise ValueError("Live testing requires explicit absolute Codex binary and source paths")
            # A new thread store; use the product's supported credential link, never copy auth or user config.
            import codex_home
            cx_home = os.path.join(home, "cx-test")
            codex_home.bootstrap(cx_home, source=live_source, mode="none")
            os.environ.update(CODEX_BIN=binary, CODEX_HOME=cx_home, POCKET_CODEX_ISOLATED="0",
                              CODEX_APP_SERVER_MODE="stdio")
        import bridge as b
        import uvicorn

        def replace(obj, name, val):
            st.enter_context(patch.object(obj, name, val))

        tids = ("claude_code:qa-cc", "codex:qa-cx")
        stores = {sid: b.carddigest.SessionCardStore() for sid in tids}

        def card(sid, cid, role, text):
            stores[sid].upsert_card(b.carddigest.make_card(
                role + cid, cid, role, "text", {"text": text, **({"client_id": cid} if role == "user" else {})}))

        async def cc(name, body):
            assert name == "qa-cc"
            msg, cid = body.get("content") or body["text"], body["client_id"]
            card(tids[0], cid, "user", msg)
            card(tids[0], cid, "agent", "fixture reply: " + msg)
            return {"ok": True}

        async def cx(thread, items, *, client_id):
            assert thread == "qa-cx"
            card(tids[1], client_id, "agent", "fixture reply: " + b._codex_user_input_text(items))

        client = Mock()
        for method in ("pending_approval_for_thread","thread_lock_info","is_active","is_thread_locked"):
            getattr(client, method).return_value = False
        client.start_turn = cx
        replacements = {
            "_DEVICE_TOKENS_PATH": os.path.join(home, "test-devices.json"),
            "PERSONAS": {}, "_registry_call_safe": lambda *a, **k: None,
            "_cc_conf_rows": lambda: [("qa-cc", home, "1")], "_v2_cc_state": AsyncMock(return_value=("idle", {})),
            "_codex_v2_visible_threads": AsyncMock(return_value=[{}]), "_CODEX_V2_STALE": False,
            "_codex_enrich_summary": lambda _: {"thread_id":"qa-cx","name":"qa-cx","workdir":home},
            "_cx_client_for_thread": lambda _: client, "_cc_capture_pane_fresh": AsyncMock(return_value="> "),
            "_cc_input_core_locked": cc, "_cx_feed_input_accepted": lambda tid, cid, msg, *a, **k: card(tids[1], cid, "user", msg),
            "_v2_card_store": AsyncMock(side_effect=lambda sid: stores[sid]),
            "_delegation_v2_sessions": AsyncMock(return_value=[]), "_delegated_codex_thread_ids": lambda: set(),
            "_hermes_pending_by_session": lambda: {}, "_pair_tunnel_url": lambda: None,
        }
        if live_source:
            for name in ("_codex_v2_visible_threads", "_codex_enrich_summary", "_cx_client_for_thread",
                         "_cx_feed_input_accepted", "_v2_card_store", "_CODEX_V2_STALE"):
                replacements.pop(name)
            replacements["_cc_conf_rows"] = lambda: []
        for name, val in replacements.items():
            replace(b, name, val)
        for obj, name in ((b.GEMINI, "configured"), (b.OPENCLAW, "configured"), (b.cc_sdk, "enabled")):
            replace(obj, name, lambda: False)
        async def serve():
            try:
                if live_source:
                    # Never select a pre-existing conversation. Seed only the empty test directory.
                    # Bridge intentionally hides provider jobs under /private/tmp on macOS.
                    wd = st.enter_context(tempfile.TemporaryDirectory(prefix=".console-qa-", dir=root.parent))
                    result = await b.CODEX_APP.call("thread/start", {
                        "cwd": wd, "sandbox":"read-only", "approvalPolicy":"never",
                        "ephemeral":False, "threadSource":"user",
                    }, timeout=25)
                    tid = result["thread"]["id"]
                    await b.CODEX_APP.start_turn(tid, [{"type":"text","text":"Reply only READY. No tools."}], cwd=wd)
                    for _ in range(90):
                        if not b.CODEX_APP.is_active(tid):
                            break
                        await asyncio.sleep(0.5)
                    else:
                        raise TimeoutError("The seed turn did not finish")
                    print("THREAD", tid, flush=True)
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    print("PORT", sock.getsockname()[1], flush=True)
                    server = uvicorn.Server(uvicorn.Config(b.app, lifespan="off", access_log=False,
                                            proxy_headers=False, timeout_graceful_shutdown=2, log_level="error"))
                    await server.serve(sockets=[sock])
            finally:
                if live_source:
                    await b.CODEX_APP.close()
        asyncio.run(serve())


if __name__ == "__main__":
    main()
