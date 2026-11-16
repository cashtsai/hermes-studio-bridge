"""OpenCodeClient(opencode_sdk + acp_rail)—— ACP 傳輸層的行為測試。

子程序以假物件頂替(monkeypatch asyncio.create_subprocess_exec),stdout
餵的是 **2026-11-16 真跑 `opencode acp`(1.19.5)錄下的 frame**
(tests/fixtures/opencode/recorded-stdio-20261116.txt;POCKET_RECORD_FIXTURE
慣例的 ACP 版):initialize / session/new / session/load /
set_config_option(含錯誤欄名那一發)/ request_permission / prompt 的
end_turn 與 cancelled、session/list 全是原樣 wire bytes。

釘住:
  1. 握手 = initialize 一發,**沒有 authenticate**(這家零認證可用)
  2. server_info 存 initialize 全文,loadSession/image 從那裡 feature-detect
  3. **沒有登入牆**:沒有任何憑證也照樣開 session(與 grok 的分水嶺)
  4. session/load 續接 + **replay 擷取**(歷史補洞的來源,零額外 RPC)
  5. 載不回 → session/new 換 acp_sid 續命
  6. 新 session 自動套用偏好模型(set_config_option,欄位名 configId)
  7. session/update 逐字外流 on_update;prompt 回 stopReason
  8. cancel = session/cancel notification(無 id);行程不在不冷啟
  9. request_permission 沒人接 → 保守回 cancelled(turn 不准懸空)
 10. session/list 原生列表;行程沒活著回 None(列表熱路徑不准冷啟)
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

import opencode_sdk  # noqa: E402

_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "opencode", "recorded-stdio-20261116.txt")


def _recorded(direction: str, needle: str) -> dict:
    """從錄音檔撈一幀(direction: '->' 送出 / '<-' 收到)。"""
    with open(_FIXTURE, encoding="utf-8") as f:
        for line in f:
            if line.startswith(direction) and needle in line:
                return json.loads(line[2:].strip())
    raise AssertionError(f"fixture 裡沒有 {direction} {needle}")


#: 真機 initialize 回應(原樣)。
REAL_INIT = _recorded("<-", '"id":1')["result"]
#: 真機 session/new 回應(帶 sessionId + configOptions)。
REAL_NEW = _recorded("<-", '"sessionId":"ses_e1b948663ffeYHXKtOYCeHNNDV","configOptions"')["result"]
#: 真機 session/load 回應(只有 configOptions,歷史走 replay 通知)。
REAL_LOAD = _recorded("<-", '"id":102')["result"]
#: 真機 set_config_option 成功回應。
REAL_SETCFG = _recorded("<-", '"id":103')["result"]
#: 真機 set_config_option **錯誤欄名**(configOptionId)的 -32602。
REAL_SETCFG_ERR = _recorded("<-", '"id":3')["error"]
#: 真機 request_permission 請求(options = once/always/reject)。
REAL_PERM = _recorded("<-", "session/request_permission")
#: 真機 session/list 回應。
REAL_LIST = _recorded("<-", '"sessions":[')["result"]
#: 真機 prompt 回應(end_turn / cancelled)。
REAL_END = _recorded("<-", '"stopReason":"end_turn"')["result"]
REAL_CANCELLED = _recorded("<-", '"stopReason":"cancelled"')["result"]
#: 真機 replay 幀(session/load 之後流回來的 user_message_chunk)。
REAL_REPLAY = _recorded("<-", '"sessionUpdate":"user_message_chunk"')

REAL_SID = "ses_e1b948663ffeYHXKtOYCeHNNDV"


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
    """腳本化的 `opencode acp` 替身。script: method -> handler(req)。
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
    """一條測試用 client + 假行程(所有落檔都在 tmp)。"""

    def __init__(self, script, api_key: str = ""):
        self.tmp = tempfile.mkdtemp(prefix="oce-client-")
        self.script = script
        #: FakeProc 要在事件圈內才能建(StreamReader 綁 loop)。
        self.proc = None
        self.patches = [
            patch.object(opencode_sdk, "_STATE_DIR", self.tmp),
            patch.object(opencode_sdk, "_SESSIONS_FILE",
                         os.path.join(self.tmp, "sessions.json")),
            patch.object(opencode_sdk, "_CONFIG_FILE",
                         os.path.join(self.tmp, "opencode.json")),
            patch.object(opencode_sdk, "_DEFAULT_CWD",
                         os.path.join(self.tmp, "workspace")),
            patch.object(opencode_sdk, "_AUTH_FILE",
                         os.path.join(self.tmp, "auth.json")),
            patch.object(opencode_sdk, "opencode_bin",
                         lambda: "/fake/bin/opencode"),
        ]
        self._api_key = api_key
        self.spawned = []

        async def fake_spawn(*argv, **kw):
            self.spawned.append((argv, kw))
            self.proc = FakeProc(self.script)
            return self.proc
        self.patches.append(patch.object(
            opencode_sdk.acp_rail.asyncio, "create_subprocess_exec",
            fake_spawn))

    def __enter__(self):
        for p in self.patches:
            p.start()
        if self._api_key:
            opencode_sdk.save_config(api_key=self._api_key)
        self.client = opencode_sdk.OpenCodeClient()
        return self

    def __exit__(self, *a):
        for p in self.patches:
            p.stop()
        return False

    def sent(self, method):
        return [f for f in self.proc.stdin.frames if f.get("method") == method]


