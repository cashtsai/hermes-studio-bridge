"""CC provider v2(cc2,Agent SDK)—— 旗標閘門/卡片流契約/佇列/審批
fail-closed/失效 resume 自癒/state 檔往返。

SDK 全假(cc_sdk._sdk_mod 注入假模組),絕不 spawn 真的 claude CLI;
所有落地路徑(canonical DB / registry DB / CC_SDK_STATE)都指 temp。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="cc-sdk-test-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("POCKET_REGISTRY_DB", os.path.join(_TMP, "bridge-registry.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ.setdefault("CC_SDK_STATE", os.path.join(_TMP, "cc-sdk-sessions.json"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
import cc_sdk  # noqa: E402
from fastapi import HTTPException  # noqa: E402


# ───────────────────────── 假 SDK(import 面全替換)─────────────────────────

class FakeTextBlock:
    def __init__(self, text):
        self.text = text


class FakeToolUseBlock:
    def __init__(self, id, name, input):  # noqa: A002
        self.id, self.name, self.input = id, name, input


class FakeAssistantMessage:
    def __init__(self, content, session_id=None):
        self.content = content
        self.session_id = session_id


class FakeResultMessage:
    def __init__(self, subtype="success", is_error=False, session_id="sdk-sess-1",
                 result=None):
        self.subtype, self.is_error = subtype, is_error
        self.session_id, self.result = session_id, result


class FakeStreamEvent:
    def __init__(self, event, session_id="sdk-sess-1"):
        self.event, self.session_id = event, session_id
        self.uuid = "u"


class FakeSystemMessage:
    def __init__(self, subtype="init", data=None):
        self.subtype, self.data = subtype, dict(data or {})


class FakePermAllow:
    def __init__(self, **kw):
        self.behavior = "allow"


class FakePermDeny:
    def __init__(self, message="", **kw):
        self.behavior = "deny"
        self.message = message


class FakeOptions:
    """ClaudeAgentOptions 替身:收下 kwargs 原樣掛屬性(cc_sdk 只讀不驗)。"""

    def __init__(self, **kw):
        self.cwd = kw.get("cwd")
        self.permission_mode = kw.get("permission_mode")
        self.include_partial_messages = kw.get("include_partial_messages")
        self.resume = kw.get("resume")
        self.can_use_tool = kw.get("can_use_tool")


class FakeClient:
    """ClaudeSDKClient 替身。腳本由類屬性驅動(每個測試前 reset()):
    - connect_error(options) -> Exception|None:connect 是否炸
    - batches:list[list[msg]],第 n 次 query 對應第 n 批 receive_response
    - gate:asyncio.Event|None:receive_response 吐完非終態訊息後等它,
      測試才能在「turn 進行中」的窗口塞排隊輸入。
    """
    instances = []
    batches = []
    connect_error = staticmethod(lambda options: None)
    gate = None

    @classmethod
    def reset(cls):
        cls.instances, cls.batches, cls.gate = [], [], None
        cls.connect_error = staticmethod(lambda options: None)

    def __init__(self, options=None, transport=None):
        self.options = options
        self.queries = []
        self.interrupted = False
        self.disconnected = False
        FakeClient.instances.append(self)

    async def connect(self, prompt=None):
        err = FakeClient.connect_error(self.options)
        if err:
            raise err

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)

    async def receive_response(self):
        batch = FakeClient.batches.pop(0) if FakeClient.batches else []
        for msg in batch:
            if isinstance(msg, FakeResultMessage) and FakeClient.gate is not None:
                await FakeClient.gate.wait()
            yield msg

    async def interrupt(self):
        self.interrupted = True

    async def disconnect(self):
        self.disconnected = True


class FakeSDK:
    ClaudeSDKClient = FakeClient
    ClaudeAgentOptions = FakeOptions
    PermissionResultAllow = FakePermAllow
    PermissionResultDeny = FakePermDeny
    AssistantMessage = FakeAssistantMessage
    ResultMessage = FakeResultMessage
    StreamEvent = FakeStreamEvent
    SystemMessage = FakeSystemMessage
    TextBlock = FakeTextBlock
    ToolUseBlock = FakeToolUseBlock


class FakeRequest:
    headers = {"authorization": "Bearer test-unit-token"}
    client = None

    def __init__(self, body=None):
        self._body = body or {}

    async def json(self):
        return self._body


def _events(store, *types):
    return [e for e in store.events if e["type"] in types]


async def _drain(sess, timeout=2.0):
    """等 session 完全收工(turn task 結束 + 佇列清空)。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while sess.busy or sess.queue or (sess._task and not sess._task.done()):
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("session did not drain in time")
        await asyncio.sleep(0.005)


