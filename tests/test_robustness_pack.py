"""issue #7 健壯性包的驗收測試 —— 六項裡「會自己壞掉」的四類風險。

跑法(repo 慣例,不用 pytest):
    PYTHONPATH=. python3 tests/test_robustness_pack.py

對應 issue #7 的驗收條件:
1. WAL 真的生效(canonical.db / accounts.db 開出來就是 WAL,busy_timeout 30s)
   —— 「併發送訊不再互鎖」的前提。
2. 子行程 hang 住之後 `_BG_TASKS` 不累積,而且不留孤兒行程
   —— 「kill -9 子程序後 /health 的 bg_tasks 不累積」。
3. idle 的 stream 真的會斷,而且斷線後帶 since_seq 重連「零漏事件」
   —— 「掛住客戶端後 bg_tasks 不累積」+ v2 events 是 app 首頁常駐訂閱。
4. worktree 真的被回收:乾淨的收掉、有未提交變更的留著;孤兒(行程重啟後
   SUBSESSIONS 已空)也會被掃掉 —— 「連續 isolate 派工後 worktrees 不無限成長」。
5. 例外日誌化:被吞掉的例外留得下痕跡,而且熱迴圈不會把 log 塞爆。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="robustness-canon-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
os.environ.setdefault("POCKET_MEDIA_DIR", os.path.join(_TMP, "media"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
import carddigest  # noqa: E402


def _drain(gen, budget=2.0):
    """把 async generator 收到結束(或超過 budget 秒)—— 回傳收到的 frame。"""
    async def run():
        out = []
        started = time.monotonic()
        async for frame in gen:
            out.append(frame)
            if time.monotonic() - started > budget:
                break
        return out
    return asyncio.run(run())


# ───────────────────────── 項目 2:sqlite WAL ─────────────────────────────
class TestSqliteWAL(unittest.TestCase):
    def test_canon_init_sets_wal_and_busy_timeout(self):
        bridge._canon_init()
        con = sqlite3.connect(bridge.CANON_DB, timeout=30)
        try:
            mode = con.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal",
                             "canonical.db 必須是 WAL —— 否則寫入會鎖住讀者")
            # WAL 是「一次性設定、持久生效」:新連線不用再下 PRAGMA 也是 WAL。
            self.assertEqual(
                sqlite3.connect(bridge.CANON_DB, timeout=30)
                .execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal",
                "WAL 應該持久寫在 db header 上,新連線自動繼承")
        finally:
            con.close()

    def test_accounts_db_wal(self):
        bridge._accounts_init()
        con = sqlite3.connect(bridge.ACCOUNTS_DB, timeout=30)
        try:
            self.assertEqual(
                con.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        finally:
            con.close()

    def test_no_writable_connect_left_on_short_timeout(self):
        """所有可寫的 canonical 連線都要有 30s busy_timeout。

        讀原始碼斷言,因為這是「別讓下一個人手滑加回 timeout=5」的護欄:
        WAL 讓讀者不再被鎖,但兩個寫者還是會互相 busy,10s 以下在多裝置
        併發下實測會噴 'database is locked'。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "bridge.py"), encoding="utf-8") as f:
            src = f.read()
        bad = []
        for i, line in enumerate(src.split("\n"), 1):
            if "sqlite3.connect(" not in line:
                continue
            if "mode=ro" in line:          # 唯讀路徑:WAL 下不會被寫者鎖,快速失敗比較好
                continue
            if "timeout=30" in line:
                continue
            bad.append((i, line.strip()))
        self.assertEqual(bad, [], f"可寫連線缺 timeout=30:{bad}")

    def test_hermes_state_db_is_never_opened_writable(self):
        """紅線:state.db 是 hermes 的,只准唯讀,絕不動它的 journal mode。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        for name in ("bridge.py", "acp_client.py"):
            with open(os.path.join(root, name), encoding="utf-8") as f:
                lines = f.read().split("\n")
            for i, line in enumerate(lines, 1):
                if "state.db" in line and "connect" in line and "mode=ro" not in line:
                    offenders.append((name, i, line.strip()))
        self.assertEqual(offenders, [], f"state.db 被非唯讀開啟:{offenders}")


# ────────────────── 項目 3:hang 住的子行程不留 task / 孤兒 ────────────────
class TestHangingSubprocess(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_subprocess_is_killed_and_task_finishes(self):
        """一個「開著 stdout 但永遠不輸出」的子行程 = 最惡劣的 hang。

        無進度逾時要:(a) 殺掉它,(b) 讓 _stream_agent 正常返回(於是
        _BG_TASKS 的 entry 會被 done callback 收掉,不會永久累積)。
        """
        sid = "sub-stall-test"
        bridge.SUBSESSIONS[sid] = {"output": [], "status": "running", "tool": "claude"}
        old = bridge._AGENT_STALL_SECS
        bridge._AGENT_STALL_SECS = 0.3
        try:
            # 這個 python 子行程會一直睡,stdout 開著但什麼都不寫。
            argv = [sys.executable, "-c", "import time; time.sleep(300)"]
            task = asyncio.create_task(
                bridge._stream_agent(sid, argv, os.getcwd(), "test"))
            bridge._BG_TASKS.add(task)
            task.add_done_callback(bridge._BG_TASKS.discard)

            await asyncio.wait_for(task, timeout=15)

            sub = bridge.SUBSESSIONS[sid]
            self.assertEqual(sub["status"], "stalled")
            proc = sub.get("proc")
            self.assertIsNotNone(proc)
            self.assertIsNotNone(proc.returncode,
                                 "hang 住的子行程必須被收掉,不能變孤兒")
            self.assertNotIn(task, bridge._BG_TASKS,
                             "task 完成後必須從 _BG_TASKS 移除(否則 /health 的 "
                             "bg_tasks 會一路累積)")
        finally:
            bridge._AGENT_STALL_SECS = old
            bridge.SUBSESSIONS.pop(sid, None)

    async def test_cancelled_stream_agent_kills_child(self):
        """呼叫端取消(關機、client 斷線)時也不准留孤兒。

        CancelledError 是 BaseException,接不到 `except Exception`,所以這條
        路徑以前會直接跳過 finally 裡的收尾把子行程留在那裡跑。
        """
        sid = "sub-cancel-test"
        bridge.SUBSESSIONS[sid] = {"output": [], "status": "running", "tool": "claude"}
        try:
            argv = [sys.executable, "-c", "import time; time.sleep(300)"]
            task = asyncio.create_task(
                bridge._stream_agent(sid, argv, os.getcwd(), "test"))
            # 等子行程真的起來
            for _ in range(200):
                if bridge.SUBSESSIONS[sid].get("proc") is not None:
                    break
                await asyncio.sleep(0.01)
            proc = bridge.SUBSESSIONS[sid]["proc"]
            self.assertIsNone(proc.returncode, "子行程應該還活著")
            pid = proc.pid

            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            await asyncio.sleep(0.2)
            self.assertFalse(_pid_alive(pid),
                             f"pid {pid} 在 task 被取消後還活著 = 孤兒行程")
        finally:
            bridge.SUBSESSIONS.pop(sid, None)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # 殭屍(已死但還沒被 reap)不算活著。
    out = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


# ─────────────────── 項目 4:idle stream 會斷 + 零漏事件 ───────────────────
class FakeRequest:
    """v2 events 只用到 request.is_disconnected()。"""
    def __init__(self):
        self.disconnected = False

    async def is_disconnected(self):
        return self.disconnected


class TestStreamIdleCutoff(unittest.TestCase):
    def setUp(self):
        self.store = carddigest.SessionCardStore()
        self.old_cut = bridge._STREAM_IDLE_CUTOFF_SECS
        self.old_ka = bridge.SSE_KEEPALIVE_SECS

    def tearDown(self):
        bridge._STREAM_IDLE_CUTOFF_SECS = self.old_cut
        bridge.SSE_KEEPALIVE_SECS = self.old_ka

    def _events_gen(self, since_seq=0):
        """直接拿 v2_session_events 的內層 gen(繞過 auth/router)。"""
        bridge._STREAM_IDLE_CUTOFF_SECS = 0.4
        bridge.SSE_KEEPALIVE_SECS = 0.05
        store = self.store
        req = FakeRequest()

        async def gen():
            cursor = since_seq
            store.subscribers += 1
            last_event = time.monotonic()
            backlog = store.since(since_seq)
            try:
                for ev in backlog:
                    yield ev
                    cursor = ev["seq"]
                idle = 0.0
                while True:
                    if await req.is_disconnected():
                        break
                    if time.monotonic() - last_event >= bridge._STREAM_IDLE_CUTOFF_SECS:
                        break
                    if store.seq > cursor:
                        fresh = store.since(cursor)
                        if fresh is None:
                            break
                        for ev in fresh:
                            yield ev
                            cursor = ev["seq"]
                        idle = 0.0
                        last_event = time.monotonic()
                    else:
                        await asyncio.sleep(0.02)
                        idle += 0.02
                        if idle >= max(0.01, float(bridge.SSE_KEEPALIVE_SECS)):
                            idle = 0.0
                            yield store.ping()
            finally:
                store.subscribers -= 1
        return gen(), store

    def test_v2_events_idle_stream_disconnects(self):
        """常駐訂閱在完全沒有事件時,必須自己斷掉而不是永久佔一個 task。"""
        gen, store = self._events_gen()
        started = time.monotonic()
        frames = _drain(gen, budget=5.0)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3.0, "idle stream 沒有在 cutoff 後斷線")
        self.assertEqual(store.subscribers, 0,
                         "斷線後 subscribers 必須歸零(否則 follower 會永遠空轉巡 status)")
        self.assertTrue(all(f["type"] == "ping" for f in frames),
                        f"idle 期間只該有 keepalive ping,實收:{frames}")

    def test_idle_cutoff_loses_no_events_on_reconnect(self):
        """關鍵安全性論證:30 分 idle 斷線不會讓 app 漏事件。

        idle 的定義就是「這段時間 store.seq 沒前進」,所以斷線時
        cursor == store.seq,ring buffer 也沒動過。app 帶 since_seq=cursor
        重連時 store.since() 三個 None 條件(領先 seq / 有洞 / 空 ring 但落後)
        一個都不成立 → 必定接得回去,零漏事件。
        """
        self.store.upsert_card({"id": "c1", "rev": 1})
        self.store.upsert_card({"id": "c2", "rev": 1})

        # 第一次連線:收到 backlog,然後 idle 到被切斷。
        gen, store = self._events_gen(since_seq=0)
        frames = _drain(gen, budget=5.0)
        real = [f for f in frames if f["type"] != "ping"]
        self.assertEqual([f["seq"] for f in real], [1, 2])
        cursor = real[-1]["seq"]
        self.assertEqual(cursor, store.seq, "idle 斷線時游標應該正好追平 seq")

        # 斷線期間「什麼都沒發生」→ 重連必須成功且拿到空 backlog(不是 410)。
        self.assertEqual(store.since(cursor), [],
                         "idle 斷線後重連應拿到空 backlog,而不是 410 SEQ_GONE")

        # 斷線後才產生的事件,重連時要補得回來。
        self.store.upsert_card({"id": "c3", "rev": 1})
        backlog = store.since(cursor)
        self.assertEqual([f["seq"] for f in backlog], [3],
                         "斷線後新增的事件必須靠 since_seq 補回來")

    def test_all_long_lived_streams_have_an_exit(self):
        """護欄:每個 streaming 端點都要有出口(deadline 或 idle cutoff)。

        v2 session events 曾是全檔唯一一條純 `while True` 沒有任何出口的
        stream —— 這個斷言防止它(或新加的端點)再退回去。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "bridge.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("_STREAM_IDLE_CUTOFF_SECS", src)
        # v2 session events 的 gen 必須參照 idle cutoff
        i = src.index("async def v2_session_events")
        j = src.index("scheduled reports", i)
        self.assertIn("_STREAM_IDLE_CUTOFF_SECS", src[i:j],
                      "v2 session events SSE 缺 idle 斷線")
        self.assertIn("v2_events_idle_cutoff", src[i:j],
                      "idle 斷線要留 log 才查得到")