def _basic_script(sid=REAL_SID):
    new_res = dict(REAL_NEW)
    new_res["sessionId"] = sid
    return {
        "initialize": lambda req: ([], REAL_INIT, None),
        "session/new": lambda req: ([], new_res, None),
        "session/set_config_option": lambda req: ([], REAL_SETCFG, None),
    }


class HandshakeTest(unittest.TestCase):
    def test_initialize_only_no_authenticate(self):
        with ClientHarness(_basic_script()) as h:
            acp_sid = asyncio.run(h.client.ensure_session("k1"))
            self.assertEqual(acp_sid, REAL_SID)
            self.assertEqual(len(h.sent("initialize")), 1)
            self.assertEqual(h.sent("authenticate"), [],
                             "opencode 不需要 authenticate 握手")
            self.assertTrue(h.client.supports_load(),
                            "loadSession 要從 server_info feature-detect")
            self.assertTrue(h.client.supports_image(),
                            "promptCapabilities.image 實測為 true")
            self.assertEqual(opencode_sdk.get_session("k1")["acp_sid"], REAL_SID)

    def test_no_login_wall_zero_credentials_still_opens_session(self):
        """**這家與 grok 的分水嶺**:沒有任何憑證(auth.json 不存在、沒有
        OpenRouter key)也要開得起 session —— 內建免費模型池實測可用。
        把 logged_in() 當閘門等於把一個能用的 provider 關掉。"""
        with ClientHarness(_basic_script()) as h:
            self.assertFalse(h.client.logged_in(), "前提:真的沒有憑證")
            acp_sid = asyncio.run(h.client.ensure_session("k1"))
            self.assertEqual(acp_sid, REAL_SID)
            self.assertEqual(len(h.spawned), 1, "沒憑證也要照開行程")

    def test_openrouter_key_lands_in_child_env(self):
        with ClientHarness(_basic_script(), api_key="sk-or-test") as h:
            asyncio.run(h.client.ensure_session("k1"))
            env = h.spawned[0][1]["env"]
            self.assertEqual(env.get("OPENROUTER_API_KEY"), "sk-or-test",
                             "落檔的 key 要進子程序 env(官方文件的接法)")

    def test_spawn_argv_is_acp_without_model_flag(self):
        """模型**不是** spawn 參數(與 grok 的差別)—— argv 永遠是 `acp`,
        模型走 per-session 的 set_config_option。"""
        with ClientHarness(_basic_script()) as h:
            opencode_sdk.save_config(model="openrouter/some/model")
            asyncio.run(h.client.ensure_session("k1"))
            argv = list(h.spawned[0][0])
            self.assertEqual(argv, ["/fake/bin/opencode", "acp"])

    def test_missing_binary_is_no_binary_error(self):
        with ClientHarness(_basic_script()) as h:
            with patch.object(opencode_sdk, "opencode_bin", lambda: ""):
                with self.assertRaises(opencode_sdk.OpenCodeError) as ctx:
                    asyncio.run(h.client.ensure_session("k1"))
            self.assertEqual(ctx.exception.code, "NO_BINARY")
            self.assertIn("opencode.ai/install", str(ctx.exception),
                          "錯誤要含安裝指令")