class CC2Base(unittest.TestCase):
    """每個測試:獨立 state 檔 + 旗標開 + 假 SDK + 全新註冊表。"""

    def setUp(self):
        self.state = tempfile.mktemp(suffix=".json", dir=_TMP)
        self._env = patch.dict(os.environ, {
            "CC_SDK_PROVIDER": "1", "CC_SDK_STATE": self.state})
        self._env.start()
        cc_sdk.reset_registry_for_tests()
        cc_sdk._sdk_mod = FakeSDK
        FakeClient.reset()

    def tearDown(self):
        cc_sdk._sdk_mod = None
        cc_sdk.reset_registry_for_tests()
        self._env.stop()


# ───────────────────────── 旗標閘門 ─────────────────────────

class TestFlagGate(CC2Base):
    def test_flag_off_everything_404(self):
        with patch.dict(os.environ, {"CC_SDK_PROVIDER": ""}):
            self.assertFalse(cc_sdk.enabled())
            with self.assertRaises(HTTPException) as cm:
                bridge._v2_card_source("cc2:alpha")
            self.assertEqual(cm.exception.status_code, 404)
            with self.assertRaises(HTTPException) as cm:
                asyncio.run(bridge.v2_cc2_create("alpha", FakeRequest()))
            self.assertEqual(cm.exception.status_code, 404)

    def test_flag_on_resolves_after_create(self):
        # 沒建過的名字照樣 404(不隱式建 session)
        with self.assertRaises(HTTPException) as cm:
            bridge._v2_card_source("cc2:alpha")
        self.assertEqual(cm.exception.status_code, 404)
        res = asyncio.run(bridge.v2_cc2_create(
            "alpha", FakeRequest({"workdir": _TMP,
                                  "permission_mode": "bypassPermissions"})))
        self.assertEqual(res["session_id"], "cc2:alpha")
        self.assertEqual(res["permission_mode"], "bypassPermissions")
        self.assertEqual(bridge._v2_card_source("cc2:alpha"), ("cc2", "alpha"))
        store = asyncio.run(bridge._v2_card_store("cc2:alpha"))
        self.assertIs(store, cc_sdk.registry().get("alpha").digest.store)
        # sessions 列表亮相
        rows = cc_sdk.registry().v2_rows()
        self.assertEqual([r["id"] for r in rows], ["cc2:alpha"])
        self.assertEqual(rows[0]["status"], "idle")

    def test_bad_name_and_mode_400(self):
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(bridge.v2_cc2_create("bad name!", FakeRequest()))
        self.assertEqual(cm.exception.status_code, 400)
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(bridge.v2_cc2_create(
                "ok", FakeRequest({"permission_mode": "yolo"})))
        self.assertEqual(cm.exception.status_code, 400)


# ───────────────────────── 卡片流契約 ─────────────────────────

