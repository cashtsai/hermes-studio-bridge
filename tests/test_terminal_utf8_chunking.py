"""Drive the live terminal WebSocket handler without starting a shell."""
import _isolation  # noqa: F401
import asyncio
import json
import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge


class Socket:
    query_params = {}
    client_state = bridge.WebSocketState.CONNECTED

    def __init__(self):
        self.frames = []

    async def accept(self):
        pass

    async def send_text(self, raw):
        self.frames.append(json.loads(raw))

    async def receive_text(self):
        await asyncio.Future()

    async def close(self, **kwargs):
        pass


class TerminalUTF8(unittest.IsolatedAsyncioTestCase):
    async def stream(self, chunks):
        master, slave = os.pipe()
        chunks = iter([*chunks, b""])
        real_read = os.read
        sock = Socket()
        proc = Mock()
        proc.poll.return_value = 0
        def read(fd, n):
            return next(chunks) if fd == master else real_read(fd, n)
        with (patch.object(bridge, "_terminal_enabled", return_value=True),
              patch.object(bridge, "_ws_bearer_token", return_value="fixture"),
              patch.object(bridge, "_ws_token_authorized", return_value=True),
              patch.object(bridge, "_terminal_tmux_bin", return_value=None),
              patch.object(bridge.pty, "openpty", return_value=(master, slave)),
              patch.object(bridge.os, "read", side_effect=read),
              patch.object(bridge.subprocess, "Popen", return_value=proc)):
            await bridge.app_v1_terminal(sock)
        return [frame["data"] for frame in sock.frames if frame["type"] == "output"]

    async def test_byte_by_byte_and_escape_sequences(self):
        text = "\x1b[32m測試𠮷\x1b[0m"
        frames = await self.stream([bytes([b]) for b in text.encode()])
        self.assertEqual("".join(frames), text)
        self.assertNotIn("", frames)

    async def test_eof_flush_is_local_to_the_connection(self):
        raw = "中".encode()
        self.assertEqual(await self.stream([raw[:2]]), ["\ufffd"])
        self.assertEqual(await self.stream([raw[:2], raw[2:]]), ["中"])


if __name__ == "__main__":
    unittest.main()
