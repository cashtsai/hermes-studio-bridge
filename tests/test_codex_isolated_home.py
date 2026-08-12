"""codex 家目錄隔離(`POCKET_CODEX_ISOLATED`)的回歸測試。

背景見 `codex_home.py` 的檔頭:bridge 要有自己的 `CODEX_HOME`,才不會跟
ChatGPT 桌面 app 搶 thread-store 的 writer lock,也才能在龍蝦那台沒有桌面
app 的無頭 Ubuntu 上活著。

這裡守住四件事:
  1. bootstrap 只碰隔離家目錄,**一個位元組都不寫進來源家目錄**;
  2. 旗標關閉時 spawn 呼叫與今天**逐字相同**(連 `env=` 都不傳);
  3. 旗標開啟時真的用隔離家目錄,而且不去碰桌面版的 socket;
  4. 搬遷可重複執行、來源唯讀、預演不寫入。

跑法:
    PYTHONPATH=. python -m unittest tests.test_codex_isolated_home
    PYTHONPATH=. python tests/test_codex_isolated_home.py
"""
import asyncio
import contextlib
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="cxiso-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
# `import bridge` 在模組層就會 new 一顆 AgentRegistry(建構子建表)——
# 不蓋掉就寫到 production 的 ~/.pocket/agent-registry.db。
os.environ.setdefault("POCKET_REGISTRY_DB", os.path.join(_TMP, "registry.db"))
os.environ.setdefault("HARNESS_DB", os.path.join(_TMP, "harness.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
# 安全帶:整個測試檔跑的期間,隔離家目錄一律指到 tmp。萬一哪個 case 忘了
# 蓋掉環境變數,也絕對打不到 ~/.pocket。
os.environ["POCKET_CODEX_HOME"] = os.path.join(_TMP, "guard-home")
os.environ["CODEX_HOME"] = os.path.join(_TMP, "guard-shared")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
import codex_home  # noqa: E402

REAL_CODEX = os.path.expanduser("~/.codex")
REAL_POCKET = os.path.expanduser("~/.pocket")


def tree_fingerprint(root: str):
    """整棵樹的 (相對路徑, 內容雜湊/symlink 目標) 集合。"""
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            if os.path.islink(full):
                out[rel] = "link:" + os.readlink(full)
                continue
            try:
                with open(full, "rb") as fh:
                    out[rel] = hashlib.sha256(fh.read()).hexdigest()
            except OSError:
                out[rel] = "unreadable"
    return out


def make_source_home(root: str, config_text: str = 'model_reasoning_effort = "xhigh"\n'):
    """做一個假的「共用家目錄」:auth.json + config.toml。"""
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "auth.json"), "w", encoding="utf-8") as fh:
        fh.write('{"tokens": {"access_token": "NOT-A-REAL-TOKEN"}}')
    with open(os.path.join(root, "config.toml"), "w", encoding="utf-8") as fh:
        fh.write(config_text)
    return root