class TestCardStream(CC2Base):
    def test_delta_stream_draft_rev_then_final(self):
        async def run():
            sess = cc_sdk.registry().ensure("s1", workdir=_TMP)
            FakeClient.batches = [[
                FakeStreamEvent({"type": "content_block_delta",
                                 "delta": {"type": "text_delta", "text": "你好"}}),
                FakeStreamEvent({"type": "content_block_delta",
                                 "delta": {"type": "text_delta", "text": ",世界"}}),
                FakeAssistantMessage([FakeTextBlock("你好,世界")]),
                FakeResultMessage(),
            ]]
            res = await sess.submit("hi")
            self.assertEqual(res["delivery"], "accepted")
            await _drain(sess)
            return sess

        sess = asyncio.run(run())
        store = sess.digest.store
        # draft 卡:同 id、rev 遞增、final False→False→True
        ups = [e["data"]["card"] for e in _events(store, "card.upsert")
               if e["data"]["card"]["role"] == "assistant"
               and e["data"]["card"]["kind"] == "markdown"]
        self.assertEqual(len(ups), 3)
        self.assertEqual(len({c["id"] for c in ups}), 1)
        self.assertEqual([c["rev"] for c in ups], [1, 2, 3])
        self.assertEqual([c["final"] for c in ups], [False, False, True])
        self.assertEqual([c["body"]["text"] for c in ups],
                         ["你好", "你好,世界", "你好,世界"])
        # user 卡在 assistant 卡之前
        order = [store.cards[i]["role"] for i in store.order]
        self.assertEqual(order[0], "user")

    def test_tool_use_and_error_result(self):
        async def run():
            sess = cc_sdk.registry().ensure("s2", workdir=_TMP)
            FakeClient.batches = [[
                FakeAssistantMessage([
                    FakeTextBlock("我來跑指令"),
                    FakeToolUseBlock("t1", "Bash", {"command": "ls -la\nrest"}),
                ]),
                FakeResultMessage(is_error=True, result="boom"),
            ]]
            await sess.submit("go")
            await _drain(sess)
            return sess

        sess = asyncio.run(run())
        store = sess.digest.store
        texts = [store.cards[i]["body"]["text"] for i in store.order]
        self.assertIn("🔧 Bash: ls -la", texts)
        self.assertTrue(any(t.startswith("⚠️ boom") for t in texts))
        self.assertFalse(store.status["busy"])

    def test_turn_end_idle_and_queue_drain(self):
        async def run():
            sess = cc_sdk.registry().ensure("s3", workdir=_TMP)
            FakeClient.gate = asyncio.Event()
            FakeClient.batches = [
                [FakeAssistantMessage([FakeTextBlock("first")]),
                 FakeResultMessage()],
                [FakeAssistantMessage([FakeTextBlock("second")]),
                 FakeResultMessage()],
            ]
            r1 = await sess.submit("msg-1")
            self.assertEqual(r1["delivery"], "accepted")
            await asyncio.sleep(0.02)          # 讓 turn 跑到 gate 前
            self.assertTrue(sess.busy)
            self.assertTrue(sess.digest.store.status["busy"])
            r2 = await sess.submit("msg-2")    # 忙碌 → 排隊,不回 4xx
            self.assertEqual(r2, {"delivery": "queued", "queued": True,
                                  "queue_depth": 1})
            FakeClient.gate.set()              # 放行第一輪的 ResultMessage
            await _drain(sess)
            return sess

        sess = asyncio.run(run())
        store = sess.digest.store
        # turn begin/end ×2,排隊那則在 turn end 後 drain
        turns = [e["data"]["state"] for e in _events(store, "turn")]
        self.assertEqual(turns, ["begin", "end", "begin", "end"])
        self.assertFalse(store.status["busy"])
        self.assertEqual(store.status["phase"], "idle")
        client = FakeClient.instances[0]       # 同一顆 client 跨 turn 保活
        self.assertEqual(client.queries, ["msg-1", "msg-2"])
        self.assertEqual(len(FakeClient.instances), 1)

    def test_bridge_input_route_and_interrupt(self):
        async def run():
            cc_sdk.registry().ensure("s4", workdir=_TMP)
            FakeClient.gate = asyncio.Event()
            FakeClient.batches = [[FakeResultMessage()]]
            res = await bridge.v2_session_input(
                "cc2:s4", FakeRequest({"content": "hello"}))
            self.assertTrue(res["accepted"])
            self.assertEqual(res["delivery"], "accepted")
            await asyncio.sleep(0.02)
            ok = await bridge.v2_session_interrupt("cc2:s4", FakeRequest())
            self.assertTrue(ok["interrupted"])
            self.assertTrue(FakeClient.instances[0].interrupted)
            FakeClient.gate.set()
            await _drain(cc_sdk.registry().get("s4"))
            # 收工後 interrupt → 409
            with self.assertRaises(HTTPException) as cm:
                await bridge.v2_session_interrupt("cc2:s4", FakeRequest())
            self.assertEqual(cm.exception.status_code, 409)
            # 空輸入 → 400
            with self.assertRaises(HTTPException) as cm:
                await bridge.v2_session_input("cc2:s4", FakeRequest({}))
            self.assertEqual(cm.exception.status_code, 400)

        asyncio.run(run())


