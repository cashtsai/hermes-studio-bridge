"""agent_call 互調(1c)——政策/護欄/收割/audit 卡/registry addendum 鐵律。

涵蓋:政策 default DENY 與 allowlist 放行、深度/循環/預算拒絕、
AGENT_CALL 旗標關閉 = 404、await_reply happy path + 逾時轉背景、
audit 卡雙邊落卡、絕不代審(approval 核心零觸碰)、call 帳本 CRUD、
addendum:spawn 路徑 parent/purpose 落籍 + parent 驗證 + children 端點。
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_TMP = tempfile.mkdtemp(prefix="agent-call-test-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("POCKET_REGISTRY_DB", os.path.join(_TMP, "bridge-registry.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_call  # noqa: E402
import agent_registry  # noqa: E402
import bridge  # noqa: E402
import carddigest  # noqa: E402
from fastapi import HTTPException  # noqa: E402


def _fresh_registry() -> agent_registry.AgentRegistry:
    return agent_registry.AgentRegistry(
        tempfile.mktemp(suffix=".db", dir=_TMP),
        task_ttl=100.0, ephemeral_ttl=50.0, max_children=3,
        task_cap=12, max_depth=2, idle_secs=10.0)


def _write_policy(rules) -> str:
    path = tempfile.mktemp(suffix=".json", dir=_TMP)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "rules": rules}, f, ensure_ascii=False)
    return path


class FakeRequest:
    headers = {"authorization": "Bearer test-unit-token"}
    client = None

    def __init__(self, body=None):
        self._body = body or {}

    async def json(self):
        return self._body


CALLER = "hermes:yuanfang"
TARGET = "claude_code:tirith"
_KNOWN = {CALLER: ("hp", "yuanfang"), TARGET: ("cc", "tirith", "/tmp"),
          "codex:c1": ("cx", "c1"), "claude_code:b": ("cc", "b", "/tmp")}


def _fake_card_source(sid):
    if sid in _KNOWN:
        return _KNOWN[sid]
    raise bridge.http_err(404, "SESSION_NOT_FOUND", "unknown session")


class _Env:
    """打包 agent_call 端點測試的標準 patch 組。"""

    def __init__(self, rules=None, extra_env=None, dispatch=None):
        self.reg = _fresh_registry()
        policy = _write_policy(rules if rules is not None else [])
        env = {"AGENT_CALL": "1", "AGENT_CALL_POLICY": policy}
        env.update(extra_env or {})
        self.stores = {}          # sid -> SessionCardStore
        self.dispatch = dispatch or AsyncMock(return_value={"ok": True})

        async def _store_for(sid):
            if sid not in self.stores:
                self.stores[sid] = carddigest.SessionCardStore()
            return self.stores[sid]

        self._patches = [
            patch.dict(os.environ, env),
            patch.object(bridge, "REGISTRY", self.reg),
            patch.object(bridge, "_v2_card_source", side_effect=_fake_card_source),
            patch.object(bridge, "_v2_card_store", side_effect=_store_for),
            patch.object(bridge, "v2_session_input", self.dispatch),
            # 絕不代審:approval 核心全上哨兵,測試尾聲驗證零觸碰。
            patch.object(bridge, "_approval_decide_core", MagicMock()),
            patch.object(bridge, "_cc_key_core", MagicMock()),
            patch.object(bridge.CODEX_APP, "decide_thread_approval", MagicMock()),
        ]

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *a):
        for p in reversed(self._patches):
            p.stop()

    def assert_no_auto_approve(self, tc):
        bridge._approval_decide_core.assert_not_called()
        bridge._cc_key_core.assert_not_called()
        bridge.CODEX_APP.decide_thread_approval.assert_not_called()

    def cards(self, sid):
        store = self.stores.get(sid)
        return list(store.cards.values()) if store else []


def _post(body):
    return asyncio.run(bridge.v2_agent_call(FakeRequest(body)))


# ───────────────────────── 政策模組(純邏輯)─────────────────────────

class TestPolicyModule(unittest.TestCase):
    def test_missing_or_bad_policy_denies_all(self):
        pol = agent_call.load_policy(os.path.join(_TMP, "no-such.json"))
        self.assertFalse(agent_call.allowed(pol, "a", "b"))
        bad = tempfile.mktemp(suffix=".json", dir=_TMP)
        with open(bad, "w") as f:
            f.write("{not json")
        self.assertFalse(agent_call.allowed(agent_call.load_policy(bad), "a", "b"))

    def test_allowlist_fnmatch(self):
        pol = agent_call.load_policy(_write_policy(
            [{"caller": "hermes:yuanfang", "targets": ["claude_code:*"]}]))
        self.assertTrue(agent_call.allowed(pol, "hermes:yuanfang",
                                           "claude_code:tirith"))
        self.assertFalse(agent_call.allowed(pol, "hermes:yuanfang", "codex:x"))
        self.assertFalse(agent_call.allowed(pol, "hermes:other",
                                            "claude_code:tirith"))
        # 自呼永遠拒
        pol2 = agent_call.load_policy(_write_policy(
            [{"caller": "*", "targets": ["*"]}]))
        self.assertFalse(agent_call.allowed(pol2, "a", "a"))
        self.assertEqual(agent_call.allowed_target_patterns(
            pol, "hermes:yuanfang"), ["claude_code:*"])

    def test_check_chain_root_depth_cycle_budget(self):
        self.assertEqual(agent_call.check_chain(None, [], 0, "a", "b"),
                         (None, None, 1))
        parent = {"id": "call-1", "root_call_id": "call-1", "depth": 1,
                  "caller": "a", "target": "b"}
        root, pid, depth = agent_call.check_chain(parent, [parent], 1, "b", "c")
        self.assertEqual((root, pid, depth), ("call-1", "call-1", 2))
        # 深度:parent 已在第 2 層 → 拒
        deep = {**parent, "id": "call-2", "depth": 2, "caller": "b",
                "target": "c"}
        with self.assertRaises(agent_call.CallDenied) as cm:
            agent_call.check_chain(deep, [deep, parent], 2, "c", "d")
        self.assertEqual(cm.exception.code, "AGENT_CALL_DEPTH")
        # 循環:A→B 後 B→A → 拒
        with self.assertRaises(agent_call.CallDenied) as cm:
            agent_call.check_chain(parent, [parent], 1, "b", "a")
        self.assertEqual(cm.exception.code, "AGENT_CALL_CYCLE")
        # 預算:chain 已 6 個 call → 拒
        with self.assertRaises(agent_call.CallDenied) as cm:
            agent_call.check_chain(parent, [parent], 6, "b", "c")
        self.assertEqual(cm.exception.code, "AGENT_CALL_BUDGET")


# ───────────────────────── call 帳本(registry DB)─────────────────────────

class TestCallLedger(unittest.TestCase):
    def test_create_update_get_list(self):
        reg = _fresh_registry()
        row = reg.call_create("call-1", caller="a", target="b",
                              mode="await_reply", message="hi")
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["root_call_id"], "call-1")   # root = 自己
        self.assertIsNone(row["finished_ts"])
        row = reg.call_update("call-1", status="done", reply="回覆")
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["reply"], "回覆")
        self.assertIsNotNone(row["finished_ts"])
        self.assertIsNone(reg.call_update("call-x", status="done"))
        rows = reg.call_list(session="a")
        self.assertEqual([r["id"] for r in rows], ["call-1"])
        self.assertEqual(reg.call_list(session="zzz"), [])

    def test_active_for_target_and_chain_helpers(self):
        reg = _fresh_registry()
        reg.call_create("call-1", caller="a", target="b", mode="await_reply",
                        message="m", status="running")
        parent = reg.call_active_for_target("b")
        self.assertEqual(parent["id"], "call-1")
        self.assertIsNone(reg.call_active_for_target("a"))
        # sent(fire_and_forget)在回看窗內也算 parent;窗外不算
        reg.call_create("call-2", caller="a", target="c",
                        mode="fire_and_forget", message="m", status="sent")
        self.assertIsNotNone(reg.call_active_for_target("c", recent_secs=600))
        self.assertIsNone(reg.call_active_for_target("c", recent_secs=-1))
        # ancestors + chain size
        child = reg.call_create("call-3", caller="b", target="c",
                                mode="await_reply", message="m",
                                root_call_id="call-1", parent_call_id="call-1",
                                depth=2)
        chain = reg.call_ancestors(child)
        self.assertEqual([r["id"] for r in chain], ["call-3", "call-1"])
        self.assertEqual(reg.call_chain_size("call-1"), 2)
        # denied 不佔 chain 預算
        reg.call_create("call-4", caller="b", target="d", mode="await_reply",
                        message="m", status="denied", root_call_id="call-1")
        self.assertEqual(reg.call_chain_size("call-1"), 2)


# ───────────────────────── 端點:旗標與政策 ─────────────────────────

class TestFlagAndPolicy(unittest.TestCase):
    def test_flag_off_all_endpoints_404(self):
        with patch.dict(os.environ, {"AGENT_CALL": ""}):
            for coro in (
                    bridge.v2_agent_call(FakeRequest({})),
                    bridge.v2_agent_call_result("call-1", FakeRequest()),
                    bridge.v2_agent_calls(FakeRequest()),
                    bridge.v2_agent_targets(FakeRequest(), caller="x")):
                with self.assertRaises(HTTPException) as cm:
                    asyncio.run(coro)
                self.assertEqual(cm.exception.status_code, 404)

    def test_deny_by_default_and_audit(self):
        with _Env(rules=[]) as env:   # 空政策 = default DENY
            with self.assertRaises(HTTPException) as cm:
                _post({"caller": CALLER, "target": TARGET, "message": "去做事",
                       "mode": "fire_and_forget"})
            self.assertEqual(cm.exception.status_code, 403)
            env.dispatch.assert_not_called()      # 拒絕就不投遞
            rows = env.reg.call_list(session=CALLER)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "denied")
            # 拒絕 audit 卡落 caller 卡片流,帶 🔗 前綴
            cards = env.cards(CALLER)
            self.assertEqual(len(cards), 1)
            self.assertIn("🔗 代理互調", cards[0]["body"]["text"])
            self.assertIn("拒絕", cards[0]["body"]["text"])
            env.assert_no_auto_approve(self)

    def test_allowlist_grant_fire_and_forget(self):
        rules = [{"caller": "hermes:yuanfang", "targets": ["claude_code:*"]}]
        with _Env(rules=rules) as env:
            res = _post({"caller": CALLER, "target": TARGET,
                         "message": "幫我跑測試", "mode": "fire_and_forget"})
            self.assertEqual(res["status"], "sent")
            env.dispatch.assert_awaited_once()
            sid, shim = env.dispatch.await_args.args
            self.assertEqual(sid, TARGET)
            body = asyncio.run(shim.json())
            self.assertIn("幫我跑測試", body["content"])
            self.assertIn("[agent_call hermes:yuanfang", body["content"])
            self.assertEqual(body["client_id"], res["call_id"])
            # audit request 卡雙邊都有
            for side in (CALLER, TARGET):
                texts = [c["body"]["text"] for c in env.cards(side)]
                self.assertTrue(any("🔗 代理互調" in t for t in texts), side)
            row = env.reg.call_get(res["call_id"])
            self.assertEqual(row["status"], "sent")
            self.assertEqual(row["depth"], 1)
            env.assert_no_auto_approve(self)

    def test_bad_inputs(self):
        rules = [{"caller": "*", "targets": ["*"]}]
        with _Env(rules=rules):
            for body, status in (
                    ({"caller": CALLER, "target": TARGET, "message": "x",
                      "mode": "weird"}, 400),                       # 壞 mode
                    ({"caller": CALLER, "message": "x"}, 400),      # 缺 target
                    ({"caller": CALLER, "target": CALLER,
                      "message": "x"}, 400),                        # 自呼
                    ({"caller": "hermes:nobody", "target": TARGET,
                      "message": "x"}, 400),                        # 未知 caller
                    ({"caller": CALLER, "target": "codex:ghost",
                      "message": "x"}, 404)):                       # 未知 target
                with self.assertRaises(HTTPException) as cm:
                    _post(body)
                self.assertEqual(cm.exception.status_code, status, body)


# ───────────────────────── 端點:chain 護欄 ─────────────────────────

class TestChainGuards(unittest.TestCase):
    RULES = [{"caller": "*", "targets": ["*"]}]   # 政策全開,單測 chain 護欄

    def test_depth_rejected(self):
        with _Env(rules=self.RULES) as env:
            env.reg.call_create("call-1", caller=CALLER, target="claude_code:b",
                                mode="await_reply", message="m", depth=1)
            env.reg.call_create("call-2", caller="claude_code:b",
                                target="codex:c1", mode="await_reply",
                                message="m", root_call_id="call-1",
                                parent_call_id="call-1", depth=2)
            with self.assertRaises(HTTPException) as cm:
                _post({"caller": "codex:c1", "target": TARGET, "message": "x"})
            self.assertEqual(cm.exception.status_code, 429)
            denied = [r for r in env.reg.call_list(session="codex:c1")
                      if r["status"] == "denied"]
            self.assertEqual(len(denied), 1)
            self.assertIn("深度", denied[0]["error"])

    def test_cycle_rejected(self):
        with _Env(rules=self.RULES) as env:
            env.reg.call_create("call-1", caller=CALLER, target="claude_code:b",
                                mode="await_reply", message="m", depth=1)
            with self.assertRaises(HTTPException) as cm:
                _post({"caller": "claude_code:b", "target": CALLER,
                       "message": "回打"})
            self.assertEqual(cm.exception.status_code, 429)

    def test_budget_rejected(self):
        with _Env(rules=self.RULES) as env:
            env.reg.call_create("call-1", caller=CALLER, target="claude_code:b",
                                mode="await_reply", message="m", depth=1)
            for i in range(5):
                env.reg.call_create(f"call-x{i}", caller="claude_code:b",
                                    target="codex:c1", mode="fire_and_forget",
                                    message="m", status="sent",
                                    root_call_id="call-1",
                                    parent_call_id="call-1", depth=2)
            with self.assertRaises(HTTPException) as cm:
                _post({"caller": "claude_code:b", "target": "codex:c1",
                       "message": "x"})
            self.assertEqual(cm.exception.status_code, 429)

    def test_explicit_bad_parent_400(self):
        with _Env(rules=self.RULES):
            with self.assertRaises(HTTPException) as cm:
                _post({"caller": CALLER, "target": TARGET, "message": "x",
                       "parent_call_id": "call-ghost"})
            self.assertEqual(cm.exception.status_code, 400)


# ───────────────────────── await_reply 收割 ─────────────────────────

class TestAwaitReply(unittest.TestCase):
    RULES = [{"caller": "*", "targets": ["*"]}]

    def test_happy_path(self):
        env = _Env(rules=self.RULES)

        async def _dispatch(sid, shim):
            # 投遞後目標回覆:assistant 卡 + turn end(卡先出、end 後到,
            # 與 PersonaDigest 的實際順序一致)。
            store = env.stores[TARGET]
            store.upsert_card(carddigest.make_card(
                "card-r1", "t1", "assistant", "text", {"text": "測試全綠"}))
            store.push_turn("end", "t1")
            return {"ok": True}

        env.dispatch = AsyncMock(side_effect=_dispatch)
        env._patches[4] = patch.object(bridge, "v2_session_input", env.dispatch)
        with env:
            res = _post({"caller": CALLER, "target": TARGET,
                         "message": "跑測試", "mode": "await_reply",
                         "timeout_secs": 10})
            self.assertEqual(res["status"], "done")
            self.assertEqual(res["reply"], "測試全綠")
            row = env.reg.call_get(res["call_id"])
            self.assertEqual(row["status"], "done")
            # 回覆 audit 卡雙邊都有
            for side in (CALLER, TARGET):
                texts = [c["body"]["text"] for c in env.cards(side)]
                self.assertTrue(any("已回覆" in t for t in texts), side)
            env.assert_no_auto_approve(self)

    def test_reply_excludes_audit_and_user_cards(self):
        env = _Env(rules=self.RULES)

        async def _dispatch(sid, shim):
            store = env.stores[TARGET]
            store.upsert_card(carddigest.make_card(
                "card-u1", "t1", "user", "text", {"text": "使用者回顯"}))
            store.upsert_card(carddigest.make_card(
                "card-a1", "t1", "assistant", "text",
                {"text": "🔗 假 audit", "origin": "agent_call"}))
            store.upsert_card(carddigest.make_card(
                "card-r1", "t1", "assistant", "markdown", {"text": "真回覆"}))
            store.push_turn("end", "t1")
            return {"ok": True}

        env.dispatch = AsyncMock(side_effect=_dispatch)
        env._patches[4] = patch.object(bridge, "v2_session_input", env.dispatch)
        with env:
            res = _post({"caller": CALLER, "target": TARGET, "message": "m",
                         "mode": "await_reply", "timeout_secs": 10})
            self.assertEqual(res["reply"], "真回覆")

    def test_await_timeout_then_background_harvest(self):
        # 投遞後沒人回 → await 1s 逾時轉背景;之後回覆到了,waiter 收割,
        # GET /app/v2/agent_call/{id} 拿得到 done+reply。
        with _Env(rules=self.RULES,
                  extra_env={"AGENT_CALL_BG_TIMEOUT": "30"}) as env:
            async def _run():
                res = await bridge.v2_agent_call(FakeRequest(
                    {"caller": CALLER, "target": TARGET, "message": "慢工",
                     "mode": "await_reply", "timeout_secs": 1}))
                self.assertEqual(res["status"], "timeout")
                call_id = res["call_id"]
                self.assertEqual(env.reg.call_get(call_id)["status"], "running")
                store = env.stores[TARGET]
                store.upsert_card(carddigest.make_card(
                    "card-late", "t1", "assistant", "text", {"text": "遲到的回覆"}))
                store.push_turn("end", "t1")
                await bridge._AGENT_CALL_WAITERS[call_id]
                got = await bridge.v2_agent_call_result(call_id, FakeRequest())
                self.assertEqual(got["status"], "done")
                self.assertEqual(got["reply"], "遲到的回覆")
            asyncio.run(_run())

    def test_background_timeout_terminal(self):
        with _Env(rules=self.RULES,
                  extra_env={"AGENT_CALL_BG_TIMEOUT": "0.3"}) as env:
            async def _run():
                res = await bridge.v2_agent_call(FakeRequest(
                    {"caller": CALLER, "target": TARGET, "message": "沒人理",
                     "mode": "background"}))
                self.assertEqual(res["status"], "running")
                await bridge._AGENT_CALL_WAITERS[res["call_id"]]
                row = env.reg.call_get(res["call_id"])
                self.assertEqual(row["status"], "timeout")
                # timeout audit 卡雙邊都有
                for side in (CALLER, TARGET):
                    texts = [c["body"]["text"] for c in env.cards(side)]
                    self.assertTrue(any("逾時" in t for t in texts), side)
            asyncio.run(_run())

    def test_dispatch_failure_marks_error(self):
        env = _Env(rules=self.RULES,
                   dispatch=AsyncMock(side_effect=bridge.http_err(
                       409, "BUSY", "target 忙線")))
        with env:
            with self.assertRaises(HTTPException) as cm:
                _post({"caller": CALLER, "target": TARGET, "message": "x",
                       "mode": "fire_and_forget"})
            self.assertEqual(cm.exception.status_code, 502)
            rows = [r for r in env.reg.call_list(session=CALLER)
                    if r["status"] == "error"]
            self.assertEqual(len(rows), 1)


# ───────────────────────── agent_targets / agent_calls ─────────────────────────

class TestTargetsAndLedgerApi(unittest.TestCase):
    def test_targets_filtered_by_policy(self):
        rules = [{"caller": "hermes:yuanfang", "targets": ["claude_code:*"]}]
        with _Env(rules=rules) as env:
            env.reg.register("claude_code:tirith", provider="claude_code",
                             purpose="常駐 CC", cls="persistent")
            env.reg.register("codex:c1", provider="codex", purpose="cx 工")
            with patch.object(bridge, "_registry_ensure_personas"), \
                    patch.object(bridge, "_registry_legacy_rows",
                                 AsyncMock(return_value=[])), \
                    patch.object(bridge, "_registry_is_busy",
                                 AsyncMock(return_value=True)):
                res = asyncio.run(bridge.v2_agent_targets(
                    FakeRequest(), caller="yuanfang"))   # 裸 persona id 可
            self.assertEqual(res["caller"], "hermes:yuanfang")
            self.assertEqual([t["id"] for t in res["targets"]],
                             ["claude_code:tirith"])
            self.assertTrue(res["targets"][0]["busy"])
            self.assertEqual(res["targets"][0]["purpose"], "常駐 CC")

    def test_calls_ledger_endpoint(self):
        with _Env(rules=[]) as env:
            env.reg.call_create("call-1", caller=CALLER, target=TARGET,
                                mode="await_reply", message="m")
            res = asyncio.run(bridge.v2_agent_calls(FakeRequest(),
                                                    session=CALLER))
            self.assertEqual([c["call_id"] for c in res["calls"]], ["call-1"])
            res = asyncio.run(bridge.v2_agent_calls(FakeRequest(),
                                                    root="call-1"))
            self.assertEqual(len(res["calls"]), 1)


# ───────────────────────── addendum:spawn 落籍 + children ─────────────────────────

class TestRegistryAddendum(unittest.TestCase):
    def test_spawn_fields_records_parent_and_purpose(self):
        reg = _fresh_registry()
        with patch.object(bridge, "REGISTRY", reg):
            reg.register("hermes:yuanfang", provider="hermes",
                         cls="persistent", purpose="常駐")
            parent, cls, purpose = bridge._registry_spawn_fields(
                {"parent": "hermes:yuanfang", "purpose": "查資料"})
            self.assertEqual((parent, cls, purpose),
                             ("hermes:yuanfang", "task", "查資料"))
            row = bridge._registry_register(
                "codex:t9", provider="codex", purpose=purpose, cls=cls,
                parent=parent)
            self.assertEqual(row["parent"], "hermes:yuanfang")
            self.assertEqual(row["purpose"], "查資料")
            self.assertEqual(row["depth"], 1)

    def test_unknown_parent_rejected_400(self):
        reg = _fresh_registry()
        with patch.object(bridge, "REGISTRY", reg), \
                patch.object(bridge, "_v2_card_source",
                             side_effect=_fake_card_source):
            with self.assertRaises(HTTPException) as cm:
                bridge._registry_spawn_fields({"parent": "codex:ghost",
                                               "purpose": "x"})
            self.assertEqual(cm.exception.status_code, 400)

    def test_live_bypass_parent_backfilled_unregistered(self):
        reg = _fresh_registry()
        with patch.object(bridge, "REGISTRY", reg), \
                patch.object(bridge, "_v2_card_source",
                             side_effect=_fake_card_source):
            bridge._registry_validate_parent(TARGET)   # 旁路 live CC lane
            row = reg.get(TARGET)
            self.assertIsNotNone(row)
            self.assertFalse(row["registered"])        # reaper 永不碰
            # 配額照算:depth/子額以此 parent 為錨
            depth = reg.precheck(TARGET, "task")
            self.assertEqual(depth, 1)

    def test_children_endpoint_shape_and_busy_merge(self):
        reg = _fresh_registry()
        busy_mock = AsyncMock(side_effect=lambda sid: sid == "codex:busy1")
        with patch.object(bridge, "REGISTRY", reg), \
                patch.object(bridge, "_registry_is_busy", busy_mock), \
                patch.dict(bridge._REGISTRY_BUSY_CACHE, clear=True):
            reg.register("hermes:yuanfang", provider="hermes",
                         cls="persistent", purpose="常駐")
            reg.register("codex:busy1", provider="codex", purpose="忙工",
                         parent="hermes:yuanfang")
            reg.register("codex:idle1", provider="codex", purpose="閒工",
                         parent="hermes:yuanfang")
            reg.register("codex:other", provider="codex", purpose="別家的")
            res = asyncio.run(bridge.v2_registry_children(
                "hermes:yuanfang", FakeRequest()))
            kids = {c["id"]: c for c in res["children"]}
            self.assertEqual(set(kids), {"codex:busy1", "codex:idle1"})
            self.assertTrue(kids["codex:busy1"]["busy"])
            self.assertFalse(kids["codex:idle1"]["busy"])
            for c in kids.values():
                for key in ("id", "provider", "name", "purpose", "class",
                            "state", "busy", "last_active_ts"):
                    self.assertIn(key, c)
            # TTL 快取:窗內第二次 poll 不再打 provider busy 探測
            n = busy_mock.await_count
            asyncio.run(bridge.v2_registry_children("hermes:yuanfang",
                                                    FakeRequest()))
            self.assertEqual(busy_mock.await_count, n)
            # archived 孩子不列
            reg.archive("codex:idle1", "manual")
            res = asyncio.run(bridge.v2_registry_children(
                "hermes:yuanfang", FakeRequest()))
            self.assertEqual([c["id"] for c in res["children"]],
                             ["codex:busy1"])




# ── 母子邊放行(善彰鐵律:cc/cx 互相調閱只有母子)─────────────────────

class FamilyEdgeTests(unittest.TestCase):
    """registry 家譜直接母子邊自動放行,不需政策維護。"""

    class _FakeRegistry:
        def __init__(self, rows): self.rows = rows
        def get(self, sid): return self.rows.get(sid)

    def _reg(self, **parents):
        return self._FakeRegistry({sid: {"parent": p} for sid, p in parents.items()})

    def test_child_can_call_parent(self):
        reg = self._reg(**{"claude_code:child": "claude_code:mom"})
        self.assertTrue(agent_call.allowed(
            {"rules": []}, "claude_code:child", "claude_code:mom", registry=reg))

    def test_parent_can_call_child(self):
        reg = self._reg(**{"claude_code:child": "claude_code:mom"})
        self.assertTrue(agent_call.allowed(
            {"rules": []}, "claude_code:mom", "claude_code:child", registry=reg))

    def test_siblings_denied(self):
        reg = self._reg(**{"claude_code:a": "claude_code:mom",
                           "claude_code:b": "claude_code:mom"})
        self.assertFalse(agent_call.allowed(
            {"rules": []}, "claude_code:a", "claude_code:b", registry=reg))

    def test_grandparent_denied(self):
        """深度 2 的孫不能直接叫爺爺 —— 只有**直接**母子邊算數。"""
        reg = self._reg(**{"claude_code:kid": "claude_code:mom",
                           "claude_code:mom": "hermes:yuanfang"})
        self.assertFalse(agent_call.allowed(
            {"rules": []}, "claude_code:kid", "hermes:yuanfang", registry=reg))

    def test_stranger_denied(self):
        reg = self._reg(**{"claude_code:a": "", "codex:b": ""})
        self.assertFalse(agent_call.allowed(
            {"rules": []}, "claude_code:a", "codex:b", registry=reg))

    def test_self_call_denied_even_on_family_path(self):
        reg = self._reg(**{"claude_code:a": "claude_code:a"})
        self.assertFalse(agent_call.allowed(
            {"rules": []}, "claude_code:a", "claude_code:a", registry=reg))

    def test_registry_none_falls_back_to_allowlist(self):
        """registry 不可用 → 舊行為(純白名單)完全不變。"""
        pol = {"rules": [{"caller": "hermes:*", "targets": ["claude_code:*"]}]}
        self.assertTrue(agent_call.allowed(
            pol, "hermes:yuanfang", "claude_code:ops", registry=None))
        self.assertFalse(agent_call.allowed(
            {"rules": []}, "hermes:yuanfang", "claude_code:ops", registry=None))

    def test_registry_exception_does_not_break_calls(self):
        class Boom:
            def get(self, sid): raise RuntimeError("registry down")
        pol = {"rules": [{"caller": "hermes:*", "targets": ["claude_code:*"]}]}
        self.assertTrue(agent_call.allowed(
            pol, "hermes:yuanfang", "claude_code:ops", registry=Boom()))

    def test_allowlist_still_works_alongside_family(self):
        """母子邊是新增放行來源,既有白名單一字不變。"""
        reg = self._reg(**{"claude_code:x": "hermes:y"})
        pol = {"rules": [{"caller": "codex:*", "targets": ["claude_code:*"]}]}
        self.assertTrue(agent_call.allowed(
            pol, "codex:z", "claude_code:x", registry=reg))


if __name__ == "__main__":
    unittest.main()
