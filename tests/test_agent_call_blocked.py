"""agent_call blocked-aware(herdr 對照 H2/H4,2026-08-16)。

H4:派單前 target 已停在待審 → 拒送不排隊(409 AGENT_CALL_TARGET_BLOCKED,
    force=true 可硬排);偵測失敗絕不擋派單。
H2:派單後 target 中途轉入待審 → 提前告知 caller 一次(audit 卡 + background
    模式注入提醒),不結案不代審;審核通過後收割照常。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_TMP = tempfile.mkdtemp(prefix="agent-call-blocked-test-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("POCKET_REGISTRY_DB",
                      os.path.join(_TMP, "bridge-registry.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_registry  # noqa: E402
import bridge  # noqa: E402
import carddigest  # noqa: E402
from fastapi import HTTPException  # noqa: E402


def _fresh_registry():
    return agent_registry.AgentRegistry(
        tempfile.mktemp(suffix=".db", dir=_TMP),
        task_ttl=100.0, ephemeral_ttl=50.0, max_children=3,
        task_cap=12, max_depth=2, idle_secs=10.0)


def _write_policy(rules):
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
          "codex:0123": ("cx", "0123")}
RULES = [{"caller": "*", "targets": ["*"]}]


def _fake_card_source(sid):
    if sid in _KNOWN:
        return _KNOWN[sid]
    raise bridge.http_err(404, "SESSION_NOT_FOUND", "unknown session")


class _Env:
    def __init__(self, extra_env=None):
        self.reg = _fresh_registry()
        env = {"AGENT_CALL": "1", "AGENT_CALL_POLICY": _write_policy(RULES),
               "AGENT_CALL_BLOCKED_CHECK_SECS": "0.05"}
        env.update(extra_env or {})
        self.stores = {}
        self.dispatch = AsyncMock(return_value={"ok": True})

        async def _store_for(sid):
            if sid not in self.stores:
                self.stores[sid] = carddigest.SessionCardStore()
            return self.stores[sid]

        self._patches = [
            patch.dict(os.environ, env),
            patch.object(bridge, "REGISTRY", self.reg),
            patch.object(bridge, "_v2_card_source",
                         side_effect=_fake_card_source),
            patch.object(bridge, "_v2_card_store", side_effect=_store_for),
            patch.object(bridge, "v2_session_input", self.dispatch),
            patch.object(bridge, "_approval_decide_core", MagicMock()),
            patch.object(bridge, "_cc_key_core", MagicMock()),
            patch.object(bridge.CODEX_APP, "decide_thread_approval",
                         MagicMock()),
        ]

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *a):
        for p in reversed(self._patches):
            p.stop()

    def cards(self, sid):
        store = self.stores.get(sid)
        return list(store.cards.values()) if store else []


# ───────────────────── H4:派單前拒送 ─────────────────────

class TestBlockedRejectsDispatch(unittest.TestCase):
    def test_blocked_target_rejected_409(self):
        with _Env() as env:
            with patch.object(bridge, "_agent_call_target_blocked",
                              AsyncMock(return_value="Claude Code 停在權限審核選單")):
                with self.assertRaises(HTTPException) as ctx:
                    asyncio.run(bridge.v2_agent_call(FakeRequest(
                        {"caller": CALLER, "target": TARGET, "message": "m",
                         "mode": "background"})))
            self.assertEqual(ctx.exception.status_code, 409)
            # 帳本留 denied 紀錄,絕不投遞
            row = env.reg.call_list()[0]
            self.assertEqual(row["status"], "denied")
            env.dispatch.assert_not_awaited()

    def test_force_true_dispatches_anyway(self):
        with _Env() as env:
            with patch.object(bridge, "_agent_call_target_blocked",
                              AsyncMock(return_value="codex 停在待審核")):
                res = asyncio.run(bridge.v2_agent_call(FakeRequest(
                    {"caller": CALLER, "target": TARGET, "message": "m",
                     "mode": "fire_and_forget", "force": True})))
            self.assertEqual(res["status"], "sent")
            env.dispatch.assert_awaited()

    def test_detection_failure_never_blocks(self):
        # _agent_call_target_blocked 內部吞例外回 None —— 這裡驗證整條鏈:
        # provider 查詢炸掉,派單照走。
        with _Env() as env:
            with patch.object(bridge.CODEX_APP, "pending_approval_for_thread",
                              MagicMock(side_effect=RuntimeError("boom"))):
                res = asyncio.run(bridge.v2_agent_call(FakeRequest(
                    {"caller": CALLER, "target": "codex:0123", "message": "m",
                     "mode": "fire_and_forget"})))
            self.assertEqual(res["status"], "sent")

    def test_not_blocked_passes(self):
        with _Env():
            with patch.object(bridge, "_agent_call_target_blocked",
                              AsyncMock(return_value=None)):
                res = asyncio.run(bridge.v2_agent_call(FakeRequest(
                    {"caller": CALLER, "target": TARGET, "message": "m",
                     "mode": "fire_and_forget"})))
            self.assertEqual(res["status"], "sent")


# ───────────────────── H2:派單後提前告知 ─────────────────────

class TestBlockedNoticeMidCall(unittest.TestCase):
    def test_notice_once_then_harvest_continues(self):
        with _Env(extra_env={"AGENT_CALL_BG_TIMEOUT": "30"}) as env:
            async def _run():
                # 派單當下沒 blocked
                with patch.object(bridge, "_agent_call_target_blocked",
                                  AsyncMock(return_value=None)):
                    res = await bridge.v2_agent_call(FakeRequest(
                        {"caller": CALLER, "target": TARGET, "message": "去辦",
                         "mode": "background"}))
                call_id = res["call_id"]
                # 之後 target 停在待審 → waiter 下一輪偵測到
                with patch.object(bridge, "_agent_call_target_blocked",
                                  AsyncMock(return_value="Claude Code 停在權限審核選單")) as mb:
                    await asyncio.sleep(0.4)   # 幾個節流窗,驗證只通知一次
                    row = env.reg.call_get(call_id)
                    self.assertEqual(row["meta"].get("blocked_notice"),
                                     "Claude Code 停在權限審核選單")
                    # audit 卡雙邊
                    for side in (CALLER, TARGET):
                        texts = [c["body"]["text"] for c in env.cards(side)]
                        self.assertTrue(any("待審" in t or "審核" in t
                                            for t in texts), side)
                    # background → caller 收到注入提醒(一次)
                    notices = [c for c in env.dispatch.await_args_list
                               if c.args and c.args[0] == CALLER]
                    self.assertEqual(len(notices), 1)
                    body = await notices[0].args[1].json()
                    self.assertIn("提醒", body["content"])
                    self.assertEqual(body["client_id"], f"{call_id}-blocked")
                # 審核通過 → 回覆到 → 照常收割 + 送達
                store = env.stores[TARGET]
                store.upsert_card(carddigest.make_card(
                    "card-r1", "t1", "assistant", "text", {"text": "批完辦好了"}))
                store.push_turn("end", "t1")
                await bridge._AGENT_CALL_WAITERS[call_id]
                row = env.reg.call_get(call_id)
                self.assertEqual(row["status"], "done")
                self.assertEqual(row["reply"], "批完辦好了")
            asyncio.run(_run())


# ───────────────────── 偵測器本體(provider 分支)─────────────────────

class TestTargetBlockedDetector(unittest.TestCase):
    def test_codex_pending_approval(self):
        with patch.object(bridge.CODEX_APP, "pending_approval_for_thread",
                          MagicMock(return_value={"id": "a1"})):
            got = asyncio.run(bridge._agent_call_target_blocked("codex:0123"))
        self.assertIn("codex", got)

    def test_codex_provider_flags(self):
        with patch.object(bridge.CODEX_APP, "pending_approval_for_thread",
                          MagicMock(return_value=None)), \
             patch.dict(bridge.CODEX_APP.provider_status,
                        {"0123": {"type": "active",
                                  "flags": ["waitingOnApproval"]}}):
            got = asyncio.run(bridge._agent_call_target_blocked("codex:0123"))
        self.assertIsNotNone(got)

    def test_cc_waiting_approval(self):
        async def _state(name):
            return ("waiting_approval", {"question": "可以嗎"})
        with patch.object(bridge, "_v2_cc_state", side_effect=_state):
            got = asyncio.run(
                bridge._agent_call_target_blocked("claude_code:tirith"))
        self.assertIn("Claude Code", got)

    def test_hermes_never_blocked(self):
        got = asyncio.run(bridge._agent_call_target_blocked("hermes:yuanfang"))
        self.assertIsNone(got)


if __name__ == "__main__":
    unittest.main(verbosity=2)
