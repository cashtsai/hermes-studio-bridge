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
        # 以下四行全都不該成卡:tool_result 水管 / system-reminder / 純工具
        # assistant / 壞 JSON。
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
        self.assertEqual([c["role"] for c in cards],
                         ["user", "assistant", "user", "assistant"])
        self.assertEqual([c["body"]["text"] for c in cards],
                         ["第一句", "回覆一", "第二句", "回覆二"])
        self.assertTrue(all(c["final"] for c in cards))
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
        texts = [store.cards[i]["body"]["text"] for i in store.order]
        self.assertEqual(texts[:4], ["第一句", "回覆一", "第二句", "回覆二"])
        self.assertEqual(texts[4:], ["live 輸入", "live 回覆"])

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
        self.assertEqual(len(first), 4)
        sess._seed_history()          # 模擬自癒 / adopt-by-cwd 再次觸發
        self.assertEqual(sess.digest.store.order, first,
                         "重種必須是對同一批 card id 做 upsert,不得新增卡")
        texts = [sess.digest.store.cards[i]["body"]["text"] for i in first]
        self.assertEqual(texts, ["第一句", "回覆一", "第二句", "回覆二"])

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
