"""B3 讀側活性死線(grok-build steal #6,治 401 靜默斷五天族)。

釘三件事:
  1. openclaw WS:假流靜默(半開)→ READ_LIVENESS 內零 inbound frame →
     判死、close、走重連路;有 inbound 就重計,**不誤殺**。
  2. read_loop 把 tick/health 也算活性(濾掉不外流,但是 liveness 主食)。
  3. relay frpc:退避 reset 的「憑證被接受證據」= `start proxy success`
     日誌行 —— _relay_run_frpc 回傳 proxied 旗標,沒看到就是 False
     (行程起來/TCP 連上都不是證據)。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="b3-liveness-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge             # noqa: E402
import openclaw_provider  # noqa: E402


class _FakeWS:
    def __init__(self, frames=None):
        self.closed = False
        self._frames = list(frames or [])

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)


class TestOpenClawReadLiveness(unittest.TestCase):
    def _client(self):
        c = openclaw_provider.OpenClawClient(log=lambda *a, **k: None)
        return c

    def test_silent_stream_is_judged_dead_and_closed(self):
        async def scenario():
            c = self._client()
            ws = _FakeWS()
            c._ws = ws
            c._last_rx = time.monotonic() - 999      # 靜默很久了
            reader = asyncio.get_event_loop().create_task(asyncio.sleep(30))
            with patch.object(openclaw_provider, "_READ_LIVENESS_SECS", 0.3), \
                 patch.object(openclaw_provider, "_LIVENESS_POLL_SECS", 0.05):
                verdict = await asyncio.wait_for(c._watch_reader(reader), 2)
            reader.cancel()
            return verdict, ws, c

        verdict, ws, c = asyncio.run(scenario())
        self.assertEqual(verdict, "liveness")
        self.assertTrue(ws.closed, "判死必須真的 close,讓 reader 走既有清理路")
        self.assertIsNone(c._ws)

    def test_inbound_frames_prevent_false_kill(self):
        """半開才判死 —— 有 inbound 就重計,慢聊不誤殺。"""
        async def scenario():
            c = self._client()
            c._ws = _FakeWS()
            c._last_rx = time.monotonic()
            reader = asyncio.get_event_loop().create_task(asyncio.sleep(30))

            async def bumper():
                for _ in range(12):
                    await asyncio.sleep(0.05)
                    c._last_rx = time.monotonic()
                reader.cancel()                 # 結束測試:讓 reader「自然」退場

            with patch.object(openclaw_provider, "_READ_LIVENESS_SECS", 0.3), \
                 patch.object(openclaw_provider, "_LIVENESS_POLL_SECS", 0.05):
                b = asyncio.get_event_loop().create_task(bumper())
                verdict = await asyncio.wait_for(c._watch_reader(reader), 3)
                await b
            return verdict, c

        verdict, c = asyncio.run(scenario())
        self.assertEqual(verdict, "reader_exited", "有 inbound 不准判死")
        self.assertFalse(getattr(c._ws, "closed", False))

    def test_read_loop_counts_tick_as_liveness_but_filters_it(self):
        async def scenario():
            c = self._client()
            seen = []
            c.on_event = lambda ev, payload: seen.append(ev)
            frames = [json.dumps({"type": "event", "event": "tick", "payload": {}}),
                      json.dumps({"type": "event", "event": "chat", "payload": {}})]
            ws = _FakeWS(frames)
            c._ws = ws
            before = c._last_rx
            await c._read_loop(ws)
            return c, seen, before

        c, seen, before = asyncio.run(scenario())
        self.assertGreater(c._last_rx, before, "tick 必須算活性")
        self.assertEqual(seen, ["chat"], "tick/health 照樣濾掉不外流")

    def test_backoff_reset_requires_handshake_evidence(self):
        """backoff=1.0 的 reset 只在 hello-ok(token 驗過)那一行 —— 用源碼釘住
        位置,防止日後被挪到「連線建立」就 reset。"""
        import inspect
        src = inspect.getsource(openclaw_provider.OpenClawClient._ensure_ws)
        hs = src.index("self._handshake(")
        reset = src.index("self._backoff = 1.0")
        self.assertGreater(reset, hs, "backoff reset 必須在握手(憑證驗證)之後")


class TestRelayCredentialEvidence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="b3-relay-")

    def _script(self, body: str) -> str:
        path = os.path.join(self._tmp, "fake-frpc")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)
        return path

    def _run(self, binary: str):
        async def scenario():
            with patch.object(bridge, "_relay_enabled", lambda: True), \
                 patch.object(bridge, "_RELAY_FLAG_POLL_SECS", 0.2):
                return await asyncio.wait_for(bridge._relay_run_frpc(binary), 10)
        return asyncio.run(scenario())

    def test_proxy_success_is_the_evidence(self):
        tail, proxied = self._run(self._script('echo "ok: start proxy success"'))
        self.assertTrue(proxied)
        self.assertIn("start proxy success", tail)

    def test_process_running_is_not_evidence(self):
        tail, proxied = self._run(self._script(
            'echo "login to server success"; echo "start error: not entitled"'))
        self.assertFalse(proxied, "行程起來/登入成功都不是證據 —— 只有 NewProxy 放行才是")

    def test_loop_resets_backoff_only_on_evidence(self):
        """_relay_loop 的退避 reset 必須以 proxied 為條件 —— 源碼釘住,
        防止回歸成「跑 frpc 前就 reset」的舊世界。"""
        import inspect
        src = inspect.getsource(bridge._relay_loop)
        self.assertIn("if proxied:", src)
        run_at = src.index("_relay_run_frpc(")
        # 開頭的 `backoff = 30.0` 是初始化;盯的是 run 之後的 reset 那一顆。
        reset_at = src.index("backoff = 30.0", run_at)
        evidence_at = src.index("if proxied:")
        self.assertGreater(reset_at, evidence_at,
                           "backoff reset 必須在拿到 frpc 證據之後")
        self.assertNotIn("backoff = 30.0",
                         src[src.index("while True"):run_at],
                         "不准在跑 frpc 之前就 reset 退避")


if __name__ == "__main__":
    unittest.main()
