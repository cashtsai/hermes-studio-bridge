"""全機發現與收編(SUBPROCESS_HARNESS_DESIGN §2.3)。

涵蓋:tmux/ps 解析(含 pane_current_command 是版本字串的真實案例)、
managed vs discovered 判定、cx 來源分類、收編寫 ccsess 名單(冪等/保留
格式與註解/先備份)、**收編絕不產生 kill/send-keys**、registry 收編/釋放
狀態轉換、單一 provider 掛掉只降級不 500、api key 只回報有無。

⚠️ 這裡的 ccsess 設定檔一律指向 tmp,**絕不碰真的
`~/.config/ccsess/sessions.conf`**(那是活的常駐名單)。
"""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="host-discovery-test-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("POCKET_REGISTRY_DB", os.path.join(_TMP, "registry.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_registry  # noqa: E402
import bridge  # noqa: E402
import host_discovery as hd  # noqa: E402


# 真機 `tmux list-panes -a -F TMUX_PANE_FORMAT` 的實際輸出形狀。
# 注意第二欄:**是版本字串 `2.1.207`,不是 `claude`** —— 這就是不能靠
# pane_current_command 認 agent 的原因。
TMUX_FIXTURE = "\n".join([
    "FLiPER|2.1.207|2722|/Users/xcash/apps/fliper-mobile|1786355258",
    "amulet-hunter|2.1.207|6756|/Users/xcash/apps/amulet-hunter|1786355516",
    "sidequest|zsh|9001|/Users/xcash/apps/sidequest|1786355600",
    "just-a-shell|zsh|9500|/Users/xcash|1786355700",
    "",
])

PS_FIXTURE = {
    1: (0, "/sbin/launchd"),
    2721: (1, "tmux: server"),
    2722: (2721, "/bin/bash --noprofile --norc"),
    2723: (2722, "/Users/xcash/.local/bin/claude --resume "
                 "69fd4b82-050d-4191-8251-3537a037f74e --remote-control FLiPER "
                 "--model opus --debug-file /tmp/rc-fliper.log"),
    6756: (2721, "/bin/bash --noprofile --norc"),
    6757: (6756, "-zsh"),
    6758: (6757, "/Users/xcash/.local/bin/claude --remote-control amulet-hunter"),
    9001: (2721, "/bin/zsh"),
    9002: (9001, "node /Users/xcash/.nvm/versions/node/v22/lib/node_modules/"
                 "@anthropic-ai/claude-code/cli.js --model sonnet"),
    9500: (2721, "/bin/zsh"),
    9501: (9500, "tail -f /Users/xcash/.claude/claude.log"),
}

# 名單裡有 FLiPER(啟用)與 amulet-hunter(封存,enabled=0);sidequest 完全
# 不在名單裡 = 使用者自己 tmux 開的。
CONF_FIXTURE = """\
# ccsess 常駐 Claude Code remote session 設定
# 一行一個 session。欄位以 | 分隔: name | workdir | enabled

FLiPER|/Users/xcash/apps/fliper-mobile|1
amulet-hunter|/Users/xcash/apps/amulet-hunter|0
xw-old-lane-20260711|/Users/xcash/apps/pocket-connect|0
"""


def _claude_by_pane(panes, procs):
    kids = hd.build_child_map(procs)
    out = {}
    for p in panes:
        hit = hd.find_agent_proc(p["pane_pid"], procs, kids, hd.is_claude_cmdline)
        if hit:
            out[p["pane_pid"]] = {"pid": hit[0], "cmdline": hit[1]}
    return out


def _fresh_registry(**kw):
    path = tempfile.mktemp(suffix=".db", dir=_TMP)
    defaults = dict(task_ttl=100.0, ephemeral_ttl=50.0, max_children=3,
                    task_cap=12, max_depth=2, idle_secs=10.0)
    defaults.update(kw)
    return agent_registry.AgentRegistry(path, **defaults)


class FakeRequest:
    def __init__(self, body=None):
        self._body = body or {}
        self.headers = {"authorization": "Bearer test-unit-token"}

    async def json(self):
        return self._body


# ───────────────────── 1. tmux / ps 解析 ─────────────────────