# ───────────────────────── 審批(fail-closed)─────────────────────────

class TestApprovals(CC2Base):
    def test_no_decision_denies_after_timeout(self):
        async def run():
            sess = cc_sdk.registry().ensure("a1", workdir=_TMP)
            with patch.dict(os.environ, {"CC_SDK_APPROVAL_TIMEOUT": "0.05"}):
                res = await sess._can_use_tool("Bash", {"command": "rm -rf /"},
                                               type("Ctx", (), {})())
            return sess, res

        sess, res = asyncio.run(run())
        self.assertEqual(res.behavior, "deny")
        self.assertEqual(cc_sdk.registry().pending_approvals, {})
        # 審批卡:pending(final:false 有 options)→ resolved expired(options 清空)
        appr = [e["data"]["card"] for e in _events(sess.digest.store, "card.upsert")
                if e["data"]["card"]["kind"] == "approval"]
        self.assertEqual(len(appr), 2)
        self.assertTrue(appr[0]["body"]["options"])
        self.assertEqual(appr[1]["body"]["resolved"], "expired")
        self.assertEqual(appr[1]["body"]["options"], [])

    def test_decision_approve_allows_via_decide_endpoint_contract(self):
        async def run():
            sess = cc_sdk.registry().ensure("a2", workdir=_TMP)

            async def decide_soon():
                while not cc_sdk.registry().pending_approvals:
                    await asyncio.sleep(0.005)
                aid = next(iter(cc_sdk.registry().pending_approvals))
                # 與 /app/v1/approvals/{id}/decision 的 cc2 分支同一顆核心
                status = cc_sdk.registry().decide_approval(aid, "approve")
                self.assertEqual(status, "approved")
                # 二度決議 → None(呼叫端回 409)
                self.assertIsNone(cc_sdk.registry().decide_approval(aid, "deny"))
                return aid

            with patch.dict(os.environ, {"CC_SDK_APPROVAL_TIMEOUT": "5"}):
                task = asyncio.get_event_loop().create_task(decide_soon())
                res = await sess._can_use_tool("Edit", {"file_path": "/tmp/x"},
                                               type("Ctx", (), {})())
                aid = await task
            return sess, res, aid

        sess, res, aid = asyncio.run(run())
        self.assertEqual(res.behavior, "allow")
        appr = [e["data"]["card"] for e in _events(sess.digest.store, "card.upsert")
                if e["data"]["card"]["kind"] == "approval"]
        self.assertEqual(appr[-1]["body"]["resolved"], "approved")
        self.assertEqual(appr[-1]["body"]["approval_id"], aid)

    def test_pending_marks_session_waiting_approval(self):
        async def run():
            sess = cc_sdk.registry().ensure("a3", workdir=_TMP)
            with patch.dict(os.environ, {"CC_SDK_APPROVAL_TIMEOUT": "5"}):
                task = asyncio.get_event_loop().create_task(
                    sess._can_use_tool("Bash", {"command": "x"},
                                       type("Ctx", (), {})()))
                while not cc_sdk.registry().pending_approvals:
                    await asyncio.sleep(0.005)
                rows = cc_sdk.registry().v2_rows()
                self.assertEqual(rows[0]["status"], "waiting_approval")
                self.assertIn("approve", rows[0]["capabilities"])
                aid = next(iter(cc_sdk.registry().pending_approvals))
                cc_sdk.registry().decide_approval(aid, "deny")
                res = await task
            self.assertEqual(res.behavior, "deny")

        asyncio.run(run())