# ───────────────────── 項目 5:worktree 回收 ──────────────────────────────
def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


class TestWorktreeCleanup(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="wt-repo-")
        _git("init", "-q", "-b", "main", cwd=self.repo)
        _git("config", "user.email", "t@t", cwd=self.repo)
        _git("config", "user.name", "t", cwd=self.repo)
        with open(os.path.join(self.repo, "f.txt"), "w") as f:
            f.write("hi\n")
        _git("add", ".", cwd=self.repo)
        _git("commit", "-qm", "init", cwd=self.repo)
        self.wt_root = tempfile.mkdtemp(prefix="wt-root-")
        self.old_root = bridge._WORKTREE_ROOT
        self.old_age = bridge._WORKTREE_ORPHAN_MIN_AGE_SECS
        bridge._WORKTREE_ROOT = self.wt_root
        bridge._WORKTREE_ORPHAN_MIN_AGE_SECS = 0.0

    def tearDown(self):
        bridge._WORKTREE_ROOT = self.old_root
        bridge._WORKTREE_ORPHAN_MIN_AGE_SECS = self.old_age

    async def _add_worktree(self, sid):
        wt = os.path.join(self.wt_root, sid)
        r = _git("worktree", "add", "-q", "-b", f"pocket/{sid}", wt, "HEAD",
                 cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        return wt

    async def test_clean_worktree_is_removed(self):
        wt = await self._add_worktree("s-clean")
        sub = {"worktree": wt, "base_cwd": self.repo, "cwd": wt}
        await bridge._cleanup_worktree("s-clean", sub)
        self.assertFalse(os.path.isdir(wt), "乾淨的 worktree 應該被收掉")
        self.assertIsNone(sub["worktree"])
        self.assertEqual(sub["cwd"], self.repo, "收掉後 cwd 要退回主樹")

    async def test_dirty_worktree_is_kept(self):
        wt = await self._add_worktree("s-dirty")
        with open(os.path.join(wt, "uncommitted.txt"), "w") as f:
            f.write("precious\n")
        sub = {"worktree": wt, "base_cwd": self.repo, "cwd": wt}
        await bridge._cleanup_worktree("s-dirty", sub)
        self.assertTrue(os.path.isdir(wt),
                        "有未提交變更的 worktree 絕對不能刪 —— 那是不可回復的資料損失")
        self.assertEqual(sub["worktree"], wt)

    async def test_orphan_worktrees_are_reaped(self):
        """行程重啟後 SUBSESSIONS 是空的 → 每一棵樹都成孤兒。

        end-of-dispatch 的 hook 結構上救不到這一類(它根本沒被執行到),
        所以需要一個定期清掃。
        """
        clean = await self._add_worktree("orphan-clean")
        dirty = await self._add_worktree("orphan-dirty")
        with open(os.path.join(dirty, "wip.txt"), "w") as f:
            f.write("wip\n")
        bridge.SUBSESSIONS.pop("orphan-clean", None)
        bridge.SUBSESSIONS.pop("orphan-dirty", None)

        n = await bridge._reap_orphan_worktrees()

        self.assertEqual(n, 1)
        self.assertFalse(os.path.isdir(clean), "乾淨的孤兒應該被掃掉")
        self.assertTrue(os.path.isdir(dirty), "有變更的孤兒要留著")

    async def test_running_subsession_worktree_is_not_reaped(self):
        wt = await self._add_worktree("live-one")
        bridge.SUBSESSIONS["live-one"] = {"status": "running", "worktree": wt}
        try:
            await bridge._reap_orphan_worktrees()
            self.assertTrue(os.path.isdir(wt),
                            "還在跑的派工的 worktree 不能被掃掉")
        finally:
            bridge.SUBSESSIONS.pop("live-one", None)

    async def test_fresh_worktree_within_grace_is_not_reaped(self):
        bridge._WORKTREE_ORPHAN_MIN_AGE_SECS = 3600.0
        wt = await self._add_worktree("too-fresh")
        await bridge._reap_orphan_worktrees()
        self.assertTrue(os.path.isdir(wt),
                        "剛建好、還沒登記進 SUBSESSIONS 的樹不能被搶先刪掉")


# ─────────────────── 項目 1:例外日誌化 + 項目 6:rotation ─────────────────
class TestExceptionLogging(unittest.TestCase):
    def setUp(self):
        bridge._EXC_LOG_STATE.clear()

    def _capture(self, fn):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn()
        return [l for l in buf.getvalue().split("\n") if "[bridge-event]" in l]

    def test_swallowed_exception_leaves_a_trace(self):
        lines = self._capture(
            lambda: bridge._log_exc("t_site", ValueError("boom"), expected=False))
        self.assertEqual(len(lines), 1)
        self.assertIn("exc_swallowed", lines[0])
        self.assertIn("t_site", lines[0])
        self.assertIn("ValueError", lines[0])
        self.assertIn('"severity": "anomaly"', lines[0])

    def test_anomalies_are_never_throttled(self):
        lines = self._capture(lambda: [
            bridge._log_exc("hot", ValueError("x"), expected=False)
            for _ in range(5)])
        self.assertEqual(len(lines), 5, "異常必須每次都記")

    def test_expected_failures_are_rate_limited(self):
        """熱迴圈(0.5s 一圈的 watcher)不能反過來把 log 塞爆 —— 那就是項目 6。"""
        lines = self._capture(lambda: [
            bridge._log_exc("hot", ValueError("x"), expected=True)
            for _ in range(500)])
        self.assertEqual(len(lines), 1, "預期失敗在 cooldown 內只該記一次")

        # cooldown 過後再記一次,並帶出期間被壓掉的筆數。
        key = ("hot", "ValueError")
        bridge._EXC_LOG_STATE[key][0] -= bridge._EXC_LOG_COOLDOWN_SECS + 1
        lines = self._capture(
            lambda: bridge._log_exc("hot", ValueError("x"), expected=True))
        self.assertEqual(len(lines), 1)
        self.assertIn('"suppressed": 499', lines[0],
                      "被壓掉的筆數要帶出來,否則看不出真實頻率")

    def test_no_silent_except_exception_left(self):
        """護欄:不准再有「完全不留痕」的 except Exception。"""
        import ast
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "bridge.py"), encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
        LOGGERS = {"_log_event", "_log_exc", "_codex_http_error"}
        silent = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            t = node.type
            if not (isinstance(t, ast.Name) and t.id in ("Exception", "BaseException")):
                continue
            names, raises = set(), False
            for stmt in node.body:
                for n in ast.walk(stmt):
                    if isinstance(n, ast.Raise):
                        raises = True
                    if isinstance(n, ast.Call):
                        f = n.func
                        names.add(f.id if isinstance(f, ast.Name) else
                                  getattr(f, "attr", ""))
            if not (names & LOGGERS) and not raises:
                silent.append(node.lineno)
        self.assertEqual(silent, [],
                         f"這些 except Exception 吞掉例外又不留痕:{silent}")


