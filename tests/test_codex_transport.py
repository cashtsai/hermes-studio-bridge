"""CX 傳輸選擇(managed daemon ↔ 自己 spawn stdio)的回歸測試。

背景(2026-08-12,`codex/fix-managed-codex-transport`):為了解掉 thread-store
writer lock 衝突,bridge 改成去接桌面版 Codex 的 **managed daemon**
(`~/.codex/app-server-control/app-server-control.sock`,socket 上跑 WebSocket
frames)。那個修正本身是對的,但選傳輸的方式埋了兩顆 P0:

  P0-1 stdio 退路根本走不到:`CODEX_APP_SERVER_MODE` 預設 `managed`,而 stdio
       分支被 `if CODEX_APP_SERVER_MODE != "stdio": raise` 擋住,launchd plist
       又沒設這個變數 → 開機後使用者還沒開 ChatGPT.app、或龍蝦那台無頭
       Ubuntu 根本沒有桌面 app,CX 直接全滅。
  P0-2 stale socket 是死路:是否走 managed 用 `os.path.exists(socket)` 判斷,
       但 daemon 崩潰/被強制結束後 **inode 會留在磁碟上** → exists() 為 True
       → 走 managed → connect 丟 ConnectionRefusedError(Errno 61)→ raise,
       永遠碰不到 stdio。桌面 app 自動更新留下殭屍就是這個情境。

修法:三態 `auto`(新預設)/`managed`/`stdio`,而且「有沒有 daemon」一律用
**連得上嗎**判定,fallback 寫在 `except` 路徑裡。

這裡刻意**用真的 unix socket / 真的子行程**而不是整包 mock —— 原本的
mock 測試就是因為只 mock 到 happy path,才讓這兩顆雷活著上 production。

跑法:
    PYTHONPATH=. python -m unittest tests.test_codex_transport
"""
import asyncio
import contextlib
import io
import json
import os
import socket
import sqlite3
import sys
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="cxtransport-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

# 假的 `codex app-server --stdio`:只回 initialize,證明 spawn 這條路真的活著。
FAKE_STDIO_APP_SERVER = '''
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    if msg.get("method") == "initialize" and "id" in msg:
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0", "id": msg["id"],
            "result": {"userAgent": "fake-stdio-app-server",
                       "codexHome": "/tmp/fake-codex-home"}}) + "\\n")
        sys.stdout.flush()
'''


def run(coro):
    return asyncio.run(coro)


class FakeManagedDaemon:
    """真的 unix-socket WebSocket 伺服器,模擬桌面版的 managed app-server。"""

    def __init__(self, path: str):
        self.path = path
        self.server = None
        self.connections = 0
        self.seen: list = []

    async def start(self):
        from websockets.asyncio.server import unix_serve
        self.server = await unix_serve(self._handle, path=self.path)

    async def _handle(self, ws):
        self.connections += 1
        try:
            async for raw in ws:
                msg = json.loads(raw)
                self.seen.append(msg)
                if msg.get("method") == "__test_close":
                    await ws.close(code=1000, reason="bye")
                    return
                if msg.get("method") == "initialize" and "id" in msg:
                    await ws.send(json.dumps({
                        "id": msg["id"],
                        "result": {"userAgent": "fake-managed-daemon",
                                   "codexHome": "/tmp/fake-codex-home"}}))
        except Exception:  # noqa: BLE001
            pass

    async def stop(self, unlink: bool = True):
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()
            self.server = None
        if unlink and os.path.exists(self.path):
            os.unlink(self.path)


