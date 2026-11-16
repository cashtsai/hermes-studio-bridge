"""GrokClient(grok_sdk)—— ACP 傳輸層的行為測試。

子程序以假物件頂替(monkeypatch asyncio.create_subprocess_exec),stdout
餵的是 **2026-11-16 真跑 `grok agent stdio`(1.0.77)錄下的 frame**
(tests/fixtures/grok/recorded-stdio-20261116.txt;POCKET_RECORD_FIXTURE
慣例的 ACP 版):initialize 回應與 session/new 的 Authentication required
錯誤都是原樣 wire bytes。串流 turn 的幀(message/thought/tool_call)因錄製
機未登入拿不到,按標準 ACP 形狀合成 —— 與 gemini 線上跑的同構,來源已標注。

釘住:
  1. 握手 = initialize 一發,**沒有 authenticate**(grok 憑證是 ambient 的)
  2. server_info 存 initialize 全文,loadSession 從那裡讀(不硬編)
  3. 未登入 → ensure_session 在 spawn 前就 NOT_LOGGED_IN(不白開行程)
  4. session/load 續接;載不回退 session/new 換 acp_sid 續命
  5. session/update 逐字外流 on_update;prompt 回 stopReason
  6. cancel = session/cancel notification(無 id)
  7. request_permission 沒人接 → 保守回 cancelled(turn 不准懸空)
  8. x.ai/session/updates:-32601 探測退場,之後不再敲
"""
import _isolation  # noqa: F401
import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grok_sdk  # noqa: E402

_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "grok", "recorded-stdio-20261116.txt")


def _recorded(direction: str, needle: str) -> dict:
    """從錄音檔撈一幀(direction: '->' 送出 / '<-' 收到)。"""
    with open(_FIXTURE, encoding="utf-8") as f:
        for line in f:
            if line.startswith(direction) and needle in line:
                return json.loads(line[2:].strip())
    raise AssertionError(f"fixture 裡沒有 {direction} {needle}")


#: 真機 initialize 回應(原樣)。
REAL_INIT = _recorded("<-", '"id":1')["result"]
#: 真機 session/new 未登入錯誤(原樣)。
REAL_AUTH_ERR = _recorded("<-", "Authentication required")["error"]


class FakeStdin:
    def __init__(self, proc):
        self.proc = proc
        self.frames: list[dict] = []     # client 寫出的每一幀(含 notification)

    def write(self, data: bytes):
        for line in data.decode().splitlines():
            if not line.strip():
                continue
            f = json.loads(line)
            self.frames.append(f)
            self.proc._on_client_frame(f)

    async def drain(self):
        pass

    def close(self):
        pass


class FakeProc:
    """腳本化的 `grok agent stdio` 替身。script: method -> handler(req)。
    handler 回 (先餵的通知列表, result|None, error|None)。"""

    def __init__(self, script):
        self.script = script
        self.stdout = asyncio.StreamReader()
        self.stdin = FakeStdin(self)
        self.returncode = None

    def feed(self, obj: dict):
        self.stdout.feed_data((json.dumps(obj) + "\n").encode())

    def _on_client_frame(self, f: dict):
        method = f.get("method")
        if method is None or "id" not in f:
            return                       # notification / request 的回覆
        handler = self.script.get(method)
        if handler is None:
            self.feed({"jsonrpc": "2.0", "id": f["id"],
                       "error": {"code": -32601,
                                 "message": f"method not found: {method}"}})
            return
        notifs, result, error = handler(f)
        for n in notifs:
            self.feed(n)
        if error is not None:
            self.feed({"jsonrpc": "2.0", "id": f["id"], "error": error})
        elif result is not None:
            self.feed({"jsonrpc": "2.0", "id": f["id"], "result": result})

    def kill(self):
        self.returncode = -9
        self.stdout.feed_eof()


def _upd(sid, kind, text):
    return {"jsonrpc": "2.0", "method": "session/update",
            "params": {"sessionId": sid,
                       "update": {"sessionUpdate": kind,
                                  "content": {"type": "text", "text": text}}}}


