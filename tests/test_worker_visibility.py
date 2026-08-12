"""Worker 可見層(設計書 §2.4)——「session 裡面在跑什麼」。

涵蓋:report/upsert 語意、靜默期過期(+ 再回報復活)、ring 上限汰換、
旗標關 ⇒ 404 且零背景成本、per-session 隔離(A 看不到 B 的工人)、
CX child thread 投影(**外加主清單不變的回歸護欄**)、Hermes SUBSESSIONS 投影、
CC hook 腳本(fixture payload → 正確 body;bridge 連不上時安靜失敗;token 不外洩)。
"""
import asyncio
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="worker-visibility-test-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("POCKET_REGISTRY_DB", os.path.join(_TMP, "bridge-registry.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
import workers as workers_store  # noqa: E402

_HOOK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "scripts", "worker-report-hook.py")
_spec = importlib.util.spec_from_file_location("worker_report_hook", _HOOK_PATH)
hook_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook_mod)


class FakeURL:
    path = "/app/v2/workers"


class FakeRequest:
    def __init__(self, body=None, token="test-unit-token"):
        self._body = body or {}
        self.headers = {"authorization": f"Bearer {token}"}
        self.client = None
        self.url = FakeURL()

    async def json(self):
        return self._body


def _pre_hook(tool_use_id="toolu_01A", desc="審 PR #92 的 blocker",
              subagent_type="Explore", session_id="s-uuid-1", **extra):
    """實測自 claude 2.1.207 binary 的 PreToolUse payload 形狀。"""
    out = {
        "session_id": session_id,
        "transcript_path": "/Users/xcash/.claude/projects/x/y.jsonl",
        "cwd": "/Users/xcash/apps/hermes-openwebui-bridge",
        "prompt_id": "p-uuid",
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"description": desc, "subagent_type": subagent_type,
                       "prompt": "x" * 5000},
        "tool_use_id": tool_use_id,
    }
    out.update(extra)
    return out


