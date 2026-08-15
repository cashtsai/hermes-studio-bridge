"""任務看板聚合器(/app/v2/taskboard,spec §6-1):四欄判定、單源失敗降級、空源。

看板把五個唯讀源(hermes kanban.db、delegations、agent_calls、CC live、CX live
+ approvals 超時)聚成 queued/running/stuck/done。產品鐵律:「stuck」欄是靈魂
—— 認領逾期、連續失敗、委派 failed、CX 鎖死、待核准超過 10 分鐘都要浮上來;
而且**單一源壞掉只能降級為缺席(sources_ok=false),絕不拖垮整個端點**。
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="taskboard-canon-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("POCKET_REGISTRY_DB", os.path.join(_TMP, "agent-registry.db"))
os.environ.setdefault("POCKET_KANBAN_DB", os.path.join(_TMP, "kanban.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

# 安全閂:這個檔的 fixture 會 DELETE/INSERT canonical、registry 表。如果
# bridge 被別的測試模組**先** import(setdefault 來不及生效),CANON_DB /
# REGISTRY 會指向 production 路徑 —— 那時寧可整檔炸掉,也不准碰真 DB。
# (2026-08-16 實錄:跨模組合跑一次就把 production approvals/agent_calls
# 清掉了。這不是假想敵。)
assert bridge.CANON_DB == os.environ["POCKET_CANON_DB"], \
    "bridge 已被先 import,CANON_DB 指向非測試路徑;請單獨跑本測試檔"
assert bridge.REGISTRY.db_path == os.environ["POCKET_REGISTRY_DB"], \
    "bridge 已被先 import,REGISTRY 指向非測試路徑;請單獨跑本測試檔"
assert bridge.KANBAN_DB == os.environ["POCKET_KANBAN_DB"], \
    "bridge 已被先 import,KANBAN_DB 指向非測試路徑;請單獨跑本測試檔"

KANBAN_DB = os.environ["POCKET_KANBAN_DB"]
STATEDB_FIXTURE = os.path.join(_TMP, "hermes-state.db")
NOW = time.time()


def _init_statedb(rows=()):
    """hermes state.db 的 async_delegations fixture(修 ①:委派持久兜底)。
    只建看板 SELECT 會摸到的欄位。每次重建;預設空表=持久層活著但沒委派。"""
    if os.path.exists(STATEDB_FIXTURE):
        os.remove(STATEDB_FIXTURE)
    con = sqlite3.connect(STATEDB_FIXTURE)
    con.execute("""CREATE TABLE async_delegations(
        delegation_id TEXT PRIMARY KEY, origin_session TEXT, state TEXT,
        dispatched_at REAL, completed_at REAL, updated_at REAL,
        result_json TEXT, task_json TEXT)""")
    for r in rows:
        cols = ", ".join(r.keys())
        marks = ", ".join("?" for _ in r)
        con.execute(f"INSERT INTO async_delegations({cols}) VALUES({marks})",
                    list(r.values()))
    con.commit()
    con.close()


_init_statedb()   # 預設 fixture:所有既有測試都在「持久層健康且空」下跑


def _init_kanban(rows=()):
    """最小 kanban.db fixture:只建看板 SELECT 會摸到的欄位。每次重建。"""
    if os.path.exists(KANBAN_DB):
        os.remove(KANBAN_DB)
    con = sqlite3.connect(KANBAN_DB)
    con.execute("""CREATE TABLE tasks(
        id INTEGER PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
        priority INTEGER, claim_expires INTEGER, consecutive_failures INTEGER,
        worker_pid INTEGER, created_at INTEGER, started_at INTEGER,
        completed_at INTEGER, last_heartbeat_at INTEGER,
        last_failure_error TEXT, result TEXT)""")
    for r in rows:
        cols = ", ".join(r.keys())
        marks = ", ".join("?" for _ in r)
        con.execute(f"INSERT INTO tasks({cols}) VALUES({marks})",
                    list(r.values()))
    con.commit()
    con.close()


def _clear_canon():
    con = sqlite3.connect(bridge.CANON_DB)
    con.execute("DELETE FROM delegations")
    con.execute("DELETE FROM approvals")
    con.commit()
    con.close()


def _insert_delegation(did, status, provider="codex", title="", updated=None,
                       created=None, last_error="", tid="", cc_name=""):
    con = sqlite3.connect(bridge.CANON_DB)
    con.execute(
        "INSERT INTO delegations(id, work_order, parent_persona, provider,"
        " title, status, codex_thread_id, cc_session_name, created_at,"
        " updated_at, last_error, meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (did, f"WO-{did}", "yuanfang", provider, title or did, status,
         tid, cc_name, created or NOW - 3600, updated or NOW - 60,
         last_error, "{}"))
    con.commit()
    con.close()


def _insert_approval(aid, provider, session_id, created_at, status="pending",
                     title="要不要跑 rm -rf?"):
    con = sqlite3.connect(bridge.CANON_DB)
    con.execute(
        "INSERT INTO approvals(id, title, source, risk, detail, created_at,"
        " expires_at, status, session_id, provider)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (aid, title, session_id, "high", "detail", created_at,
         created_at + 86400, status, session_id, provider))
    con.commit()
    con.close()


def _clear_registry():
    con = sqlite3.connect(bridge.REGISTRY.db_path)
    con.execute("DELETE FROM agent_calls")
    con.commit()
    con.close()


async def _no_cx(_wanted=20):
    return []


async def _cc_idle(_name):
    return {"busy": False, "running": True, "mode": "normal", "prompt": None}


class _Req:
    headers = {"authorization": "Bearer test-unit-token"}


def _base_patches(**over):
    """預設把 live 源(cc/cx)靜音,單測各源不互相干擾。"""
    return [
        patch.object(bridge, "_check_auth", return_value=None),
        patch.object(bridge, "STATE_DB",
                     over.get("state_db", STATEDB_FIXTURE)),
        patch.object(bridge, "_cc_conf_rows",
                     return_value=over.get("cc_rows", [])),
        patch.object(bridge, "_cc_status_core",
                     over.get("cc_status", _cc_idle)),
        patch.object(bridge, "_codex_v2_visible_threads",
                     over.get("cx_threads", _no_cx)),
    ]


async def _board(**over):
    patches = _base_patches(**over)
    for p in patches:
        p.start()
    try:
        return await bridge.v2_taskboard(_Req())
    finally:
        for p in patches:
            p.stop()


def _col_ids(res, col):
    return [c["id"] for c in res["columns"][col]]


class TestEmptySources(unittest.IsolatedAsyncioTestCase):
    async def test_all_empty_sources_alive_columns_empty(self):
        _init_kanban()
        _clear_canon()
        _clear_registry()
        res = await _board()
        self.assertEqual(res["counts"],
                         {"queued": 0, "running": 0, "stuck": 0, "done": 0})
        for key in ("kanban", "delegations", "registry", "approvals", "cc", "cx"):
            self.assertTrue(res["sources_ok"][key], f"{key} 應標為活著")
        # 卡片欄位契約:columns 四鍵恆在。
        self.assertEqual(set(res["columns"]), {"queued", "running", "stuck", "done"})


class TestKanbanColumns(unittest.IsolatedAsyncioTestCase):
    """kanban 四欄判定:queued/未認領、claimed 心跳未過期、逾期/連續失敗、近 24h 完成。"""

    async def test_kanban_four_column_mapping(self):
        _init_kanban([
            {"id": 1, "title": "排隊的", "status": "todo", "priority": 2,
             "created_at": int(NOW - 100)},
            {"id": 2, "title": "跑著的", "status": "running", "assignee": "worker-a",
             "claim_expires": int(NOW + 300), "started_at": int(NOW - 50)},
            {"id": 3, "title": "心跳斷的", "status": "running",
             "claim_expires": int(NOW - 120)},
            {"id": 4, "title": "連續失敗的", "status": "ready",
             "consecutive_failures": 3, "last_failure_error": "boom",
             "created_at": int(NOW - 500)},
            {"id": 5, "title": "剛完成的", "status": "done",
             "completed_at": int(NOW - 100)},
            {"id": 6, "title": "上週完成的", "status": "done",
             "completed_at": int(NOW - 86400 * 3)},
            {"id": 7, "title": "封存的", "status": "archived"},
            {"id": 8, "title": "卡關的", "status": "blocked",
             "created_at": int(NOW - 200)},
        ])
        _clear_canon()
        _clear_registry()
        res = await _board()
        self.assertEqual(_col_ids(res, "queued"), ["kanban:1"])
        self.assertEqual(_col_ids(res, "running"), ["kanban:2"])
        self.assertEqual(set(_col_ids(res, "stuck")),
                         {"kanban:3", "kanban:4", "kanban:8"})
        self.assertEqual(_col_ids(res, "done"), ["kanban:5"])   # 24h 窗外的 6 不上板
        # 靈魂欄的細節要說人話:逾期/失敗原因得看得到。
        stuck = {c["id"]: c for c in res["columns"]["stuck"]}
        self.assertIn("認領逾期", stuck["kanban:3"]["detail"])
        self.assertIn("連續失敗 3 次", stuck["kanban:4"]["detail"])
        self.assertIn("boom", stuck["kanban:4"]["detail"])

    async def test_kanban_card_shape(self):
        _init_kanban([{"id": 9, "title": "形狀", "status": "todo",
                       "created_at": int(NOW - 10)}])
        _clear_canon()
        _clear_registry()
        res = await _board()
        card = res["columns"]["queued"][0]
        self.assertEqual(set(card), {"id", "source", "title", "state",
                                     "since_ts", "session_ref", "detail"})
        self.assertEqual(card["source"], "kanban")
        self.assertEqual(card["state"], "queued")
        self.assertAlmostEqual(card["since_ts"], NOW - 10, delta=2)


class TestDelegationColumns(unittest.IsolatedAsyncioTestCase):
    async def test_delegation_status_mapping(self):
        _init_kanban()
        _clear_canon()
        _clear_registry()
        _insert_delegation("d1", "created", title="剛派的")
        _insert_delegation("d2", "running", title="在跑的")
        _insert_delegation("d3", "failed", title="跑掛的", last_error="exit 1")
        _insert_delegation("d4", "idle", title="剛收工的", updated=NOW - 300)
        _insert_delegation("d5", "idle", title="早收工的", updated=NOW - 86400 * 2)
        _insert_delegation("d6", "archived", title="封存的")
        res = await _board()
        self.assertEqual(_col_ids(res, "queued"), ["delegation:d1"])
        self.assertEqual(_col_ids(res, "running"), ["delegation:d2"])
        self.assertEqual(_col_ids(res, "stuck"), ["delegation:d3"])
        self.assertEqual(_col_ids(res, "done"), ["delegation:d4"])
        stuck = res["columns"]["stuck"][0]
        self.assertIn("exit 1", stuck["detail"])
        self.assertEqual(stuck["session_ref"], "delegation:d3")

    async def test_delegated_cx_thread_not_double_counted(self):
        """委派中的 CX thread:delegation 卡 + CX live 卡只能出現一張。"""
        _init_kanban()
        _clear_canon()
        _clear_registry()
        _insert_delegation("d7", "running", provider="codex",
                           title="委派的 codex", tid="tid-777")

        async def cx_threads(_wanted=20):
            return [{"id": "tid-777"}, {"id": "tid-888"}]

        def summary(t):
            return dict(t)

        def enrich(s):
            tid = s.get("id")
            return {"id": tid, "thread_id": tid, "name": f"cx-{tid}",
                    "runtimeStatus": "running", "activeTurn": True,
                    "locked": False, "lastEventAt": NOW - 5}

        with patch.object(bridge, "_codex_session_summary", summary), \
             patch.object(bridge, "_codex_enrich_summary", enrich):
            res = await _board(cx_threads=cx_threads)
        running = _col_ids(res, "running")
        self.assertIn("delegation:d7", running)
        self.assertIn("cx:tid-888", running)
        self.assertNotIn("cx:tid-777", running)   # 已由 delegation 卡代表


class TestHermesDelegationPersistence(unittest.IsolatedAsyncioTestCase):
    """修 ①:委派源的持久兜底 —— bridge 重啟後 canonical/記憶體是空的,
    hermes state.db 的 async_delegations 必須把失敗委派留在 stuck 欄。"""

    def tearDown(self):
        _init_statedb()   # 別把持久層 rows 漏給別班測試

    async def test_statedb_rows_mapped_to_columns(self):
        """重啟情境:canonical 全空,只剩持久層 —— 四欄映射照譜來。"""
        _init_kanban()
        _clear_canon()
        _clear_registry()
        _init_statedb([
            {"delegation_id": "h-pend", "origin_session": "yuanfang",
             "state": "pending", "dispatched_at": NOW - 50},
            {"delegation_id": "h-run", "origin_session": "yuanfang",
             "state": "running", "dispatched_at": NOW - 100,
             "task_json": '{"goal": "查資料\\n附帶第二行"}'},
            {"delegation_id": "h-fin", "origin_session": "yuanfang",
             "state": "finalizing", "dispatched_at": NOW - 90},
            {"delegation_id": "h-err", "origin_session": "yuanfang",
             "state": "error", "dispatched_at": NOW - 3600,
             "completed_at": NOW - 3000,
             "result_json": '{"status":"error","error":"TimeoutError: 卡死\\n第二行"}'},
            {"delegation_id": "h-lost", "origin_session": "yuanfang",
             "state": "unknown", "dispatched_at": NOW - 7200,
             "completed_at": NOW - 7000},
            {"delegation_id": "h-done", "origin_session": "yuanfang",
             "state": "completed", "dispatched_at": NOW - 800,
             "completed_at": NOW - 600},
            {"delegation_id": "h-old", "origin_session": "yuanfang",
             "state": "delivered", "dispatched_at": NOW - 86400 * 3,
             "completed_at": NOW - 86400 * 2},
        ])
        res = await _board()
        self.assertTrue(res["sources_ok"]["delegations"])
        self.assertEqual(_col_ids(res, "queued"), ["delegation:h-pend"])
        self.assertEqual(set(_col_ids(res, "running")),
                         {"delegation:h-run", "delegation:h-fin"})
        self.assertEqual(set(_col_ids(res, "stuck")),
                         {"delegation:h-err", "delegation:h-lost"})
        self.assertEqual(_col_ids(res, "done"), ["delegation:h-done"])
        # 24h 窗外的完成不上板。
        for col in res["columns"].values():
            self.assertNotIn("delegation:h-old", [c["id"] for c in col])
        # stuck 卡帶 result_json 錯誤**首行**;標題取 task_json goal 首行。
        stuck = {c["id"]: c for c in res["columns"]["stuck"]}
        self.assertIn("TimeoutError: 卡死", stuck["delegation:h-err"]["detail"])
        self.assertNotIn("第二行", stuck["delegation:h-err"]["detail"])
        running = {c["id"]: c for c in res["columns"]["running"]}
        self.assertEqual(running["delegation:h-run"]["title"], "查資料")

    async def test_dedup_by_delegation_id_memory_wins(self):
        """canonical(記憶體側,較新鮮)與 DB 同 id → 只出 canonical 那張。
        canonical 說 running、DB 舊資料說 error:板上是 running,不是 stuck。"""
        _init_kanban()
        _clear_canon()
        _clear_registry()
        _insert_delegation("dd-1", "running", title="還在跑的")
        _init_statedb([
            {"delegation_id": "dd-1", "origin_session": "yuanfang",
             "state": "error", "dispatched_at": NOW - 500,
             "result_json": '{"error":"舊的失敗紀錄"}'},
            {"delegation_id": "dd-2", "origin_session": "yuanfang",
             "state": "error", "dispatched_at": NOW - 400,
             "completed_at": NOW - 300, "result_json": '{"error":"真的掛了"}'},
        ])
        res = await _board()
        self.assertEqual(_col_ids(res, "running"), ["delegation:dd-1"])
        self.assertEqual(_col_ids(res, "stuck"), ["delegation:dd-2"])
        all_ids = sum((_col_ids(res, c) for c in res["columns"]), [])
        self.assertEqual(all_ids.count("delegation:dd-1"), 1, "同 id 只能一張卡")

    async def test_statedb_broken_degrades_but_keeps_canonical(self):
        """持久層壞掉:canonical 卡照出(不吞卡),sources_ok.delegations
        誠實標 false(維持既有降級語意)。"""
        _init_kanban()
        _clear_canon()
        _clear_registry()
        _insert_delegation("d-mem", "failed", title="記憶體側的失敗",
                           last_error="exit 9")
        res = await _board(state_db="/nonexistent/state.db")
        self.assertFalse(res["sources_ok"]["delegations"])
        self.assertEqual(_col_ids(res, "stuck"), ["delegation:d-mem"])
        self.assertIn("exit 9", res["columns"]["stuck"][0]["detail"])

    async def test_statedb_missing_table_degrades(self):
        """檔案在但表不在(舊版 hermes)→ 同樣只降級,不 500、不吞 canonical。"""
        _init_kanban()
        _clear_canon()
        _clear_registry()
        _insert_delegation("d-ok", "running", title="活著的")
        bare = os.path.join(_TMP, "bare-state.db")
        sqlite3.connect(bare).close()
        res = await _board(state_db=bare)
        self.assertFalse(res["sources_ok"]["delegations"])
        self.assertEqual(_col_ids(res, "running"), ["delegation:d-ok"])


class TestRegistryAndLiveColumns(unittest.IsolatedAsyncioTestCase):
    async def test_registry_running_call(self):
        _init_kanban()
        _clear_canon()
        _clear_registry()
        bridge.REGISTRY.call_create("c1", caller="hermes:yuanfang",
                                    target="codex:tid-1", mode="await",
                                    message="幫我查個東西")
        bridge.REGISTRY.call_create("c2", caller="hermes:yuanfang",
                                    target="codex:tid-2", mode="await",
                                    status="done")
        res = await _board()
        self.assertEqual(_col_ids(res, "running"), ["call:c1"])
        card = res["columns"]["running"][0]
        self.assertEqual(card["session_ref"], "codex:tid-1")
        self.assertIn("hermes:yuanfang", card["title"])

    async def test_cc_busy_is_running(self):
        _init_kanban()
        _clear_canon()
        _clear_registry()

        async def busy(name):
            return {"busy": name == "alpha", "running": True,
                    "mode": None, "prompt": None}

        res = await _board(cc_rows=[("alpha", "/w/a", "1"),
                                    ("beta", "/w/b", "1"),
                                    ("gamma", "/w/c", "0")],
                           cc_status=busy)
        self.assertEqual(_col_ids(res, "running"), ["cc:alpha"])
        self.assertEqual(res["columns"]["running"][0]["session_ref"],
                         "claude_code:alpha")

    async def test_cx_states_running_locked_stalled(self):
        _init_kanban()
        _clear_canon()
        _clear_registry()

        async def cx_threads(_wanted=20):
            return [{"id": "t-run"}, {"id": "t-lock"}, {"id": "t-stall"},
                    {"id": "t-idle"}]

        def enrich(s):
            tid = s.get("id")
            base = {"id": tid, "thread_id": tid, "name": f"cx-{tid}",
                    "runtimeStatus": "idle", "activeTurn": False,
                    "locked": False, "lastEventAt": NOW - 30}
            if tid == "t-run":
                base.update(runtimeStatus="running", activeTurn=True)
            elif tid == "t-lock":
                base.update(locked=True, lockedSince=NOW - 900,
                            lockMessage="桌面版搶走 writer lock")
            elif tid == "t-stall":
                base.update(runtimeStatus="stalled")
            return base

        with patch.object(bridge, "_codex_session_summary", dict), \
             patch.object(bridge, "_codex_enrich_summary", enrich):
            res = await _board(cx_threads=cx_threads)
        self.assertEqual(_col_ids(res, "running"), ["cx:t-run"])
        self.assertEqual(set(_col_ids(res, "stuck")), {"cx:t-lock", "cx:t-stall"})
        stuck = {c["id"]: c for c in res["columns"]["stuck"]}
        self.assertIn("writer lock", stuck["cx:t-lock"]["detail"])
        self.assertNotIn("cx:t-idle", _col_ids(res, "queued") +
                         _col_ids(res, "running") + _col_ids(res, "done"))


class TestApprovalStuck(unittest.IsolatedAsyncioTestCase):
    async def test_pending_approval_over_10min_is_stuck(self):
        _init_kanban()
        _clear_canon()
        _clear_registry()
        _insert_approval("a-old", "codex", "codex:tid-9", NOW - 1200)
        _insert_approval("a-new", "codex", "codex:tid-8", NOW - 120)
        _insert_approval("a-done", "codex", "codex:tid-7", NOW - 9999,
                         status="approved")
        _insert_approval("a-hermes", "hermes", "hermes:mirror", NOW - 9999)
        res = await _board()
        self.assertEqual(_col_ids(res, "stuck"), ["approval:a-old"])
        card = res["columns"]["stuck"][0]
        self.assertEqual(card["session_ref"], "codex:tid-9")
        self.assertIn("分鐘", card["detail"])
        self.assertAlmostEqual(card["since_ts"], NOW - 1200, delta=2)

    async def test_stuck_approval_suppresses_running_card_same_session(self):
        """同一條 session:超時待核准(stuck)贏過 waiting_approval 的 running 卡。"""
        _init_kanban()
        _clear_canon()
        _clear_registry()
        _insert_approval("a-x", "codex", "codex:tid-w", NOW - 1200)

        async def cx_threads(_wanted=20):
            return [{"id": "tid-w"}]

        def enrich(s):
            return {"id": "tid-w", "thread_id": "tid-w", "name": "cx-w",
                    "runtimeStatus": "waiting_approval", "activeTurn": False,
                    "locked": False, "lastEventAt": NOW - 700}

        with patch.object(bridge, "_codex_session_summary", dict), \
             patch.object(bridge, "_codex_enrich_summary", enrich):
            res = await _board(cx_threads=cx_threads)
        self.assertEqual(_col_ids(res, "stuck"), ["approval:a-x"])
        self.assertEqual(_col_ids(res, "running"), [])   # running 卡被 stuck 吃掉


class TestDegradation(unittest.IsolatedAsyncioTestCase):
    """單一源壞掉不能拖垮整個端點 —— 缺席 + sources_ok=false,其他源照常。"""

    async def test_kanban_missing_file_degrades_only_kanban(self):
        _clear_canon()
        _clear_registry()
        _insert_delegation("d-alive", "running", title="活著的委派")
        with patch.object(bridge, "KANBAN_DB", "/nonexistent/kanban.db"):
            res = await _board()
        self.assertFalse(res["sources_ok"]["kanban"])
        self.assertTrue(res["sources_ok"]["delegations"])
        self.assertEqual(_col_ids(res, "running"), ["delegation:d-alive"])

    async def test_cx_source_exception_degrades_only_cx(self):
        _init_kanban([{"id": 1, "title": "還在排", "status": "todo",
                       "created_at": int(NOW - 5)}])
        _clear_canon()
        _clear_registry()

        async def cx_boom(_wanted=20):
            raise RuntimeError("app-server 掛了")

        res = await _board(cx_threads=cx_boom)
        self.assertFalse(res["sources_ok"]["cx"])
        self.assertTrue(res["sources_ok"]["kanban"])
        self.assertEqual(_col_ids(res, "queued"), ["kanban:1"])

    async def test_cc_source_exception_degrades_only_cc(self):
        _init_kanban()
        _clear_canon()
        _clear_registry()

        async def cc_boom(_name):
            raise RuntimeError("tmux 炸了")

        res = await _board(cc_rows=[("alpha", "/w/a", "1")], cc_status=cc_boom)
        self.assertFalse(res["sources_ok"]["cc"])
        self.assertTrue(res["sources_ok"]["kanban"])

    async def test_delegations_failure_does_not_break_cx_dedup(self):
        """delegations 源掛掉 → 去重集合為空,CX live 卡照常出現(寧可多一張
        卡也不能整欄消失)。"""
        _init_kanban()
        _clear_registry()

        async def cx_threads(_wanted=20):
            return [{"id": "tid-solo"}]

        def enrich(s):
            return {"id": "tid-solo", "thread_id": "tid-solo", "name": "solo",
                    "runtimeStatus": "running", "activeTurn": True,
                    "locked": False, "lastEventAt": NOW - 1}

        real = bridge._tb_ro_rows

        def ro_rows(db_path, sql, args=()):
            if db_path == bridge.CANON_DB:
                raise sqlite3.OperationalError("db locked")
            return real(db_path, sql, args)

        with patch.object(bridge, "_tb_ro_rows", ro_rows), \
             patch.object(bridge, "_codex_session_summary", dict), \
             patch.object(bridge, "_codex_enrich_summary", enrich):
            res = await _board(cx_threads=cx_threads)
        self.assertFalse(res["sources_ok"]["delegations"])
        self.assertFalse(res["sources_ok"]["approvals"])
        self.assertTrue(res["sources_ok"]["cx"])
        self.assertEqual(_col_ids(res, "running"), ["cx:tid-solo"])


class TestOrdering(unittest.IsolatedAsyncioTestCase):
    async def test_stuck_sorted_oldest_first(self):
        """stuck 欄卡最久的排最上面(最該先看);queued/done 新的在前。"""
        _init_kanban([
            {"id": 1, "title": "剛斷的", "status": "running",
             "claim_expires": int(NOW - 60)},
            {"id": 2, "title": "斷很久的", "status": "running",
             "claim_expires": int(NOW - 7200)},
        ])
        _clear_canon()
        _clear_registry()
        res = await _board()
        self.assertEqual(_col_ids(res, "stuck"), ["kanban:2", "kanban:1"])


if __name__ == "__main__":
    unittest.main()