class ClientHarness:
    """一條測試用 client + 假行程 + 登入狀態假裝(auth 檔在 tmp)。"""

    def __init__(self, script, logged_in=True):
        self.tmp = tempfile.mkdtemp(prefix="gk-client-")
        self.script = script
        #: FakeProc 要在事件圈內才能建(StreamReader 綁 loop)——
        #: 由 fake_spawn 延後生,spawn 前讀 self.proc 是 None。
        self.proc = None
        self.patches = [
            patch.object(grok_sdk, "_STATE_DIR", self.tmp),
            patch.object(grok_sdk, "_SESSIONS_FILE",
                         os.path.join(self.tmp, "sessions.json")),
            patch.object(grok_sdk, "_CONFIG_FILE",
                         os.path.join(self.tmp, "grok.json")),
            patch.object(grok_sdk, "_DEFAULT_CWD",
                         os.path.join(self.tmp, "workspace")),
            patch.object(grok_sdk, "_AUTH_FILE",
                         os.path.join(self.tmp, "auth.json")),
            patch.object(grok_sdk, "grok_bin",
                         lambda: "/fake/bin/grok"),
        ]
        if logged_in:
            with open(os.path.join(self.tmp, "auth.json"), "w") as f:
                f.write("{}")
        self.spawned = []

        async def fake_spawn(*argv, **kw):
            self.spawned.append((argv, kw))
            self.proc = FakeProc(self.script)
            return self.proc
        self.patches.append(patch.object(
            grok_sdk.asyncio, "create_subprocess_exec", fake_spawn))

    def __enter__(self):
        for p in self.patches:
            p.start()
        self.client = grok_sdk.GrokClient()
        return self

    def __exit__(self, *a):
        for p in self.patches:
            p.stop()
        return False

    def sent(self, method):
        return [f for f in self.proc.stdin.frames if f.get("method") == method]


def _basic_script(sid="acp-123"):
    return {
        "initialize": lambda req: ([], REAL_INIT, None),
        "session/new": lambda req: ([], {"sessionId": sid}, None),
    }


class HandshakeTest(unittest.TestCase):
    def test_initialize_only_no_authenticate(self):
        with ClientHarness(_basic_script()) as h:
            acp_sid = asyncio.run(h.client.ensure_session("k1"))
            self.assertEqual(acp_sid, "acp-123")
            self.assertEqual(len(h.sent("initialize")), 1)
            self.assertEqual(h.sent("authenticate"), [],
                             "grok 憑證是 ambient 的,不可以發 authenticate")
            self.assertTrue(h.client.server_info["agentCapabilities"]
                            ["loadSession"], "server_info 要存 initialize 全文")
            # 名錄要記下 key ↔ acp_sid
            self.assertEqual(grok_sdk.get_session("k1")["acp_sid"], "acp-123")

    def test_not_logged_in_raises_before_spawn(self):
        with ClientHarness(_basic_script(), logged_in=False) as h:
            with self.assertRaises(grok_sdk.GrokError) as ctx:
                asyncio.run(h.client.ensure_session("k1"))
            self.assertEqual(ctx.exception.code, "NOT_LOGGED_IN")
            self.assertIn("grok login", str(ctx.exception), "錯誤要含登入指引")
            self.assertEqual(h.spawned, [], "未登入不准白開行程")

    def test_api_key_counts_as_logged_in_and_lands_in_env(self):
        with ClientHarness(_basic_script(), logged_in=False) as h:
            grok_sdk.save_config(api_key="xai-test-key")
            asyncio.run(h.client.ensure_session("k1"))
            env = h.spawned[0][1]["env"]
            self.assertEqual(env.get("XAI_API_KEY"), "xai-test-key",
                             "落檔的 key 要進子程序 env(CLI 憑證解析序)")

    def test_real_auth_required_error_surfaces(self):
        """真機錄到的 session/new 錯誤(Authentication required)要原樣
        變成 GrokError —— bridge 的失敗規則靠這段字翻登入指引。"""
        script = {"initialize": lambda req: ([], REAL_INIT, None),
                  "session/new": lambda req: ([], None, REAL_AUTH_ERR)}
        with ClientHarness(script) as h:
            with self.assertRaises(grok_sdk.GrokError) as ctx:
                asyncio.run(h.client.ensure_session("k1"))
            self.assertIn("Authentication required", str(ctx.exception))


class ResumeTest(unittest.TestCase):
    def test_resume_uses_session_load(self):
        calls = []

        def load(req):
            calls.append(req)
            return ([], {}, None)
        script = {**_basic_script(), "session/load": load}
        with ClientHarness(script) as h:
            grok_sdk.remember_session("k1", "acp-old", cwd=h.tmp)
            sid = asyncio.run(h.client.ensure_session("k1"))
            self.assertEqual(sid, "acp-old")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["params"]["sessionId"], "acp-old")
            self.assertEqual(h.sent("session/new"), [],
                             "載得回就不准另開新 session(歷史會分家)")

    def test_load_failure_falls_back_to_new_sid(self):
        script = {**_basic_script("acp-fresh"),
                  "session/load": lambda req: (
                      [], None, {"code": -32000, "message": "not found"})}
        with ClientHarness(script) as h:
            grok_sdk.remember_session("k1", "acp-gone", cwd=h.tmp)
            sid = asyncio.run(h.client.ensure_session("k1"))
            self.assertEqual(sid, "acp-fresh")
            self.assertEqual(grok_sdk.get_session("k1")["acp_sid"], "acp-fresh",
                             "名錄要換 acp_sid 續命,bridge 端歷史不動")


