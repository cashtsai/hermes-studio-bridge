#!/usr/bin/env python3
"""Pocket console 的 tailnet HTTPS 前門::8443 (TLS) → 127.0.0.1:8081 (純 HTTP)。

## 為什麼是「TCP 轉發」而不是反向代理

console 的 `_pair_local_only` 只要在請求上看到**任何**轉送標頭
(`X-Forwarded-For` 等)就一律 403 —— 那是刻意的,因為公網訪客可以偽造 XFF
冒充 tailnet 來源。`tailscale serve` / nginx / caddy 都會補那個標頭,接上去
等於把自己鎖在門外。

這支只做 TLS 終結 + **位元組層**轉發:不解析 HTTP、不添加任何標頭。bridge
那端看到的是一條從 127.0.0.1 來的連線,於是放行。

## 為什麼不直接把 :8081 改成 TLS

還有兩條東西靠 8081 的純 HTTP 跑:
  * 具名 tunnel `pocket.tsai.cash → http://127.0.0.1:8081`(iPhone 在用)
  * Pocket.app 內部**寫死**的 `cloudflared --url http://127.0.0.1:8081`(改不到)
所以 8081 必須保持原樣,TLS 另開一道門。

## 憑證熱重載

`pocket-console-cert-renew.sh` 每日檢查,到期前 30 天才換檔,而且**刻意不重啟
任何服務**(重啟 bridge 會中斷進行中的 agent 回合)。這支是獨立行程,重載只影響
console 連線,所以這裡自己盯 mtime:憑證檔一變就重新 `load_cert_chain`,
不必有人記得手動重啟,也就不會在續期後還拿舊憑證去握手。
"""
from __future__ import annotations

import asyncio
import os
import ssl
import sys
from pathlib import Path

BIND = os.environ.get("POCKET_CONSOLE_TLS_BIND", "100.67.0.12")
PORT = int(os.environ.get("POCKET_CONSOLE_TLS_PORT", "8443"))
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = int(os.environ.get("POCKET_CONSOLE_TLS_UPSTREAM", "8081"))

CERT_DIR = Path.home() / ".config" / "pocket" / "tls"
CRT = CERT_DIR / "console.crt"
KEY = CERT_DIR / "console.key"

BUF = 65536
CERT_POLL_SECS = 300


def log(msg: str) -> None:
    # launchd 會把 stdout 導到 ~/Library/Logs/,保持行緩衝才看得到即時輸出。
    print(msg, flush=True)


def build_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=str(CRT), keyfile=str(KEY))
    return ctx


def cert_stamp() -> tuple:
    """(mtime, size) 對,用來偵測續期腳本換過檔沒有。"""
    out = []
    for p in (CRT, KEY):
        try:
            st = p.stat()
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


async def watch_cert(ctx: ssl.SSLContext) -> None:
    """憑證檔一變就重新載入同一個 context —— 後續握手自動吃到新憑證。

    已建立的連線不受影響(它們的 TLS 已經談好了),所以不需要斷線。
    """
    last = cert_stamp()
    while True:
        await asyncio.sleep(CERT_POLL_SECS)
        now = cert_stamp()
        if now == last:
            continue
        try:
            ctx.load_cert_chain(certfile=str(CRT), keyfile=str(KEY))
        except Exception as exc:  # noqa: BLE001 — 換檔換到一半就等下一輪
            log(f"! 憑證重載失敗(維持舊憑證服務中):{type(exc).__name__}: {exc}")
            continue
        last = now
        log("✓ 偵測到憑證更新,已熱重載")


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """單向搬位元組直到來源 EOF。不看內容,不改內容。"""
    try:
        while True:
            chunk = await reader.read(BUF)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass          # 任何一端先掛都算正常收場
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def handle(client_r: asyncio.StreamReader,
                 client_w: asyncio.StreamWriter) -> None:
    try:
        up_r, up_w = await asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT)
    except OSError as exc:
        log(f"! 連不上上游 {UPSTREAM_HOST}:{UPSTREAM_PORT}: {exc}")
        client_w.close()
        return

    await asyncio.gather(
        pipe(client_r, up_w),
        pipe(up_r, client_w),
        return_exceptions=True,
    )

    for w in (client_w, up_w):
        try:
            await w.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def main() -> int:
    for p in (CRT, KEY):
        if not p.exists():
            log(f"✗ 憑證不存在:{p}")
            return 2
    try:
        ctx = build_context()
    except Exception as exc:  # noqa: BLE001
        log(f"✗ 載入憑證失敗:{type(exc).__name__}: {exc}")
        return 3

    try:
        server = await asyncio.start_server(handle, host=BIND, port=PORT, ssl=ctx)
    except OSError as exc:
        # 開機時 tailscaled 還沒把 100.x 位址掛上來 → 綁不到。
        # 記一行然後非 0 離開,讓 launchd 依 ThrottleInterval 重試。
        log(f"✗ 綁不上 {BIND}:{PORT}(tailscaled 可能還沒就緒):{exc}")
        return 1

    log(f"✓ 監聽 {BIND}:{PORT} → {UPSTREAM_HOST}:{UPSTREAM_PORT} (TLS 終結,TCP 轉發)")
    asyncio.create_task(watch_cert(ctx))
    async with server:
        await server.serve_forever()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