def make_stale_socket(path: str) -> None:
    """做出一顆「檔案在、沒人聽」的 unix socket —— 就是 P0-2 的現場。

    bind + listen + close:close 不會刪掉 inode,檔案留著,連進去是
    ConnectionRefusedError(Errno 61)。
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    s.listen(1)
    s.close()


async def cancel_task(task) -> None:
    if task is None:
        return
    task.cancel()
    # CancelledError 是 BaseException,suppress(Exception) 接不到。
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def shutdown_client(client) -> None:
    for task in (client._reader_task, client._stderr_task):
        await cancel_task(task)
    if client.ws is not None:
        with contextlib.suppress(Exception):
            await client.ws.close()
    if client.proc is not None:
        with contextlib.suppress(Exception):
            client.proc.kill()
        with contextlib.suppress(Exception):
            await client.proc.wait()


class CodexTransportTest(unittest.TestCase):
    def setUp(self):
        # AF_UNIX 路徑上限 104 bytes(macOS),所以 socket 目錄不要往 _TMP 底下
        # 再疊一層,名字也取短的;超長會直接 OSError: AF_UNIX path too long。
        self.dir = tempfile.mkdtemp(prefix="cxs-")
        self.sock_path = os.path.join(self.dir, "s.sock")
        if len(self.sock_path.encode()) > 100:
            self.dir = tempfile.mkdtemp(prefix="cxs-", dir="/tmp")
            self.sock_path = os.path.join(self.dir, "s.sock")
        self._saved_socket = bridge.CODEX_APP_SERVER_SOCKET
        self._saved_mode = bridge.CODEX_APP_SERVER_MODE
        self._saved_resolve = bridge._resolve_codex_bin
        bridge.CODEX_APP_SERVER_SOCKET = self.sock_path
        self.fake_bin = os.path.join(self.dir, "fake-codex")
        with open(self.fake_bin, "w", encoding="utf-8") as fh:
            fh.write("#!" + sys.executable + "\n" + FAKE_STDIO_APP_SERVER)
        os.chmod(self.fake_bin, 0o755)
        self.resolve_calls = []

        def _fake_resolve():
            self.resolve_calls.append(1)
            return self.fake_bin

        bridge._resolve_codex_bin = _fake_resolve

    def tearDown(self):
        bridge.CODEX_APP_SERVER_SOCKET = self._saved_socket
        bridge.CODEX_APP_SERVER_MODE = self._saved_mode
        bridge._resolve_codex_bin = self._saved_resolve

    # ── 預設值本身就是 P0-1 的修法 ────────────────────────────────────────
    def test_default_mode_is_auto(self):
        """plist 沒設 CODEX_APP_SERVER_MODE,所以預設值必須是會 fallback 的 auto。"""
        self.assertEqual(self._module_default_mode(), "auto")
        self.assertEqual(tuple(bridge.CODEX_APP_SERVER_MODES),
                         ("auto", "managed", "stdio"))

    @staticmethod
    def _module_default_mode() -> str:
        """重讀原始碼裡的預設值,避免被跑測試時的環境變數蓋掉。"""
        with open(bridge.__file__, encoding="utf-8") as fh:
            src = fh.read()
        marker = 'os.environ.get("CODEX_APP_SERVER_MODE", "'
        i = src.index(marker) + len(marker)
        return src[i:src.index('"', i)]

    # ── auto:有活的 daemon 就用 daemon ───────────────────────────────────
    def test_auto_uses_managed_socket_when_live(self):
        async def main():
            daemon = FakeManagedDaemon(self.sock_path)
            await daemon.start()
            bridge.CODEX_APP_SERVER_MODE = "auto"
            client = bridge.CodexAppServerClient()
            try:
                async with client._lock:
                    await client._ensure_started_locked()
                self.assertEqual(client.transport, "unix-websocket")
                self.assertIsNotNone(client.ws)
                self.assertIsNone(client.proc)
                self.assertEqual(client.spawned_bin, "managed-daemon")
                self.assertEqual(self.resolve_calls, [], "不該去 spawn 第二顆 app-server")
                self.assertEqual(daemon.connections, 1)
                # `initialized` 是 notification,伺服端可能還沒收到就往下跑
                deadline = time.time() + 5
                while len(daemon.seen) < 2 and time.time() < deadline:
                    await asyncio.sleep(0.02)
                methods = [m.get("method") for m in daemon.seen]
                self.assertIn("initialize", methods)
                self.assertIn("initialized", methods)
                # 走 ws 的 wire 不帶 jsonrpc 欄位(維持這個分支原本的行為)
                self.assertTrue(all("jsonrpc" not in m for m in daemon.seen))
            finally:
                await shutdown_client(client)
                await daemon.stop()
        run(main())

    def test_managed_connect_params_are_unchanged(self):
        """socket 健康時的行為必須和原分支一模一樣(連線參數逐項比對)。"""
        async def main():
            daemon = FakeManagedDaemon(self.sock_path)
            await daemon.start()
            bridge.CODEX_APP_SERVER_MODE = "auto"
            import websockets
            captured = {}
            real = websockets.unix_connect

            async def spy(path, **kwargs):
                captured["path"] = path
                captured.update(kwargs)
                return await real(path, **kwargs)

            websockets.unix_connect = spy
            client = bridge.CodexAppServerClient()
            try:
                async with client._lock:
                    await client._ensure_started_locked()
                self.assertEqual(captured["path"], self.sock_path)
                self.assertEqual(captured["uri"], "ws://localhost/")
                self.assertIsNone(captured["compression"])
                self.assertEqual(captured["max_size"], 128 * 1024 * 1024)
                self.assertEqual(captured["user_agent_header"],
                                 "PocketAgent-Bridge/0.1")
            finally:
                websockets.unix_connect = real
                await shutdown_client(client)
                await daemon.stop()
        run(main())

    # ── P0-1:socket 檔不存在 → auto 要退回 stdio ─────────────────────────
    def test_auto_falls_back_to_stdio_when_socket_missing(self):
        async def main():
            self.assertFalse(os.path.exists(self.sock_path))
            bridge.CODEX_APP_SERVER_MODE = "auto"
            client = bridge.CodexAppServerClient()
            try:
                async with client._lock:
                    await client._ensure_started_locked()
                self.assertEqual(client.transport, "stdio")
                self.assertIsNone(client.ws)
                self.assertIsNotNone(client.proc)
                self.assertIsNone(client.proc.returncode)
                self.assertEqual(client.spawned_bin, self.fake_bin)
                self.assertTrue(client.is_server_alive())
            finally:
                await shutdown_client(client)
        run(main())

    # ── P0-2:socket 檔在、但是死的 → auto 一樣要退回 stdio ────────────────
    def test_auto_falls_back_to_stdio_when_socket_is_stale(self):
        async def main():
            make_stale_socket(self.sock_path)
            # 先證明現場條件成立:檔案在(舊碼的 exists() 判斷會說「有 daemon」)
            self.assertTrue(os.path.exists(self.sock_path))
            # …但連進去是 ConnectionRefusedError,不是 FileNotFoundError。
            with self.assertRaises(ConnectionRefusedError):
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    probe.connect(self.sock_path)
                finally:
                    probe.close()

            bridge.CODEX_APP_SERVER_MODE = "auto"
            client = bridge.CodexAppServerClient()
            try:
                async with client._lock:
                    await client._ensure_started_locked()
                self.assertEqual(client.transport, "stdio",
                                 "stale socket 必須退回 stdio,不能直接 raise")
                self.assertIsNone(client.ws)
                self.assertIsNotNone(client.proc)
                self.assertEqual(self.resolve_calls, [1])
            finally:
                await shutdown_client(client)
        run(main())

    def test_auto_falls_back_when_socket_is_a_plain_file(self):
        """另一種假陽性:路徑存在但根本不是 socket(handshake/連線都會炸)。"""
        async def main():
            with open(self.sock_path, "w", encoding="utf-8") as fh:
                fh.write("not a socket")
            bridge.CODEX_APP_SERVER_MODE = "auto"
            client = bridge.CodexAppServerClient()
            try:
                async with client._lock:
                    await client._ensure_started_locked()
                self.assertEqual(client.transport, "stdio")
            finally:
                await shutdown_client(client)
        run(main())

    # ── managed:刻意 daemon-only 的人保持嚴格 ─────────────────────────────
    def test_managed_mode_raises_instead_of_falling_back(self):
        async def main():
            bridge.CODEX_APP_SERVER_MODE = "managed"
            client = bridge.CodexAppServerClient()
            try:
                with self.assertRaises(bridge.CodexAppServerError) as ctx:
                    async with client._lock:
                        await client._ensure_started_locked()
                self.assertIn("refusing to spawn", str(ctx.exception))
                self.assertIsNone(client.ws)
                self.assertIsNone(client.proc)
                self.assertEqual(self.resolve_calls, [],
                                 "managed 模式不得偷偷 spawn 第二顆 app-server")
            finally:
                await shutdown_client(client)
        run(main())

    def test_managed_mode_raises_on_stale_socket_too(self):
        async def main():
            make_stale_socket(self.sock_path)
            bridge.CODEX_APP_SERVER_MODE = "managed"
            client = bridge.CodexAppServerClient()
            try:
                with self.assertRaises(bridge.CodexAppServerError):
                    async with client._lock:
                        await client._ensure_started_locked()
                self.assertEqual(self.resolve_calls, [])
            finally:
                await shutdown_client(client)
        run(main())

    # ── stdio:完全不碰 socket ─────────────────────────────────────────────
    def test_stdio_mode_never_touches_socket(self):
        async def main():
            daemon = FakeManagedDaemon(self.sock_path)
            await daemon.start()          # daemon 是活的,但 stdio 模式不該去連
            bridge.CODEX_APP_SERVER_MODE = "stdio"
            client = bridge.CodexAppServerClient()
            try:
                async with client._lock:
                    await client._ensure_started_locked()
                self.assertEqual(client.transport, "stdio")
                self.assertEqual(daemon.connections, 0,
                                 "stdio 模式碰了 socket")
                self.assertEqual(daemon.seen, [])
                self.assertIsNotNone(client.proc)
            finally:
                await shutdown_client(client)
                await daemon.stop()
        run(main())

    # ── P1-3:連不上時不准把待批准的 approval 作廢 ─────────────────────────
    def _seed_pending_approval(self, aid: str) -> None:
        con = sqlite3.connect(bridge.CANON_DB, timeout=30)
        con.execute(
            "INSERT OR REPLACE INTO approvals"
            "(id,title,source,risk,detail,created_at,expires_at,status)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (aid, "t", "codex:thread-1", "low", "d",
             time.time(), time.time() + 600, "pending"))
        con.commit()
        con.close()

    def _approval_status(self, aid: str) -> str:
        con = sqlite3.connect(bridge.CANON_DB, timeout=30)
        row = con.execute("SELECT status FROM approvals WHERE id=?", (aid,)).fetchone()
        con.close()
        return row[0] if row else ""

    def test_failed_connect_does_not_expire_approvals(self):
        async def main():
            self._seed_pending_approval("appr-keep")
            bridge.CODEX_APP_SERVER_MODE = "managed"    # socket 不存在 → 必失敗
            client = bridge.CodexAppServerClient()
            client.pending_approvals["appr-keep"] = {"id": "appr-keep"}
            client.pending_approvals_by_thread["thread-1"]["appr-keep"] = {"id": "appr-keep"}
            try:
                for _ in range(3):     # 重試也不准打 DB
                    with self.assertRaises(bridge.CodexAppServerError):
                        async with client._lock:
                            await client._ensure_started_locked()
                self.assertEqual(self._approval_status("appr-keep"), "pending")
                self.assertIn("appr-keep", client.pending_approvals)
                self.assertIn("appr-keep", client.pending_approvals_by_thread["thread-1"])
            finally:
                await shutdown_client(client)
        run(main())

    def test_successful_connect_still_expires_approvals(self):
        """正向對照:清理只是被搬到「接上之後」,不是被刪掉。"""
        async def main():
            self._seed_pending_approval("appr-expire")
            daemon = FakeManagedDaemon(self.sock_path)
            await daemon.start()
            bridge.CODEX_APP_SERVER_MODE = "auto"
            client = bridge.CodexAppServerClient()
            client.pending_approvals["appr-expire"] = {"id": "appr-expire"}
            try:
                async with client._lock:
                    await client._ensure_started_locked()
                self.assertEqual(self._approval_status("appr-expire"), "expired")
                self.assertEqual(client.pending_approvals, {})
            finally:
                await shutdown_client(client)
                await daemon.stop()
        run(main())

    # ── P1-4:ws.send 失敗要包成 CodexAppServerError ──────────────────────
    def test_ws_send_failure_surfaces_as_codex_app_server_error(self):
        async def main():
            daemon = FakeManagedDaemon(self.sock_path)
            await daemon.start()
            bridge.CODEX_APP_SERVER_MODE = "auto"
            client = bridge.CodexAppServerClient()
            try:
                async with client._lock:
                    await client._ensure_started_locked()
                ws = client.ws
                # 停掉 reader,然後真的把連線關掉 → 之後 send 會丟
                # websockets 的 ConnectionClosed(不是 CodexAppServerError)。
                await cancel_task(client._reader_task)
                await ws.close()
                client.ws = ws          # cleanup 會把 ws 清成 None,這裡放回去
                with self.assertRaises(bridge.CodexAppServerError) as ctx:
                    await client._write_locked({"jsonrpc": "2.0", "id": 99,
                                                "method": "thread/list"})
                self.assertIn("send failed", str(ctx.exception))
            finally:
                client.ws = None
                await shutdown_client(client)
                await daemon.stop()
        run(main())

    # ── P2-6:正常關閉不是故障 ────────────────────────────────────────────
    def test_clean_close_is_not_logged_as_failure(self):
        async def main():
            daemon = FakeManagedDaemon(self.sock_path)
            await daemon.start()
            bridge.CODEX_APP_SERVER_MODE = "auto"
            client = bridge.CodexAppServerClient()
            buf = io.StringIO()
            try:
                async with client._lock:
                    await client._ensure_started_locked()
                reader = client._reader_task
                with contextlib.redirect_stdout(buf):
                    # 使用者關掉 ChatGPT.app = daemon 這端好好地 close(1000)
                    await client.ws.send(json.dumps({"method": "__test_close"}))
                    await asyncio.wait_for(reader, timeout=5)
                out = buf.getvalue()
                self.assertNotIn("codex_app_server_websocket_failed", out)
                self.assertIn("codex_app_server_websocket_closed", out)
            finally:
                await shutdown_client(client)
                await daemon.stop()
        run(main())

    def test_clean_closure_classifier(self):
        from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
        from websockets.frames import Close

        ok = ConnectionClosedOK(Close(1000, "bye"), None)
        self.assertTrue(bridge._is_clean_ws_closure(ok))
        going_away = ConnectionClosedOK(Close(1001, "going away"), None)
        self.assertTrue(bridge._is_clean_ws_closure(going_away))
        broken = ConnectionClosedError(Close(1006, "abnormal"), None)
        self.assertFalse(bridge._is_clean_ws_closure(broken))
        self.assertFalse(bridge._is_clean_ws_closure(RuntimeError("boom")))

    # ── 日誌:每次「換傳輸」印一行,不是每次嘗試都印 ──────────────────────
    def test_transport_log_is_once_per_transition(self):
        client = bridge.CodexAppServerClient()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            client._note_transport("stdio")
            client._note_transport("stdio")
            client._note_transport("stdio")
            client._note_transport("unix-websocket")
        lines = [ln for ln in buf.getvalue().splitlines()
                 if "codex_transport_selected" in ln]
        self.assertEqual(len(lines), 2, buf.getvalue())
        self.assertIn('"transport": "stdio"', lines[0])
        self.assertIn('"transport": "unix-websocket"', lines[1])


if __name__ == "__main__":
    unittest.main()