class TurnTest(unittest.TestCase):
    def test_prompt_streams_updates_and_returns_stop_reason(self):
        def prompt(req):
            sid = req["params"]["sessionId"]
            return ([_upd(sid, "agent_thought_chunk", "想一下"),
                     _upd(sid, "agent_message_chunk", "你好"),
                     _upd(sid, "agent_message_chunk", ",世界"),
                     {"jsonrpc": "2.0", "method": "session/update",
                      "params": {"sessionId": sid,
                                 "update": {"sessionUpdate": "tool_call",
                                            "title": "ReadFile: a.txt"}}}],
                    {"stopReason": "end_turn"}, None)
        script = {**_basic_script(), "session/prompt": prompt}
        with ClientHarness(script) as h:
            got = []
            h.client.on_update = lambda sid, u: got.append(
                (u.get("sessionUpdate"),
                 (u.get("content") or {}).get("text", "")))

            async def main():
                sid = await h.client.ensure_session("k1")
                return await h.client.call(
                    "session/prompt",
                    {"sessionId": sid,
                     "prompt": [{"type": "text", "text": "hi"}]})
            res = asyncio.run(main())
            self.assertEqual(res, {"stopReason": "end_turn"})
            self.assertIn(("agent_message_chunk", "你好"), got)
            self.assertIn(("agent_message_chunk", ",世界"), got)
            self.assertIn(("agent_thought_chunk", "想一下"), got)
            self.assertEqual(got[-1][0], "tool_call")

    def test_cancel_sends_notification_without_id(self):
        with ClientHarness(_basic_script()) as h:
            async def main():
                await h.client.ensure_session("k1")
                await h.client.cancel("k1")
            asyncio.run(main())
            cancels = h.sent("session/cancel")
            self.assertEqual(len(cancels), 1)
            self.assertNotIn("id", cancels[0], "cancel 是 notification")
            self.assertEqual(cancels[0]["params"]["sessionId"], "acp-123")

    def test_unanswered_permission_request_gets_cancelled(self):
        """沒掛 on_request 時,server→client 的 request_permission 要收到
        保守的 cancelled 回覆 —— 不回,grok 的 turn 會永遠掛著。"""
        def prompt(req):
            return ([{"jsonrpc": "2.0", "id": 777,
                      "method": "session/request_permission",
                      "params": {"sessionId": req["params"]["sessionId"],
                                 "toolCall": {"title": "Bash"},
                                 "options": [{"optionId": "a",
                                              "kind": "allow_once",
                                              "name": "Allow"}]}}],
                    {"stopReason": "end_turn"}, None)
        script = {**_basic_script(), "session/prompt": prompt}
        with ClientHarness(script) as h:
            async def main():
                sid = await h.client.ensure_session("k1")
                await h.client.call("session/prompt",
                                    {"sessionId": sid,
                                     "prompt": [{"type": "text", "text": "x"}]})
                await asyncio.sleep(0.05)    # 讓回答 task 跑完
            asyncio.run(main())
            answers = [f for f in h.proc.stdin.frames if f.get("id") == 777
                       and "result" in f]
            self.assertEqual(len(answers), 1)
            self.assertEqual(answers[0]["result"],
                             {"outcome": {"outcome": "cancelled"}})


class UpdatesFeatureDetectTest(unittest.TestCase):
    def test_method_not_found_marks_unsupported_and_stops_asking(self):
        hits = []

        def updates(req):
            hits.append(1)
            return ([], None, {"code": -32601, "message": "method not found"})
        script = {**_basic_script(), "x.ai/session/updates": updates}
        with ClientHarness(script) as h:
            async def main():
                await h.client.ensure_session("k1")
                a = await h.client.updates_tail("k1", limit=5)
                b = await h.client.updates_tail("k1", limit=5)
                return a, b
            a, b = asyncio.run(main())
            self.assertIsNone(a)
            self.assertIsNone(b)
            self.assertEqual(len(hits), 1, "-32601 之後不准再敲(探測退場)")

    def test_native_tail_returns_updates_with_negative_offset(self):
        seen = {}

        def updates(req):
            seen.update(req["params"])
            return ([], {"updates": [{"update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "hi"}}}],
                "totalCount": 1, "hasMore": False}, None)
        script = {**_basic_script(), "x.ai/session/updates": updates}
        with ClientHarness(script) as h:
            async def main():
                await h.client.ensure_session("k1")
                return await h.client.updates_tail("k1", limit=5)
            out = asyncio.run(main())
            self.assertEqual(len(out), 1)
            self.assertEqual(seen["offset"], -5, "負 offset 取尾(研究報告 §3)")

    def test_dead_proc_returns_none_never_cold_starts(self):
        with ClientHarness(_basic_script()) as h:
            # 行程根本沒起 → None,且不准為了歷史冷啟(spawned 保持空)
            out = asyncio.run(h.client.updates_tail("k1", limit=5))
            self.assertIsNone(out)
            self.assertEqual(h.spawned, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