class ModelPreferenceTest(unittest.TestCase):
    def test_new_session_applies_preferred_model_with_config_id_field(self):
        """新 session 要自動套用偏好模型,否則設定面板選的模型對新對話無效。
        欄位名必須是 `configId` —— 實測寫 `configOptionId` 回 -32602
        (那一發錯誤也在 fixture 裡,見下面那條測試)。"""
        with ClientHarness(_basic_script()) as h:
            opencode_sdk.save_config(model="opencode/glm-5.3-flash-free")
            asyncio.run(h.client.ensure_session("k1"))
            sets = h.sent("session/set_config_option")
            self.assertEqual(len(sets), 1)
            self.assertEqual(sets[0]["params"], {
                "sessionId": REAL_SID, "configId": "model",
                "value": "opencode/glm-5.3-flash-free"})

    def test_wrong_field_name_is_the_recorded_minus_32602(self):
        """錄音檔釘住這個坑:`configOptionId` 是錯的欄位名。"""
        self.assertEqual(REAL_SETCFG_ERR["code"], -32602)
        self.assertIn("configId", json.dumps(REAL_SETCFG_ERR))

    def test_no_preference_means_no_write(self):
        """沒設偏好 → 不要去動 session 的模型(使用者/CLI 的預設是他的選擇)。"""
        with ClientHarness(_basic_script()) as h:
            asyncio.run(h.client.ensure_session("k1"))
            self.assertEqual(h.sent("session/set_config_option"), [])

    def test_model_apply_failure_does_not_block_session(self):
        script = {**_basic_script(),
                  "session/set_config_option": lambda req: (
                      [], None, {"code": -32000, "message": "nope"})}
        with ClientHarness(script) as h:
            opencode_sdk.save_config(model="bad/model")
            sid = asyncio.run(h.client.ensure_session("k1"))
            self.assertEqual(sid, REAL_SID, "套不上模型不該擋掉開 session")

    def test_current_config_projects_contract_shape(self):
        with ClientHarness(_basic_script()) as h:
            asyncio.run(h.client.ensure_session("k1"))
            cfg = h.client.current_config(REAL_SID)
            self.assertEqual(cfg["model"], "opencode/big-pickle")
            self.assertEqual(cfg["mode"], "build")
            self.assertTrue(any(o["value"] == "opencode/beam-free"
                                for o in cfg["model_options"]),
                            "可選模型列要帶出來(app 要畫選單)")