class TestPaneParsing(unittest.TestCase):
    def test_parse_tmux_panes_fields(self):
        panes = hd.parse_tmux_panes(TMUX_FIXTURE)
        self.assertEqual(len(panes), 4)
        first = panes[0]
        self.assertEqual(first["session"], "FLiPER")
        self.assertEqual(first["command"], "2.1.207")   # 版本字串,不是 claude
        self.assertEqual(first["pane_pid"], 2722)
        self.assertEqual(first["path"], "/Users/xcash/apps/fliper-mobile")
        self.assertEqual(first["created_ts"], 1786355258.0)

    def test_path_with_pipe_is_preserved(self):
        out = "weird|2.1.207|42|/Users/xcash/a|b/c|1786355258"
        pane = hd.parse_tmux_panes(out)[0]
        self.assertEqual(pane["path"], "/Users/xcash/a|b/c")
        self.assertEqual(pane["created_ts"], 1786355258.0)

    def test_rows_without_pid_are_dropped(self):
        self.assertEqual(hd.parse_tmux_panes("bad|zsh|notapid|/tmp|1"), [])
        self.assertEqual(hd.parse_tmux_panes("too|few|fields"), [])
        self.assertEqual(hd.parse_tmux_panes(""), [])

    def test_missing_created_column_is_tolerated(self):
        pane = hd.parse_tmux_panes("s|zsh|7|/Users/xcash")[0]
        self.assertIsNone(pane["created_ts"])
        self.assertEqual(pane["path"], "/Users/xcash")

    def test_claude_detected_through_process_tree_not_command_name(self):
        """pane_current_command 是 `2.1.207`,只有走行程樹才認得出 claude。"""
        panes = hd.parse_tmux_panes(TMUX_FIXTURE)
        found = _claude_by_pane(panes, PS_FIXTURE)
        self.assertEqual(sorted(found), [2722, 6756, 9001])
        self.assertIn("--remote-control FLiPER", found[2722]["cmdline"])
        # 隔一層 zsh 也要找得到
        self.assertEqual(found[6756]["pid"], 6758)
        # node 包一層的 cli.js 形狀
        self.assertEqual(found[9001]["pid"], 9002)
        # `tail -f claude.log` 這種旁觀者不算 agent
        self.assertNotIn(9500, found)

    def test_is_claude_cmdline_edge_cases(self):
        self.assertTrue(hd.is_claude_cmdline("/Users/x/.local/bin/claude --model opus"))
        self.assertTrue(hd.is_claude_cmdline("claude"))
        self.assertTrue(hd.is_claude_cmdline("node /x/@anthropic-ai/claude-code/cli.js"))
        self.assertFalse(hd.is_claude_cmdline("tail -f /var/log/claude.log"))
        self.assertFalse(hd.is_claude_cmdline("grep claude bridge.py"))
        self.assertFalse(hd.is_claude_cmdline("node /x/server.js --claude"))
        self.assertFalse(hd.is_claude_cmdline(""))

    def test_descendants_survives_a_cycle(self):
        procs = {10: (11, "a"), 11: (10, "b")}
        kids = hd.build_child_map(procs)
        self.assertEqual(sorted(hd.descendants(10, kids)), [10, 11])

    def test_parse_cc_cmdline(self):
        got = hd.parse_cc_cmdline(PS_FIXTURE[2723][1])
        self.assertEqual(got["model"], "opus")
        self.assertEqual(got["remote_control"], "FLiPER")
        self.assertEqual(got["session_id"], "69fd4b82-050d-4191-8251-3537a037f74e")
        self.assertNotIn("permission_mode", got)
        self.assertEqual(
            hd.parse_cc_cmdline("claude --permission-mode=acceptEdits"
                                " --model=sonnet")["permission_mode"],
            "acceptEdits")


# ─────────────── 2. managed vs discovered 判定 ───────────────

