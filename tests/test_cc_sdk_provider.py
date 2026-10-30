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
import time
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="cc-sdk-test-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("POCKET_REGISTRY_DB", os.path.join(_TMP, "bridge-registry.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ.setdefault("CC_SDK_STATE", os.path.join(_TMP, "cc-sdk-sessions.json"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
import carddigest  # noqa: E402
import cc_sdk  # noqa: E402
from fastapi import HTTPException  # noqa: E402


# ───────────────────────── 假 SDK(import 面全替換)─────────────────────────

class FakeTextBlock:
    def __init__(self, text):
        self.text = text


class FakeToolUseBlock:
    def __init__(self, id, name, input):  # noqa: A002
        self.id, self.name, self.input = id, name, input


class ToolResultBlock:
    """類別名**必須**叫 `ToolResultBlock` —— `_collect_tool_results` 用
    `type(block).__name__` 判型(SDK 的 block 不是我們的類)。"""

    def __init__(self, tool_use_id, content, is_error=False):
        self.tool_use_id, self.content, self.is_error = tool_use_id, content, is_error


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


def _async_const(value):
    """把一個值包成 async 函式(patch `_cc_sessions` 這種 awaitable 用)。"""
    async def _f(*a, **k):
        return value
    return _f


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
        # transcript root 也指 temp:歷史種子絕不去讀真的 ~/.claude。
        self.projects = tempfile.mkdtemp(prefix="cc-proj-", dir=_TMP)
        self._env = patch.dict(os.environ, {
            "CC_SDK_PROVIDER": "1", "CC_SDK_STATE": self.state,
            "CC_SDK_CLAUDE_PROJECTS": self.projects})
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
        cards = [store.cards[i] for i in store.order]
        # 2026-10-29 契約改變:工具呼叫出 `kind="tool_call"`(與 tmux 同形狀),
        # body 是 `{tool, summary, patch?}` 而**不是** `text` —— app 的工具列
        # 渲染與 diff chip 綁的是這個 kind。一律索引 `body["text"]` 會 KeyError。
        tc = [c for c in cards if c["kind"] == "tool_call"]
        self.assertEqual(len(tc), 1, "工具呼叫必須出 tool_call 卡")
        self.assertEqual(tc[0]["body"]["tool"], "Bash")
        self.assertEqual(tc[0]["body"]["summary"], "ls -la",
                         "summary 只取第一行")
        self.assertIn("ls -la", tc[0]["body"]["fallback_text"])
        # 錯誤卡仍是 text(system role)
        texts = [c["body"].get("text") or "" for c in cards]
        self.assertTrue(any(t.startswith("⚠️ boom") for t in texts))
        self.assertFalse(store.status["busy"])

    def test_live_tool_cards_match_tmux_shape(self):
        """live 工具卡的形狀必須與 tmux 路徑一致(2026-10-29)。

        cc2 原本把工具呼叫/結果/圖都壓成 `kind:"text"`。app 不會壞(fallback_text
        鐵律),但**看起來就是退步**:工具列變純文字、結果框失效、**diff chip
        整個消失**。cc2 的目標是取代 tmux,這一格不能退。
        守三件:tool_call 帶 patch、tool_result 是獨立卡、路徑圖補 attachment。
        """
        async def run():
            sess = cc_sdk.registry().ensure("lt1", workdir=_TMP)
            FakeClient.batches = [[
                FakeAssistantMessage([
                    FakeToolUseBlock("e1", "Edit", {
                        "file_path": "/tmp/a.py",
                        "old_string": "old line", "new_string": "new line"}),
                    FakeToolUseBlock("r1", "Read", {"file_path": "/tmp/pic.png"}),
                ]),
                # 結果必須在**同一回合內**到達:`turn_end` 會清 `_tool_cards`
                # (刻意的 —— 不跨回合累積,長 session 才不會無上限長大)。
                FakeAssistantMessage([ToolResultBlock("e1", "patched ok")]),
                FakeResultMessage(),
            ]]
            await sess.submit("go")
            await _drain(sess)
            return sess

        sess = asyncio.run(run())
        cards = [sess.digest.store.cards[i] for i in sess.digest.store.order]
        # ⚠️ 用 list 而不是 dict —— 這一則有**兩張** tool_call(Edit 與 Read),
        # 以 kind 當 key 會互相覆蓋(我自己先踩了一次)。
        by_kind = {}
        for c in cards:
            by_kind.setdefault(c["kind"], []).append(c)

        # ① Edit 的 tool_call 必須帶 patch —— 那是 app diff chip 的唯一來源
        self.assertEqual(len(by_kind.get("tool_call", [])), 2,
                         "兩個工具呼叫要各出一張 tool_call")
        edit = next(c for c in by_kind["tool_call"] if c["body"]["tool"] == "Edit")
        self.assertIn("patch", edit["body"],
                      "Edit 沒有合成 patch —— 改檔案就看不到 diff")

        # ② tool_result 是**獨立**卡,不是把 tool_call 蓋掉
        self.assertEqual(len(by_kind.get("tool_result", [])), 1,
                         "工具結果必須是獨立的 tool_result 卡")
        self.assertEqual(by_kind["tool_result"][0]["body"]["text"], "patched ok")
        self.assertTrue(any(c["body"]["tool"] == "Edit"
                            for c in by_kind["tool_call"]),
                        "tool_result 不該蓋掉對應的 tool_call")

        # ③ 路徑圖(agent Read 一張 PNG)要補 attachment 卡
        self.assertEqual(len(by_kind.get("attachment", [])), 1,
                         "agent 看了圖,手機上卻沒有縮圖")
        self.assertEqual(by_kind["attachment"][0]["body"]["filename"], "pic.png")

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


# ───────────────────────── 歷史種子(gap #1)─────────────────────────

class TestHistorySeed(CC2Base):
    """transcript jsonl → 種子卡:角色/順序/原 ts、final:true、水管訊息與
    壞行過濾、上限截尾、缺檔安靜跳過、種子先於 live 事件。"""

    FIXTURE = [
        json.dumps({"type": "user", "message": {"role": "user", "content": "第一句"},
                    "timestamp": "2026-08-16T01:00:00Z"}, ensure_ascii=False),
        json.dumps({"type": "assistant", "message": {
            "content": [{"type": "text", "text": "回覆一"}]},
            "timestamp": "2026-08-16T01:00:05Z"}, ensure_ascii=False),
        # ⚠️ 2026-10-29 契約改變:seed 改走 `carddigest.cc_event_to_cards`
        # (與 tmux 路徑同一套翻譯)之後,這四行**只有兩行**不該成卡:
        #   · tool_result 水管  → 現在是 `tool_result` 卡(tmux 一直都有)
        #   · 純 tool_use      → 現在是 `tool_call` 卡(diff chip 的來源)
        #   · <system-reminder> → 仍然濾掉(carddigest.PLUMBING_TAGS)
        #   · 壞 JSON          → 仍然跳過
        # 原本只撈純文字,所以進場看不到工具步驟 —— 而 cc2 的目標是**取代** tmux,
        # 進場內容不能比它少。
        json.dumps({"type": "user", "message": {
            "content": [{"type": "tool_result", "content": "ok"}]}}),
        json.dumps({"type": "user", "message": {
            "content": "<system-reminder>plumbing</system-reminder>"}}),
        json.dumps({"type": "assistant", "message": {
            "content": [{"type": "tool_use", "name": "Bash", "input": {}}]}}),
        "{not json",
        json.dumps({"type": "user", "message": {"content": "第二句"},
                    "timestamp": "2026-08-16T01:01:00Z"}, ensure_ascii=False),
        json.dumps({"type": "assistant", "message": {
            "content": [{"type": "text", "text": "回覆二"}]},
            "timestamp": "2026-08-16T01:01:05Z"}, ensure_ascii=False),
    ]

    def _write_jsonl(self, sid, lines):
        # ⚠️ **用 production 的 `_project_key`,不要自己重刻一遍規則。**
        # 原本寫死 `_TMP.replace("/", "-")`,而 2026-10-29 的修正把 `_project_key`
        # 改成呼叫 SDK 官方的 `project_key_for_directory`(原本自己算會跟 CLI
        # 實際落地的目錄對不上 → 歷史整段不見且毫無訊息)。官方版會解 symlink,
        # macOS 上 `/var` → `/private/var`,於是這裡算出來的路徑與程式要讀的
        # 差一截 —— 種子一張都種不出來,而測試看起來像是功能壞了。
        # 測試重刻實作細節就會這樣:**改對了反而紅**。
        d = os.path.join(self.projects, cc_sdk._project_key(_TMP))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{sid}.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _restore(self, name, sid):
        """模擬 bridge 重啟後 registry 從 state 檔重建 session(種子時機)。"""
        reg = cc_sdk.registry()
        sess = reg.ensure(name, workdir=_TMP)
        sess.sdk_session_id = sid
        reg.save()
        cc_sdk.reset_registry_for_tests()
        return cc_sdk.registry().get(name)

    def test_seed_roles_ts_order_and_filtering(self):
        self._write_jsonl("sdk-h1", self.FIXTURE)
        sess = self._restore("h1", "sdk-h1")
        store = sess.digest.store
        cards = [store.cards[i] for i in store.order]
        # 新契約(2026-10-29):工具步驟也要進場 —— 順序照 transcript 原順序。
        self.assertEqual([(c["kind"], c["role"]) for c in cards], [
            ("text", "user"), ("markdown", "assistant"),
            ("tool_result", "assistant"),   # 以前被當水管丟掉
            ("tool_call", "assistant"),     # 以前被當水管丟掉
            ("text", "user"), ("markdown", "assistant"),
        ], "工具卡不見了 —— 那就是「進場歷史比 tmux 少」的那個退步")
        # 對話那四張的文字(工具卡沒有 body.text,不能一律索引)
        self.assertEqual([c["body"].get("text") for c in cards
                          if c["kind"] in ("text", "markdown")],
                         ["第一句", "回覆一", "第二句", "回覆二"])
        self.assertTrue(all(c["final"] for c in cards),
                        "歷史一律定稿,不是草稿")
        # 原 ts(權威時間),不是 seed 當下的 now
        self.assertAlmostEqual(cards[0]["ts"],
                               carddigest._epoch("2026-08-16T01:00:00Z"),
                               places=3)
        self.assertAlmostEqual(cards[-1]["ts"],
                               carddigest._epoch("2026-08-16T01:01:05Z"),
                               places=3)

    def test_seed_limit_takes_last_n(self):
        self._write_jsonl("sdk-h2", self.FIXTURE)
        with patch.dict(os.environ, {"CC_SDK_HISTORY_SEED": "2"}):
            sess = self._restore("h2", "sdk-h2")
        store = sess.digest.store
        self.assertEqual([store.cards[i]["body"]["text"] for i in store.order],
                         ["第二句", "回覆二"])

    def test_seed_zero_disables(self):
        self._write_jsonl("sdk-h0", self.FIXTURE)
        with patch.dict(os.environ, {"CC_SDK_HISTORY_SEED": "0"}):
            sess = self._restore("h0", "sdk-h0")
        self.assertEqual(sess.digest.store.order, [])
        # ⚠️ 反向斷言(2026-10-29 加):光是「空的」**無法區分**「上限生效」與
        # 「根本沒讀到檔」。那次事故裡 `_project_key` 換了演算法而測試還自己重刻,
        # seed 靜默讀不到任何檔 —— 這一條照樣綠,只剩另外三條紅去指認問題,
        # 追查成本高得離譜。同一份 fixture 不設 env 時必須種得出卡,
        # 這條才算真的在測上限,而不是在測「檔案找不到」。
        sess2 = self._restore("h0b", "sdk-h0")
        self.assertTrue(sess2.digest.store.order,
                        "同一份 fixture 在沒有 CC_SDK_HISTORY_SEED=0 時必須種出卡;"
                        "空的代表讀不到檔(路徑算錯),不是上限生效")

    def test_seed_cards_precede_live_events(self):
        self._write_jsonl("sdk-h3", self.FIXTURE)
        sess = self._restore("h3", "sdk-h3")

        async def run():
            FakeClient.batches = [[
                FakeAssistantMessage([FakeTextBlock("live 回覆")]),
                FakeResultMessage(session_id="sdk-h3"),
            ]]
            await sess.submit("live 輸入")
            await _drain(sess)

        asyncio.run(run())
        store = sess.digest.store
        # `body.get` 而不是 `body["text"]`:工具卡沒有 text 欄(2026-10-29 起
        # seed 會產工具卡),一律索引會 KeyError。
        texts = [store.cards[i]["body"].get("text") for i in store.order]
        # 契約的重點是**順序**:歷史在前、live 在後(§7 snapshot 語意)。
        self.assertEqual(texts[-2:], ["live 輸入", "live 回覆"],
                         "live 事件必須在歷史之後")
        self.assertEqual([t for t in texts[:-2] if t],
                         ["第一句", "回覆一", "ok", "第二句", "回覆二"],
                         "歷史(含工具結果)必須是 live 之前的前綴")

    def test_missing_jsonl_silent(self):
        sess = self._restore("h4", "sdk-no-file")
        self.assertEqual(sess.digest.store.order, [])
        # 「安靜」是指不出錯誤卡、不擋 session —— 但**有 sdk_session_id 卻找不到
        # transcript** 這件事本身要留痕(2026-10-29):它與「全新對話」不同,
        # 而原本這條路完全無聲,正是上次零線索追查的來源。
        with patch.object(cc_sdk, "LOG") as log:
            self._restore("h4b", "sdk-no-file")
        events = [c.args[0] for c in log.call_args_list if c.args]
        self.assertIn("cc2_history_seed_missing", events)

    def test_reseed_is_idempotent_not_duplicated(self):
        """同一條對話重種不得產生第二份歷史(2026-10-29)。

        原本 `seed_card` 用共用的遞增計數器產 id,重種時 `_seq` 已往前走 →
        同樣的歷史拿到不同 card id → app 端(快取 key = session_id+card.id+rev)
        會把整段歷史疊第二份。今天僥倖沒出事只因為兩個呼叫點互斥;任何未來的
        「自癒後重種」都會當場重複。
        """
        self._write_jsonl("sdk-h6", self.FIXTURE)
        sess = self._restore("h6", "sdk-h6")
        first = list(sess.digest.store.order)
        # 刻意**不綁數量** —— fixture 改了不該讓這條假性紅。要守的是「重種不增卡」。
        self.assertTrue(first, "種子一張都沒有,這條測不到冪等")
        before = {i: sess.digest.store.cards[i]["body"].get("text") for i in first}
        sess._seed_history()          # 模擬自癒 / adopt-by-cwd 再次觸發
        self.assertEqual(sess.digest.store.order, first,
                         "重種必須是對同一批 card id 做 upsert,不得新增卡")
        after = {i: sess.digest.store.cards[i]["body"].get("text") for i in first}
        self.assertEqual(after, before, "重種把內容改掉了")

    def test_unreadable_jsonl_logged_not_fatal(self):
        # 路徑是個目錄 → open 直接炸;種子必須容忍,session 照常可用。
        # ⚠️ 2026-10-29:這裡原本也寫死 `_TMP.replace("/", "-")`(`_write_jsonl`
        # 的修正漏了這條)。後果是那個「不可讀目錄」被建在**錯的 project key**
        # 底下,`_seed_history` 走的是 `FileNotFoundError` 分支 → 這條測試
        # **因為另一個理由通過**,它宣稱覆蓋的「整檔讀不動必須容忍」實際零覆蓋。
        # 直接證據:修之前跑這組,h1/h2 都有 `cc2_history_seeded` 事件,
        # h5 連一個 seed 事件都沒有。
        d = os.path.join(self.projects, cc_sdk._project_key(_TMP))
        os.makedirs(os.path.join(d, "sdk-h5.jsonl"), exist_ok=True)
        with patch.object(cc_sdk, "LOG") as log:
            sess = self._restore("h5", "sdk-h5")
        self.assertIsNotNone(sess)
        self.assertEqual(sess.digest.store.order, [])
        # 「容忍」不等於「沉默」:讀不動必須留痕,否則下次又是零線索追查。
        events = [c.args[0] for c in log.call_args_list if c.args]
        self.assertIn("cc2_history_seed_failed", events,
                      "整檔讀不動必須 log,不能靜默吞掉")


# ───────────────────────── 審批第三態(gap #2)─────────────────────────

class TestApprovalParity(CC2Base):
    """審批面與 tmux 的 parity(2026-10-29/30)。cc2 的目標是取代 tmux,
    這幾條守的是「切過去不會比現在差」。"""

    ASK = {"questions": [
        {"header": "資料庫", "question": "用哪個?", "multiSelect": True,
         "options": [{"label": "Postgres", "description": "預設"},
                     {"label": "MySQL", "description": "舊系統相容"}]},
        {"header": "快取", "question": "要 Redis 嗎?",
         "options": [{"label": "要"}, {"label": "不要"}]},
    ]}

    def test_ask_card_carries_multiselect_and_question_count(self):
        """app 要畫得出「複選」與「第 N/M 題」—— record 原本根本沒有 meta 鍵。"""
        async def run():
            sess = cc_sdk.registry().ensure("ap1", workdir=_TMP)
            with patch.dict(os.environ, {"CC_SDK_APPROVAL_TIMEOUT": "0.05"}):
                await sess._can_use_tool("AskUserQuestion", self.ASK,
                                         type("Ctx", (), {})())
            return sess

        sess = asyncio.run(run())
        card = next(e["data"]["card"] for e in
                    _events(sess.digest.store, "card.upsert")
                    if e["data"]["card"]["kind"] == "approval")
        b = card["body"]
        self.assertTrue(b.get("multiselect"),
                        "複選題沒標 multiselect → app 只能當單選畫,使用者在盲選")
        self.assertEqual(b.get("q_total"), 2, "看不出總共幾題")
        self.assertEqual(b.get("q_index"), 1)
        # 選項說明必須保留(原本只取 label → 說明空白)
        opt = next(o for o in b["options"] if o["label"] == "Postgres")
        self.assertEqual(opt.get("detail"), "預設",
                         "選項說明被丟掉 —— 使用者看不到每個選項是什麼意思")

    def test_ask_record_kind_is_question_not_permission(self):
        """提問與權限是不同的東西:DB 鏡像原本把 kind 硬寫成 permission,
        審核中心於是把「請選 Postgres / MySQL」渲染成權限確認。"""
        captured = []
        async def run():
            sess = cc_sdk.registry().ensure("ap2", workdir=_TMP)
            with patch.object(cc_sdk, "APPROVAL_DB_UPSERT", captured.append), \
                 patch.dict(os.environ, {"CC_SDK_APPROVAL_TIMEOUT": "0.05"}):
                await sess._can_use_tool("AskUserQuestion", self.ASK,
                                         type("Ctx", (), {})())
        asyncio.run(run())
        self.assertEqual(captured[0]["kind"], "question")
        self.assertEqual(captured[0]["risk"], "low", "回答問題不是危險操作")

    def test_timeout_message_distinguishes_from_user_decline(self):
        """逾時與「使用者按了拒絕」必須分開講。

        原本兩者共用「未核准(逾時或拒絕)」—— 使用者看到「拒絕執行」卻從沒按過
        任何東西,只會以為 app 壞了。而 110s 上限不能放寬(必須早於 CLI 端 stdio
        逾時才保證送得出明確 deny),所以解法是把原因講清楚 + 推播讓人及時知道。
        """
        async def run():
            sess = cc_sdk.registry().ensure("ap3", workdir=_TMP)
            with patch.dict(os.environ, {"CC_SDK_APPROVAL_TIMEOUT": "0.05"}):
                return await sess._can_use_tool("Bash", {"command": "ls"},
                                                type("Ctx", (), {})())

        res = asyncio.run(run())
        self.assertEqual(res.behavior, "deny")
        self.assertIn("No one answered", res.message,
                      "逾時的訊息必須說清楚是「沒人回答」,不是「使用者拒絕」")
        # 不可以說成「使用者拒絕了」(`auto-declined` 裡也有 declined,
        # 所以驗的是那整句主張,不是單字 —— 我自己先用單字驗,錯了一次)。
        self.assertNotIn("The user declined", res.message)


class TestCcListMergesCc2(CC2Base):
    """遷移階段 1:`/ccsessions` 併入 cc2 列(2026-10-30)。

    cc2 的目標是取代 tmux,但兩個 app 的 CC 分頁都只讀 `/ccsessions`
    (tmux 專屬)→ cc2 session 在使用者眼前整個不存在。
    守三件:旗標關時零行為改變、開了之後列得出來且形狀對、撞名不出兩條。
    """

    def _rows(self):
        return asyncio.run(bridge.cc_list(FakeRequest()))["sessions"]

    def test_flag_off_changes_nothing(self):
        """最重要的一條:旗標預設關 → 與 2026-10-30 之前**逐字相同**。"""
        cc_sdk.registry().ensure("merged1", workdir=_TMP)
        with patch.object(bridge, "CC_LIST_INCLUDES_CC2", False), \
             patch.object(bridge, "_cc_conf_rows", lambda: []):
            self.assertEqual(self._rows(), [],
                             "旗標關著卻把 cc2 列塞進去了 —— 那不是可回退的改動")

    def test_flag_on_lists_cc2_in_cc_shape(self):
        sess = cc_sdk.registry().ensure("merged2", workdir=_TMP)
        sess.sdk_session_id = "sdk-m2"
        with patch.object(bridge, "CC_LIST_INCLUDES_CC2", True), \
             patch.object(bridge, "_cc_conf_rows", lambda: []):
            rows = self._rows()
        self.assertEqual([r["name"] for r in rows], ["merged2"])
        r = rows[0]
        # `status` 要照 tmux 的約定:status 是「這條在」,忙碌看 busy。
        self.assertEqual(r["status"], "running")
        self.assertFalse(r["busy"], "閒置的 session 不該標 busy")
        self.assertEqual(r["workdir"], _TMP)
        self.assertEqual(r["sessionId"], "sdk-m2")
        self.assertIsNone(r["claudeTitle"],
                          "cc2 沒有 pane title —— 不可以拿 session 名充當")
        self.assertEqual(r["backend"], "cc2", "診斷要分得出後端")

    def test_name_collision_keeps_tmux_and_logs(self):
        """同名時保守選既有的 tmux 那條,不讓使用者看到兩個一樣的名字。

        兩邊是獨立命名空間(tmux 來自 ccsess 設定檔、cc2 來自自己的 registry),
        遷移期間真的會撞。撞名代表有歷史殘留要清,所以要 log,不能安靜吞掉。
        """
        cc_sdk.registry().ensure("dup", workdir=_TMP)
        events = []
        with patch.object(bridge, "CC_LIST_INCLUDES_CC2", True), \
             patch.object(bridge, "_cc_conf_rows",
                          lambda: [("dup", "/tmp/x", "1")]), \
             patch.object(bridge, "_cc_sessions",
                          _async_const([{"name": "dup", "workdir": "/tmp/x",
                                         "status": "running", "busy": False}])), \
             patch.object(bridge, "_log_event",
                          lambda ev, **kw: events.append(ev)):
            rows = self._rows()
        self.assertEqual([r["name"] for r in rows], ["dup"],
                         "撞名出了兩條 —— 使用者會看到兩個一樣的 session")
        self.assertNotIn("backend", rows[0], "撞名時該留 tmux 那條")
        self.assertIn("cc_name_collision", events, "撞名必須留痕")

    def test_archived_query_includes_cc2(self):
        gone = cc_sdk.registry().ensure("arch1", workdir=_TMP)
        gone.archived = True
        with patch.object(bridge, "CC_LIST_INCLUDES_CC2", True), \
             patch.object(bridge, "_cc_conf_rows", lambda: []):
            rows = asyncio.run(bridge.cc_list(FakeRequest(),
                                              archived=True))["sessions"]
        self.assertEqual([r["name"] for r in rows], ["arch1"])
        self.assertEqual(rows[0]["status"], "archived")


class TestCcIdResolvesToCc2(CC2Base):
    """遷移階段 2:`claude_code:{name}` 解析到 cc2 後端(2026-10-30)。

    階段 1 讓 cc2 出現在 `/ccsessions` 列表,但 app 點進去送的是
    `claude_code:{name}` —— 解析不到就 404,使用者看到「列表上有、點進去說
    找不到」,**比完全看不到更糟**。
    """

    def test_claude_code_id_falls_through_to_cc2(self):
        cc_sdk.registry().ensure("r2a", workdir=_TMP)
        with patch.object(bridge, "CC_LIST_INCLUDES_CC2", True), \
             patch.object(bridge, "_cc_conf_rows", lambda: []):
            self.assertEqual(bridge._v2_card_source("claude_code:r2a"),
                             ("cc2", "r2a"))

    def test_tmux_wins_on_collision_same_rule_as_list(self):
        """撞名時 tmux 勝 —— **必須與階段 1 的列表去重同一條規則**。

        不一致的話會變成「列表顯示 tmux 那條、點進去開到 cc2」,那是最難查的
        一種錯:兩邊各自看起來都對。
        """
        cc_sdk.registry().ensure("dup2", workdir=_TMP)
        with patch.object(bridge, "CC_LIST_INCLUDES_CC2", True), \
             patch.object(bridge, "_cc_conf_rows",
                          lambda: [("dup2", "/tmp/y", "1")]):
            self.assertEqual(bridge._v2_card_source("claude_code:dup2"),
                             ("cc", "dup2", "/tmp/y"))

    def test_flag_off_keeps_404(self):
        """旗標關 → 與今天逐字相同(不解析、照樣 404)。"""
        cc_sdk.registry().ensure("r2b", workdir=_TMP)
        with patch.object(bridge, "CC_LIST_INCLUDES_CC2", False), \
             patch.object(bridge, "_cc_conf_rows", lambda: []):
            with self.assertRaises(HTTPException) as cm:
                bridge._v2_card_source("claude_code:r2b")
            self.assertEqual(cm.exception.status_code, 404)

    def test_unknown_name_still_404(self):
        with patch.object(bridge, "CC_LIST_INCLUDES_CC2", True), \
             patch.object(bridge, "_cc_conf_rows", lambda: []):
            with self.assertRaises(HTTPException) as cm:
                bridge._v2_card_source("claude_code:never-existed")
            self.assertEqual(cm.exception.status_code, 404)


class TestCcWriteEndpointsDispatch(CC2Base):
    """遷移階段 3:`/ccsessions/{name}/*` 的寫入面 dispatch(2026-10-30)。

    **這不是選配** —— 階段 1+2 讓 cc2 session 對 app 長得像 `claude_code:`,
    於是 app 的 ⋮ 選單(改名/模型/審核模式/壓縮)會對它顯示,而那些端點原本
    一律 404(名字不在 ccsess 設定檔裡)。不補就是把階段 1+2 留在壞的狀態。
    """

    def setUp(self):
        super().setUp()
        self._flag = patch.object(bridge, "CC_LIST_INCLUDES_CC2", True)
        self._conf = patch.object(bridge, "_cc_conf_rows", lambda: [])
        self._flag.start(); self._conf.start()
        cc_sdk.registry().ensure("w1", workdir=_TMP)

    def tearDown(self):
        self._conf.stop(); self._flag.stop()
        super().tearDown()

    def test_backend_resolver_matches_list_and_card_rules(self):
        """三處規則必須一致:列表去重、`_v2_card_source`、`_cc_backend`。"""
        self.assertEqual(bridge._cc_backend("w1"), "cc2")
        self.assertEqual(bridge._cc_backend("never"), "")
        with patch.object(bridge, "_cc_conf_rows",
                          lambda: [("w1", "/tmp/z", "1")]):
            self.assertEqual(bridge._cc_backend("w1"), "cc",
                             "撞名時 tmux 勝 —— 與列表/卡片流同一條規則")

    def test_mode_and_model_go_to_cc2_settings(self):
        hits = []
        async def fake(name, request):
            hits.append(name); return {"ok": True}
        with patch.object(bridge, "v2_cc2_settings", fake):
            asyncio.run(bridge.cc_session_mode("w1", FakeRequest({"mode": "plan"})))
            asyncio.run(bridge.cc_session_model("w1", FakeRequest({"model": "opus"})))
        self.assertEqual(hits, ["w1", "w1"],
                         "⋮ 選單的模式/模型切換沒轉到 cc2 → 使用者按了 404")

    def test_rename_and_archive_and_compress_dispatch(self):
        seen = []
        async def mk(tag):
            async def f(name, request):
                seen.append(tag); return {"ok": True}
            return f
        with patch.object(bridge, "v2_cc2_rename", asyncio.run(mk("rename"))), \
             patch.object(bridge, "v2_cc2_archive", asyncio.run(mk("archive"))), \
             patch.object(bridge, "v2_cc2_compact", asyncio.run(mk("compress"))):
            asyncio.run(bridge.cc_session_rename("w1", FakeRequest({"name": "w1b"})))
            asyncio.run(bridge.cc_session_archive("w1", FakeRequest({})))
            asyncio.run(bridge.cc_session_compress("w1", FakeRequest({})))
        self.assertEqual(sorted(seen), ["archive", "compress", "rename"])

    def test_answer_is_honest_409_not_fake_success(self):
        """TUI 式多選作答對 cc2 不存在。回 409 + 指路 —— 不可以回 404
        (app 會以為 session 不存在,它明明在列表上)、更不可以假成功
        (使用者按了以為送出,模型什麼都沒收到)。"""
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(bridge.cc_session_answer("w1", FakeRequest({"keys": ["1"]})))
        self.assertEqual(cm.exception.status_code, 409)
        self.assertIn("approvals", str(cm.exception.detail),
                      "要指路到可行的那條(結構化審批決議)")

    def test_diff_shares_workdir_logic(self):
        got = []
        async def fake(workdir, path):
            got.append((workdir, path)); return {"text": ""}
        with patch.object(bridge, "_session_workdir_diff", fake):
            asyncio.run(bridge.cc_session_diff("w1", FakeRequest(), path="a.py"))
        self.assertEqual(got, [(_TMP, "a.py")],
                         "diff 只需要 workdir,cc2 也有 —— 該共用同一支")

    def test_history_uses_shared_formatter_shape(self):
        """回應形狀必須是 app 已經在吃的 `{text, more}`,不是 cc2 原生的
        `{messages,…}` —— 兩邊 transcript 同格式,給對路徑就自動同形狀。"""
        sess = cc_sdk.registry().get("w1")
        sess.sdk_session_id = "sdk-w1"
        out = asyncio.run(bridge.cc_session_history("w1", FakeRequest()))
        self.assertIn("text", out)
        self.assertIn("more", out)
        self.assertNotIn("messages", out)


class TestNewSessionBackend(CC2Base):
    """遷移階段 4:新建的 CC session 走哪個後端(2026-10-30)。

    這是**真正的切換點** —— 翻成 cc2 之後新開的每一條都是 SDK 後端。
    與階段 1-3 的本質差別:那三階段關旗標就全回原狀,這一階段已經建出來的
    cc2 session 不會變回 tmux。
    """

    # ⚠️ workdir 必須在 home 底下 —— `/ccsessions` 有這條檢查(沒有它,
    # 不存在的路徑會讓 ccsess 悄悄退回 $HOME 並帶 --continue,劫持家目錄那條
    # 對話)。`_TMP` 在 /var/folders,所以這組測試要自己的 home 內目錄。
    def setUp(self):
        super().setUp()
        self.wd = tempfile.mkdtemp(prefix="cc2-stage4-",
                                   dir=os.path.expanduser("~"))

    def tearDown(self):
        try:
            os.rmdir(self.wd)
        except OSError:
            pass
        super().tearDown()

    def _create(self, name, **extra):
        return asyncio.run(bridge.cc_session_create(
            FakeRequest({"name": name, "workdir": self.wd, **extra})))

    def test_default_still_tmux(self):
        """最重要的一條:**預設不改變任何行為**。

        ⚠️ 這條原本寫成 `with patch.object(..., "tmux")` 再斷言結果是 tmux ——
        那是**空綠**:它 patch 掉了要測的那個值,所以把模組預設改成 cc2 也照樣
        通過(鑑別力驗證當場抓到)。要測「預設」就不能 patch 它,
        直接讀模組常數 + 確認沒設 env 時的解析結果。
        """
        self.assertEqual(bridge.CC_NEW_SESSIONS_BACKEND, "tmux",
                         "模組預設不是 tmux —— 那就不是「可選擇啟用」的遷移")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CC_NEW_SESSIONS_BACKEND", None)
            self.assertEqual(
                (os.environ.get("CC_NEW_SESSIONS_BACKEND", "")
                 .strip().lower() or "tmux"), "tmux")

    def test_bad_flag_value_falls_back_to_tmux(self):
        """旗標寫錯不該讓「開新 session」整個壞掉。"""
        for bad in ("", "sdk", "CC2 ", "tmux2"):
            with patch.object(bridge, "CC_NEW_SESSIONS_BACKEND", bad):
                self.assertEqual(bridge._cc_new_backend(), "tmux",
                                 f"旗標 {bad!r} 應退回 tmux")

    def test_cc2_requires_cc2_enabled(self):
        with patch.object(bridge, "CC_NEW_SESSIONS_BACKEND", "cc2"), \
             patch.dict(os.environ, {"CC_SDK_PROVIDER": ""}):
            self.assertEqual(bridge._cc_new_backend(), "tmux",
                             "cc2 本身沒啟用時不該把新 session 丟給它")

    def test_creates_cc2_and_returns_tmux_shape(self):
        """app 零改動是整個遷移策略的前提 —— 回傳必須是 tmux 的形狀。"""
        with patch.object(bridge, "CC_NEW_SESSIONS_BACKEND", "cc2"), \
             patch.object(bridge, "_cc_conf_rows", lambda: []), \
             patch.object(bridge, "_registry_precheck_or_429", lambda *a, **k: None), \
             patch.object(bridge, "_registry_register", lambda *a, **k: None):
            out = self._create("n4a")
        self.assertTrue(out["ok"])
        sess = out["session"]
        self.assertEqual(sess["name"], "n4a")
        self.assertEqual(sess["workdir"], os.path.realpath(self.wd))
        # cc2 是 lazy 連線,剛建好還沒連 SDK → 照實說 starting
        self.assertEqual(sess["status"], "starting")
        self.assertEqual(sess["backend"], "cc2")
        self.assertNotIn("session_id", out, "不可以回 cc2 原生形狀")
        # 真的建在 cc2 registry 裡
        self.assertIsNotNone(cc_sdk.registry().get("n4a"))

    def test_name_collision_across_backends_is_409(self):
        """命名空間互斥(規格 §4 規則 1):名字存在於**任一**後端就 409。
        從源頭擋撞名,比事後在列表/解析/寫入三處各自去重可靠。"""
        cc_sdk.registry().ensure("taken1", workdir=self.wd)
        with patch.object(bridge, "CC_LIST_INCLUDES_CC2", True), \
             patch.object(bridge, "_cc_conf_rows", lambda: []):
            with self.assertRaises(HTTPException) as cm:
                self._create("taken1")
        self.assertEqual(cm.exception.status_code, 409)

        with patch.object(bridge, "CC_NEW_SESSIONS_BACKEND", "cc2"), \
             patch.object(bridge, "_cc_conf_rows",
                          lambda: [("taken2", "/tmp/q", "1")]):
            with self.assertRaises(HTTPException) as cm:
                self._create("taken2")
        self.assertEqual(cm.exception.status_code, 409,
                         "tmux 已有同名,cc2 不該硬建一條")

    def test_cc2_rejects_names_tmux_would_accept(self):
        """tmux 只擋 `/|:` 與空白,cc2 限英數/底線/連字號 —— 提前講清楚,
        而不是讓它在 v2 端點 400 時回一個 app 沒預期的錯誤碼。"""
        with patch.object(bridge, "CC_NEW_SESSIONS_BACKEND", "cc2"), \
             patch.object(bridge, "_cc_conf_rows", lambda: []):
            with self.assertRaises(HTTPException) as cm:
                self._create("has.dot")
        self.assertEqual(cm.exception.status_code, 400)


class TestGovernanceRegistration(CC2Base):
    """cc2 session 要進戶政 + 吃配額(2026-10-30)。

    tmux 的 `POST /ccsessions` 一直有 `_registry_precheck_or_429` 與
    `_registry_register`,cc2 的建立路徑整個沒有 → cc2 session 不進
    workers/discovery/治理視圖,配額也擋不住。cc2 的目標是取代 tmux,
    這一格不能少。
    """

    def test_create_registers_in_governance(self):
        calls = []
        with patch.object(bridge, "_registry_register",
                          lambda sid, **kw: calls.append((sid, kw))):
            asyncio.run(bridge.v2_cc2_create(
                "gov1", FakeRequest({"workdir": _TMP})))
        self.assertEqual(len(calls), 1, "新建的 cc2 session 沒進戶政")
        sid, kw = calls[0]
        self.assertEqual(sid, "cc2:gov1")
        self.assertEqual(kw["provider"], "cc2")

    def test_idempotent_repost_does_not_recharge_quota(self):
        """重 POST 同名只更新設定(刻意冪等,不打斷進行中的 turn)——
        那不是開新 session,不該再吃一次配額。否則配額滿了連「改個 model」
        都會 429。"""
        pre, reg = [], []
        with patch.object(bridge, "_registry_precheck_or_429",
                          lambda *a, **k: pre.append(a)), \
             patch.object(bridge, "_registry_register",
                          lambda sid, **kw: reg.append(sid)):
            asyncio.run(bridge.v2_cc2_create("gov2", FakeRequest({"workdir": _TMP})))
            asyncio.run(bridge.v2_cc2_create("gov2", FakeRequest({"model": "opus"})))
        self.assertEqual(len(pre), 1, "冪等重 POST 又吃了一次配額")
        self.assertEqual(len(reg), 1, "冪等重 POST 又註冊了一次")


class TestArchivedVisibility(CC2Base):
    def test_archived_sessions_are_queryable_not_lost(self):
        """封存不得等於消失(2026-10-30)。

        `v2_rows` 原本只是**跳過**封存的 session,而 cc2 沒有任何「列出封存」
        的管道 → 封存一條就從 app 永久消失、使用者也無從得知要解除哪一條
        (解除本身是有的)。tmux 一直有 `GET /ccsessions?archived=true`。
        """
        reg = cc_sdk.registry()
        reg.ensure("live1", workdir=_TMP)
        gone = reg.ensure("gone1", workdir=_TMP)
        gone.archived = True

        ids = [r["id"] for r in reg.v2_rows()]
        self.assertIn("cc2:live1", ids)
        self.assertNotIn("cc2:gone1", ids, "預設列表不該出封存的")

        arch = reg.v2_rows(include_archived=True)
        self.assertEqual([r["id"] for r in arch], ["cc2:gone1"],
                         "封存列查不到 —— 那條 session 等於被刪了")
        self.assertEqual(arch[0]["status"], "archived",
                         "狀態字彙要與 tmux 一致")


class TestApprovalForSession(CC2Base):
    def test_options_shape_has_for_session(self):
        """卡片契約:三顆選項,形狀鏡像 codex(key/label/style),app 零改動。"""
        async def run():
            sess = cc_sdk.registry().ensure("fs0", workdir=_TMP)
            with patch.dict(os.environ, {"CC_SDK_APPROVAL_TIMEOUT": "0.05"}):
                await sess._can_use_tool("Bash", {"command": "x"},
                                         type("Ctx", (), {})())
            return sess

        sess = asyncio.run(run())
        appr = [e["data"]["card"] for e in _events(sess.digest.store, "card.upsert")
                if e["data"]["card"]["kind"] == "approval"]
        opts = appr[0]["body"]["options"]
        self.assertEqual([o["key"] for o in opts],
                         ["approve", "approve_for_session", "deny"])
        self.assertEqual(opts[1]["label"], "本次全允許")
        self.assertEqual(opts[1]["style"], "secondary")

    def test_for_session_allows_then_auto_allows_same_tool(self):
        async def run():
            sess = cc_sdk.registry().ensure("fs1", workdir=_TMP)

            async def decide_soon():
                while not cc_sdk.registry().pending_approvals:
                    await asyncio.sleep(0.005)
                aid = next(iter(cc_sdk.registry().pending_approvals))
                status = cc_sdk.registry().decide_approval(
                    aid, "approve_for_session")
                # 對外狀態字彙不擴:仍是 approved
                self.assertEqual(status, "approved")

            with patch.dict(os.environ, {"CC_SDK_APPROVAL_TIMEOUT": "5"}):
                task = asyncio.get_event_loop().create_task(decide_soon())
                res1 = await sess._can_use_tool("Bash", {"command": "ls"},
                                                type("Ctx", (), {})())
                await task
                # 同工具第二次:直接放行,不出新卡、不留 pending
                res2 = await sess._can_use_tool("Bash", {"command": "pwd"},
                                                type("Ctx", (), {})())
            return sess, res1, res2

        sess, res1, res2 = asyncio.run(run())
        self.assertEqual(res1.behavior, "allow")
        self.assertEqual(res2.behavior, "allow")
        self.assertEqual(sess.session_allowed_tools, {"Bash"})
        self.assertEqual(cc_sdk.registry().pending_approvals, {})
        appr = [e["data"]["card"] for e in _events(sess.digest.store, "card.upsert")
                if e["data"]["card"]["kind"] == "approval"]
        # 只有第一次出卡(pending + resolved 同一張),第二次零卡
        self.assertEqual(len(appr), 2)
        self.assertEqual(len({c["id"] for c in appr}), 1)
        self.assertEqual(appr[-1]["body"]["resolved"], "approved")
        # 其他工具不受惠(per-tool 記憶)
        self.assertNotIn("Edit", sess.session_allowed_tools)

    def test_for_session_memory_clears_on_rebuild(self):
        async def run():
            sess = cc_sdk.registry().ensure("fs2", workdir=_TMP)
            sess.session_allowed_tools.add("Bash")

        asyncio.run(run())
        cc_sdk.reset_registry_for_tests()       # 模擬 bridge 重啟/重建
        self.assertEqual(
            cc_sdk.registry().get("fs2").session_allowed_tools, set())

    def test_deny_still_denies_and_no_memory(self):
        async def run():
            sess = cc_sdk.registry().ensure("fs3", workdir=_TMP)

            async def decide_soon():
                while not cc_sdk.registry().pending_approvals:
                    await asyncio.sleep(0.005)
                aid = next(iter(cc_sdk.registry().pending_approvals))
                self.assertEqual(cc_sdk.registry().decide_approval(aid, "deny"),
                                 "denied")

            with patch.dict(os.environ, {"CC_SDK_APPROVAL_TIMEOUT": "5"}):
                task = asyncio.get_event_loop().create_task(decide_soon())
                res = await sess._can_use_tool("Bash", {"command": "rm"},
                                               type("Ctx", (), {})())
                await task
            return sess, res

        sess, res = asyncio.run(run())
        self.assertEqual(res.behavior, "deny")
        self.assertEqual(sess.session_allowed_tools, set())


# ───────────────────────── 重啟孤兒審批列(gap #3)─────────────────────────

class TestStartupReconcile(CC2Base):
    def _insert(self, aid, source, provider, status):
        import sqlite3
        con = sqlite3.connect(bridge.CANON_DB)
        now = time.time()
        con.execute(
            "INSERT OR REPLACE INTO approvals(id,title,source,risk,detail,"
            "created_at,expires_at,status,decided_at,result,session_id,"
            "provider,kind,options) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, "t", source, "medium", "d", now, now + 100, status,
             None, None, source, provider, "permission", "[]"))
        con.commit()
        con.close()

    def _statuses(self):
        import sqlite3
        con = sqlite3.connect(bridge.CANON_DB)
        rows = dict(con.execute("SELECT id,status FROM approvals").fetchall())
        con.close()
        return rows

    def test_orphan_cc2_pending_rows_expired_others_untouched(self):
        self._insert("cc2-orph1", "cc2:alpha", "cc2", "pending")
        self._insert("cc2-done1", "cc2:alpha", "cc2", "approved")
        self._insert("codex-keep1", "codex:tid", "codex", "pending")
        n = bridge._cc2_reconcile_orphan_approvals()
        self.assertEqual(n, 1)
        rows = self._statuses()
        self.assertEqual(rows["cc2-orph1"], "expired")     # 孤兒收掉
        self.assertEqual(rows["cc2-done1"], "approved")    # 已決不復活
        self.assertEqual(rows["codex-keep1"], "pending")   # 別家不碰
        # 冪等:再跑一次零改動;startup 包裝不炸
        self.assertEqual(bridge._cc2_reconcile_orphan_approvals(), 0)
        asyncio.run(bridge._cc2_approval_reconcile_startup())


# ───────────────────────── 附件限制可見(gap #4)─────────────────────────

class TestAttachmentsNotice(CC2Base):
    """附件契約 **2026-10-29 反轉**:cc2 從「一律拒收」變成「照 tmux 那套轉發」。

    舊的兩條測試釘的是「丟棄 + 回 attachments_dropped + 出一張『不支援附件』卡
    + 純附件回 400」。那是功能**不存在**時的誠實行為;現在端點會把附件存檔、
    語音轉文字、並把 on-disk 路徑注入送出的文字(bridge.py 的 cc2 分支,與
    tmux `_cc_input_core` 同一套 —— Claude Code 用 Read 就讀得到,圖片原生看得懂)。
    繼續釘舊契約等於斷言一個已經修掉的限制,所以改釘新的。

    ⚠️ 這裡刻意驗**送進模型的那串文字真的帶路徑**,而不只是「回應 200」——
    附件只進回顯卡、沒進 query 的話,使用者會在對話裡看到自己的圖、
    以為模型看過了,那是比「不支援」更糟的靜默失敗。
    """

    # ⚠️ 附件**落盤**在測試裡永遠跑不到:`bridge.UPLOAD_DIR` 是寫死的
    # production 路徑(~/apps/hermes-agent/home/uploads),不吃 env,而隔離閂的
    # audit hook 會擋住寫入。所以這裡把 `_save_attachment` 換成固定路徑 ——
    # 要驗的是**端點的組裝邏輯**(路徑有沒有進到送給模型的文字),不是磁碟 I/O。
    # (UPLOAD_DIR 不可注入本身是基礎設施缺口,已另記。)
    FAKE_PATH = "/tmp/pocket-test-upload/x.png"

    def test_attachment_path_reaches_the_model_not_just_the_card(self):
        async def run():
            cc_sdk.registry().ensure("att1", workdir=_TMP)
            FakeClient.batches = [[FakeResultMessage()]]
            with patch.object(bridge, "_save_attachment",
                              lambda a, d="file": self.FAKE_PATH):
                res = await bridge.v2_session_input(
                    "cc2:att1", FakeRequest({"content": "這張圖是什麼",
                                             "attachments": [{"kind": "image",
                                                              "filename": "x.png"}]}))
            await _drain(cc_sdk.registry().get("att1"))
            return res

        res = asyncio.run(run())
        self.assertTrue(res["accepted"])
        self.assertEqual(res.get("attachments"), 1)
        sent = FakeClient.instances[0].queries[0]
        self.assertIn("這張圖是什麼", sent)
        # **這條是整個檔最重要的斷言**:附件只進回顯卡、沒進 query 的話,
        # 使用者會在對話裡看到自己的圖、以為模型看過了 —— 比「不支援」更糟。
        self.assertIn(self.FAKE_PATH, sent, "附件路徑沒有進到送給模型的文字")
        store = cc_sdk.registry().get("att1").digest.store
        texts = [c["body"].get("text", "") for c in store.cards.values()]
        self.assertFalse(any("不支援附件" in t for t in texts),
                         "附件已經支援了,不該再出『不支援』的卡")

    def test_attachments_only_is_accepted_now(self):
        """純附件(沒有文字)以前回 400,現在應該收下 —— 路徑本身就是內容。"""
        async def run():
            cc_sdk.registry().ensure("att2", workdir=_TMP)
            FakeClient.batches = [[FakeResultMessage()]]
            with patch.object(bridge, "_save_attachment",
                              lambda a, d="file": self.FAKE_PATH):
                res = await bridge.v2_session_input(
                    "cc2:att2", FakeRequest({"attachments": [{"kind": "image",
                                                              "filename": "y.png"}]}))
            await _drain(cc_sdk.registry().get("att2"))
            return res

        res = asyncio.run(run())
        self.assertTrue(res["accepted"])
        self.assertIn(self.FAKE_PATH, FakeClient.instances[0].queries[0])

    def test_no_attachments_unchanged(self):
        async def run():
            cc_sdk.registry().ensure("att3", workdir=_TMP)
            FakeClient.batches = [[FakeResultMessage()]]
            res = await bridge.v2_session_input(
                "cc2:att3", FakeRequest({"content": "plain"}))
            await _drain(cc_sdk.registry().get("att3"))
            return res

        res = asyncio.run(run())
        self.assertNotIn("attachments_dropped", res)


class TestResumeHintCard(CC2Base):
    """終端接手指令卡(2026-08-16):cc2 沒畫面可 attach,接手靠
    `claude --resume <sid>`。固定卡 id 不洗版;sid 變了原位更新。"""

    def test_hint_card_on_session_id_capture(self):
        sess = cc_sdk.registry().ensure("hint1", workdir=_TMP)
        sess._note_sdk_session("sid-abc-123")
        cards = [c for c in sess.digest.store.cards.values()
                 if (c.get("body") or {}).get("origin") == "cc2.resume_hint"]
        self.assertEqual(len(cards), 1)
        self.assertIn("claude --resume sid-abc-123",
                      cards[0]["body"]["text"])
        # sid 換人(自癒重建)→ 同卡原位更新,不多一張
        sess._note_sdk_session("sid-def-456")
        cards2 = [c for c in sess.digest.store.cards.values()
                  if (c.get("body") or {}).get("origin") == "cc2.resume_hint"]
        self.assertEqual(len(cards2), 1)
        self.assertIn("sid-def-456", cards2[0]["body"]["text"])

    def test_v2_rows_carry_resume_command(self):
        sess = cc_sdk.registry().ensure("hint2", workdir=_TMP)
        sess._note_sdk_session("sid-xyz")
        rows = cc_sdk.registry().v2_rows()
        row = next(r for r in rows if r["id"] == "cc2:hint2")
        self.assertEqual(row["meta"]["resume_command"],
                         "claude --resume sid-xyz")


if __name__ == "__main__":
    unittest.main()
