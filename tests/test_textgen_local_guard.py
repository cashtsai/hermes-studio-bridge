"""2026-11-28 Ollama 飽和事故的兩道護欄:
① harness.model.ollama_text 預設關思考(think=False),不支援的模型 400 自動退;
② text_gen 本機 lane 連續逾時 3 次就冷卻,堵住「棄單佔滿 slot → 永遠逾時」迴圈。
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import text_gen
from harness import model as harness_model


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeResponse:
    def __init__(self, status_code=200, text="", content=""):
        self.status_code = status_code
        self.text = text
        self._content = content

    def json(self):
        return {"message": {"content": self._content}}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class FakeClient:
    """httpx.AsyncClient 替身 —— 記錄 payload,照腳本回應。"""
    def __init__(self, script):
        self.script = list(script)
        self.payloads = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        self.payloads.append(json)
        return self.script.pop(0)


class OllamaThinkTests(unittest.TestCase):
    def _client(self, script):
        client = FakeClient(script)
        fake_httpx = MagicMock()
        fake_httpx.AsyncClient = lambda **kw: client
        return client, patch.dict("sys.modules", {"httpx": fake_httpx})

    def test_think_disabled_by_default(self):
        client, ctx = self._client([FakeResponse(content="好")])
        with ctx:
            out = run(harness_model.ollama_text("hi", model="m"))
        self.assertEqual(out, "好")
        self.assertIs(client.payloads[0]["think"], False)

    def test_unsupported_model_retries_without_think(self):
        client, ctx = self._client([
            FakeResponse(400, text='"m" does not support thinking'),
            FakeResponse(content="好"),
        ])
        with ctx:
            out = run(harness_model.ollama_text("hi", model="m"))
        self.assertEqual(out, "好")
        self.assertEqual(len(client.payloads), 2)
        self.assertNotIn("think", client.payloads[1])

    def test_unrelated_400_does_not_retry(self):
        client, ctx = self._client([FakeResponse(400, text="model not found")])
        with ctx:
            with self.assertRaises(RuntimeError):
                run(harness_model.ollama_text("hi", model="m"))
        self.assertEqual(len(client.payloads), 1)


class LocalTimeoutStreakTests(unittest.TestCase):
    def setUp(self):
        text_gen.reset_local_cooldown()
        self.addCleanup(text_gen.reset_local_cooldown)

    def lane(self):
        return run(text_gen._lane_local("p", "s", 0.1))

    def test_three_consecutive_timeouts_trigger_cooldown(self):
        with patch.object(harness_model, "ollama_text",
                          AsyncMock(side_effect=asyncio.TimeoutError)) as mk:
            for _ in range(3):
                self.assertEqual(self.lane().reason, "local_timeout")
            self.assertEqual(self.lane().reason, "local_cooldown")  # 第 4 次不打模型
            self.assertEqual(mk.await_count, 3)

    def test_success_resets_the_streak(self):
        boom = AsyncMock(side_effect=asyncio.TimeoutError)
        ok = AsyncMock(return_value="好")
        with patch.object(harness_model, "ollama_text", boom):
            self.lane(); self.lane()
        with patch.object(harness_model, "ollama_text", ok):
            self.assertTrue(self.lane().ok)
        with patch.object(harness_model, "ollama_text", boom):
            self.lane(); self.lane()                       # 重新數,還沒到 3
            self.assertEqual(self.lane().reason, "local_timeout")
            self.assertEqual(self.lane().reason, "local_cooldown")

    def test_single_timeout_does_not_cool_down(self):
        with patch.object(harness_model, "ollama_text",
                          AsyncMock(side_effect=asyncio.TimeoutError)):
            self.assertEqual(self.lane().reason, "local_timeout")
        with patch.object(harness_model, "ollama_text", AsyncMock(return_value="好")):
            self.assertTrue(self.lane().ok)                # 沒被冷卻擋住


if __name__ == "__main__":
    unittest.main()