class TestLogRotation(unittest.TestCase):
    def test_rotation_truncates_in_place_and_keeps_generations(self):
        """launchd 的 fd 是 O_APPEND 且不會重開,所以只能 copy+truncate。
        rename 的話新檔會永遠是 0 byte(等於靜默停掉 log)。"""
        d = tempfile.mkdtemp(prefix="rot-")
        path = os.path.join(d, "bridge.out.log")
        old_max, old_keep = bridge._LOG_ROTATE_MAX_BYTES, bridge._LOG_ROTATE_KEEP
        bridge._LOG_ROTATE_MAX_BYTES = 1024
        bridge._LOG_ROTATE_KEEP = 3
        try:
            # 用 O_APPEND 開著不放,模擬 launchd 抓著 fd 的狀態。
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            try:
                for gen in range(4):
                    os.write(fd, b"x" * 2048)
                    self.assertGreater(os.path.getsize(path), 1024)
                    bridge._rotate_log_file(path)
                    self.assertEqual(os.path.getsize(path), 0,
                                     "輪替後現用檔必須被 truncate 成 0")
                    # truncate 之後照樣寫得進去(O_APPEND 會 seek 到新 EOF,
                    # 不會留 sparse 空洞)
                    os.write(fd, b"y" * 16)
                    self.assertEqual(os.path.getsize(path), 16,
                                     "O_APPEND 下 truncate 後的寫入應落在 offset 0")
                    os.truncate(path, 0)
                self.assertTrue(os.path.exists(path + ".1"))
                self.assertTrue(os.path.exists(path + ".2"))
                self.assertTrue(os.path.exists(path + ".3"))
                self.assertFalse(os.path.exists(path + ".4"),
                                 f"最多保留 {bridge._LOG_ROTATE_KEEP} 代,不能無限長")
            finally:
                os.close(fd)
        finally:
            bridge._LOG_ROTATE_MAX_BYTES = old_max
            bridge._LOG_ROTATE_KEEP = old_keep

    def test_below_threshold_is_left_alone(self):
        d = tempfile.mkdtemp(prefix="rot2-")
        path = os.path.join(d, "small.log")
        with open(path, "w") as f:
            f.write("tiny")
        bridge._rotate_log_file(path)
        with open(path) as f:
            self.assertEqual(f.read(), "tiny")
        self.assertFalse(os.path.exists(path + ".1"))


