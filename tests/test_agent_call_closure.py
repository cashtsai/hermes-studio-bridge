"""agent_call 派單閉環(closure 契約,2026-08-16)。

Cindy/Orca 對照後補上的四條鐵律:
  1. accepted 才有副作用 —— send 被目標接受前帳上是 dispatching,不冒充 running
  2. 終態不被覆蓋 —— 先到的終態贏,晚到的結算不得改寫 status
  3. auto-bridge —— background/await-逾時 的 call 結案時把結果送回 caller
     的 session 喚醒它(冪等,await 同步拿到結果的不重送)
  4. 重啟對帳 —— 收割人只活在記憶體;開機把未結案 call 接回(投遞中斷結案/
     窗過結 timeout/窗內重掛收割人,收割窗沿用原 created_ts)
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_TMP = tempfile.mkdtemp(prefix="agent-call-closure-test-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("POCKET_REGISTRY_DB",
                      os.path.join(_TMP, "bridge-registry.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_registry  # noqa: E402
import bridge  # noqa: E402
import carddigest  # noqa: E402


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
_KNOWN = {CALLER: ("hp", "yuanfang"), TARGET: ("cc", "tirith", "/tmp")}
RULES = [{"caller": "*", "targets": ["*"]}]


def _fake_card_source(sid):
    if sid in _KNOWN:
        return _KNOWN[sid]
    raise bridge.http_err(404, "SESSION_NOT_FOUND", "unknown session")


class _Env:
    """同 test_agent_call 的標準 patch 組(獨立複本,兩檔互不相依)。"""

    def __init__(self, rules=None, extra_env=None, dispatch=None):
        self.reg = _fresh_registry()
        policy = _write_policy(rules if rules is not None else RULES)
        env = {"AGENT_CALL": "1", "AGENT_CALL_POLICY": policy}
        env.update(extra_env or {})
        self.stores = {}
        self.dispatch = dispatch or AsyncMock(return_value={"ok": True})

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

    def set_dispatch(self, fn):
        self.dispatch = AsyncMock(side_effect=fn)
        self._patches[4] = patch.object(bridge, "v2_session_input",
                                        self.dispatch)


# ───────────────────── 1. accepted 才有副作用 ─────────────────────

class TestAcceptedSemantics(unittest.TestCase):
    def test_status_is_dispatching_until_send_accepted(self):
        env = _Env()
        seen = {}

        async def _dispatch(sid, shim):
            # send 尚未被接受的瞬間,帳上必須是 dispatching(不是 running)。
            rows = env.reg.call_list()
            seen["status_at_dispatch"] = rows[0]["status"]
            return {"ok": True}

        env.set_dispatch(_dispatch)
        with env:
            res = asyncio.run(bridge.v2_agent_call(FakeRequest(
                {"caller": CALLER, "target": TARGET, "message": "m",
                 "mode": "background"})))
            self.assertEqual(seen["status_at_dispatch"], "dispatching")
            self.assertEqual(env.reg.call_get(res["call_id"])["status"],
                             "running")
            # 收割基準 seq 落在 meta(重啟對帳的錨)
            meta = env.reg.call_get(res["call_id"])["meta"]
            self.assertIn("since_seq", meta)

    def test_dispatch_failure_never_leaves_running(self):
        env = _Env()

        async def _boom(sid, shim):
            raise bridge.http_err(503, "DOWN", "target 掛了")

        env.set_dispatch(_boom)
        with env:
            with self.assertRaises(Exception):
                asyncio.run(bridge.v2_agent_call(FakeRequest(
                    {"caller": CALLER, "target": TARGET, "message": "m",
                     "mode": "background"})))
            row = env.reg.call_list()[0]
            self.assertEqual(row["status"], "error")
            self.assertIsNotNone(row["finished_ts"])


# ───────────────────── 2. 終態不被覆蓋(registry)─────────────────────

class TestTerminalStateWins(unittest.TestCase):
    def test_terminal_status_not_overwritten(self):
        reg = _fresh_registry()
        reg.call_create("c-1", caller=CALLER, target=TARGET,
                        mode="background", status="running")
        reg.call_update("c-1", status="done", reply="第一個終態")
        # 晚到的結算(對帳 timeout / 收割人 error)不得改寫
        reg.call_update("c-1", status="timeout", error="晚到")
        row = reg.call_get("c-1")
        self.assertEqual(row["status"], "done")
        # 但 meta/reply 照常可補
        reg.call_update("c-1", meta_merge={"note": 1})
        self.assertEqual(reg.call_get("c-1")["meta"]["note"], 1)

    def test_dispatching_counts_as_unfinished(self):
        reg = _fresh_registry()
        reg.call_create("c-2", caller=CALLER, target=TARGET,
                        mode="background", status="dispatching")
        row = reg.call_get("c-2")
        self.assertIsNone(row["finished_ts"])
        ids = [r["id"] for r in reg.call_list_unfinished()]
        self.assertIn("c-2", ids)


# ───────────────────── 3. auto-bridge 送達 caller ─────────────────────

class TestAutoDeliver(unittest.TestCase):
    def _run_background_call(self, env):
        async def _run():
            res = await bridge.v2_agent_call(FakeRequest(
                {"caller": CALLER, "target": TARGET, "message": "去辦",
                 "mode": "background"}))
            call_id = res["call_id"]
            store = env.stores[TARGET]
            store.upsert_card(carddigest.make_card(
                "card-r1", "t1", "assistant", "text", {"text": "辦完了"}))
            store.push_turn("end", "t1")
            await bridge._AGENT_CALL_WAITERS[call_id]
            return call_id
        return asyncio.run(_run())

    def test_background_reply_wakes_caller(self):
        with _Env() as env:
            call_id = self._run_background_call(env)
            row = env.reg.call_get(call_id)
            self.assertEqual(row["status"], "done")
            self.assertTrue(row["meta"].get("reply_delivered"))
            # 送達 = 對 caller 的 session input(喚醒),內容帶回報頭
            deliveries = [c for c in env.dispatch.await_args_list
                          if c.args and c.args[0] == CALLER]
            self.assertEqual(len(deliveries), 1)
            body = asyncio.run(deliveries[0].args[1].json())
            self.assertIn("[agent_call 回報", body["content"])
            self.assertIn("辦完了", body["content"])
            self.assertEqual(body["client_id"], f"{call_id}-reply")

    def test_await_reply_synchronous_result_not_redelivered(self):
        env = _Env()

        async def _dispatch(sid, shim):
            store = env.stores[TARGET]
            store.upsert_card(carddigest.make_card(
                "card-r1", "t1", "assistant", "text", {"text": "同步拿到"}))
            store.push_turn("end", "t1")
            return {"ok": True}

        env.set_dispatch(_dispatch)
        with env:
            res = asyncio.run(bridge.v2_agent_call(FakeRequest(
                {"caller": CALLER, "target": TARGET, "message": "m",
                 "mode": "await_reply", "timeout_secs": 10})))
            self.assertEqual(res["status"], "done")
            # caller 已在 HTTP 回應同步拿到 → 不得再注入喚醒(雙份)
            deliveries = [c for c in env.dispatch.await_args_list
                          if c.args and c.args[0] == CALLER]
            self.assertEqual(len(deliveries), 0)

    def test_deliver_is_idempotent(self):
        with _Env() as env:
            call_id = self._run_background_call(env)
            before = len(env.dispatch.await_args_list)
            asyncio.run(bridge._agent_call_maybe_deliver(call_id))
            self.assertEqual(len(env.dispatch.await_args_list), before)


# ───────────────────── 4. 重啟對帳 ─────────────────────

class TestReconcile(unittest.TestCase):
    def test_reconcile_settles_and_rearms(self):
        with _Env(extra_env={"AGENT_CALL_BG_TIMEOUT": "60"}) as env:
            reg = env.reg
            now = time.time()
            # a) 投遞中斷:dispatching 遺留
            reg.call_create("c-disp", caller=CALLER, target=TARGET,
                            mode="background", status="dispatching")
            # b) 收割窗已過的 running(created_ts 造舊)
            reg.call_create("c-old", caller=CALLER, target=TARGET,
                            mode="background", status="running",
                            meta={"since_seq": 0})
            with reg._lock:
                con = reg._connect()
                con.execute("UPDATE agent_calls SET created_ts=? WHERE id=?",
                            (now - 9999, "c-old"))
                con.commit()
                con.close()
            # c) 窗內的 running → 重掛收割人
            reg.call_create("c-live", caller=CALLER, target=TARGET,
                            mode="await_reply", status="running",
                            meta={"since_seq": 0})

            async def _run():
                stats = await bridge._agent_call_reconcile_once()
                self.assertEqual(stats["dispatch_interrupted"], 1)
                self.assertEqual(stats["timeout"], 1)
                self.assertEqual(stats["rearmed"], 1)
                self.assertEqual(reg.call_get("c-disp")["status"], "error")
                self.assertEqual(reg.call_get("c-old")["status"], "timeout")
                self.assertEqual(reg.call_get("c-live")["status"], "running")
                # 重掛的收割人真的能收:補上回覆 → done + 送達 caller
                store = env.stores[TARGET]
                store.upsert_card(carddigest.make_card(
                    "card-r1", "t1", "assistant", "text", {"text": "重啟後收到"}))
                store.push_turn("end", "t1")
                await bridge._AGENT_CALL_WAITERS["c-live"]
                row = reg.call_get("c-live")
                self.assertEqual(row["status"], "done")
                self.assertEqual(row["reply"], "重啟後收到")
                # await_reply 重啟後同步等待者已死 → 轉背景語意,必須送達
                self.assertTrue(row["meta"].get("reply_delivered"))
            asyncio.run(_run())

    def test_reconcile_legacy_row_without_since_seq(self):
        with _Env(extra_env={"AGENT_CALL_BG_TIMEOUT": "60"}) as env:
            env.reg.call_create("c-legacy", caller=CALLER, target=TARGET,
                                mode="background", status="running")

            async def _run():
                stats = await bridge._agent_call_reconcile_once()
                self.assertEqual(stats["timeout"], 1)
                self.assertEqual(env.reg.call_get("c-legacy")["status"],
                                 "timeout")
            asyncio.run(_run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