class IsolationFlagTest(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_explicit_values_win(self):
        for raw in ("1", "true", "TRUE", "yes", "on", "enabled"):
            os.environ["POCKET_CODEX_ISOLATED"] = raw
            self.assertTrue(codex_home.isolation_enabled(), raw)
        for raw in ("0", "false", "no", "off", "disabled"):
            os.environ["POCKET_CODEX_ISOLATED"] = raw
            self.assertFalse(codex_home.isolation_enabled(), raw)

    def test_auto_is_off_on_macos_and_on_elsewhere(self):
        """auto 的意思:善彰的 Mac 預設關(這次合併零風險),龍蝦無頭機預設開。"""
        for raw in ("auto", "", "garbage"):
            os.environ["POCKET_CODEX_ISOLATED"] = raw
            with mock.patch.object(codex_home.sys, "platform", "darwin"):
                self.assertFalse(codex_home.isolation_enabled(), raw)
            with mock.patch.object(codex_home.sys, "platform", "linux"):
                self.assertTrue(codex_home.isolation_enabled(), raw)
        os.environ.pop("POCKET_CODEX_ISOLATED", None)
        with mock.patch.object(codex_home.sys, "platform", "darwin"):
            self.assertFalse(codex_home.isolation_enabled(), "沒設 = auto")

    def test_effective_home_and_child_env(self):
        os.environ["CODEX_HOME"] = "/tmp/shared-home"
        os.environ["POCKET_CODEX_HOME"] = "/tmp/iso-home"
        os.environ["POCKET_CODEX_ISOLATED"] = "0"
        self.assertEqual(codex_home.effective_home(), "/tmp/shared-home")
        self.assertIsNone(codex_home.child_env(),
                          "關閉時必須回 None —— 子行程環境要跟今天一模一樣")
        os.environ["POCKET_CODEX_ISOLATED"] = "1"
        self.assertEqual(codex_home.effective_home(), "/tmp/iso-home")
        self.assertEqual(codex_home.child_env()["CODEX_HOME"], "/tmp/iso-home")

    def test_sessions_dirs_covers_both_homes_when_isolated(self):
        os.environ["CODEX_HOME"] = "/tmp/shared-home"
        os.environ["POCKET_CODEX_HOME"] = "/tmp/iso-home"
        os.environ["POCKET_CODEX_ISOLATED"] = "0"
        self.assertEqual(codex_home.sessions_dirs(), ["/tmp/shared-home/sessions"])
        os.environ["POCKET_CODEX_ISOLATED"] = "1"
        # rate limit 是帳號層級的事實,桌面 app 寫的那筆一樣算數 → 兩邊都掃
        self.assertEqual(codex_home.sessions_dirs(),
                         ["/tmp/iso-home/sessions", "/tmp/shared-home/sessions"])


class BootstrapTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cxiso-bs-")
        self.source = make_source_home(os.path.join(self.root, "shared"))
        self.home = os.path.join(self.root, "pocket", "codex-home")
        self.before = tree_fingerprint(self.source)

    def assert_source_untouched(self):
        self.assertEqual(tree_fingerprint(self.source), self.before,
                         "bootstrap 寫到來源家目錄了 —— 那是紅線")

    def test_creates_0700_home_with_auth_symlink_and_config_copy(self):
        report = codex_home.bootstrap(self.home, self.source, mode="copy")
        self.assertTrue(report["created"])
        self.assertEqual(report["auth"], "symlinked")
        self.assertEqual(report["config"], "copied")
        self.assertEqual(os.stat(self.home).st_mode & 0o777, 0o700)
        self.assertTrue(os.path.isdir(os.path.join(self.home, "sessions")))
        auth = os.path.join(self.home, "auth.json")
        self.assertTrue(os.path.islink(auth), "憑證要 symlink,不要複製第二份")
        self.assertEqual(os.path.realpath(auth),
                         os.path.realpath(os.path.join(self.source, "auth.json")))
        with open(os.path.join(self.home, "config.toml"), encoding="utf-8") as fh:
            self.assertIn("xhigh", fh.read(), "善彰的設定不能被默默丟掉")
        self.assert_source_untouched()

    def test_is_idempotent(self):
        codex_home.bootstrap(self.home, self.source)
        snapshot = tree_fingerprint(self.home)
        report = codex_home.bootstrap(self.home, self.source)
        self.assertEqual(report["created"], False)
        self.assertEqual(report["auth"], "symlink-ok")
        self.assertEqual(report["config"], "up-to-date")
        after = tree_fingerprint(self.home)
        # marker 的 copied_at 會變,其餘必須一模一樣
        del snapshot[codex_home.CONFIG_ORIGIN_MARKER]
        del after[codex_home.CONFIG_ORIGIN_MARKER]
        self.assertEqual(snapshot, after)
        self.assert_source_untouched()

    def test_config_refreshes_when_source_changes(self):
        codex_home.bootstrap(self.home, self.source)
        with open(os.path.join(self.source, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write('model_reasoning_effort = "xhigh"\nnew_key = 1\n')
        self.before = tree_fingerprint(self.source)
        report = codex_home.bootstrap(self.home, self.source)
        self.assertEqual(report["config"], "refreshed")
        with open(os.path.join(self.home, "config.toml"), encoding="utf-8") as fh:
            self.assertIn("new_key", fh.read())
        self.assert_source_untouched()

    def test_local_edit_in_isolated_copy_is_never_clobbered(self):
        codex_home.bootstrap(self.home, self.source)
        with open(os.path.join(self.home, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write("# 我在隔離家目錄手改的\n")
        with open(os.path.join(self.source, "config.toml"), "a", encoding="utf-8") as fh:
            fh.write("upstream_change = true\n")
        self.before = tree_fingerprint(self.source)
        report = codex_home.bootstrap(self.home, self.source)
        self.assertEqual(report["config"], "local-edit-kept")
        with open(os.path.join(self.home, "config.toml"), encoding="utf-8") as fh:
            self.assertIn("手改", fh.read())
        self.assert_source_untouched()

    def test_existing_regular_auth_file_is_respected(self):
        os.makedirs(self.home)
        with open(os.path.join(self.home, "auth.json"), "w", encoding="utf-8") as fh:
            fh.write('{"tokens": {"access_token": "SEPARATE-ACCOUNT"}}')
        report = codex_home.bootstrap(self.home, self.source)
        self.assertEqual(report["auth"], "regular-file-kept")
        self.assertFalse(os.path.islink(os.path.join(self.home, "auth.json")))
        self.assert_source_untouched()

    def test_broken_auth_symlink_is_relinked(self):
        os.makedirs(self.home)
        os.symlink(os.path.join(self.root, "gone", "auth.json"),
                   os.path.join(self.home, "auth.json"))
        report = codex_home.bootstrap(self.home, self.source)
        self.assertEqual(report["auth"], "symlinked")
        self.assertTrue(os.path.exists(os.path.join(self.home, "auth.json")))

    def test_missing_source_auth_is_reported_not_fatal(self):
        os.unlink(os.path.join(self.source, "auth.json"))
        self.before = tree_fingerprint(self.source)
        report = codex_home.bootstrap(self.home, self.source)
        self.assertEqual(report["auth"], "source-missing")
        self.assertTrue(os.path.isdir(self.home))
        self.assert_source_untouched()

    def test_symlink_config_mode(self):
        report = codex_home.bootstrap(self.home, self.source, mode="symlink")
        self.assertEqual(report["config"], "symlinked")
        self.assertTrue(os.path.islink(os.path.join(self.home, "config.toml")))
        # 切回 copy 模式要能拆掉連結,不然編輯隔離副本會寫回 ~/.codex
        report = codex_home.bootstrap(self.home, self.source, mode="copy")
        self.assertEqual(report["config"], "copied")
        self.assertFalse(os.path.islink(os.path.join(self.home, "config.toml")))
        self.assert_source_untouched()

    def test_refuses_when_home_equals_source(self):
        with self.assertRaises(codex_home.CodexHomeError):
            codex_home.bootstrap(self.source, self.source)


# ─────────────────────────── 傳輸 × 隔離 的矩陣 ───────────────────────────

class ModeMatrixTest(unittest.TestCase):
    def setUp(self):
        self._saved_mode = bridge.CODEX_APP_SERVER_MODE
        self._saved_env = dict(os.environ)
        bridge._CODEX_ISOLATION_CONFLICT_LOGGED = False

    def tearDown(self):
        bridge.CODEX_APP_SERVER_MODE = self._saved_mode
        os.environ.clear()
        os.environ.update(self._saved_env)

    def test_flag_off_passes_transport_mode_through(self):
        os.environ["POCKET_CODEX_ISOLATED"] = "0"
        for mode in ("auto", "managed", "stdio"):
            bridge.CODEX_APP_SERVER_MODE = mode
            self.assertEqual(bridge._effective_app_server_mode(), mode)
        bridge.CODEX_APP_SERVER_MODE = "nonsense"
        self.assertEqual(bridge._effective_app_server_mode(), "auto")

    def test_isolation_forces_stdio(self):
        """隔離 ⇒ 一定自己 spawn。去接桌面版 daemon 就等於沒有隔離。"""
        os.environ["POCKET_CODEX_ISOLATED"] = "1"
        for mode in ("auto", "managed", "stdio"):
            bridge.CODEX_APP_SERVER_MODE = mode
            self.assertEqual(bridge._effective_app_server_mode(), "stdio", mode)

    def test_isolation_plus_managed_logs_a_conflict_once(self):
        os.environ["POCKET_CODEX_ISOLATED"] = "1"
        bridge.CODEX_APP_SERVER_MODE = "managed"
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bridge._effective_app_server_mode()
            bridge._effective_app_server_mode()
        lines = [ln for ln in buf.getvalue().splitlines()
                 if "codex_isolation_mode_conflict" in ln]
        self.assertEqual(len(lines), 1, buf.getvalue())


# ─────────────────────────── 真的 spawn 一顆 ───────────────────────────

# 假的 app-server:把自己看到的 CODEX_HOME 原封不動回報在 initialize 裡。
FAKE_APP_SERVER = '''
import json, os, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    if msg.get("method") == "initialize" and "id" in msg:
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0", "id": msg["id"],
            "result": {"userAgent": "fake",
                       "codexHome": os.environ.get("CODEX_HOME", "<unset>")}}) + "\\n")
        sys.stdout.flush()
'''


async def shutdown_client(client):
    for task in (client._reader_task, client._stderr_task):
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    if client.ws is not None:
        with contextlib.suppress(Exception):
            await client.ws.close()
    if client.proc is not None:
        with contextlib.suppress(Exception):
            client.proc.kill()
        with contextlib.suppress(Exception):
            await client.proc.wait()


class SpawnTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cxiso-spawn-", dir="/tmp")
        self.source = make_source_home(os.path.join(self.root, "shared"))
        self.home = os.path.join(self.root, "iso")
        self.sock = os.path.join(self.root, "s.sock")
        self.fake_bin = os.path.join(self.root, "fake-codex")
        with open(self.fake_bin, "w", encoding="utf-8") as fh:
            fh.write("#!" + sys.executable + "\n" + FAKE_APP_SERVER)
        os.chmod(self.fake_bin, 0o755)
        self._saved = {"mode": bridge.CODEX_APP_SERVER_MODE,
                       "sock": bridge.CODEX_APP_SERVER_SOCKET,
                       "resolve": bridge._resolve_codex_bin,
                       "env": dict(os.environ)}
        bridge.CODEX_APP_SERVER_SOCKET = self.sock
        bridge._resolve_codex_bin = lambda: self.fake_bin
        os.environ["CODEX_HOME"] = self.source
        os.environ["POCKET_CODEX_HOME"] = self.home
        self.captured_env = {}
        self._real_spawn = asyncio.create_subprocess_exec

        async def spy(*argv, **kwargs):
            self.captured_env = dict(kwargs)
            return await self._real_spawn(*argv, **kwargs)

        asyncio.create_subprocess_exec = spy

    def tearDown(self):
        asyncio.create_subprocess_exec = self._real_spawn
        bridge.CODEX_APP_SERVER_MODE = self._saved["mode"]
        bridge.CODEX_APP_SERVER_SOCKET = self._saved["sock"]
        bridge._resolve_codex_bin = self._saved["resolve"]
        os.environ.clear()
        os.environ.update(self._saved["env"])

    def _start(self):
        client = bridge.CodexAppServerClient()

        async def main():
            async with client._lock:
                await client._ensure_started_locked()
            return client
        return asyncio.run(main()), client

    def test_isolated_spawn_uses_the_isolated_home(self):
        os.environ["POCKET_CODEX_ISOLATED"] = "1"
        bridge.CODEX_APP_SERVER_MODE = "auto"
        _, client = self._start()
        try:
            self.assertEqual(client.transport, "stdio")
            self.assertEqual(client.codex_home, self.home)
            # 子行程真的收到 CODEX_HOME(fake server 把它回報在 initialize)
            self.assertEqual(self.captured_env["env"]["CODEX_HOME"], self.home)
            # 而且家目錄被 bootstrap 好了
            self.assertEqual(os.stat(self.home).st_mode & 0o777, 0o700)
            self.assertTrue(os.path.islink(os.path.join(self.home, "auth.json")))
            self.assertTrue(os.path.exists(os.path.join(self.home, "config.toml")))
        finally:
            asyncio.run(shutdown_client(client))

    def test_flag_off_spawn_is_byte_identical_to_today(self):
        """關閉時連 `env=` 都不准出現在 spawn 參數裡。"""
        os.environ["POCKET_CODEX_ISOLATED"] = "0"
        bridge.CODEX_APP_SERVER_MODE = "auto"
        _, client = self._start()
        try:
            self.assertEqual(client.transport, "stdio")
            self.assertNotIn("env", self.captured_env,
                             "旗標關閉卻多傳了 env= —— 那就不是今天的行為了")
            self.assertEqual(set(self.captured_env),
                             {"stdout", "stderr", "stdin", "cwd", "limit"})
            self.assertEqual(client.codex_home, self.source)
            self.assertFalse(os.path.exists(self.home),
                             "旗標關閉時不准生出隔離家目錄")
        finally:
            asyncio.run(shutdown_client(client))

    def test_headless_host_has_no_daemon_and_still_works(self):
        """龍蝦那台:沒有桌面 app、沒有 socket、隔離預設開 → 照樣起得來。"""
        os.environ.pop("POCKET_CODEX_ISOLATED", None)
        bridge.CODEX_APP_SERVER_MODE = "auto"
        self.assertFalse(os.path.exists(self.sock))
        with mock.patch.object(codex_home.sys, "platform", "linux"):
            _, client = self._start()
            try:
                self.assertEqual(client.transport, "stdio")
                self.assertEqual(client.codex_home, self.home)
            finally:
                asyncio.run(shutdown_client(client))

    def test_isolated_never_touches_the_desktop_socket(self):
        """就算桌面 daemon 活著,隔離模式也不准去接它(接了就等於沒隔離)。"""
        os.environ["POCKET_CODEX_ISOLATED"] = "1"
        bridge.CODEX_APP_SERVER_MODE = "auto"
        state = {"connections": 0}

        async def main():
            from websockets.asyncio.server import unix_serve

            async def handle(ws):
                state["connections"] += 1
                async for _ in ws:
                    pass

            server = await unix_serve(handle, path=self.sock)
            client = bridge.CodexAppServerClient()
            try:
                async with client._lock:
                    await client._ensure_started_locked()
                self.assertEqual(client.transport, "stdio")
                self.assertEqual(state["connections"], 0)
            finally:
                await shutdown_client(client)
                server.close()
                with contextlib.suppress(Exception):
                    await server.wait_closed()
        asyncio.run(main())

    def test_usage_scan_is_unchanged_when_flag_is_off(self):
        """回歸護欄:旗標關閉時,用量掃描的檔案清單與今天完全相同。"""
        os.environ["POCKET_CODEX_ISOLATED"] = "0"
        sessions = os.path.join(self.source, "sessions", "2026", "08", "12")
        os.makedirs(sessions)
        path = os.path.join(sessions, "rollout-x.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        saved = bridge.CODEX_SESSIONS_DIR
        bridge.CODEX_SESSIONS_DIR = os.path.join(self.source, "sessions")
        try:
            self.assertEqual(
                bridge._codex_session_files(),
                bridge._usage_newest_files(bridge.CODEX_SESSIONS_DIR, "*.jsonl",
                                           bridge._USAGE_MAX_CODEX_FILES))
            os.environ["POCKET_CODEX_ISOLATED"] = "1"
            iso_sessions = os.path.join(self.home, "sessions")
            os.makedirs(iso_sessions, exist_ok=True)
            iso_file = os.path.join(iso_sessions, "rollout-iso.jsonl")
            with open(iso_file, "w", encoding="utf-8") as fh:
                fh.write("{}\n")
            found = bridge._codex_session_files()
            self.assertIn(iso_file, found)
            self.assertIn(path, found, "共用家目錄的用量紀錄不該被漏掉")
        finally:
            bridge.CODEX_SESSIONS_DIR = saved


# ─────────────────────────── 舊 thread 搬遷 ───────────────────────────

THREAD_COLUMNS = (
    "id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at INTEGER NOT NULL,"
    " updated_at INTEGER NOT NULL, source TEXT NOT NULL, model_provider TEXT NOT NULL,"
    " cwd TEXT NOT NULL, title TEXT NOT NULL, sandbox_policy TEXT NOT NULL,"
    " approval_mode TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0,"
    " preview TEXT NOT NULL DEFAULT '', recency_at_ms INTEGER NOT NULL DEFAULT 0,"
    " updated_at_ms INTEGER, name TEXT, thread_section_id TEXT"
    " REFERENCES thread_sections(id) ON DELETE SET NULL")


def make_state_db(path: str, version: int = 46):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE _sqlx_migrations (version INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO _sqlx_migrations(version) VALUES(?)", (version,))
    con.execute("CREATE TABLE thread_sections (id TEXT PRIMARY KEY, name TEXT NOT NULL)")
    con.execute(f"CREATE TABLE threads ({THREAD_COLUMNS})")
    con.execute("CREATE TABLE thread_dynamic_tools (thread_id TEXT NOT NULL,"
                " position INTEGER NOT NULL, name TEXT NOT NULL,"
                " PRIMARY KEY(thread_id, position))")
    con.execute("CREATE TABLE thread_spawn_edges (parent_thread_id TEXT NOT NULL,"
                " child_thread_id TEXT PRIMARY KEY, status TEXT NOT NULL)")
    con.commit()
    con.close()


def seed_thread(home: str, tid: str, name: str, section: tuple | None = None):
    db = codex_home.state_db_path(home)
    rollout = os.path.join(home, "sessions", "2026", "08", f"rollout-{tid}.jsonl")
    os.makedirs(os.path.dirname(rollout), exist_ok=True)
    with open(rollout, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"thread_id": tid, "payload": {"type": "user"}}) + "\n")
    con = sqlite3.connect(db)
    if section:
        con.execute("INSERT OR IGNORE INTO thread_sections(id,name) VALUES(?,?)", section)
    con.execute(
        "INSERT INTO threads(id,rollout_path,created_at,updated_at,source,"
        "model_provider,cwd,title,sandbox_policy,approval_mode,archived,preview,"
        "recency_at_ms,updated_at_ms,name,thread_section_id)"
        " VALUES(?,?,1,2,'vscode','openai','/tmp','t','ro','never',0,'p',10,10,?,?)",
        (tid, rollout, name, section[0] if section else None))
    con.execute("INSERT INTO thread_dynamic_tools(thread_id,position,name)"
                " VALUES(?,0,'tool-a')", (tid,))
    con.commit()
    con.close()
    return rollout


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cxiso-mig-")
        self.source = make_source_home(os.path.join(self.root, "shared"))
        make_state_db(os.path.join(self.source, "state_5.sqlite"))
        self.target = os.path.join(self.root, "iso")
        os.makedirs(self.target)
        make_state_db(os.path.join(self.target, "state_5.sqlite"))
        self.rollout = seed_thread(self.source, "tid-1", "Cashcamp",
                                   section=("sec-1", "Pinned"))
        seed_thread(self.source, "tid-2", "FLiPER")
        self.before = tree_fingerprint(self.source)

    def assert_source_untouched(self):
        self.assertEqual(tree_fingerprint(self.source), self.before,
                         "搬遷寫到來源家目錄了 —— 那是紅線")

    def test_dry_run_writes_nothing(self):
        target_before = tree_fingerprint(self.target)
        result = codex_home.migrate_threads(thread_ids=["tid-1"],
                                            source=self.source, target=self.target)
        self.assertEqual([m["id"] for m in result["migrated"]], ["tid-1"])
        self.assertEqual(tree_fingerprint(self.target), target_before)
        self.assert_source_untouched()

    def test_apply_copies_row_and_rollout_and_rewrites_path(self):
        result = codex_home.migrate_threads(
            thread_ids=["tid-1"], source=self.source, target=self.target, apply=True)
        self.assertEqual([m["id"] for m in result["migrated"]], ["tid-1"])
        con = sqlite3.connect(os.path.join(self.target, "state_5.sqlite"))
        row = con.execute("SELECT rollout_path,name,thread_section_id FROM threads"
                          " WHERE id='tid-1'").fetchone()
        tools = con.execute("SELECT COUNT(*) FROM thread_dynamic_tools"
                            " WHERE thread_id='tid-1'").fetchone()[0]
        section = con.execute("SELECT name FROM thread_sections WHERE id='sec-1'").fetchone()
        con.close()
        self.assertEqual(row[1], "Cashcamp")
        self.assertEqual(tools, 1)
        self.assertEqual(section[0], "Pinned", "外鍵指到的 section 也要跟著搬")
        self.assertTrue(row[0].startswith(self.target),
                        f"rollout_path 沒改寫到新家:{row[0]}")
        self.assertTrue(os.path.exists(row[0]))
        with open(row[0], encoding="utf-8") as fh:
            self.assertIn("tid-1", fh.read())
        self.assert_source_untouched()

    def test_is_idempotent(self):
        codex_home.migrate_threads(thread_ids=["tid-1"], source=self.source,
                                    target=self.target, apply=True)
        snapshot = tree_fingerprint(self.target)
        result = codex_home.migrate_threads(thread_ids=["tid-1"], source=self.source,
                                            target=self.target, apply=True)
        self.assertEqual(result["migrated"], [])
        self.assertEqual([s["reason"] for s in result["skipped"]], ["already-present"])
        after = tree_fingerprint(self.target)
        after.pop(codex_home.CONFIG_ORIGIN_MARKER, None)
        snapshot.pop(codex_home.CONFIG_ORIGIN_MARKER, None)
        self.assertEqual(snapshot, after)
        self.assert_source_untouched()

    def test_recent_selects_by_recency(self):
        result = codex_home.migrate_threads(recent=2, source=self.source,
                                            target=self.target, apply=True)
        self.assertEqual(sorted(m["id"] for m in result["migrated"]),
                         ["tid-1", "tid-2"])
        self.assert_source_untouched()

    def test_missing_thread_and_missing_rollout_are_reported(self):
        os.unlink(self.rollout)
        self.before = tree_fingerprint(self.source)
        result = codex_home.migrate_threads(
            thread_ids=["tid-1", "nope"], source=self.source,
            target=self.target, apply=True)
        reasons = {s["id"]: s["reason"] for s in result["skipped"]}
        self.assertEqual(reasons, {"tid-1": "rollout-missing", "nope": "not-found"})
        self.assert_source_untouched()

    def test_schema_version_mismatch_refuses(self):
        os.unlink(os.path.join(self.target, "state_5.sqlite"))
        make_state_db(os.path.join(self.target, "state_5.sqlite"), version=45)
        with self.assertRaises(codex_home.CodexHomeError):
            codex_home.migrate_threads(thread_ids=["tid-1"], source=self.source,
                                       target=self.target, apply=True)
        self.assert_source_untouched()

    def test_list_threads_is_read_only(self):
        rows = codex_home.list_threads(self.source, limit=10)
        self.assertEqual(sorted(r["id"] for r in rows), ["tid-1", "tid-2"])
        self.assertTrue(all(r["rollout_exists"] for r in rows))
        self.assert_source_untouched()

    def test_refuses_same_home(self):
        with self.assertRaises(codex_home.CodexHomeError):
            codex_home.migrate_threads(thread_ids=["tid-1"], source=self.source,
                                       target=self.source, apply=True)


class RealHomesUntouchedTest(unittest.TestCase):
    """最後一道安全帶:這個測試檔不准碰到真的 `~/.codex` / `~/.pocket`。

    (先前有測試檔覆寫過 `~/.pocket/openclaw.json`、寫過 production 的
    `agent-registry.db`,所以這裡明著驗一次。)

    注意不能拿「目錄 mtime 沒變」當判準 —— 這台機器上 production bridge
    和 ChatGPT 桌面 app 都在同時寫這兩個目錄,那種斷言天生會偽紅。
    改驗**只有我們會留下的足跡**:預設隔離家目錄、以及 bootstrap 的 marker。
    """

    def test_default_isolated_home_was_never_created(self):
        self.assertFalse(
            os.path.exists(os.path.expanduser(codex_home.DEFAULT_ISOLATED_HOME)),
            "測試把真的 ~/.pocket/codex-home 生出來了")

    def test_no_bootstrap_marker_leaked_into_real_homes(self):
        for root in (REAL_CODEX, REAL_POCKET):
            self.assertFalse(
                os.path.exists(os.path.join(root, codex_home.CONFIG_ORIGIN_MARKER)),
                f"bootstrap 寫進真的家目錄了:{root}")

    def test_env_guard_keeps_every_lookup_inside_tmp(self):
        self.assertTrue(codex_home.isolated_home().startswith(_TMP))
        self.assertTrue(codex_home.shared_home().startswith(_TMP))


if __name__ == "__main__":
    unittest.main()