class ResumeTest(unittest.TestCase):
    def test_resume_uses_session_load_and_captures_replay(self):
        """session/load 續接,**並且**把它 replay 回來的歷史擷下來 ——
        這是這家補歷史的來源(grok 要探私有擴展,這家是 load 的副產品)。"""
        def load(req):
            return ([REAL_REPLAY,
                     _upd(REAL_SID, "agent_message_chunk", "PONG")],
                    REAL_LOAD, None)
        script = {**_basic_script(), "session/load": load}
        with ClientHarness(script) as h:
            opencode_sdk.remember_session("k1", REAL_SID, cwd=h.tmp)
            sid = asyncio.run(h.client.ensure_session("k1"))
            self.assertEqual(sid, REAL_SID)
            self.assertEqual(h.sent("session/new"), [],
                             "載得回就不准另開新 session(歷史會分家)")
            msgs = h.client.replay_messages("k1")
            self.assertEqual([(m["role"], m["content"]) for m in msgs],
                             [("user", "Reply with exactly: PONG"),
                              ("assistant", "PONG")])

    def test_load_failure_falls_back_to_new_sid(self):
        script = {**_basic_script("ses_fresh"),
                  "session/load": lambda req: (
                      [], None, {"code": -32000, "message": "not found"})}
        with ClientHarness(script) as h:
            opencode_sdk.remember_session("k1", "ses_gone", cwd=h.tmp)
            sid = asyncio.run(h.client.ensure_session("k1"))
            self.assertEqual(sid, "ses_fresh")
            self.assertEqual(opencode_sdk.get_session("k1")["acp_sid"],
                             "ses_fresh",
                             "名錄要換 acp_sid 續命,bridge 端歷史不動")

    def test_live_turn_frames_are_not_mistaken_for_replay(self):
        """replay 擷取只在 load 的窗口內 —— 回合中的逐字不准被當成歷史
        (否則 history 會把現在這輪再貼一次)。"""
        def prompt(req):
            return ([_upd(REAL_SID, "agent_message_chunk", "live")],
                    REAL_END, None)
        script = {**_basic_script(), "session/prompt": prompt}
        with ClientHarness(script) as h:
            async def main():
                sid = await h.client.ensure_session("k1")
                await h.client.call("session/prompt",
                                    {"sessionId": sid,
                                     "prompt": [{"type": "text", "text": "x"}]})
            asyncio.run(main())
            self.assertIsNone(h.client.replay_messages("k1"))


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
                                            "title": "bash"}}}],
                    REAL_END, None)
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
            self.assertEqual(res["stopReason"], "end_turn")
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
            self.assertEqual(cancels[0]["params"]["sessionId"], REAL_SID)

    def test_cancel_never_cold_starts(self):
        """行程不在就不要為了「中斷」開一顆行程(冷啟 5~26 秒,而且根本
        沒有進行中的回合可中斷)。"""
        with ClientHarness(_basic_script()) as h:
            opencode_sdk.remember_session("k1", REAL_SID)
            asyncio.run(h.client.cancel("k1"))
            self.assertEqual(h.spawned, [])

    def test_recorded_cancelled_stop_reason_is_a_real_outcome(self):
        """實測:cancel 之後 prompt 在 0.7 秒內回 cancelled —— 這就是
        caps 敢宣告 interrupt=True 的證據(不是 advisory)。"""
        self.assertEqual(REAL_CANCELLED["stopReason"], "cancelled")

    def test_unanswered_permission_request_gets_cancelled(self):
        """沒掛 on_request 時,server→client 的 request_permission 要收到
        保守的 cancelled 回覆 —— 不回,opencode 的 turn 會永遠掛著。"""
        perm = dict(REAL_PERM)
        perm["id"] = 777

        def prompt(req):
            return ([perm], REAL_END, None)
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

    def test_recorded_permission_options_are_once_always_reject(self):
        kinds = [o["kind"] for o in REAL_PERM["params"]["options"]]
        self.assertEqual(kinds, ["allow_once", "allow_always", "reject_once"])


class NativeListTest(unittest.TestCase):
    def test_list_native_returns_rows(self):
        script = {**_basic_script(),
                  "session/list": lambda req: ([], REAL_LIST, None)}
        with ClientHarness(script) as h:
            async def main():
                await h.client.ensure_session("k1")
                return await h.client.list_native()
            rows = asyncio.run(main())
            self.assertTrue(rows)
            self.assertEqual(rows[0]["sessionId"], REAL_SID)
            self.assertTrue(rows[0]["title"], "原生列表有 agent 生成的標題")

    def test_dead_proc_returns_none_never_cold_starts(self):
        with ClientHarness(_basic_script()) as h:
            out = asyncio.run(h.client.list_native())
            self.assertIsNone(out)
            self.assertEqual(h.spawned, [], "列表熱路徑不准冷啟")

    def test_method_not_found_is_swallowed_to_none(self):
        with ClientHarness(_basic_script()) as h:   # script 沒有 session/list
            async def main():
                await h.client.ensure_session("k1")
                return await h.client.list_native()
            self.assertIsNone(asyncio.run(main()))


class ProcessLossTest(unittest.TestCase):
    def test_in_flight_call_fails_with_process_lost(self):
        """行程死掉時在途呼叫要收到 PROCESS_LOST,不是無限等
        (acp_rail 的共用保證,兩家一起受益)。"""
        def prompt(req):
            # 不回應,直接讓行程死
            req_proc.kill()
            return ([], None, None)
        script = {**_basic_script(), "session/prompt": prompt}
        with ClientHarness(script) as h:
            async def main():
                global req_proc
                sid = await h.client.ensure_session("k1")
                req_proc = h.proc
                with self.assertRaises(opencode_sdk.OpenCodeError) as ctx:
                    await h.client.call("session/prompt",
                                        {"sessionId": sid, "prompt": []},
                                        timeout=5)
                return ctx.exception
            exc = asyncio.run(main())
            self.assertEqual(exc.code, "PROCESS_LOST")
            self.assertTrue(exc.retryable)


if __name__ == "__main__":
    unittest.main(verbosity=2)
