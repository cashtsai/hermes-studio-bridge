"""CX 傳輸兩個結構性弱點的回歸測試(2026-08-15 事故收尾)。

事故實錄(當天日誌可對):
  弱點一:daemon 一斷,stdio 退路的 `_resolve_codex_bin()` 候選序把
    `/Applications/ChatGPT.app/Contents/Resources/codex` 排第一(7/10 事故遺留)。
    桌面內建版永遠比 standalone 超前(0.148-alpha vs 0.147),抓到它 = 和
    0.147 daemon 版本分裂 —— 一天 581 次 codex_app_server_unmatched_response,
    還會把 thread-store 目錄升級成 daemon 讀不回來的格式。
    → 修法:候選序反轉,standalone 優先、桌面 binary 墊底。
  弱點二:auto 模式掉到 stdio 之後**永遠不會自己回去** daemon(12:43 掉下去,
    15:4x 人工重啟才歸位)。
    → 修法:stdio 期間每 60s 探一次 socket,活了就挑「沒有 in-flight」的空檔
    優雅切回 managed;升級失敗 10 分鐘內不再試(防震盪)。

傳輸切換沿用 test_codex_transport 的做法:真 unix socket + 真子行程,
只有「探測通過但 WS handshake 失敗」這種毫秒級競態才用假 listener 模擬。

跑法:
    PYTHONPATH=. python -m unittest tests.test_cx_transport_upgrade
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import asyncio
import contextlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="cxupgrade-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

from tests.test_codex_transport import (  # noqa: E402
    FakeManagedDaemon, make_stale_socket, shutdown_client)

# 假的 `codex app-server --stdio`,**每個帶 id 的請求都回**(和
# test_codex_transport 的只回 initialize 不同):升級判定看的是
# 「_pending 空不空」,連線後的 align_provider_status 會發 thread/loaded/list,
# 若沒人回,它會佔住 _pending 30 秒,把每個測試都變成 busy。
FAKE_STDIO_APP_SERVER_ALL = '''
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    if "id" not in msg:
        continue
    method = msg.get("method")
    if method == "initialize":
        result = {"userAgent": "fake-stdio-app-server",
                  "codexHome": "/tmp/fake-codex-home"}
    elif method == "thread/loaded/list":
        result = {"data": []}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                 "result": result}) + "\\n")
    sys.stdout.flush()
'''


def run(coro):
    return asyncio.run(coro)


async def settle_align_tasks():
    """等 align_provider_status 背景任務跑完(它會佔用 _pending)。"""
    for t in list(bridge._BG_TASKS):
        coro = getattr(t, "get_coro", lambda: None)()
        qn = getattr(coro, "__qualname__", "") if coro is not None else ""
        if qn.endswith("align_provider_status"):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(t), timeout=10)


# ═════════════════════════ 弱點一:候選序 ═════════════════════════

class ResolveCodexBinOrderTest(unittest.TestCase):
    """`_resolve_codex_bin()` 的候選序:standalone 優先,桌面 binary 墊底。"""

    DESKTOP_CODEX = "/Applications/Codex.app/Contents/Resources/codex"
    DESKTOP_CHATGPT = "/Applications/ChatGPT.app/Contents/Resources/codex"

    def setUp(self):
        # ~ 開頭的候選路徑要在 setUp 展開,不能當 class 屬性在 import 時定死:
        # 全套 discover 一個行程跑時,別的測試檔會把 HOME 改到各自的 tmp,
        # import 時展開的 ~ 和 _resolve_codex_bin() 呼叫當下展開的 ~ 會是
        # 兩個不同目錄 → 三條斷言全假紅(單檔跑永遠看不到)。
        self.STANDALONE = os.path.expanduser(
            "~/.codex/packages/standalone/current/codex")
        self.LOCAL_BIN = os.path.expanduser("~/.local/bin/codex")
        self._saved_exists = os.path.exists
        self._saved_which = shutil.which
        self._saved_env = os.environ.get("CODEX_BIN")
        os.environ.pop("CODEX_BIN", None)

    def tearDown(self):
        os.path.exists = self._saved_exists
        shutil.which = self._saved_which
        if self._saved_env is not None:
            os.environ["CODEX_BIN"] = self._saved_env
        else:
            os.environ.pop("CODEX_BIN", None)

    def _pretend(self, existing: set, which: str | None = None):
        real_exists = self._saved_exists

        def fake_exists(p):
            # 只攔截候選路徑,別的照真(測試環境自己的檔案還是要看得到)
            if p in (self.STANDALONE, self.LOCAL_BIN,
                     self.DESKTOP_CODEX, self.DESKTOP_CHATGPT,
                     which, os.environ.get("CODEX_BIN")):
                return p in existing
            return real_exists(p)

        os.path.exists = fake_exists
        shutil.which = lambda name: which

    def test_env_override_wins(self):
        os.environ["CODEX_BIN"] = "/opt/custom/codex"
        self._pretend({"/opt/custom/codex", self.STANDALONE,
                       self.DESKTOP_CHATGPT})
        self.assertEqual(bridge._resolve_codex_bin(), "/opt/custom/codex")

    def test_standalone_beats_everything(self):
        self._pretend({self.STANDALONE, self.LOCAL_BIN,
                       self.DESKTOP_CODEX, self.DESKTOP_CHATGPT},
                      which="/somewhere/codex")
        self.assertEqual(bridge._resolve_codex_bin(), self.STANDALONE)

    def test_desktop_binary_is_last_resort(self):
        """2026-08-15 的病灶:桌面 binary 只要有任何 standalone 在就不能選。"""
        self._pretend({self.LOCAL_BIN, self.DESKTOP_CODEX, self.DESKTOP_CHATGPT})
        self.assertEqual(bridge._resolve_codex_bin(), self.LOCAL_BIN)

    def test_which_beats_desktop(self):
        self._pretend({self.DESKTOP_CHATGPT, "/usr/local/bin/codex"},
                      which="/usr/local/bin/codex")
        self.assertEqual(bridge._resolve_codex_bin(), "/usr/local/bin/codex")

    def test_desktop_only_machine_still_works(self):
        """整台機器只裝了桌面 app:寧可版本超前也不能沒有 codex。"""
        self._pretend({self.DESKTOP_CHATGPT})
        self.assertEqual(bridge._resolve_codex_bin(), self.DESKTOP_CHATGPT)

    def test_nothing_installed_returns_default(self):
        self._pretend(set())
        self.assertEqual(bridge._resolve_codex_bin(), self.LOCAL_BIN)

    def test_source_order_desktop_paths_are_after_standalone(self):
        """結構性斷言:原始碼裡桌面路徑必須排在 standalone 之後。
        (防止之後有人「順手」把桌面 binary 搬回前面 —— 7/10 那次的修法
        方向已經被 8/15 證明過期了。)"""
        with open(bridge.__file__, encoding="utf-8") as fh:
            src = fh.read()
        i_fn = src.index("def _resolve_codex_bin")
        block = src[i_fn:i_fn + 2000]
        # 用完整的 expanduser 呼叫當 needle:註解裡也會提到這些路徑,
        # 裸字串會先撞到註解。
        i_standalone = block.index(
            'os.path.expanduser("~/.codex/packages/standalone/current/codex")')
        i_local = block.index('os.path.expanduser("~/.local/bin/codex")')
        i_codexapp = block.index("/Applications/Codex.app/Contents/Resources/codex")
        i_chatgpt = block.index("/Applications/ChatGPT.app/Contents/Resources/codex")
        self.assertLess(i_standalone, i_local)
        self.assertLess(i_local, i_codexapp)
        self.assertLess(i_local, i_chatgpt)


# ═════════════════════════ socket 探測 ═════════════════════════

class DaemonSocketProbeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cxp-")
        self.sock_path = os.path.join(self.dir, "s.sock")
        if len(self.sock_path.encode()) > 100:
            self.dir = tempfile.mkdtemp(prefix="cxp-", dir="/tmp")
            self.sock_path = os.path.join(self.dir, "s.sock")

    def test_missing_socket_is_dead(self):
        self.assertFalse(bridge._codex_daemon_socket_alive(self.sock_path))

    def test_stale_socket_is_dead(self):
        """inode 在、沒人聽(daemon 崩潰現場)—— exists() 會騙,探測不能騙。"""
        make_stale_socket(self.sock_path)
        self.assertTrue(os.path.exists(self.sock_path))
        self.assertFalse(bridge._codex_daemon_socket_alive(self.sock_path))

    def test_live_listener_is_alive(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(self.sock_path)
            s.listen(1)
            self.assertTrue(bridge._codex_daemon_socket_alive(self.sock_path))
        finally:
            s.close()

    def test_empty_path_is_dead(self):
        self.assertFalse(bridge._codex_daemon_socket_alive(""))


# ═════════════════════ 弱點二:stdio → daemon 升級 ═════════════════════

class RawAcceptingListener:
    """探測(raw connect)會過、WS handshake 會炸的 listener ——
    模擬「探測那一刻活著、真連的時候 daemon 已經死了」的競態。"""

    def __init__(self, path: str):
        self.path = path
        self.server = None

    async def start(self):
        async def handler(reader, writer):
            writer.close()          # 一接就掛電話 → handshake 失敗
            with contextlib.suppress(Exception):
                await writer.wait_closed()

        self.server = await asyncio.start_unix_server(handler, path=self.path)

    async def stop(self):
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()
            self.server = None
        if os.path.exists(self.path):
            os.unlink(self.path)


class TransportUpgradeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cxu-")
        self.sock_path = os.path.join(self.dir, "s.sock")
        if len(self.sock_path.encode()) > 100:
            self.dir = tempfile.mkdtemp(prefix="cxu-", dir="/tmp")
            self.sock_path = os.path.join(self.dir, "s.sock")
        self._saved_socket = bridge.CODEX_APP_SERVER_SOCKET
        self._saved_mode = bridge.CODEX_APP_SERVER_MODE
        self._saved_resolve = bridge._resolve_codex_bin
        bridge.CODEX_APP_SERVER_SOCKET = self.sock_path
        bridge.CODEX_APP_SERVER_MODE = "auto"
        self.fake_bin = os.path.join(self.dir, "fake-codex")
        with open(self.fake_bin, "w", encoding="utf-8") as fh:
            fh.write("#!" + sys.executable + "\n" + FAKE_STDIO_APP_SERVER_ALL)
        os.chmod(self.fake_bin, 0o755)
        bridge._resolve_codex_bin = lambda: self.fake_bin

    def tearDown(self):
        bridge.CODEX_APP_SERVER_SOCKET = self._saved_socket
        bridge.CODEX_APP_SERVER_MODE = self._saved_mode
        bridge._resolve_codex_bin = self._saved_resolve

    async def _start_stdio_client(self):
        """讓 client 走 auto → socket 不在 → stdio 的既有退路,並等
        align_provider_status 收工(不然 _pending 非空,判定永遠 busy)。"""
        client = bridge.CodexAppServerClient()
        async with client._lock:
            await client._ensure_started_locked()
        assert client.transport == "stdio", client.transport
        await settle_align_tasks()
        assert not client._pending, client._pending
        return client

    # ── 升級主線 ─────────────────────────────────────────────────────────
    def test_upgrade_happy_path(self):
        async def main():
            client = await self._start_stdio_client()
            old_proc = client.proc
            daemon = FakeManagedDaemon(self.sock_path)
            await daemon.start()
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    outcome = await client._maybe_upgrade_transport()
                self.assertEqual(outcome, "upgraded")
                self.assertEqual(client.transport, "unix-websocket")
                self.assertIsNotNone(client.ws)
                self.assertIsNone(client.proc)
                self.assertEqual(client.spawned_bin, "managed-daemon")
                self.assertIsNotNone(old_proc.returncode,
                                     "stdio 子程序必須被收掉,不能留殭屍")
                out = buf.getvalue()
                self.assertIn("codex_transport_upgraded", out)
                self.assertIn('"previous": "stdio"', out)
                # 升級也是一次「換傳輸」,codex_transport_selected 要跟著印
                self.assertIn('"transport": "unix-websocket"', out)
                # 升級成功後 backoff 歸零
                self.assertEqual(client._upgrade_backoff_until, 0.0)
            finally:
                await shutdown_client(client)
                await daemon.stop()
        run(main())

    def test_probe_task_started_on_auto_fallback(self):
        async def main():
            client = await self._start_stdio_client()
            try:
                self.assertIsNotNone(client._upgrade_task)
                self.assertFalse(client._upgrade_task.done())
            finally:
                await shutdown_client(client)
        run(main())

    def test_probe_task_not_started_in_explicit_stdio_mode(self):
        """使用者明講要 stdio(或隔離強制 stdio)就不要偷偷升級。"""
        async def main():
            bridge.CODEX_APP_SERVER_MODE = "stdio"
            client = bridge.CodexAppServerClient()
            async with client._lock:
                await client._ensure_started_locked()
            try:
                self.assertEqual(client.transport, "stdio")
                self.assertIsNone(client._upgrade_task)
                self.assertEqual(await client._maybe_upgrade_transport(), "mode")
            finally:
                await shutdown_client(client)
        run(main())

    # ── 升級時機判定 ─────────────────────────────────────────────────────
    def test_upgrade_deferred_while_requests_in_flight(self):
        async def main():
            client = await self._start_stdio_client()
            daemon = FakeManagedDaemon(self.sock_path)
            await daemon.start()
            try:
                fut = asyncio.get_running_loop().create_future()
                client._pending[999] = fut          # 有 in-flight 請求
                self.assertEqual(await client._maybe_upgrade_transport(), "busy")
                self.assertEqual(client.transport, "stdio")
                self.assertIsNotNone(client.proc)
                # busy 不是失敗,不准進 backoff —— 空檔一到就要能升級
                self.assertEqual(client._upgrade_backoff_until, 0.0)
                client._pending.pop(999).cancel()
                self.assertEqual(await client._maybe_upgrade_transport(),
                                 "upgraded")
            finally:
                await shutdown_client(client)
                await daemon.stop()
        run(main())

    def test_upgrade_deferred_while_turn_active(self):
        """turn 跑到一半 _pending 可能是空的(事件用 notification 流),
        這時切換一樣會腰斬回合 —— active_turns 也要擋。"""
        async def main():
            client = await self._start_stdio_client()
            daemon = FakeManagedDaemon(self.sock_path)
            await daemon.start()
            try:
                client.active_turns["thread-x"] = "turn-1"
                self.assertEqual(await client._maybe_upgrade_transport(), "busy")
                self.assertEqual(client.transport, "stdio")
                client.active_turns.clear()
                self.assertEqual(await client._maybe_upgrade_transport(),
                                 "upgraded")
            finally:
                await shutdown_client(client)
                await daemon.stop()
        run(main())

    def test_no_upgrade_when_daemon_still_dead(self):
        async def main():
            client = await self._start_stdio_client()
            try:
                self.assertEqual(await client._maybe_upgrade_transport(),
                                 "daemon-dead")
                self.assertEqual(client.transport, "stdio")
                self.assertIsNotNone(client.proc)
                self.assertIsNone(client.proc.returncode,
                                  "探測失敗不准動 stdio 子程序")
            finally:
                await shutdown_client(client)
        run(main())

    def test_stale_socket_probe_says_dead(self):
        async def main():
            client = await self._start_stdio_client()
            try:
                make_stale_socket(self.sock_path)
                self.assertEqual(await client._maybe_upgrade_transport(),
                                 "daemon-dead")
            finally:
                await shutdown_client(client)
        run(main())

    # ── 震盪抑制 ─────────────────────────────────────────────────────────
    def test_failed_upgrade_backs_off_ten_minutes(self):
        """探測通過、真連失敗(daemon 在毫秒間死掉/handshake 對不上):
        要退回 stdio 繼續活,並且 10 分鐘內不再試 —— 不准每 60 秒重啟
        一次子程序的震盪。"""
        async def main():
            client = await self._start_stdio_client()
            listener = RawAcceptingListener(self.sock_path)
            await listener.start()
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    outcome = await client._maybe_upgrade_transport()
                self.assertEqual(outcome, "failed")
                # 殘局要收乾淨:還是有一顆能用的 stdio app-server
                self.assertEqual(client.transport, "stdio")
                self.assertIsNotNone(client.proc)
                self.assertIsNone(client.proc.returncode)
                self.assertTrue(client.is_server_alive())
                # backoff 已設(約 10 分鐘後)
                remain = client._upgrade_backoff_until - time.monotonic()
                self.assertGreater(remain, 500)
                self.assertLessEqual(
                    remain, bridge.CODEX_TRANSPORT_UPGRADE_BACKOFF_SECS)
                self.assertIn("codex_transport_upgrade_failed", buf.getvalue())
                # backoff 期間再叫也不動手 —— 連探測都不做
                self.assertEqual(await client._maybe_upgrade_transport(),
                                 "backoff")
                # backoff 過期後恢復嘗試(手動撥快時鐘)。先等重生的 stdio
                # 把 align 跑完,不然 _pending 非空會判成 busy。
                await settle_align_tasks()
                client._upgrade_backoff_until = time.monotonic() - 1
                self.assertEqual(await client._maybe_upgrade_transport(),
                                 "failed")   # listener 還是假的,再進 backoff
                self.assertGreater(client._upgrade_backoff_until,
                                   time.monotonic())
            finally:
                await shutdown_client(client)
                await listener.stop()
        run(main())

    def test_backoff_skips_probe_entirely(self):
        async def main():
            client = await self._start_stdio_client()
            daemon = FakeManagedDaemon(self.sock_path)
            await daemon.start()      # daemon 是活的,但 backoff 未過就不准碰
            try:
                client._upgrade_backoff_until = time.monotonic() + 600
                self.assertEqual(await client._maybe_upgrade_transport(),
                                 "backoff")
                self.assertEqual(client.transport, "stdio")
                self.assertEqual(daemon.connections, 0)
            finally:
                await shutdown_client(client)
                await daemon.stop()
        run(main())

    def test_not_stdio_is_noop(self):
        async def main():
            client = bridge.CodexAppServerClient()   # transport == ""
            self.assertEqual(await client._maybe_upgrade_transport(),
                             "not-stdio")
        run(main())

    # ── 探測協程本體 ─────────────────────────────────────────────────────
    def test_upgrade_loop_exits_after_upgrade(self):
        async def main():
            saved = bridge.CODEX_TRANSPORT_UPGRADE_PROBE_SECS
            bridge.CODEX_TRANSPORT_UPGRADE_PROBE_SECS = 0.01
            client = bridge.CodexAppServerClient()
            client.transport = "unix-websocket"      # 已經在 managed 上
            try:
                await asyncio.wait_for(client._transport_upgrade_loop(),
                                       timeout=5)
            finally:
                bridge.CODEX_TRANSPORT_UPGRADE_PROBE_SECS = saved
        run(main())

    def test_ensure_upgrade_probe_is_idempotent(self):
        async def main():
            client = bridge.CodexAppServerClient()
            client.transport = "stdio"
            client._ensure_upgrade_probe()
            first = client._upgrade_task
            client._ensure_upgrade_probe()
            self.assertIs(client._upgrade_task, first)
            await shutdown_client(client)
        run(main())

    def test_end_to_end_probe_loop_upgrades(self):
        """整條線:掉到 stdio → 探測協程自己發現 daemon 活了 → 自動升級。"""
        async def main():
            saved = bridge.CODEX_TRANSPORT_UPGRADE_PROBE_SECS
            bridge.CODEX_TRANSPORT_UPGRADE_PROBE_SECS = 0.05
            client = None
            daemon = FakeManagedDaemon(self.sock_path)
            try:
                client = await self._start_stdio_client()
                await daemon.start()
                deadline = time.time() + 10
                while client.transport != "unix-websocket" and time.time() < deadline:
                    await asyncio.sleep(0.05)
                self.assertEqual(client.transport, "unix-websocket")
                self.assertIsNone(client.proc)
                # 升級完成後探測協程要收工,不能留著空轉
                deadline = time.time() + 5
                while not client._upgrade_task.done() and time.time() < deadline:
                    await asyncio.sleep(0.05)
                self.assertTrue(client._upgrade_task.done())
            finally:
                bridge.CODEX_TRANSPORT_UPGRADE_PROBE_SECS = saved
                if client is not None:
                    await shutdown_client(client)
                await daemon.stop()
        run(main())


# ═════════════════ 順手修:unmatched response 要講人話 ═════════════════

class UnmatchedResponseLogTest(unittest.TestCase):
    def test_late_reply_logs_method_and_summary(self):
        async def main():
            client = bridge.CodexAppServerClient()
            client._note_sent_method(7, "thread/turns/list")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                await client._dispatch_wire_message(json.dumps(
                    {"id": 7, "result": {"turns": [], "nextCursor": None}}))
            out = buf.getvalue()
            self.assertIn("codex_app_server_unmatched_response", out)
            self.assertIn('"method": "thread/turns/list"', out)
            self.assertIn("result keys=nextCursor,turns", out)
            self.assertIn('"late": false', out)
        run(main())

    def test_foreign_id_logs_empty_method(self):
        """method 空白 = 我們沒發過這個 id = 版本分裂的指紋。"""
        async def main():
            client = bridge.CodexAppServerClient()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                await client._dispatch_wire_message(json.dumps(
                    {"id": 424242,
                     "error": {"code": -32600, "message": "unknown variant"}}))
            out = buf.getvalue()
            self.assertIn("codex_app_server_unmatched_response", out)
            self.assertIn('"method": ""', out)
            self.assertIn("error -32600: unknown variant", out)
        run(main())

    def test_done_future_counts_as_late(self):
        async def main():
            client = bridge.CodexAppServerClient()
            fut = asyncio.get_running_loop().create_future()
            fut.set_result({"already": "answered"})
            client._pending[9] = fut
            client._note_sent_method(9, "thread/list")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                await client._dispatch_wire_message(json.dumps(
                    {"id": 9, "result": {}}))
            out = buf.getvalue()
            self.assertIn('"late": true', out)
            self.assertIn('"method": "thread/list"', out)
        run(main())

    def test_sent_methods_capped(self):
        client = bridge.CodexAppServerClient()
        for i in range(600):
            client._note_sent_method(i, f"m{i}")
        self.assertLessEqual(len(client._sent_methods), 512)
        self.assertNotIn(0, client._sent_methods)     # 最舊的被淘汰
        self.assertIn(599, client._sent_methods)


if __name__ == "__main__":
    unittest.main()