# ───────────────────────── 失效 resume 自癒 ─────────────────────────

class TestResumeSelfHeal(CC2Base):
    def test_invalid_resume_drops_id_and_replays_once(self):
        async def run():
            sess = cc_sdk.registry().ensure("r1", workdir=_TMP)
            sess.sdk_session_id = "dead-id"
            cc_sdk.registry().save()

            def connect_error(options):
                if options.resume:
                    return RuntimeError(
                        "No conversation found with session ID: dead-id")
                return None

            FakeClient.connect_error = staticmethod(connect_error)
            FakeClient.batches = [[
                FakeAssistantMessage([FakeTextBlock("fresh")]),
                FakeResultMessage(session_id="new-sdk-id"),
            ]]
            await sess.submit("hello")
            await _drain(sess)
            return sess

        sess = asyncio.run(run())
        # 壞 id 已丟、新 id 已持久化;第二顆 client 是全新(resume=None)
        self.assertEqual(sess.sdk_session_id, "new-sdk-id")
        self.assertEqual(len(FakeClient.instances), 2)
        self.assertEqual(FakeClient.instances[0].options.resume, "dead-id")
        self.assertIsNone(FakeClient.instances[1].options.resume)
        # 輸入只重放一次(第一顆 connect 就炸,query 零次;第二顆一次)
        self.assertEqual(FakeClient.instances[0].queries, [])
        self.assertEqual(FakeClient.instances[1].queries, ["hello"])
        with open(self.state, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["sessions"]["r1"]["sdk_session_id"], "new-sdk-id")
        # 沒有錯誤卡(自癒成功不該嚇使用者)
        texts = [c["body"].get("text", "") for c in sess.digest.store.cards.values()]
        self.assertFalse(any(t.startswith("⚠️") for t in texts))

    def test_other_error_ends_turn_with_error_card(self):
        async def run():
            sess = cc_sdk.registry().ensure("r2", workdir=_TMP)
            FakeClient.connect_error = staticmethod(
                lambda options: RuntimeError("CLI not found"))
            await sess.submit("hello")
            await _drain(sess)
            return sess

        sess = asyncio.run(run())
        self.assertFalse(sess.busy)
        self.assertIsNone(sess.client)          # teardown 完,下一則 lazy 重建
        texts = [c["body"].get("text", "") for c in sess.digest.store.cards.values()]
        self.assertTrue(any("CLI not found" in t for t in texts))
        self.assertEqual(sess.digest.store.status["phase"], "idle")


# ───────────────────────── state 檔往返 ─────────────────────────

class TestStateRoundTrip(CC2Base):
    def test_round_trip(self):
        reg = cc_sdk.registry()
        sess = reg.ensure("keeper", workdir=_TMP,
                          permission_mode="bypassPermissions")
        sess.sdk_session_id = "sdk-abc"
        reg.save()
        cc_sdk.reset_registry_for_tests()       # 模擬 bridge 重啟
        reg2 = cc_sdk.registry()
        s2 = reg2.get("keeper")
        self.assertIsNotNone(s2)
        self.assertEqual(s2.workdir, _TMP)
        self.assertEqual(s2.permission_mode, "bypassPermissions")
        self.assertEqual(s2.sdk_session_id, "sdk-abc")

    def test_corrupt_state_file_does_not_block(self):
        with open(self.state, "w", encoding="utf-8") as f:
            f.write("{not json")
        reg = cc_sdk.registry()
        self.assertEqual(reg.sessions, {})
        reg.ensure("fresh")                     # 壞檔被覆蓋重建
        cc_sdk.reset_registry_for_tests()
        self.assertIsNotNone(cc_sdk.registry().get("fresh"))

    def test_default_workdir_is_home(self):
        sess = cc_sdk.registry().ensure("homey")
        self.assertEqual(sess.workdir, os.path.expanduser("~"))
        self.assertEqual(sess.permission_mode, "acceptEdits")


if __name__ == "__main__":
    unittest.main()