# ───────────────────────── store 語意 ─────────────────────────
class TestWorkerStore(unittest.TestCase):
    def test_report_then_upsert_keeps_started_ts(self):
        st = workers_store.WorkerStore(ttl_secs=300, cap=50)
        st.report("claude_code:Main", "w1", label="查 bug", state="running", now=100.0)
        st.report("claude_code:Main", "w1", state="done", now=140.0)
        rows = st.list("claude_code:Main", now=150.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "done")
        self.assertEqual(rows[0]["started_ts"], 100.0)   # 開工時間是身分,不被洗掉
        self.assertEqual(rows[0]["updated_ts"], 140.0)
        self.assertEqual(rows[0]["label"], "查 bug")      # 第二次沒給 label → 保留

    def test_unknown_state_becomes_running_not_error(self):
        # 回報端是 hook,不能因為 state 打錯字就讓工人整個消失。
        st = workers_store.WorkerStore()
        w = st.report("s", "w1", state="weird-thing", now=1.0)
        self.assertEqual(w["state"], "running")
        self.assertEqual(workers_store.normalize_state("interrupted"), "failed")
        self.assertEqual(workers_store.normalize_state("succeeded"), "done")

    def test_silence_expiry_and_revival(self):
        st = workers_store.WorkerStore(ttl_secs=300, cap=50)
        st.report("s", "w1", label="慢工", now=1000.0)
        self.assertEqual(len(st.list("s", now=1299.0)), 1)
        # 300 秒沒回報 = 當它結束了(hook 沒跑到 done 也不會永遠掛著假的執行中)
        self.assertEqual(st.list("s", now=1301.0), [])
        # 再回報 = 復活,而且拿到新的 started_ts(中間那段它確實不在視圖裡)
        st.report("s", "w1", label="慢工", now=1400.0)
        rows = st.list("s", now=1401.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["started_ts"], 1400.0)

    def test_done_workers_also_age_out(self):
        st = workers_store.WorkerStore(ttl_secs=60)
        st.report("s", "w1", state="done", now=10.0)
        self.assertEqual(len(st.list("s", now=50.0)), 1)
        self.assertEqual(st.list("s", now=100.0), [])

    def test_ring_cap_evicts_oldest(self):
        st = workers_store.WorkerStore(ttl_secs=10_000, cap=3)
        for i in range(5):
            st.report("s", f"w{i}", now=100.0 + i)
        rows = st.list("s", now=110.0)
        self.assertEqual([r["worker_id"] for r in rows], ["w2", "w3", "w4"])

    def test_ring_cap_upsert_does_not_evict(self):
        st = workers_store.WorkerStore(ttl_secs=10_000, cap=3)
        for i in range(3):
            st.report("s", f"w{i}", now=100.0 + i)
        st.report("s", "w0", state="done", now=200.0)     # upsert,不是新增
        self.assertEqual(len(st.list("s", now=201.0)), 3)

    def test_per_session_isolation(self):
        st = workers_store.WorkerStore()
        st.report("claude_code:A", "w1", label="a 的工人", now=1.0)
        st.report("claude_code:B", "w2", label="b 的工人", now=1.0)
        self.assertEqual([r["worker_id"] for r in st.list("claude_code:A", now=2.0)],
                         ["w1"])
        self.assertEqual([r["worker_id"] for r in st.list("claude_code:B", now=2.0)],
                         ["w2"])
        self.assertEqual(st.list("claude_code:C", now=2.0), [])

    def test_meta_is_bounded_and_sanitized(self):
        st = workers_store.WorkerStore()
        w = st.report("s", "w1", meta={"ok": 1, "nested": {"no": "dicts"},
                                       "big": "x" * 5000}, now=1.0)
        self.assertEqual(w["meta"]["ok"], 1)
        self.assertNotIn("nested", w["meta"])          # 非純量值丟掉
        self.assertLessEqual(len(w["meta"]["big"]), 200)
        huge = st.report("s", "w2", meta={f"k{i}": "y" * 190 for i in range(50)},
                         now=1.0)
        self.assertEqual(huge["meta"], {})             # 整包太大 → 整包不收

    def test_counts_and_merge_prefers_reported(self):
        reported = [{"worker_id": "a", "state": "running", "started_ts": 5},
                    {"worker_id": "b", "state": "failed", "started_ts": 1}]
        projected = [{"worker_id": "a", "state": "done", "started_ts": 5},
                     {"worker_id": "c", "state": "done", "started_ts": 3}]
        merged = workers_store.merge(reported, projected)
        self.assertEqual([w["worker_id"] for w in merged], ["b", "c", "a"])
        self.assertEqual(next(w for w in merged if w["worker_id"] == "a")["state"],
                         "running")     # 撞號時第一手回報贏
        self.assertEqual(workers_store.counts_of(merged),
                         {"running": 1, "done": 1, "failed": 1})


# ───────────────────────── 端點 ─────────────────────────
class TestTtlEnvParsing(unittest.TestCase):
    """TTL 解析在 import 期跑,壞值**絕不能讓 production bridge 開不起來**。"""

    def test_bad_values_fall_back_to_default(self):
        for bad in ("5m", "300s", "", "abc", "0", "-1", "nan", "inf", "  "):
            with patch.dict(os.environ, {"WORKER_TTL_SECS": bad}):
                val = bridge._worker_ttl_from_env()
            self.assertEqual(val, 300.0, f"{bad!r} 應退回預設而不是炸掉")

    def test_good_values_respected_and_capped(self):
        with patch.dict(os.environ, {"WORKER_TTL_SECS": "60"}):
            self.assertEqual(bridge._worker_ttl_from_env(), 60.0)
        with patch.dict(os.environ, {"WORKER_TTL_SECS": "999999999"}):
            self.assertEqual(bridge._worker_ttl_from_env(), 86400.0)

    def test_nan_ttl_would_disable_expiry_so_it_is_rejected(self):
        # nan 特別毒:now - nan 恆為 nan,所有比較都 False → 永遠不過期,
        # 面板掛滿假的執行中工人,比直接炸掉還難查。
        st = workers_store.WorkerStore(ttl_secs=float("nan"))
        st.report("s", "w1", now=1.0)
        self.assertEqual(len(st.list("s", now=10 ** 9)), 1)   # 證明 nan 確實有毒
        with patch.dict(os.environ, {"WORKER_TTL_SECS": "nan"}):
            self.assertEqual(bridge._worker_ttl_from_env(), 300.0)


