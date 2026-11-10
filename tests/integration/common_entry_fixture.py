"""Loopback ASGI fixture. No lifespan hooks, provider sessions or external accounts."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _isolation  # noqa: F401
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import json
import os
import socket
import tempfile
from unittest.mock import patch

import httpx
import uvicorn
from fastapi import Request
from fastapi.responses import JSONResponse
from pocket_id.app import create_app
from pocket_id.config import Config
from test_console_relay_qr import RelayQRTest, bridge


def main():
    test = RelayQRTest()
    test.setUp()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        common = "https://console.pocketone.app"
        pid = create_app(Config(db_path=root/"id.db",state_dir=root,host="127.0.0.1",port=0,
                                console_entry_origin=common,relay_domain="relay.example"))
        db = pid.state.db
        account = db.upsert_account("apple","fixture",None)
        host, secret = db.create_host(account,"Fixture","test",[],{})
        db.upsert_entitlement(account,status="active")
        db.create_relay_grant(account,host,"owner")
        transport = httpx.ASGITransport(app=pid)
        async def select_api(path, body):
            async with httpx.AsyncClient(transport=transport,base_url="https://id.example") as c:
                r = await c.post(path,json=body)
                r.raise_for_status()
                return r.json()

        async def dispatch(scope, receive, send):
            scope = dict(scope,scheme="https")
            headers = dict(scope["headers"])
            domain = headers.get(b"host",b"").decode()
            if domain == "console.pocketone.app":
                return await pid(scope,receive,send)
            if domain == test.relay:
                return await bridge.app(scope,receive,send)
            if scope["method"]=="POST" and headers.get(b"x-qa")==os.environ["ENTRY_QA_KEY"].encode():
                body=await Request(scope,receive).json()
                path={"/scan":"/app/v1/console/entry/select","/lookup":"/app/v1/console/entry/lookup",
                      "/approve":"/app/v1/console/login/requests/"+str(body.get("rid"))+"/approve"}.get(scope["path"])
                if path:
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app),
                                                  base_url="https://phone.example",headers=test.phone) as phone:
                        r=await phone.post(path,json=body)
                    return await JSONResponse(r.json(),status_code=r.status_code)(scope,receive,send)
            return await JSONResponse({},status_code=404)(scope,receive,send)

        with patch.object(bridge,"POCKET_ID_URL","https://id.example"), patch.object(bridge,"_pocket_id_state",
                return_value={"url":"https://id.example","host_id":host,"host_secret":secret}), \
                patch.object(bridge,"_pocket_id_api",select_api), socket.socket() as sock:
            sock.bind(("127.0.0.1",0))
            print(json.dumps({"port":sock.getsockname()[1]}),flush=True)
            uvicorn.Server(uvicorn.Config(dispatch,lifespan="off",log_level="error",
                                          proxy_headers=False)).run(sockets=[sock])
    test.doCleanups()


if __name__ == "__main__":
    main()
