"""Agent Registry 治理層(藍圖 §3)——CRUD/配額/sweep/reaper 鐵律。

涵蓋:registry CRUD、sweep 邏輯、配額(子額/全域 task 上限/深度)、
reaper 絕不碰未登記 session、REGISTRY_REAPER 旗標關閉 = 零 destructive、
孤兒偵測(parent 歸檔、child 還活著)。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import asyncio
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_TMP = tempfile.mkdtemp(prefix="agent-registry-test-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("POCKET_REGISTRY_DB", os.path.join(_TMP, "bridge-registry.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_registry  # noqa: E402
import bridge  # noqa: E402


def _fresh_registry(**kw) -> agent_registry.AgentRegistry:
    path = tempfile.mktemp(suffix=".db", dir=_TMP)
    defaults = dict(task_ttl=100.0, ephemeral_ttl=50.0, max_children=3,
                    task_cap=12, max_depth=2, idle_secs=10.0)
    defaults.update(kw)
    return agent_registry.AgentRegistry(path, **defaults)


class FakeRequest:
    def __init__(self, body=None):
        self._body = body or {}

    async def json(self):
        return self._body


# ───────────────────────── registry CRUD ─────────────────────────

class TestRegistryCrud(unittest.TestCase):
    def test_register_defaults_and_get(self):
        reg = _fresh_registry()
        row = reg.register("codex:t1", provider="codex", name="t1",
                           purpose="測試任務", cls="task")
        self.assertEqual(row["purpose"], "測試任務")
        self.assertEqual(row["class"], "task")
        self.assertEqual(row["state"], "active")
        self.assertEqual(row["ttl_secs"], 100.0)   # task 預設 TTL
        self.assertTrue(row["registered"])
        eph = reg.register("sub-1", provider="dispatch", cls="ephemeral")
        self.assertEqual(eph["ttl_secs"], 50.0)
        self.assertEqual(eph["purpose"], agent_registry.DEFAULT_PURPOSE)
        per = reg.register("hermes:yuanfang", provider="hermes",
                           cls="persistent", purpose="常駐")
        self.assertIsNone(per["ttl_secs"])          # persistent 無壽命
        self.assertIsNone(reg.expires_ts(per))

    def test_register_is_idempotent(self):
        reg = _fresh_registry()
        a = reg.register("codex:t1", provider="codex", purpose="第一次")
        b = reg.register("codex:t1", provider="codex", purpose="第二次")
        self.assertEqual(b["purpose"], "第一次")    # 既有戶口不被覆寫
        self.assertEqual(a["id"], b["id"])

    def test_update_purpose_class_and_ttl_extend(self):
        reg = _fresh_registry()
        reg.register("codex:t1", provider="codex", purpose="原用途", cls="task")
        row = reg.update("codex:t1", purpose="新用途")
        self.assertEqual(row["purpose"], "新用途")
        # 改班 persistent → 摘壽命
        row = reg.update("codex:t1", cls="persistent")
        self.assertIsNone(row["ttl_secs"])
        # 改回 task → 補預設壽命
        row = reg.update("codex:t1", cls="task")
        self.assertEqual(row["ttl_secs"], 100.0)
        # 續命:從現在起至少再活 1000 秒
        row = reg.update("codex:t1", ttl_extend_secs=1000)
        exp = reg.expires_ts(row)
        self.assertGreaterEqual(exp, time.time() + 999)
        self.assertIsNone(reg.update("codex:missing", purpose="x"))

    def test_archive_and_touch(self):
        reg = _fresh_registry()
        reg.register("codex:t1", provider="codex", purpose="p")
        row = reg.archive("codex:t1", "manual")
        self.assertEqual(row["state"], "archived")
        self.assertEqual(row["archive_reason"], "manual")
        # archived 不因 touch 復活
        reg.touch("codex:t1")
        self.assertEqual(reg.get("codex:t1")["state"], "archived")
        # touch 未登記 id 靜默無事
        reg.touch("codex:missing")

    def test_effective_state_idle(self):
        reg = _fresh_registry(idle_secs=10.0)
        row = reg.register("codex:t1", provider="codex", purpose="p")
        now = time.time()
        self.assertEqual(reg.effective_state(row, now), "active")
        self.assertEqual(reg.effective_state(row, now + 11), "idle")
        reg.touch("codex:t1")
        row = reg.get("codex:t1")
        self.assertEqual(reg.effective_state(row, time.time()), "active")


# ───────────────────────── 配額(藍圖 §3.3)─────────────────────────

class TestQuotas(unittest.TestCase):
    def test_max_children_per_parent(self):
        reg = _fresh_registry(max_children=3)
        reg.register("hermes:p", provider="hermes", cls="persistent")
        for i in range(3):
            reg.precheck("hermes:p", "task")
            reg.register(f"codex:c{i}", provider="codex", parent="hermes:p",
                         purpose="子")
        with self.assertRaises(agent_registry.QuotaExceeded) as cm:
            reg.precheck("hermes:p", "task")
        self.assertIn("上限 3", cm.exception.reason)
        # 歸檔一個 → 額度釋放
        reg.archive("codex:c0", "test")
        reg.precheck("hermes:p", "task")   # 不再丟

    def test_global_task_cap(self):
        reg = _fresh_registry(task_cap=2)
        reg.register("codex:a", provider="codex", purpose="1", cls="task")
        reg.register("codex:b", provider="codex", purpose="2", cls="task")
        with self.assertRaises(agent_registry.QuotaExceeded) as cm:
            reg.precheck(None, "task")
        self.assertIn("全域 task", cm.exception.reason)
        # ephemeral / persistent 不吃 task 額度
        reg.precheck(None, "ephemeral")
        reg.precheck(None, "persistent")

    def test_spawn_depth_limit(self):
        reg = _fresh_registry(max_depth=2)
        reg.register("hermes:p", provider="hermes", cls="persistent")   # depth 0
        reg.register("delegation:a", provider="codex", parent="hermes:p",
                     purpose="A")                                        # depth 1
        reg.register("delegation:b", provider="codex", parent="delegation:a",
                     purpose="B")                                        # depth 2
        self.assertEqual(reg.get("delegation:b")["depth"], 2)
        with self.assertRaises(agent_registry.QuotaExceeded) as cm:
            reg.precheck("delegation:b", "task")                         # depth 3 → 擋
        self.assertIn("深度", cm.exception.reason)

    def test_precheck_or_429_maps_to_http(self):
        reg = _fresh_registry(task_cap=0)
        with patch.object(bridge, "REGISTRY", reg):
            with self.assertRaises(bridge.HTTPException) as cm:
                bridge._registry_precheck_or_429(None, "task")
            self.assertEqual(cm.exception.status_code, 429)


# ───────────────────────── sweep 邏輯 ─────────────────────────

class TestSweepLogic(unittest.TestCase):
    def test_sweep_candidates_idle_and_ttl(self):
        reg = _fresh_registry(task_ttl=100.0, idle_secs=10.0)
        reg.register("codex:t", provider="codex", purpose="task 類", cls="task")
        reg.register("hermes:p", provider="hermes", cls="persistent")
        now = time.time()
        # 還 active → 不收
        self.assertEqual(reg.sweep_candidates(now), [])
        # idle 但 TTL 未到 → reaper 不收;🧹收工(不等 TTL)要收
        mid = now + 50
        self.assertEqual(reg.sweep_candidates(mid, require_expired=True), [])
        ids = [r["id"] for r in reg.sweep_candidates(mid, require_expired=False)]
        self.assertEqual(ids, ["codex:t"])
        # idle 且 TTL 到期 → reaper 收;persistent 永遠不在候選
        late = now + 200
        ids = [r["id"] for r in reg.sweep_candidates(late, require_expired=True)]
        self.assertEqual(ids, ["codex:t"])

    def test_done_rows_archive_without_waiting_ttl(self):
        reg = _fresh_registry(task_ttl=99999.0, idle_secs=10.0)
        reg.register("sub-x", provider="dispatch", purpose="完工的", cls="task")
        reg.mark_done("sub-x")
        ids = [r["id"] for r in reg.sweep_candidates(time.time() + 1,
                                                     require_expired=True)]
        self.assertEqual(ids, ["sub-x"])

    def test_sweep_never_includes_unregistered(self):
        reg = _fresh_registry(task_ttl=0.0, idle_secs=0.0)
        reg.register("codex:legacy", provider="codex", purpose="旁路補錄",
                     cls="task", registered=False)
        reg.register("codex:managed", provider="codex", purpose="正規",
                     cls="task")
        ids = [r["id"] for r in reg.sweep_candidates(time.time() + 60)]
        self.assertEqual(ids, ["codex:managed"])   # 未登記絕不進候選


# ───────────────────────── reaper(bridge 側)─────────────────────────

class TestReaper(unittest.IsolatedAsyncioTestCase):
    def _expired_registry(self):
        reg = _fresh_registry(task_ttl=0.0, ephemeral_ttl=0.0, idle_secs=0.0)
        reg.register("claude_code:job1", provider="claude_code", name="job1",
                     purpose="到期任務", cls="task")
        # 讓它看起來早就閒置+到期
        con = reg._connect()
        con.execute("UPDATE sessions SET last_active_ts=?, created_ts=? "
                    "WHERE id='claude_code:job1'",
                    (time.time() - 3600, time.time() - 3600))
        con.commit()
        con.close()
        return reg

    async def test_flag_off_archives_but_never_destroys(self):
        reg = self._expired_registry()
        run_ccsess = AsyncMock()
        cx_archive = AsyncMock()
        wt_remove = AsyncMock()
        with patch.object(bridge, "REGISTRY", reg), \
                patch.object(bridge, "_run_ccsess", run_ccsess), \
                patch.object(bridge, "_codex_thread_set_archived", cx_archive), \
                patch.object(bridge, "_worktree_try_remove", wt_remove), \
                patch.object(bridge, "_registry_is_busy",
                             AsyncMock(return_value=False)), \
                patch.dict(os.environ, {"REGISTRY_REAPER": "0"}):
            reaped = await bridge._registry_reap_once()
        self.assertEqual(reaped, ["claude_code:job1"])
        self.assertEqual(reg.get("claude_code:job1")["state"], "archived")
        run_ccsess.assert_not_awaited()     # 旗標關 = 零 destructive
        cx_archive.assert_not_awaited()
        wt_remove.assert_not_awaited()

    async def test_flag_on_tears_down_and_removes_recorded_worktree(self):
        reg = self._expired_registry()
        reg.set_worktree("claude_code:job1", "/tmp/wt-job1")
        run_ccsess = AsyncMock()
        wt_remove = AsyncMock(return_value=True)
        with patch.object(bridge, "REGISTRY", reg), \
                patch.object(bridge, "_run_ccsess", run_ccsess), \
                patch.object(bridge, "_worktree_try_remove", wt_remove), \
                patch.object(bridge, "_registry_is_busy",
                             AsyncMock(return_value=False)), \
                patch.dict(os.environ, {"REGISTRY_REAPER": "1"}):
            reaped = await bridge._registry_reap_once()
        self.assertEqual(reaped, ["claude_code:job1"])
        run_ccsess.assert_awaited_once_with("archive", "job1")
        # 只收 spawn 時登記過路徑的 worktree,路徑原樣傳遞、不用猜
        wt_remove.assert_awaited_once_with(
            "claude_code:job1", "/tmp/wt-job1", "registry_reap")

    async def test_reaper_skips_unregistered(self):
        reg = _fresh_registry(task_ttl=0.0, idle_secs=0.0)
        reg.register("claude_code:legacy", provider="claude_code",
                     purpose="旁路", cls="task", registered=False)
        con = reg._connect()
        con.execute("UPDATE sessions SET last_active_ts=?", (time.time() - 3600,))
        con.commit()
        con.close()
        run_ccsess = AsyncMock()
        with patch.object(bridge, "REGISTRY", reg), \
                patch.object(bridge, "_run_ccsess", run_ccsess), \
                patch.object(bridge, "_registry_is_busy",
                             AsyncMock(return_value=False)), \
                patch.dict(os.environ, {"REGISTRY_REAPER": "1"}):
            reaped = await bridge._registry_reap_once()
        self.assertEqual(reaped, [])
        self.assertNotEqual(reg.get("claude_code:legacy")["state"], "archived")
        run_ccsess.assert_not_awaited()

    async def test_active_subscribers_get_grace_cycle(self):
        reg = self._expired_registry()
        warn = MagicMock()
        with patch.object(bridge, "REGISTRY", reg), \
                patch.object(bridge, "_registry_is_busy",
                             AsyncMock(return_value=False)), \
                patch.object(bridge, "_registry_subscribers", return_value=2), \
                patch.object(bridge, "_registry_emit_reap_warning", warn), \
                patch.dict(os.environ, {"REGISTRY_REAPER": "1"}):
            reaped = await bridge._registry_reap_once()
        self.assertEqual(reaped, [])                      # 本輪寬限
        warn.assert_called_once()                         # ⏳ 即將回收卡
        self.assertNotEqual(reg.get("claude_code:job1")["state"], "archived")

    async def test_busy_session_touched_not_archived(self):
        reg = self._expired_registry()
        with patch.object(bridge, "REGISTRY", reg), \
                patch.object(bridge, "_registry_is_busy",
                             AsyncMock(return_value=True)):
            reaped = await bridge._registry_reap_once()
        self.assertEqual(reaped, [])
        row = reg.get("claude_code:job1")
        self.assertEqual(row["state"], "active")          # busy → 記活動
        self.assertGreater(row["last_active_ts"], time.time() - 5)


# ───────────────────────── 孤兒偵測 ─────────────────────────

class TestOrphanDetection(unittest.TestCase):
    def test_child_of_archived_parent_is_orphan(self):
        reg = _fresh_registry()
        reg.register("hermes:p", provider="hermes", cls="persistent")
        reg.register("delegation:a", provider="codex", parent="hermes:p",
                     purpose="父")
        reg.register("codex:kid", provider="codex", parent="delegation:a",
                     purpose="子")
        rows = reg.list_rows(include_archived=True)
        by_id = {r["id"]: r for r in rows}
        self.assertFalse(reg.is_orphan(by_id["codex:kid"], by_id))
        reg.archive("delegation:a", "test")               # 父先走
        rows = reg.list_rows(include_archived=True)
        by_id = {r["id"]: r for r in rows}
        self.assertTrue(reg.is_orphan(by_id["codex:kid"], by_id))
        # 無 parent 的、以及自己也 archived 的都不算孤兒
        self.assertFalse(reg.is_orphan(by_id["hermes:p"], by_id))
        reg.archive("codex:kid", "test")
        by_id = {r["id"]: r for r in reg.list_rows(include_archived=True)}
        self.assertFalse(reg.is_orphan(by_id["codex:kid"], by_id))


# ───────────────────────── API 端點 ─────────────────────────

class TestRegistryApi(unittest.IsolatedAsyncioTestCase):
    async def test_get_registry_view_children_expires_orphan_legacy(self):
        reg = _fresh_registry()
        reg.register("hermes:p", provider="hermes", cls="persistent",
                     purpose="常駐")
        reg.register("codex:kid", provider="codex", parent="hermes:p",
                     purpose="子任務", cls="task")

        async def _no_threads(_n=20):
            return []

        with patch.object(bridge, "REGISTRY", reg), \
                patch.object(bridge, "_check_auth", return_value=None), \
                patch.object(bridge, "_cc_conf_rows",
                             return_value=[("legacy-cc", "/tmp/x", "1")]), \
                patch.object(bridge, "_codex_v2_visible_threads", _no_threads), \
                patch.dict(bridge.SUBSESSIONS, {}, clear=True):
            res = await bridge.v2_registry_list(FakeRequest())
        by_id = {s["id"]: s for s in res["sessions"]}
        self.assertEqual(by_id["hermes:p"]["children"], ["codex:kid"])
        self.assertTrue(by_id["hermes:p"]["registered"])
        self.assertIsNone(by_id["hermes:p"]["expires_ts"])      # persistent
        self.assertIsNotNone(by_id["codex:kid"]["expires_ts"])  # task 有到期
        # 旁路 cc session:看得到、registered:false
        self.assertFalse(by_id["claude_code:legacy-cc"]["registered"])
        self.assertEqual(res["defaults"],
                         {"task_ttl": 100, "ephemeral_ttl": 50,
                          "max_children": 3})

    async def test_update_endpoint_and_404(self):
        reg = _fresh_registry()
        reg.register("codex:t", provider="codex", purpose="原", cls="task")
        with patch.object(bridge, "REGISTRY", reg), \
                patch.object(bridge, "_check_auth", return_value=None):
            res = await bridge.v2_registry_update(
                "codex:t", FakeRequest({"purpose": "改", "ttl_extend_secs": 500}))
            self.assertEqual(res["session"]["purpose"], "改")
            self.assertGreaterEqual(res["session"]["expires_ts"],
                                    time.time() + 499)
            with self.assertRaises(bridge.HTTPException) as cm:
                await bridge.v2_registry_update("codex:nope", FakeRequest({}))
            self.assertEqual(cm.exception.status_code, 404)
            with self.assertRaises(bridge.HTTPException) as cm:
                await bridge.v2_registry_update(
                    "codex:t", FakeRequest({"class": "weird"}))
            self.assertEqual(cm.exception.status_code, 400)

    async def test_archive_endpoint_flag_off_no_teardown(self):
        reg = _fresh_registry()
        reg.register("codex:t", provider="codex", purpose="p", cls="task")
        cx_archive = AsyncMock()
        with patch.object(bridge, "REGISTRY", reg), \
                patch.object(bridge, "_check_auth", return_value=None), \
                patch.object(bridge, "_codex_thread_set_archived", cx_archive), \
                patch.dict(os.environ, {"REGISTRY_REAPER": "0"}):
            res = await bridge.v2_registry_archive_one("codex:t", FakeRequest())
            self.assertTrue(res["archived"])
            self.assertFalse(res["teardown"])
            cx_archive.assert_not_awaited()
            self.assertEqual(reg.get("codex:t")["state"], "archived")
            with self.assertRaises(bridge.HTTPException) as cm:
                await bridge.v2_registry_archive_one("codex:nope", FakeRequest())
            self.assertEqual(cm.exception.status_code, 404)

    async def test_sweep_endpoint_archives_idle_without_waiting_ttl(self):
        reg = _fresh_registry(task_ttl=99999.0, idle_secs=0.0)
        reg.register("codex:idle", provider="codex", purpose="閒置", cls="task")
        reg.register("hermes:p", provider="hermes", cls="persistent")
        con = reg._connect()
        con.execute("UPDATE sessions SET last_active_ts=? WHERE id='codex:idle'",
                    (time.time() - 60,))
        con.commit()
        con.close()
        with patch.object(bridge, "REGISTRY", reg), \
                patch.object(bridge, "_check_auth", return_value=None), \
                patch.object(bridge, "_registry_is_busy",
                             AsyncMock(return_value=False)), \
                patch.dict(os.environ, {"REGISTRY_REAPER": "0"}):
            res = await bridge.v2_registry_sweep(FakeRequest())
        self.assertEqual(res, {"archived": ["codex:idle"]})
        self.assertEqual(reg.get("codex:idle")["state"], "archived")
        self.assertEqual(reg.get("hermes:p")["state"], "active")  # persistent 不收


if __name__ == "__main__":
    unittest.main()
