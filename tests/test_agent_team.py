"""lead 編隊 agent_team(Cindy cindy_orca 對照第四刀,2026-08-16)。

TOP half 的行為驗收:
  1. 旗標 off 全端點 404(merge 零風險)
  2. 一個 lead 只有一個 active team(409 帶既有 id)
  3. 招工 + 首任務:dispatched 信號真實(帳本 call 帶 team_worker_id 戳記);
     dispatch 失敗 → dispatched:false + 明確 error,worker 留在 idle
  4. send:id 精確/label 後備解析;跨隊 403(fail-closed)
  5. call 結案 → worker 狀態跟著走(done/error);終態不被覆蓋
  6. 收隊不殺 session:worker 列保留 + remaining_sessions 列出遺留
  7. status 端點形狀(live 降級不 500)
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_TMP = tempfile.mkdtemp(prefix="agent-team-test-")
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
        task_ttl=100.0, ephemeral_ttl=50.0, max_children=5,
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


LEAD = "hermes:yuanfang"
LEAD_B = "hermes:athena"
_KNOWN = {LEAD: ("hp", "yuanfang"), LEAD_B: ("hp", "athena")}
RULES = [{"caller": "*", "targets": ["*"]}]


def _fake_card_source(sid):
    if sid in _KNOWN:
        return _KNOWN[sid]
    raise bridge.http_err(404, "SESSION_NOT_FOUND", "unknown session")


async def _fake_spawn(provider, *, label, workdir, model, lead, purpose):
    """spawn mock:不碰任何 provider,只回 session 座標並讓 card source
    認得它(worker spawn 的真實路徑由各 provider 自己的測試背書)。"""
    sid = f"{provider}:{label}"
    _KNOWN[sid] = ("w", label)
    return sid


class _Env:
    """test_agent_call_closure 的標準 patch 組 + team 專屬(spawn mock、
    blocked 偵測拔真)。"""

    def __init__(self, extra_env=None, team_flag="1"):
        self.reg = _fresh_registry()
        env = {"AGENT_CALL": "1", "AGENT_TEAM": team_flag,
               "AGENT_CALL_POLICY": _write_policy(RULES)}
        env.update(extra_env or {})
        self.stores = {}
        self.dispatch = AsyncMock(return_value={"ok": True})
        self.spawn = AsyncMock(side_effect=_fake_spawn)

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
            patch.object(bridge, "_team_spawn_worker", self.spawn),
            patch.object(bridge, "_agent_call_target_blocked",
                         AsyncMock(return_value=None)),
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

    # 便利:開隊 + 招一個 worker(不帶首任務),回 (team_id, worker dict)
    def start_with_worker(self, lead=LEAD, label="b1"):
        async def _run():
            t = await bridge.v2_team_start(FakeRequest({"lead": lead}))
            w = await bridge.v2_team_worker(FakeRequest(
                {"lead": lead, "label": label, "provider": "claude_code"}))
            return t["team"]["team_id"], w
        return asyncio.run(_run())


# ───────────────────── 1. 旗標 off 全端點 404 ─────────────────────

class TestFlagGate(unittest.TestCase):
    def test_flag_off_all_endpoints_404(self):
        with _Env(team_flag="0"):
            calls = [
                bridge.v2_team_start(FakeRequest({"lead": LEAD})),
                bridge.v2_team_worker(FakeRequest(
                    {"lead": LEAD, "label": "x", "provider": "codex"})),
                bridge.v2_team_send(FakeRequest(
                    {"lead": LEAD, "worker": "x", "message": "m"})),
                bridge.v2_team_status(FakeRequest(), lead=LEAD),
                bridge.v2_team_end(FakeRequest({"lead": LEAD})),
            ]
            for coro in calls:
                with self.assertRaises(HTTPException) as ctx:
                    asyncio.run(coro)
                self.assertEqual(ctx.exception.status_code, 404)
                self.assertEqual(getattr(ctx.exception, "code", ""),
                                 "AGENT_TEAM_DISABLED")


# ───────────────────── 2. 一個 lead 一個 active team ─────────────────────

class TestOneActiveTeamPerLead(unittest.TestCase):
    def test_second_start_409_with_existing_id(self):
        with _Env():
            res = asyncio.run(bridge.v2_team_start(FakeRequest({"lead": LEAD})))
            tid = res["team"]["team_id"]
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(bridge.v2_team_start(FakeRequest({"lead": LEAD})))
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(getattr(ctx.exception, "code", ""),
                             "TEAM_ALREADY_ACTIVE")
            # 409 的 detail 直接帶既有 team id,lead 不必猜
            self.assertEqual(ctx.exception.detail, tid)

    def test_partial_index_blocks_race_insert(self):
        reg = _fresh_registry()
        reg.team_start("team-a", LEAD)
        with self.assertRaises(agent_registry.TeamActiveExists) as ctx:
            reg.team_start("team-b", LEAD)
        self.assertEqual(ctx.exception.team_id, "team-a")
        # end 之後可以再開新隊
        reg.team_end("team-a")
        row = reg.team_start("team-c", LEAD)
        self.assertEqual(row["status"], "active")

    def test_two_leads_independent_teams(self):
        with _Env():
            asyncio.run(bridge.v2_team_start(FakeRequest({"lead": LEAD})))
            res = asyncio.run(bridge.v2_team_start(FakeRequest({"lead": LEAD_B})))
            self.assertEqual(res["team"]["lead"], LEAD_B)


# ───────────────────── 3. 招工 + dispatched 信號 ─────────────────────

class TestWorkerCreate(unittest.TestCase):
    def test_no_active_team_404(self):
        with _Env():
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(bridge.v2_team_worker(FakeRequest(
                    {"lead": LEAD, "label": "b1", "provider": "codex"})))
            self.assertEqual(getattr(ctx.exception, "code", ""),
                             "TEAM_NOT_FOUND")

    def test_initial_task_dispatched_true_with_ledger_stamp(self):
        with _Env() as env:
            async def _run():
                await bridge.v2_team_start(FakeRequest({"lead": LEAD}))
                res = await bridge.v2_team_worker(FakeRequest(
                    {"lead": LEAD, "label": "b1", "provider": "claude_code",
                     "role": "備份工", "initial_task": "去備份"}))
                return res
            res = asyncio.run(_run())
            self.assertTrue(res["dispatched"])
            self.assertTrue(res.get("call_id"))
            self.assertNotIn("error", res)
            # 帳本 call 存在且 meta 戳了 team_worker_id(結案時靠它找 worker)
            row = env.reg.call_get(res["call_id"])
            self.assertIsNotNone(row)
            self.assertEqual(row["meta"].get("team_worker_id"),
                             res["worker_id"])
            self.assertEqual(row["caller"], LEAD)
            self.assertEqual(row["target"], res["session_id"])
            # dispatch 已被接受 → worker running,綁著這顆 call
            w = env.reg.worker_get(res["worker_id"])
            self.assertEqual(w["status"], "running")
            self.assertEqual(w["last_call_id"], res["call_id"])
            # spawn 走的是既有派工路徑(mock 收到 lead/label)
            env.spawn.assert_awaited_once()

    def test_dispatch_failure_explicit_error_worker_stays_idle(self):
        env = _Env()

        async def _boom(sid, shim):
            raise bridge.http_err(503, "DOWN", "target 掛了")

        env.set_dispatch(_boom)
        with env:
            async def _run():
                await bridge.v2_team_start(FakeRequest({"lead": LEAD}))
                return await bridge.v2_team_worker(FakeRequest(
                    {"lead": LEAD, "label": "b1", "provider": "claude_code",
                     "initial_task": "去備份"}))
            res = asyncio.run(_run())
            # Cindy 不變量:只有真實派發才算派發 —— 明確 false + error,
            # 不丟 HTTP 例外(worker 留著,lead 要立刻回報使用者)。
            self.assertFalse(res["dispatched"])
            self.assertIn("AGENT_CALL_DISPATCH_FAILED", res["error"])
            w = env.reg.worker_get(res["worker_id"])
            self.assertEqual(w["status"], "idle")   # 回滾到派發前

    def test_duplicate_label_409(self):
        with _Env():
            async def _run():
                await bridge.v2_team_start(FakeRequest({"lead": LEAD}))
                await bridge.v2_team_worker(FakeRequest(
                    {"lead": LEAD, "label": "b1", "provider": "codex"}))
                await bridge.v2_team_worker(FakeRequest(
                    {"lead": LEAD, "label": "b1", "provider": "codex"}))
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(_run())
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(getattr(ctx.exception, "code", ""),
                             "TEAM_LABEL_TAKEN")

    def test_fire_and_forget_rejected(self):
        with _Env():
            async def _run():
                await bridge.v2_team_start(FakeRequest({"lead": LEAD}))
                return await bridge.v2_team_worker(FakeRequest(
                    {"lead": LEAD, "label": "b1", "provider": "claude_code",
                     "initial_task": "m", "mode": "fire_and_forget"}))
            res = asyncio.run(_run())
            # spawn 成功但派發被拒(無閉環模式)→ 明確信號,不例外
            self.assertFalse(res["dispatched"])
            self.assertIn("TEAM_BAD_MODE", res["error"])


# ───────────────────── 4. send:解析 + 跨隊 403 ─────────────────────

class TestSendResolution(unittest.TestCase):
    def test_send_by_label_and_by_id(self):
        with _Env() as env:
            _tid, w = env.start_with_worker(label="b1")
            for ref in ("b1", w["worker_id"]):
                res = asyncio.run(bridge.v2_team_send(FakeRequest(
                    {"lead": LEAD, "worker": ref, "message": "跑一下"})))
                self.assertTrue(res["dispatched"])
                self.assertEqual(res["worker_id"], w["worker_id"])
                row = env.reg.call_get(res["call_id"])
                self.assertEqual(row["target"], w["session_id"])
                self.assertEqual(row["meta"].get("team_worker_id"),
                                 w["worker_id"])

    def test_cross_lead_worker_id_403(self):
        with _Env() as env:
            _tid, w = env.start_with_worker(lead=LEAD, label="b1")
            asyncio.run(bridge.v2_team_start(FakeRequest({"lead": LEAD_B})))
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(bridge.v2_team_send(FakeRequest(
                    {"lead": LEAD_B, "worker": w["worker_id"],
                     "message": "偷用"})))
            self.assertEqual(ctx.exception.status_code, 403)
            self.assertEqual(getattr(ctx.exception, "code", ""),
                             "NOT_YOUR_WORKER")

    def test_unknown_ref_404(self):
        with _Env() as env:
            env.start_with_worker(label="b1")
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(bridge.v2_team_send(FakeRequest(
                    {"lead": LEAD, "worker": "nobody", "message": "m"})))
            self.assertEqual(getattr(ctx.exception, "code", ""),
                             "TEAM_WORKER_NOT_FOUND")

    def test_blocked_worker_409_passthrough(self):
        with _Env() as env:
            _tid, w = env.start_with_worker(label="b1")
            with patch.object(bridge, "_agent_call_target_blocked",
                              AsyncMock(return_value="cc 停在待審")):
                with self.assertRaises(HTTPException) as ctx:
                    asyncio.run(bridge.v2_team_send(FakeRequest(
                        {"lead": LEAD, "worker": "b1", "message": "m"})))
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(getattr(ctx.exception, "code", ""),
                             "AGENT_CALL_TARGET_BLOCKED")
            # 拒送 → worker 不冒充 running
            self.assertEqual(env.reg.worker_get(w["worker_id"])["status"],
                             "idle")


# ───────────────────── 5. 結案 → worker 狀態 ─────────────────────

class TestSettleDrivesWorkerStatus(unittest.TestCase):
    def test_done_settle_marks_worker_done(self):
        with _Env() as env:
            _tid, w = env.start_with_worker(label="b1")

            async def _run():
                res = await bridge.v2_team_send(FakeRequest(
                    {"lead": LEAD, "worker": "b1", "message": "去辦"}))
                call_id = res["call_id"]
                self.assertEqual(env.reg.worker_get(w["worker_id"])["status"],
                                 "running")
                store = env.stores[w["session_id"]]
                store.upsert_card(carddigest.make_card(
                    "card-r1", "t1", "assistant", "text", {"text": "辦完了"}))
                store.push_turn("end", "t1")
                await bridge._AGENT_CALL_WAITERS[call_id]
                return call_id
            call_id = asyncio.run(_run())
            self.assertEqual(env.reg.call_get(call_id)["status"], "done")
            self.assertEqual(env.reg.worker_get(w["worker_id"])["status"],
                             "done")

    def test_timeout_settle_marks_worker_error(self):
        with _Env(extra_env={"AGENT_CALL_BG_TIMEOUT": "0.2"}) as env:
            _tid, w = env.start_with_worker(label="b1")

            async def _run():
                res = await bridge.v2_team_send(FakeRequest(
                    {"lead": LEAD, "worker": "b1", "message": "去辦"}))
                await bridge._AGENT_CALL_WAITERS[res["call_id"]]
                return res["call_id"]
            call_id = asyncio.run(_run())
            self.assertEqual(env.reg.call_get(call_id)["status"], "timeout")
            self.assertEqual(env.reg.worker_get(w["worker_id"])["status"],
                             "error")

    def test_terminal_worker_status_not_overwritten(self):
        reg = _fresh_registry()
        reg.team_start("team-a", LEAD)
        reg.worker_add("wk-1", team_id="team-a", session_id="codex:t1",
                       label="b1")
        reg.worker_mark_running("wk-1", "call-1")
        reg.worker_note_settled("wk-1", "call-1", "done")
        self.assertEqual(reg.worker_get("wk-1")["status"], "done")
        # 晚到的重複結算/回滾都寫不進來(終態贏)
        reg.worker_note_settled("wk-1", "call-1", "error")
        self.assertEqual(reg.worker_get("wk-1")["status"], "done")
        reg.worker_rollback_idle("wk-1", "call-1")
        self.assertEqual(reg.worker_get("wk-1")["status"], "done")
        # 過期 call 的結算也不行(CAS:last_call_id 已換人)
        reg.worker_mark_running("wk-1", "call-2")
        reg.worker_note_settled("wk-1", "call-1", "error")
        self.assertEqual(reg.worker_get("wk-1")["status"], "running")
        # 同一顆 call 已結終態 → mark_running 不得倒退(收割人先跑完的競態)
        reg.worker_note_settled("wk-1", "call-2", "done")
        reg.worker_mark_running("wk-1", "call-2")
        self.assertEqual(reg.worker_get("wk-1")["status"], "done")

    def test_settle_hook_ignores_non_team_calls(self):
        with _Env() as env:
            env.reg.call_create("call-x", caller=LEAD, target="codex:t9",
                                mode="background", status="done")
            # 不炸、不動任何 worker
            bridge._team_note_call_settled("call-x")
            bridge._team_note_call_settled("call-不存在")


# ───────────────────── 6. 收隊不殺 session ─────────────────────

class TestTeamEnd(unittest.TestCase):
    def test_end_leaves_rows_and_lists_remaining(self):
        with _Env() as env:
            tid, w = env.start_with_worker(label="b1")
            res = asyncio.run(bridge.v2_team_end(FakeRequest({"lead": LEAD})))
            self.assertEqual(res["team"]["status"], "ended")
            self.assertEqual(len(res["remaining_sessions"]), 1)
            self.assertEqual(res["remaining_sessions"][0]["session_id"],
                             w["session_id"])
            self.assertIn("保留", res["note"])
            # 戶口全在:team 標 ended、worker 列不消失
            self.assertEqual(env.reg.team_get(tid)["status"], "ended")
            self.assertEqual(len(env.reg.worker_list(tid)), 1)
            # 收隊後沒有 active team → status 404;可再開新隊
            with self.assertRaises(HTTPException):
                asyncio.run(bridge.v2_team_status(FakeRequest(), lead=LEAD))
            res2 = asyncio.run(bridge.v2_team_start(FakeRequest({"lead": LEAD})))
            self.assertNotEqual(res2["team"]["team_id"], tid)


# ───────────────────── 7. status 端點形狀 ─────────────────────

class TestStatusEndpoint(unittest.TestCase):
    def test_status_shape(self):
        with _Env() as env:
            tid, w = env.start_with_worker(label="b1")
            call_id = asyncio.run(bridge.v2_team_send(FakeRequest(
                {"lead": LEAD, "worker": "b1", "message": "去辦"})))["call_id"]
            with patch.object(bridge, "_registry_busy_cached",
                              AsyncMock(return_value=True)):
                res = asyncio.run(bridge.v2_team_status(FakeRequest(),
                                                        lead=LEAD))
            self.assertEqual(res["team"]["team_id"], tid)
            self.assertEqual(len(res["workers"]), 1)
            item = res["workers"][0]
            for key in ("worker_id", "session_id", "label", "role", "status",
                        "live", "last_call"):
                self.assertIn(key, item)
            self.assertEqual(item["worker_id"], w["worker_id"])
            self.assertEqual(item["status"], "running")
            self.assertEqual(item["live"], {"busy": True, "blocked": None})
            self.assertEqual(item["last_call"]["call_id"], call_id)
            self.assertEqual(item["last_call"]["status"], "running")

    def test_live_probe_failure_degrades_not_500(self):
        with _Env() as env:
            env.start_with_worker(label="b1")
            with patch.object(bridge, "_registry_busy_cached",
                              AsyncMock(side_effect=RuntimeError("probe 炸"))):
                res = asyncio.run(bridge.v2_team_status(FakeRequest(),
                                                        lead=LEAD))
            self.assertIsNone(res["workers"][0]["live"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