class TestWorkerEndpoints(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bridge.WORKERS.clear()

    async def test_flag_off_is_404_and_zero_background_cost(self):
        with patch.object(bridge, "WORKERS_ENABLED", False):
            with self.assertRaises(bridge.HTTPException) as cm:
                await bridge.v2_workers_list(FakeRequest(), session="claude_code:Main")
            self.assertEqual(cm.exception.status_code, 404)
            with self.assertRaises(bridge.HTTPException) as cm2:
                await bridge.v2_workers_report(
                    FakeRequest({"session": "s", "worker_id": "w"}))
            self.assertEqual(cm2.exception.status_code, 404)
        # 零背景成本:這一層沒有註冊任何計時器/背景任務,過期純惰性計算。
        # 旗標關掉時被拒的回報不該留下任何狀態。
        self.assertEqual(bridge.WORKERS.sessions(), [])

    async def test_report_then_list_roundtrip(self):
        with patch.object(bridge, "WORKERS_ENABLED", True):
            res = await bridge.v2_workers_report(FakeRequest({
                "session": "claude_code:Main", "worker_id": "toolu_1",
                "label": "審 PR #92 的 blocker", "state": "running"}))
            self.assertTrue(res["ok"])
            out = await bridge.v2_workers_list(FakeRequest(),
                                               session="claude_code:Main")
            self.assertEqual(out["counts"], {"running": 1, "done": 0, "failed": 0})
            self.assertEqual(out["workers"][0]["label"], "審 PR #92 的 blocker")
            self.assertEqual(out["session"], "claude_code:Main")

    async def test_report_requires_worker_id_and_session(self):
        with patch.object(bridge, "WORKERS_ENABLED", True):
            with self.assertRaises(bridge.HTTPException) as cm:
                await bridge.v2_workers_report(FakeRequest({"session": "s"}))
            self.assertEqual(cm.exception.status_code, 400)
            with self.assertRaises(bridge.HTTPException) as cm2:
                await bridge.v2_workers_report(FakeRequest({"worker_id": "w"}))
            self.assertEqual(cm2.exception.status_code, 400)
            with self.assertRaises(bridge.HTTPException) as cm3:
                await bridge.v2_workers_list(FakeRequest(), session="")
            self.assertEqual(cm3.exception.status_code, 400)

    async def test_bad_token_rejected(self):
        with patch.object(bridge, "WORKERS_ENABLED", True):
            with self.assertRaises(bridge.HTTPException):
                await bridge.v2_workers_list(FakeRequest(token="nope"),
                                             session="claude_code:Main")

    async def test_endpoint_scoping_a_cannot_see_b(self):
        with patch.object(bridge, "WORKERS_ENABLED", True):
            await bridge.v2_workers_report(FakeRequest({
                "session": "claude_code:A", "worker_id": "wa", "label": "a"}))
            await bridge.v2_workers_report(FakeRequest({
                "session": "claude_code:B", "worker_id": "wb", "label": "b"}))
            a = await bridge.v2_workers_list(FakeRequest(), session="claude_code:A")
            self.assertEqual([w["worker_id"] for w in a["workers"]], ["wa"])

    async def test_cc_session_id_resolves_via_existing_sid_map(self):
        # bridge 早就有 name↔sid 對應(既有 /ccsessions/_hook 建的),
        # hook 腳本不必猜 tmux 名字。
        with patch.object(bridge, "WORKERS_ENABLED", True), \
             patch.object(bridge, "_cc_name_for_sid", lambda sid: "Main"):
            res = await bridge.v2_workers_report(FakeRequest({
                "cc_session_id": "s-uuid-1", "worker_id": "toolu_1",
                "label": "查 bug"}))
            self.assertEqual(res["session"], "claude_code:Main")

    async def test_unresolvable_cc_sid_falls_back_not_dropped(self):
        with patch.object(bridge, "WORKERS_ENABLED", True), \
             patch.object(bridge, "_cc_name_for_sid", lambda sid: None):
            res = await bridge.v2_workers_report(FakeRequest({
                "cc_session_id": "s-uuid-9", "worker_id": "toolu_1"}))
            self.assertEqual(res["session"], "cc-sid:s-uuid-9")


# ───────────────────────── provider 投影 ─────────────────────────
class FakeCodexApp:
    """pages=None → 單頁;pages=[[…],[…]] → 分頁(用 nextCursor 串)。"""

    def __init__(self, threads=None, active=(), pages=None):
        self.pages = pages if pages is not None else [list(threads or [])]
        self.active = set(active)
        self.calls = []

    async def call(self, method, params, timeout=None):
        self.calls.append((method, dict(params)))
        idx = 0
        if params.get("cursor"):
            idx = int(params["cursor"])
        out = {"data": list(self.pages[idx])}
        if idx + 1 < len(self.pages):
            out["nextCursor"] = str(idx + 1)
        return out

    def is_active(self, tid):
        return tid in self.active


_PARENT = {"id": "t-parent", "cwd": "/Users/xcash/apps/x", "source": "vscode",
           "name": "operator lane", "updatedAt": 1000, "createdAt": 900}
_CHILD = {"id": "t-child", "cwd": "/Users/xcash/apps/x",
          "source": {"subagent": {"other": "guardian"}}, "thread_source": "subagent",
          "name": "guardian sweep", "updatedAt": 1000, "createdAt": 990}
_OTHER_CWD_CHILD = {"id": "t-elsewhere", "cwd": "/Users/xcash/apps/other",
                    "source": {"subagent": {"other": "guardian"}},
                    "name": "someone else's", "updatedAt": 1000, "createdAt": 990}


class TestCodexProjection(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bridge.WORKERS.clear()
        bridge._CODEX_WORKER_THREADS_CACHE = None   # 每個 case 都從冷快取開始

    async def test_paginates_to_find_parent_behind_guardian_burst(self):
        """回歸:codex 會把一大批 guardian thread 排在清單最前面(實測 390 筆)。

        只撈第一頁的話,操作者自己的 parent thread 被擠到第二頁 →
        `parent is None` → 面板回空清單,而且無聲無息。這正是
        `_codex_v2_visible_threads` 當初寫分頁要解的坑。
        """
        burst = [{**_CHILD, "id": f"g-{i}"} for i in range(100)]
        fake = FakeCodexApp(pages=[burst, [_PARENT, _CHILD]], active={"t-child"})
        with patch.object(bridge, "WORKERS_ENABLED", True), \
             patch.object(bridge, "CODEX_APP", fake), \
             patch("time.time", lambda: 1000.0):
            out = await bridge.v2_workers_list(FakeRequest(), session="codex:t-parent")
        self.assertIn("codex:t-child", [w["worker_id"] for w in out["workers"]])
        self.assertEqual(len(fake.calls), 2)     # 真的翻了第二頁

    async def test_unreadable_timestamp_does_not_fabricate_now(self):
        """時間戳讀不出來時**絕不能墊 now** —— 那會讓靜默期永遠濾不掉它,
        同 cwd 的幾百筆 guardian thread 整批變成假工人灌進面板。"""
        junk = [{**_CHILD, "id": f"j-{i}", "updatedAt": {"weird": "shape"},
                 "createdAt": None} for i in range(50)]
        live = {**_CHILD, "id": "j-live", "updatedAt": {"weird": "shape"}}
        fake = FakeCodexApp([_PARENT, live] + junk, active={"j-live"})
        with patch.object(bridge, "WORKERS_ENABLED", True), \
             patch.object(bridge, "CODEX_APP", fake), \
             patch("time.time", lambda: 1000.0):
            out = await bridge.v2_workers_list(FakeRequest(), session="codex:t-parent")
        # 認不出時間就只信 is_active:真的在跑的那筆留下,其餘 50 筆不進面板
        self.assertEqual([w["worker_id"] for w in out["workers"]], ["codex:j-live"])

    async def test_naive_iso_timestamp_treated_as_utc(self):
        # 沒有時區的 ISO 若用本地時區解讀,UTC+8 的機器會把「一秒前」讀成
        # 「八小時前」,正在跑的工人直接被靜默期濾掉。
        import datetime as _dt
        now = 1786528221.0
        iso = _dt.datetime.fromtimestamp(now, _dt.timezone.utc).replace(
            tzinfo=None).isoformat()
        self.assertAlmostEqual(bridge._codex_ts(iso), now, delta=1.0)
        self.assertAlmostEqual(bridge._codex_ts(now * 1000), now, delta=1.0)
        self.assertEqual(bridge._codex_ts({"bad": 1}), 0.0)
        self.assertEqual(bridge._codex_ts(None), 0.0)

    async def test_thread_list_is_cached_across_polls(self):
        # app 會持續 poll 這個端點;每次都撈 100 列 thread 是浪費。
        fake = FakeCodexApp([_PARENT, _CHILD], active={"t-child"})
        with patch.object(bridge, "WORKERS_ENABLED", True), \
             patch.object(bridge, "CODEX_APP", fake), \
             patch("time.time", lambda: 1000.0):
            for _ in range(5):
                await bridge.v2_workers_list(FakeRequest(), session="codex:t-parent")
        self.assertEqual(len(fake.calls), 1)

    async def test_delegation_session_id_resolves_to_its_thread(self):
        # delegation:<id> 也是 app 拿得到的正牌 v2 session id。
        fake = FakeCodexApp([_PARENT, _CHILD], active={"t-child"})
        with patch.object(bridge, "WORKERS_ENABLED", True), \
             patch.object(bridge, "CODEX_APP", fake), \
             patch.object(bridge, "_codex_thread_from_v2_session_id",
                          lambda s: "t-parent"), \
             patch("time.time", lambda: 1000.0):
            out = await bridge.v2_workers_list(FakeRequest(), session="delegation:d1")
        self.assertEqual([w["worker_id"] for w in out["workers"]], ["codex:t-child"])

    async def test_delegation_that_is_not_codex_is_empty_not_500(self):
        def boom(s):
            raise bridge.HTTPException(status_code=400, detail="not codex")

        with patch.object(bridge, "WORKERS_ENABLED", True), \
             patch.object(bridge, "_codex_thread_from_v2_session_id", boom):
            out = await bridge.v2_workers_list(FakeRequest(), session="delegation:d9")
        self.assertEqual(out["workers"], [])

    async def test_child_threads_project_onto_parent(self):
        fake = FakeCodexApp([_PARENT, _CHILD, _OTHER_CWD_CHILD], active={"t-child"})
        with patch.object(bridge, "WORKERS_ENABLED", True), \
             patch.object(bridge, "CODEX_APP", fake), \
             patch("time.time", lambda: 1000.0):
            out = await bridge.v2_workers_list(FakeRequest(), session="codex:t-parent")
        ids = [w["worker_id"] for w in out["workers"]]
        self.assertEqual(ids, ["codex:t-child"])       # 別的 cwd 的孩子不算我的
        self.assertEqual(out["workers"][0]["state"], "running")
        self.assertEqual(out["workers"][0]["meta"]["link"], "cwd")
        self.assertEqual(out["counts"]["running"], 1)
        # 投影必須明示要 children,否則 provider 根本不會回這些列
        self.assertTrue(fake.calls[0][1]["includeChildren"])

    async def test_explicit_parent_field_wins_over_cwd_heuristic(self):
        # codex 有 thread_spawn_edges 但這台機器是空的;哪天 provider 開始在
        # thread 記錄上給 parentThreadId,就該立刻改用它、不再靠 cwd 猜。
        mine = {**_CHILD, "id": "t-mine", "parentThreadId": "t-parent"}
        theirs = {**_CHILD, "id": "t-theirs", "parentThreadId": "t-someone-else"}
        fake = FakeCodexApp([_PARENT, mine, theirs])
        with patch.object(bridge, "WORKERS_ENABLED", True), \
             patch.object(bridge, "CODEX_APP", fake), \
             patch("time.time", lambda: 1000.0):
            out = await bridge.v2_workers_list(FakeRequest(), session="codex:t-parent")
        self.assertEqual([w["worker_id"] for w in out["workers"]], ["codex:t-mine"])
        self.assertEqual(out["workers"][0]["meta"]["link"], "provider")

    async def test_codex_down_degrades_to_empty_not_500(self):
        class Boom:
            async def call(self, *a, **k):
                raise RuntimeError("app-server unavailable")

            def is_active(self, tid):
                return False

        with patch.object(bridge, "WORKERS_ENABLED", True), \
             patch.object(bridge, "CODEX_APP", Boom()):
            out = await bridge.v2_workers_list(FakeRequest(), session="codex:t-parent")
        self.assertEqual(out["workers"], [])

    async def test_main_session_list_filter_is_unchanged(self):
        """回歸護欄:worker 層絕不能把 child thread 漏進操作者主清單。

        `_codex_is_child_thread` 那個過濾當初是對的(390 筆 guardian 會把
        XCash lane 擠出畫面),這裡釘死它的行為沒被這次改動動到。
        """
        self.assertTrue(bridge._codex_is_child_thread(_CHILD))
        self.assertFalse(bridge._codex_is_child_thread(_PARENT))
        self.assertTrue(bridge._codex_is_child_thread({"cwd": "/private/tmp/foo"}))
        fake = FakeCodexApp([_PARENT, _CHILD, _OTHER_CWD_CHILD])
        with patch.object(bridge, "CODEX_APP", fake):
            visible = await bridge._codex_v2_visible_threads(20)
        self.assertEqual([t["id"] for t in visible], ["t-parent"])
        self.assertFalse(fake.calls[0][1].get("includeChildren", False))


class TestHermesProjection(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bridge.WORKERS.clear()

    async def test_subsessions_project_onto_persona(self):
        subs = {
            "sub-a": {"name": "翻譯 FLiPER 稿", "parent": "yuanfang",
                      "tool": "claude", "status": "running", "lastAt": 900.0},
            "sub-b": {"name": "剛做完的", "parent": "yuanfang",
                      "tool": "codex", "status": "done", "lastAt": 990.0},
            "sub-old": {"name": "上禮拜的", "parent": "yuanfang",
                        "tool": "claude", "status": "done", "lastAt": 10.0},
            "sub-other": {"name": "別人的", "parent": "shuijing",
                          "tool": "claude", "status": "running", "lastAt": 990.0},
        }
        with patch.object(bridge, "WORKERS_ENABLED", True), \
             patch.dict(bridge.SUBSESSIONS, subs, clear=True), \
             patch("time.time", lambda: 1000.0):
            out = await bridge.v2_workers_list(FakeRequest(), session="hermes:yuanfang")
        ids = [w["worker_id"] for w in out["workers"]]
        self.assertEqual(sorted(ids), ["sub:sub-a", "sub:sub-b"])   # 陳年的不灌進來
        self.assertEqual(out["counts"], {"running": 1, "done": 1, "failed": 0})
        labels = {w["worker_id"]: w["label"] for w in out["workers"]}
        self.assertEqual(labels["sub:sub-a"], "翻譯 FLiPER 稿")

    async def test_unknown_status_or_missing_lastAt_does_not_stick_forever(self):
        """回歸:SUBSESSIONS 是**永久記錄**,一筆壞掉的列不能永遠假裝在跑。

        `normalize_state` 對認不得的狀態回 running(對 hook 是對的);但若拿它
        來豁免靜默期,一筆 status=NULL / lastAt=NULL 的陳年 sub 就會永遠掛在
        面板上,而且重啟還會從 canonical.db 讀回來,沒有任何東西收得掉它。
        """
        subs = {
            "sub-nullstatus": {"name": "壞列", "parent": "yuanfang",
                               "tool": "claude", "status": None, "lastAt": 10.0},
            "sub-nolastat": {"name": "沒時間戳", "parent": "yuanfang",
                             "tool": "claude", "status": "done", "lastAt": None},
            "sub-badlastat": {"name": "時間戳是垃圾", "parent": "yuanfang",
                              "tool": "claude", "status": "done", "lastAt": "x"},
            "sub-live": {"name": "真的在跑", "parent": "yuanfang",
                         "tool": "claude", "status": "running", "lastAt": None},
        }
        with patch.object(bridge, "WORKERS_ENABLED", True), \
             patch.dict(bridge.SUBSESSIONS, subs, clear=True), \
             patch("time.time", lambda: 100000.0):
            out = await bridge.v2_workers_list(FakeRequest(), session="hermes:yuanfang")
        # 只有明寫 running 的豁免靜默期;其餘三筆一律被時間濾掉
        self.assertEqual([w["worker_id"] for w in out["workers"]], ["sub:sub-live"])

    async def test_interrupted_subsession_reads_as_failed(self):
        subs = {"sub-x": {"name": "被 bridge 重啟砍掉的", "parent": "yuanfang",
                          "tool": "claude", "status": "interrupted", "lastAt": 990.0}}
        with patch.object(bridge, "WORKERS_ENABLED", True), \
             patch.dict(bridge.SUBSESSIONS, subs, clear=True), \
             patch("time.time", lambda: 1000.0):
            out = await bridge.v2_workers_list(FakeRequest(), session="hermes:yuanfang")
        self.assertEqual(out["workers"][0]["state"], "failed")

    async def test_projection_does_not_mutate_subsessions(self):
        """唯讀投影:SUBSESSIONS 自己的生命週期不歸這一層管。"""
        subs = {"sub-a": {"name": "n", "parent": "yuanfang", "tool": "claude",
                          "status": "running", "lastAt": 990.0}}
        snapshot = json.dumps(subs, sort_keys=True)
        with patch.object(bridge, "WORKERS_ENABLED", True), \
             patch.dict(bridge.SUBSESSIONS, subs, clear=True), \
             patch("time.time", lambda: 1000.0):
            await bridge.v2_workers_list(FakeRequest(), session="hermes:yuanfang")
            self.assertEqual(json.dumps(dict(bridge.SUBSESSIONS), sort_keys=True),
                             snapshot)


# ───────────────────────── CC hook 腳本 ─────────────────────────
class TestHookScript(unittest.TestCase):
    def test_pre_tool_use_builds_running_payload(self):
        body = hook_mod.build_payload(_pre_hook())
        self.assertEqual(body["worker_id"], "toolu_01A")
        self.assertEqual(body["state"], "running")
        self.assertEqual(body["cc_session_id"], "s-uuid-1")
        self.assertIn("審 PR #92 的 blocker", body["label"])
        self.assertEqual(body["meta"]["subagent_type"], "Explore")
        # 5000 字的 prompt 絕不能被帶進 bridge
        self.assertNotIn("xxxxx", json.dumps(body))
        self.assertLessEqual(len(body["label"]), 200)

    def test_post_tool_use_is_done_failure_event_is_failed(self):
        done = hook_mod.build_payload(_pre_hook(
            hook_event_name="PostToolUse", tool_response={"content": "ok"},
            duration_ms=4210))
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["meta"]["duration_ms"], 4210)
        # 失敗不是靠解析 tool_response,是另一個事件(2.1.207 binary 實測)
        failed = hook_mod.build_payload(_pre_hook(
            hook_event_name="PostToolUseFailure", error="boom", is_interrupt=True))
        self.assertEqual(failed["state"], "failed")
        self.assertTrue(failed["meta"]["interrupted"])

    def test_pre_and_post_share_worker_id(self):
        pre = hook_mod.build_payload(_pre_hook(tool_use_id="toolu_XYZ"))
        post = hook_mod.build_payload(_pre_hook(tool_use_id="toolu_XYZ",
                                                hook_event_name="PostToolUse"))
        self.assertEqual(pre["worker_id"], post["worker_id"])

    def test_legacy_task_tool_name_still_accepted(self):
        # 2.1.207 叫 Agent,舊版叫 Task —— 兩個都要收。
        body = hook_mod.build_payload(_pre_hook(tool_name="Task"))
        self.assertIsNotNone(body)

    def test_non_agent_tools_are_ignored(self):
        for name in ("Bash", "Read", "Edit"):
            self.assertIsNone(hook_mod.build_payload(_pre_hook(tool_name=name)))

    def test_garbage_input_is_ignored(self):
        for bad in (None, [], "str", {}, {"tool_name": "Agent"},
                    _pre_hook(hook_event_name="Stop")):
            self.assertIsNone(hook_mod.build_payload(bad))

    def test_nested_agent_id_goes_to_meta_not_parent_worker(self):
        # agent_id 與 worker_id(= tool_use_id)不同號碼空間,接不起來。
        # 塞進 parent_worker 會讓 app 去畫一棵指向不存在節點的樹。
        body = hook_mod.build_payload(_pre_hook(agent_id="agent_abc",
                                                agent_type="Explore"))
        self.assertNotIn("parent_worker", body)
        self.assertEqual(body["meta"]["agent_id"], "agent_abc")
        self.assertEqual(body["meta"]["running_inside"], "Explore")
        self.assertNotIn("agent_id", hook_mod.build_payload(_pre_hook())["meta"])

    def test_obsolete_studio_token_path_not_consulted(self):
        # repo CLAUDE.md:「Token 在 plist 的 BRIDGE_TOKEN(不是
        # ~/.config/studio/token,已過時)」。
        self.assertNotIn("~/.config/studio/token", hook_mod.TOKEN_FILES)

    def test_explicit_session_env_overrides(self):
        with patch.dict(os.environ, {"POCKET_WORKER_SESSION": "claude_code:Main"}):
            body = hook_mod.build_payload(_pre_hook())
        self.assertEqual(body["session"], "claude_code:Main")

    def test_fails_silently_when_bridge_unreachable(self):
        # 連不上 bridge 絕不能拋、絕不能拖 —— 這支腳本掛在 PreToolUse 上,
        # 它一噴錯就是善彰的 agent 派不了工。
        with patch.dict(os.environ, {"POCKET_BRIDGE_URL": "http://127.0.0.1:1",
                                     "WORKER_HOOK_TIMEOUT": "0.3"}):
            hook_mod.post({"worker_id": "w", "session": "s"})   # 不該拋

    def test_main_always_exits_zero_and_prints_nothing(self):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"POCKET_BRIDGE_URL": "http://127.0.0.1:1",
                                     "WORKER_HOOK_TIMEOUT": "0.3"}), \
             patch.object(sys, "stdin", io.StringIO(json.dumps(_pre_hook()))), \
             patch.object(sys, "stdout", buf_out), \
             patch.object(sys, "stderr", buf_err):
            rc = hook_mod.main()
        self.assertEqual(rc, 0)
        self.assertEqual(buf_out.getvalue(), "")    # PreToolUse 的 stdout 會被當控制 JSON
        self.assertEqual(buf_err.getvalue(), "")
        # 連 stdin 是垃圾都不能炸
        with patch.object(sys, "stdin", io.StringIO("not json at all")):
            self.assertEqual(hook_mod.main(), 0)

    def test_token_never_appears_in_output_or_payload(self):
        secret = "SUPER-SECRET-TOKEN-do-not-leak"
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"BRIDGE_TOKEN": secret,
                                     "POCKET_BRIDGE_URL": "http://127.0.0.1:1",
                                     "WORKER_HOOK_TIMEOUT": "0.3"}), \
             patch.object(sys, "stdin", io.StringIO(json.dumps(_pre_hook()))), \
             patch.object(sys, "stdout", buf_out), \
             patch.object(sys, "stderr", buf_err):
            self.assertEqual(hook_mod.main(), 0)
        self.assertNotIn(secret, buf_out.getvalue() + buf_err.getvalue())
        # body 裡也不能有 token(它只該進 Authorization header)
        self.assertNotIn(secret, json.dumps(hook_mod.build_payload(_pre_hook())))
        # 原始碼裡不能有硬寫死的 token
        with open(_HOOK_PATH, "r", encoding="utf-8") as fh:
            self.assertNotIn(secret, fh.read())

    def test_token_goes_in_header_only(self):
        secret = "hdr-token-123"
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.headers)
            captured["body"] = req.data.decode("utf-8")
            raise OSError("no server")   # 照樣要被吞掉

        with patch.dict(os.environ, {"BRIDGE_TOKEN": secret}), \
             patch("urllib.request.urlopen", fake_urlopen):
            hook_mod.post({"worker_id": "w", "session": "s"})
        self.assertEqual(captured["headers"].get("Authorization"), f"Bearer {secret}")
        self.assertNotIn(secret, captured["body"])

    def test_token_read_from_file_when_env_absent(self):
        # hook 子行程有沒有繼承使用者 shell 的自訂 env 未經實測驗證,
        # 所以檔案這條路必須真的能用。
        path = os.path.join(_TMP, "worker-hook.token")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("file-token-xyz\n")
        env = {k: v for k, v in os.environ.items()
               if k not in ("POCKET_WORKER_TOKEN", "BRIDGE_TOKEN")}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(hook_mod, "TOKEN_FILES", (path,)):
            self.assertEqual(hook_mod._token(), "file-token-xyz")


class TestHookScriptE2E(unittest.TestCase):
    """真的用子行程餵 fixture 進去跑一遍(驗 shebang/stdin 契約,不只純函式)。"""

    def test_subprocess_run_exits_zero_silently(self):
        import subprocess
        env = dict(os.environ, POCKET_BRIDGE_URL="http://127.0.0.1:1",
                   WORKER_HOOK_TIMEOUT="0.3")
        proc = subprocess.run([sys.executable, _HOOK_PATH],
                              input=json.dumps(_pre_hook()), env=env,
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "")


if __name__ == "__main__":
    unittest.main()