class TestCcClassification(unittest.TestCase):
    def _items(self, api=None):
        panes = hd.parse_tmux_panes(TMUX_FIXTURE)
        return {i["id"]: i for i in hd.cc_discovery_items(
            panes, hd.parse_conf_rows(CONF_FIXTURE),
            _claude_by_pane(panes, PS_FIXTURE), api_key_by_pid=api)}

    def test_conf_enabled_is_managed(self):
        it = self._items()["claude_code:FLiPER"]
        self.assertEqual(it["state"], hd.STATE_MANAGED)
        self.assertEqual(it["source"], "ccsess")
        self.assertEqual(it["workdir"], "/Users/xcash/apps/fliper-mobile")
        self.assertEqual(it["model"], "opus")
        self.assertEqual(it["since_ts"], 1786355258.0)
        self.assertTrue(it["alive"])

    def test_user_opened_tmux_pane_is_discovered(self):
        it = self._items()["claude_code:sidequest"]
        self.assertEqual(it["state"], hd.STATE_DISCOVERED)
        self.assertEqual(it["source"], "tmux")
        self.assertEqual(it["workdir"], "/Users/xcash/apps/sidequest")
        self.assertEqual(it["model"], "sonnet")

    def test_archived_conf_lane_still_alive_is_discovered(self):
        it = self._items()["claude_code:amulet-hunter"]
        self.assertEqual(it["state"], hd.STATE_DISCOVERED)
        self.assertEqual(it["source"], "ccsess-disabled")

    def test_plain_shell_session_is_not_an_agent(self):
        self.assertNotIn("claude_code:just-a-shell", self._items())

    def test_conf_lane_without_live_pane_is_not_invented(self):
        """名單裡有、但機器上沒有 pane 的 lane 不會憑空出現在發現面。"""
        self.assertNotIn("claude_code:xw-old-lane-20260711", self._items())

    def test_multi_pane_session_prefers_the_pane_running_claude(self):
        out = ("dual|zsh|700|/Users/xcash/shell|1786355000\n"
               "dual|2.1.207|800|/Users/xcash/work|1786355000\n")
        procs = {700: (1, "/bin/zsh"), 800: (1, "/bin/bash"),
                 801: (800, "/Users/xcash/.local/bin/claude --model haiku")}
        panes = hd.parse_tmux_panes(out)
        items = hd.cc_discovery_items(panes, [], _claude_by_pane(panes, procs))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["pane_pid"], 800)
        self.assertEqual(items[0]["model"], "haiku")

    def test_adopting_never_rewrites_an_existing_lane_workdir(self):
        """名單 workdir 是權威;pane 當下 cd 到別處不會污染它。"""
        out = "FLiPER|2.1.207|2722|/Users/xcash/apps/fliper-mobile/deep|1786355258"
        panes = hd.parse_tmux_panes(out)
        items = hd.cc_discovery_items(panes, hd.parse_conf_rows(CONF_FIXTURE),
                                      _claude_by_pane(panes, PS_FIXTURE))
        self.assertEqual(items[0]["workdir"], "/Users/xcash/apps/fliper-mobile")


# ─────────────── 3. cx / hermes / openclaw 來源分類 ───────────────

class TestOtherProviders(unittest.TestCase):
    SUMMARIES = [
        {"thread_id": "t-cli", "name": "本機 CLI", "source": "cli",
         "workdir": "/Users/xcash/apps/a", "status": "running",
         "updatedAt": 1786355258000},
        {"thread_id": "t-vscode", "name": "VSCode", "source": "vscode",
         "status": "idle"},
        {"thread_id": "t-app", "name": "Bridge 開的", "source": "appServer",
         "status": "idle", "modelProvider": "openai", "model": "gpt-5.5"},
        {"thread_id": "", "name": "沒有 id 的不要"},
    ]

    def test_bridge_created_threads_are_managed_rest_discovered(self):
        items = {i["id"]: i for i in
                 hd.cx_discovery_items(self.SUMMARIES, {"codex:t-app"})}
        self.assertEqual(len(items), 3)             # 空 id 被丟掉
        self.assertEqual(items["codex:t-app"]["state"], hd.STATE_MANAGED)
        self.assertEqual(items["codex:t-cli"]["state"], hd.STATE_DISCOVERED)
        self.assertEqual(items["codex:t-vscode"]["state"], hd.STATE_DISCOVERED)

    def test_source_kind_and_busy_survive(self):
        items = {i["id"]: i for i in hd.cx_discovery_items(self.SUMMARIES, set())}
        self.assertEqual(items["codex:t-cli"]["source"], "cli")
        self.assertEqual(items["codex:t-vscode"]["source"], "vscode")
        self.assertEqual(items["codex:t-app"]["source"], "appServer")
        self.assertTrue(items["codex:t-cli"]["busy"])
        self.assertFalse(items["codex:t-app"]["busy"])
        self.assertEqual(items["codex:t-cli"]["since_ts"], 1786355258.0)  # ms→s
        self.assertEqual(items["codex:t-app"]["model"], "gpt-5.5")
        self.assertEqual(items["codex:t-app"]["model_provider"], "openai")

    def test_hermes_personas_are_always_managed(self):
        items = hd.hermes_discovery_items([("yuanfang", "袁方"), ("xcash", "XCash")])
        self.assertEqual([i["id"] for i in items],
                         ["hermes:yuanfang", "hermes:xcash"])
        self.assertTrue(all(i["state"] == hd.STATE_MANAGED for i in items))

    def test_dispatch_subsessions_are_managed(self):
        items = hd.dispatch_discovery_items({
            "sub-1": {"name": "cc-hand", "parent": "hermes:yuanfang",
                      "status": "running", "worktree": "/tmp/wt",
                      "lastAt": 1786355258.0}})
        self.assertEqual(items[0]["state"], hd.STATE_MANAGED)
        self.assertEqual(items[0]["parent"], "hermes:yuanfang")
        self.assertTrue(items[0]["busy"])
        self.assertEqual(items[0]["workdir"], "/tmp/wt")

    def test_openclaw_rows_default_to_discovered(self):
        rows = [{"id": "openclaw:agent:main:main", "title": "main",
                 "subtitle": "qwen3-local", "status": "running",
                 "last_event_at": 1786355258.0},
                {"id": "openclaw:agent:main:side", "title": "side",
                 "status": "idle"}]
        items = {i["id"]: i for i in hd.openclaw_discovery_items(
            rows, {"openclaw:agent:main:side"})}
        self.assertEqual(items["openclaw:agent:main:main"]["state"],
                         hd.STATE_DISCOVERED)
        self.assertEqual(items["openclaw:agent:main:main"]["model"], "qwen3-local")
        self.assertTrue(items["openclaw:agent:main:main"]["busy"])
        self.assertEqual(items["openclaw:agent:main:side"]["state"],
                         hd.STATE_MANAGED)