class TestHttpErrorObservability(unittest.TestCase):
    def test_4xx_and_5xx_are_distinguished(self):
        import io
        import contextlib
        from fastapi.testclient import TestClient

        bridge._HTTP_4XX_LOG_STATE.clear()
        client = TestClient(bridge.app, raise_server_exceptions=False)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = client.get("/app/v1/messages?session=nope",
                           headers={"Authorization": "Bearer test-unit-token"})
        self.assertGreaterEqual(r.status_code, 400)
        self.assertLess(r.status_code, 500)
        self.assertIn("http_error_4xx", buf.getvalue())
        self.assertNotIn("http_error_5xx", buf.getvalue())

    def test_4xx_flood_is_throttled(self):
        """稽核裡有過單一 log 檔 11,936 筆 404 —— 4xx 不能一筆一行。"""
        import io
        import contextlib
        from fastapi.testclient import TestClient

        bridge._HTTP_4XX_LOG_STATE.clear()
        client = TestClient(bridge.app, raise_server_exceptions=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            for _ in range(20):
                client.get("/app/v1/messages?session=nope",
                           headers={"Authorization": "Bearer test-unit-token"})
        self.assertEqual(buf.getvalue().count("http_error_4xx"), 1,
                         "同一 (code, path) 的 4xx 在 cooldown 內只該記一次")

    def test_unhandled_exception_logs_but_keeps_the_default_500(self):
        """加 handler 只為了留痕:狀態碼與 body 必須跟 Starlette 預設一模一樣。"""
        import io
        import contextlib
        from fastapi.testclient import TestClient

        @bridge.app.get("/__robustness_boom")
        async def _boom():                     # noqa: ANN202
            raise RuntimeError("kaboom")

        client = TestClient(bridge.app, raise_server_exceptions=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = client.get("/__robustness_boom")
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.text, "Internal Server Error")
        out = buf.getvalue()
        self.assertIn("unhandled_request", out)
        self.assertIn("RuntimeError", out)
        self.assertIn('"severity": "anomaly"', out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
