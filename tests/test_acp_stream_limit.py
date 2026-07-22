"""ACP subprocess stream limits."""

import unittest
from unittest.mock import AsyncMock, Mock, patch

import acp_client


class TestACPStreamLimit(unittest.IsolatedAsyncioTestCase):
    async def test_spawn_accepts_large_json_rpc_lines(self):
        session = acp_client.ACPSession("/tmp/no-acp-state")
        proc = Mock()
        proc.returncode = None
        spawn = AsyncMock(return_value=proc)

        with (
            patch.object(acp_client.asyncio, "create_subprocess_exec", spawn),
            patch.object(session, "_read_loop", AsyncMock()),
            patch.object(session, "_request", AsyncMock(
                side_effect=[{}, {"sessionId": "test-session"}])),
            patch.object(session, "_latest_telegram_session", return_value=None),
        ):
            await session.ensure_started()

        self.assertEqual(session.session_id, "test-session")
        self.assertEqual(spawn.await_args.kwargs["limit"], acp_client.ACP_STREAM_LIMIT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