# ─────────────── 4. api key 只回報有無 ───────────────

class TestApiKeyPresenceOnly(unittest.TestCase):
    BLOB = ("claude --model opus PATH=/usr/bin "
            "ANTHROPIC_API_KEY=sk-ant-SUPERSECRET123 TERM=xterm")

    def test_presence_detected(self):
        self.assertTrue(hd.env_blob_has_key(self.BLOB, ("ANTHROPIC_API_KEY",)))
        self.assertTrue(hd.env_blob_has_key(self.BLOB,
                                            ("NOPE", "ANTHROPIC_API_KEY")))

    def test_returns_bool_never_the_value(self):
        got = hd.env_blob_has_key(self.BLOB, ("ANTHROPIC_API_KEY",))
        self.assertIsInstance(got, bool)
        self.assertNotIn("SUPERSECRET", repr(got))

    def test_empty_value_is_not_a_key(self):
        self.assertFalse(hd.env_blob_has_key("A=1 ANTHROPIC_API_KEY= B=2",
                                             ("ANTHROPIC_API_KEY",)))

    def test_suffix_lookalike_is_not_a_match(self):
        self.assertFalse(hd.env_blob_has_key("MY_ANTHROPIC_API_KEY=x",
                                             ("ANTHROPIC_API_KEY",)))

    def test_missing_key_is_false(self):
        self.assertFalse(hd.env_blob_has_key("PATH=/usr/bin",
                                             ("ANTHROPIC_API_KEY",)))
        self.assertFalse(hd.env_blob_has_key("", ("ANTHROPIC_API_KEY",)))

    def test_item_payload_carries_only_the_flag(self):
        panes = hd.parse_tmux_panes(TMUX_FIXTURE)
        items = hd.cc_discovery_items(panes, hd.parse_conf_rows(CONF_FIXTURE),
                                      _claude_by_pane(panes, PS_FIXTURE),
                                      api_key_by_pid={2723: True, 6758: False})
        by_id = {i["id"]: i for i in items}
        self.assertIs(by_id["claude_code:FLiPER"]["has_api_key"], True)
        self.assertIs(by_id["claude_code:amulet-hunter"]["has_api_key"], False)
        self.assertNotIn("SUPERSECRET", repr(items))
        # cmdline 本身也不外流(只留解析出來的欄位)
        self.assertNotIn("cmdline", by_id["claude_code:FLiPER"])


# ─────────────── 5. ccsess 名單寫入(純文字轉換)───────────────

class TestConfTransforms(unittest.TestCase):
    def test_upsert_appends_and_preserves_comments(self):
        lines = CONF_FIXTURE.splitlines()
        out = hd.conf_upsert_lines(lines, "sidequest",
                                   "/Users/xcash/apps/sidequest", "1")
        self.assertEqual(out[:len(lines)], lines)      # 原本每一行原樣保留
        self.assertEqual(out[-1], "sidequest|/Users/xcash/apps/sidequest|1")

    def test_upsert_replaces_in_place_keeping_order(self):
        out = hd.conf_upsert_lines(CONF_FIXTURE.splitlines(), "amulet-hunter",
                                   "/Users/xcash/apps/amulet-hunter", "1")
        self.assertEqual(len(out), len(CONF_FIXTURE.splitlines()))
        self.assertIn("amulet-hunter|/Users/xcash/apps/amulet-hunter|1", out)
        self.assertNotIn("amulet-hunter|/Users/xcash/apps/amulet-hunter|0", out)

    def test_remove_lines(self):
        out, hit = hd.conf_remove_lines(CONF_FIXTURE.splitlines(), "FLiPER")
        self.assertTrue(hit)
        self.assertFalse(any(line.startswith("FLiPER|") for line in out))
        self.assertTrue(any(line.startswith("#") for line in out))
        _out2, hit2 = hd.conf_remove_lines(out, "FLiPER")
        self.assertFalse(hit2)

    def test_comment_lines_are_never_matched_as_rows(self):
        lines = ["# FLiPER|x|1", "FLiPER|/a|1"]
        out, _ = hd.conf_remove_lines(lines, "FLiPER")
        self.assertEqual(out, ["# FLiPER|x|1"])


class TestConfAdoptOnDisk(unittest.TestCase):
    """收編實際落地:**tmp 路徑**,絕不碰真的 sessions.conf。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=_TMP)
        self.conf = os.path.join(self.dir, "sessions.conf")
        with open(self.conf, "w", encoding="utf-8") as f:
            f.write(CONF_FIXTURE)
        self.patch = patch.object(bridge, "CCSESS_CONF", self.conf)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def _read(self):
        with open(self.conf, encoding="utf-8") as f:
            return f.read()

    def _backups(self):
        return [n for n in os.listdir(self.dir) if ".bak." in n]

    def test_adopt_appends_backs_up_and_preserves_format(self):
        before = self._read()
        self.assertTrue(bridge._cc_conf_adopt("sidequest",
                                              "/Users/xcash/apps/sidequest"))
        after = self._read()
        self.assertIn("sidequest|/Users/xcash/apps/sidequest|1", after)
        self.assertIn("# ccsess 常駐 Claude Code remote session 設定", after)
        self.assertTrue(after.startswith(before.rstrip() + "\n"))
        self.assertEqual(len(self._backups()), 1)
        with open(os.path.join(self.dir, self._backups()[0]), encoding="utf-8") as f:
            self.assertEqual(f.read(), before)

    def test_adopt_is_idempotent_and_second_call_does_not_write(self):
        bridge._cc_conf_adopt("sidequest", "/Users/xcash/apps/sidequest")
        after_first = self._read()
        self.assertFalse(bridge._cc_conf_adopt("sidequest",
                                               "/Users/xcash/apps/sidequest"))
        self.assertEqual(self._read(), after_first)
        self.assertEqual(len(self._backups()), 1)   # 沒動檔案就不再備份

    def test_adopt_reenables_archived_lane_without_touching_its_workdir(self):
        self.assertTrue(bridge._cc_conf_adopt("amulet-hunter", "/somewhere/else"))
        self.assertIn("amulet-hunter|/Users/xcash/apps/amulet-hunter|1",
                      self._read())
        self.assertNotIn("/somewhere/else", self._read())

    def test_release_removes_line_only_when_asked(self):
        self.assertTrue(bridge._cc_conf_release("FLiPER"))
        self.assertNotIn("FLiPER|", self._read())
        self.assertIn("# ccsess 常駐", self._read())
        self.assertFalse(bridge._cc_conf_release("FLiPER"))   # 冪等

    def test_conf_rows_roundtrip_after_adopt(self):
        bridge._cc_conf_adopt("sidequest", "/Users/xcash/apps/sidequest")
        rows = dict((n, (w, e)) for n, w, e in bridge._cc_conf_rows())
        self.assertEqual(rows["sidequest"], ("/Users/xcash/apps/sidequest", "1"))
        self.assertEqual(rows["FLiPER"][1], "1")


# ─────────────── 6. 收編絕不下 kill / send-keys ───────────────

class TestAdoptIsBookkeepingOnly(unittest.IsolatedAsyncioTestCase):
    """收編是記帳,不是重啟。攔住指令建構器,證明沒有任何破壞性 tmux 動作。"""

    FORBIDDEN = ("kill-session", "kill-pane", "kill-server", "send-keys",
                 "respawn-pane", "respawn-window", "kill", "new-session")

    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(dir=_TMP)
        self.conf = os.path.join(self.dir, "sessions.conf")
        with open(self.conf, "w", encoding="utf-8") as f:
            f.write(CONF_FIXTURE)
        self.tmux_calls = []
        self.exec_calls = []

        async def fake_tmux(*args, timeout=15.0):
            self.tmux_calls.append(list(args))
            if args[:2] == ("list-panes", "-a"):
                return 0, TMUX_FIXTURE, ""
            return 0, "", ""

        async def fake_ps():
            return dict(PS_FIXTURE)

        async def fake_exec(*args, **kw):
            self.exec_calls.append(list(args))
            raise AssertionError("收編不該 spawn 任何行程")

        self.reg = _fresh_registry()
        self._patches = [
            patch.object(bridge, "CCSESS_CONF", self.conf),
            patch.object(bridge, "REGISTRY", self.reg),
            patch.object(bridge, "_tmux_run", fake_tmux),
            patch.object(bridge, "_ps_snapshot", fake_ps),
            patch.object(bridge, "_proc_env_has_api_key",
                         lambda pids, names: _done({})),
            patch.object(bridge, "_discovery_cx_items",
                         lambda ids: _done([])),
            patch.object(bridge, "_discovery_openclaw_items",
                         lambda ids: _done([])),
            patch.object(bridge, "PERSONAS", {}),
            patch.object(bridge, "SUBSESSIONS", {}),
            patch.object(asyncio, "create_subprocess_exec", fake_exec),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        bridge._DISCOVERY_CACHE.update(ts=0.0, payload=None, refreshing=False)

    async def test_sweep_lists_managed_and_discovered(self):
        res = await bridge.v2_discovery(FakeRequest())
        by_id = {i["id"]: i for i in res["items"]}
        self.assertEqual(by_id["claude_code:FLiPER"]["state"], "managed")
        self.assertEqual(by_id["claude_code:sidequest"]["state"], "discovered")
        self.assertEqual(by_id["claude_code:amulet-hunter"]["state"], "discovered")
        self.assertEqual(res["counts"]["managed"], 1)
        self.assertEqual(res["counts"]["discovered"], 2)
        for call in self.tmux_calls:
            self.assertNotIn(call[0], self.FORBIDDEN)

    async def test_adopt_emits_no_destructive_tmux_command(self):
        res = await bridge.v2_discovery_adopt(
            "claude_code:sidequest",
            FakeRequest({"purpose": "側線實驗", "class": "task"}))
        self.assertTrue(res["ok"])
        self.assertTrue(res["conf_updated"])
        self.assertFalse(res["already_adopted"])
        self.assertEqual(res["session"]["purpose"], "側線實驗")
        self.assertEqual(res["session"]["class"], "task")
        self.assertTrue(res["session"]["registered"])
        # 一顆破壞性指令都沒有,連 spawn 都沒有
        self.assertEqual(self.exec_calls, [])
        for call in self.tmux_calls:
            self.assertNotIn(call[0], self.FORBIDDEN)
            self.assertFalse(any(bad in " ".join(str(a) for a in call)
                                 for bad in ("send-keys", "kill")))
        with open(self.conf, encoding="utf-8") as f:
            self.assertIn("sidequest|/Users/xcash/apps/sidequest|1", f.read())

    async def test_adopt_is_idempotent_second_call_is_200(self):
        first = await bridge.v2_discovery_adopt(
            "claude_code:sidequest", FakeRequest({"purpose": "側線實驗"}))
        second = await bridge.v2_discovery_adopt(
            "claude_code:sidequest", FakeRequest())
        self.assertFalse(first["already_adopted"])
        self.assertTrue(second["already_adopted"])
        self.assertFalse(second["conf_updated"])
        self.assertEqual(second["session"]["purpose"], "側線實驗")
        self.assertEqual(first["session"]["created_ts"],
                         second["session"]["created_ts"])

    async def test_unknown_id_is_404(self):
        with self.assertRaises(Exception) as ctx:
            await bridge.v2_discovery_adopt("claude_code:ghost", FakeRequest())
        self.assertEqual(getattr(ctx.exception, "status_code", None), 404)

    async def test_bad_class_is_400(self):
        with self.assertRaises(Exception) as ctx:
            await bridge.v2_discovery_adopt("claude_code:sidequest",
                                            FakeRequest({"class": "forever"}))
        self.assertEqual(getattr(ctx.exception, "status_code", None), 400)

    async def test_release_keeps_conf_by_default(self):
        await bridge.v2_discovery_adopt("claude_code:sidequest", FakeRequest())
        res = await bridge.v2_discovery_release("claude_code:sidequest",
                                                FakeRequest())
        self.assertTrue(res["released"])
        self.assertFalse(res["conf_removed"])
        with open(self.conf, encoding="utf-8") as f:
            self.assertIn("sidequest|", f.read())
        self.assertFalse(self.reg.get("claude_code:sidequest")["registered"])

    async def test_release_with_flag_removes_conf_line(self):
        await bridge.v2_discovery_adopt("claude_code:sidequest", FakeRequest())
        res = await bridge.v2_discovery_release(
            "claude_code:sidequest", FakeRequest({"remove_from_conf": True}))
        self.assertTrue(res["conf_removed"])
        with open(self.conf, encoding="utf-8") as f:
            self.assertNotIn("sidequest|", f.read())
        self.assertEqual(self.exec_calls, [])

    async def test_released_row_is_immune_to_the_reaper(self):
        await bridge.v2_discovery_adopt("claude_code:sidequest",
                                        FakeRequest({"class": "ephemeral"}))
        await bridge.v2_discovery_release("claude_code:sidequest", FakeRequest())
        row = self.reg.get("claude_code:sidequest")
        row["last_active_ts"] = 0.0        # 早就過期
        self.assertNotIn("claude_code:sidequest",
                         [c["id"] for c in self.reg.sweep_candidates()])

    async def test_discovered_rows_reach_the_registry_view(self):
        await bridge._discovery_sweep()          # 快取暖起來(app 也會 poll 它)
        rows = await bridge._registry_legacy_rows(set())
        by_id = {r["id"]: r for r in rows}
        self.assertIn("claude_code:sidequest", by_id)
        self.assertFalse(by_id["claude_code:sidequest"]["registered"])
        self.assertEqual(by_id["claude_code:sidequest"]["provider"],
                         "claude_code")

    async def test_registry_view_never_blocks_on_a_cold_sweep(self):
        """快取是冷的就先回空,背景補掃 —— /app/v2/registry 是高頻 poll,
        不能為了發現面卡住。"""
        slow = asyncio.Event()

        async def never_finishes(*a, **kw):
            await slow.wait()
            return []

        with patch.object(bridge, "_discovery_cc_items", never_finishes):
            rows = await asyncio.wait_for(bridge._registry_legacy_rows(set()), 1.0)
        self.assertNotIn("claude_code:sidequest", {r["id"] for r in rows})
        slow.set()
        await asyncio.sleep(0)
        bridge._DISCOVERY_CACHE["refreshing"] = False

    async def test_adopted_row_leaves_the_unregistered_section(self):
        await bridge._discovery_sweep()
        await bridge.v2_discovery_adopt("claude_code:sidequest", FakeRequest())
        known = {r["id"] for r in self.reg.list_rows(include_archived=True)}
        rows = await bridge._registry_legacy_rows(known)
        self.assertNotIn("claude_code:sidequest", {r["id"] for r in rows})


def _done(value):
    fut = asyncio.get_event_loop().create_future()
    fut.set_result(value)
    return fut


# ─────────────── 7. provider 掛掉只降級,不 500 ───────────────

class TestProviderDegradation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conf = os.path.join(tempfile.mkdtemp(dir=_TMP), "sessions.conf")
        with open(self.conf, "w", encoding="utf-8") as f:
            f.write(CONF_FIXTURE)

        async def boom(*a, **kw):
            raise RuntimeError("codex app-server unavailable")

        async def fake_tmux(*args, timeout=15.0):
            if args[:2] == ("list-panes", "-a"):
                return 0, TMUX_FIXTURE, ""
            return 0, "", ""

        async def fake_ps():
            return dict(PS_FIXTURE)

        self._patches = [
            patch.object(bridge, "CCSESS_CONF", self.conf),
            patch.object(bridge, "REGISTRY", _fresh_registry()),
            patch.object(bridge, "_tmux_run", fake_tmux),
            patch.object(bridge, "_ps_snapshot", fake_ps),
            patch.object(bridge, "_proc_env_has_api_key",
                         lambda pids, names: _done({})),
            patch.object(bridge, "_codex_v2_visible_threads", boom),
            patch.object(bridge, "_openclaw_v2_rows", boom),
            patch.object(bridge, "PERSONAS", {"yuanfang": ("袁方", "/tmp")}),
            patch.object(bridge, "SUBSESSIONS", {}),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        bridge._DISCOVERY_CACHE.update(ts=0.0, payload=None, refreshing=False)

    async def test_failing_cx_degrades_to_empty_slice(self):
        res = await bridge.v2_discovery(FakeRequest())
        self.assertFalse(res["providers"]["codex"]["ok"])
        self.assertEqual(res["providers"]["codex"]["count"], 0)
        self.assertTrue(res["providers"]["claude_code"]["ok"])
        self.assertEqual(res["providers"]["claude_code"]["count"], 3)
        self.assertIn("hermes:yuanfang", {i["id"] for i in res["items"]})

    async def test_dead_tmux_server_does_not_kill_the_sweep(self):
        async def dead_tmux(*args, timeout=15.0):
            return 1, "", "no server running"

        with patch.object(bridge, "_tmux_run", dead_tmux):
            bridge._DISCOVERY_CACHE["ts"] = 0.0
            res = await bridge.v2_discovery(FakeRequest())
        self.assertTrue(res["providers"]["claude_code"]["ok"])
        self.assertEqual(res["providers"]["claude_code"]["count"], 0)
        self.assertIn("hermes:yuanfang", {i["id"] for i in res["items"]})

    async def test_provider_filter(self):
        res = await bridge.v2_discovery(FakeRequest(), provider="cc")
        self.assertTrue(all(i["provider"] == "claude_code" for i in res["items"]))
        self.assertEqual(res["counts"]["total"], 3)


# ─────────────── 8. registry 收編/釋放狀態轉換 ───────────────

class TestRegistryAdoptRelease(unittest.TestCase):
    def test_adopt_creates_registered_row_without_quota_check(self):
        reg = _fresh_registry(task_cap=0)      # 配額全滿也要收得進來
        row = reg.adopt("claude_code:sidequest", provider="claude_code",
                        name="sidequest", purpose="側線", cls="task",
                        worktree="/Users/xcash/apps/sidequest")
        self.assertTrue(row["registered"])
        self.assertEqual(row["purpose"], "側線")
        self.assertEqual(row["ttl_secs"], 100.0)
        self.assertEqual(row["worktree"], "/Users/xcash/apps/sidequest")
        self.assertIn("adopted_ts", row["meta"])

    def test_adopt_promotes_an_unregistered_row_in_place(self):
        reg = _fresh_registry()
        reg.register("codex:t1", provider="codex", name="t1",
                     registered=False, enforce_quota=False)
        self.assertFalse(reg.get("codex:t1")["registered"])
        row = reg.adopt("codex:t1", provider="codex", purpose="收編")
        self.assertTrue(row["registered"])
        self.assertEqual(row["purpose"], "收編")
        self.assertEqual(row["ttl_secs"], 100.0)   # 轉正補上壽命

    def test_adopt_is_idempotent_and_keeps_birth_record(self):
        reg = _fresh_registry()
        a = reg.adopt("codex:t1", provider="codex", purpose="第一次")
        b = reg.adopt("codex:t1", provider="codex")
        self.assertEqual(a["created_ts"], b["created_ts"])
        self.assertEqual(b["purpose"], "第一次")
        self.assertEqual(b["class"], a["class"])

    def test_adopt_revives_an_archived_row(self):
        reg = _fresh_registry()
        reg.adopt("codex:t1", provider="codex")
        reg.archive("codex:t1", "sweep")
        self.assertEqual(reg.get("codex:t1")["state"], "archived")
        row = reg.adopt("codex:t1", provider="codex")
        self.assertEqual(row["state"], "active")
        self.assertIsNone(row["archived_ts"])

    def test_adopt_can_change_class(self):
        reg = _fresh_registry()
        reg.adopt("codex:t1", provider="codex", cls="task")
        row = reg.adopt("codex:t1", provider="codex", cls="persistent")
        self.assertEqual(row["class"], "persistent")
        self.assertIsNone(row["ttl_secs"])
        self.assertIsNone(reg.expires_ts(row))

    def test_release_sets_registered_false_and_keeps_the_row(self):
        reg = _fresh_registry()
        reg.adopt("codex:t1", provider="codex", purpose="收編過")
        row = reg.release("codex:t1")
        self.assertFalse(row["registered"])
        self.assertEqual(row["purpose"], "收編過")     # 歷史留著
        self.assertIsNotNone(reg.get("codex:t1"))

    def test_release_unknown_id_is_none(self):
        self.assertIsNone(_fresh_registry().release("codex:ghost"))

    def test_released_row_is_never_a_sweep_candidate(self):
        reg = _fresh_registry(idle_secs=0.0, ephemeral_ttl=0.0)
        reg.adopt("codex:t1", provider="codex", cls="ephemeral")
        self.assertIn("codex:t1", [c["id"] for c in reg.sweep_candidates()])
        reg.release("codex:t1")
        self.assertNotIn("codex:t1", [c["id"] for c in reg.sweep_candidates()])

    def test_adopt_then_release_then_adopt_round_trips(self):
        reg = _fresh_registry()
        reg.adopt("codex:t1", provider="codex", purpose="一")
        reg.release("codex:t1")
        row = reg.adopt("codex:t1", provider="codex", purpose="二")
        self.assertTrue(row["registered"])
        self.assertEqual(row["purpose"], "二")


if __name__ == "__main__":
    unittest.main()
