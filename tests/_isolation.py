"""測試隔離閂 —— 每個測試檔的**第一個 import**(2026-08-15 事故防線)。

為什麼有這個檔:2026-08-15 一起事故 —— agent 測試在沒蓋掉 env 的情況下
`import bridge`,模組層就 new 出 AgentRegistry(建構子會建表/migrate),
直接打在 production 的 `~/.pocket/agent-registry.db` 上,把 registry 表清掉。
同一類地雷(模組層常數 import 當下綁死)先前已經咬過 openclaw.json /
pocket-id-enrollment.json(見 PR #91)。單靠「每個測試檔自己記得設 env」
擋不住 —— 忘一次就是 production 事故。所以做成閂,兩層:

1. **env 層**:把所有會導向 production 路徑的 env 先 setdefault 到本行程
   專屬 tmp 目錄。用 setdefault 不用硬蓋 —— scripts/run-tests.sh 或測試檔
   自己已導到 tmp 的設定要繼續有效(閂是加法防禦,不是接管)。
2. **guard 層**:`sys.addaudithook` 攔 `open`(寫模式)與 `sqlite3.connect`,
   目標落在 production 前綴下就地 raise。env 層擋「忘了設」,guard 層擋
   「env 根本沒有把手」的硬編碼路徑(ACCOUNTS_DB / CLIENT_LOG /
   device-tokens…)與未來新增的路徑。audit hook 是行程級、每次 open 只多
   幾個字串比較,便宜到可以常開。

用法(機械式,每個 tests/test_*.py 第一個 import):

    import _isolation  # noqa: F401

注意:audit hook 裝上就拆不掉(CPython 設計如此),所以本檔只該被測試
import,絕不能被 bridge/production 程式 import。
"""
import os
import sys
import tempfile

# ── env 層:所有已知的 production 路徑把手,一次導到 tmp ────────────────
_TMP = tempfile.mkdtemp(prefix="bridge-test-isolation-")

_ENV_TMP_DEFAULTS = {
    # bridge.py
    "POCKET_CANON_DB":        os.path.join(_TMP, "canonical.db"),
    "POCKET_ACCOUNTS_DB":     os.path.join(_TMP, "accounts.db"),
    "POCKET_KANBAN_DB":       os.path.join(_TMP, "kanban.db"),
    "POCKET_MEDIA_DIR":       os.path.join(_TMP, "media-artifacts"),
    "POCKET_CX_PREVIEW_CACHE": os.path.join(_TMP, "cx-previews.json"),
    "POCKET_TUNNEL_URL_FILE": os.path.join(_TMP, "tunnel-url"),
    "PAIR_BOOT_CODE_FILE":    os.path.join(_TMP, "pair-boot-code"),
    "POCKET_ID_STATE_FILE":   os.path.join(_TMP, "pocket-id-enrollment.json"),
    # agent_registry.py(2026-08-15 事故的那顆:import bridge 就建表)
    "POCKET_REGISTRY_DB":     os.path.join(_TMP, "agent-registry.db"),
    # harness/store.py
    "HARNESS_DB":             os.path.join(_TMP, "harness.db"),
    # cc_sdk.py
    "CC_SDK_STATE":           os.path.join(_TMP, "cc-sdk-sessions.json"),
    # agent_call.py / agent_context.py(共用同一份政策檔)
    "AGENT_CALL_POLICY":      os.path.join(_TMP, "agent-call-policy.json"),
    # codex_home.py
    "POCKET_CODEX_HOME":      os.path.join(_TMP, "codex-home"),
    # openclaw_provider.py
    "OPENCLAW_CONFIG_FILE":   os.path.join(_TMP, "openclaw.json"),
    # studio_memory_mcp.py
    "STUDIO_MEMORY_HOME":     os.path.join(_TMP, "studio-memory-home"),
    # bridge.py HOME_ROOT:persona home 一律指到 tmp。不導的話,測試打到的
    # 端點會去讀 production 的 state.db(tool-error/harness 報告同步),把
    # 真機資料灌進測試 DB —— test_report_reader_api 的三個假紅就是這樣來的。
    "HERMES_HOME_ROOT":       os.path.join(_TMP, "hermes-home"),
    # bridge.py:ccsess 常駐名單(活的 production 檔,收編會改寫+備份)
    "CCSESS_CONF":            os.path.join(_TMP, "ccsess", "sessions.conf"),
}
for _k, _v in _ENV_TMP_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

# 目錄型的路徑先建好:有程式把它當 cwd / 直接 listdir(codex spawn 拿
# HOME_ROOT 當工作目錄,目錄不存在 spawn 會 FileNotFoundError)。
for _d in ("hermes-home", "codex-home", "media-artifacts", "ccsess",
           "studio-memory-home"):
    os.makedirs(os.path.join(_TMP, _d), exist_ok=True)


# ── guard 層:production 前綴下的寫入/建 DB 連線,就地 raise ─────────────
class ProductionWriteBlocked(RuntimeError):
    """測試行程試圖寫 production 路徑 —— 這永遠是 bug,不是可以吞掉的錯。"""


# 紅線前綴(repo CLAUDE.md):~/.pocket、canonical/accounts(pocket-agent)、
# hermes-agent 的 persona home(state.db 只准讀)、真的 ~/.codex、
# ccsess 常駐名單目錄(spawn/secret/resume 都是活的)。
# 同時比對 expanduser 原字串與 realpath 兩種形態,symlink 繞不過去。
#
# 注意:在**本檔 import 當下**就展開 ~(此時 HOME 還是真的)並凍結成
# PRODUCTION_ROOTS —— 有些測試檔之後會把 os.environ["HOME"] 改到 tmp,
# 閂的紅線不能跟著漂(test_isolation_latch 也要用同一份錨點)。
PRODUCTION_ROOTS = {
    "pocket":             os.path.expanduser("~/.pocket"),
    "pocket_agent_share": os.path.expanduser("~/.local/share/pocket-agent"),
    "hermes_home":        os.path.expanduser("~/apps/hermes-agent/home"),
    "codex":              os.path.expanduser("~/.codex"),
    "ccsess":             os.path.expanduser("~/.config/ccsess"),
}
_FORBIDDEN = []
for _e in PRODUCTION_ROOTS.values():
    _FORBIDDEN.append(_e + os.sep)
    _r = os.path.realpath(_e)
    if _r != _e:
        _FORBIDDEN.append(_r + os.sep)
_FORBIDDEN = tuple(_FORBIDDEN)


def _forbidden_hit(path) -> str | None:
    """path 落在紅線前綴下就回擊中的絕對路徑;安全(或看不懂)回 None。"""
    if not isinstance(path, (str, bytes, os.PathLike)):
        return None          # fd / None 等形態:不是路徑寫入,放行
    try:
        s = os.fsdecode(os.fspath(path))
    except Exception:  # noqa: BLE001 — 看不懂的路徑物件,guard 寧可放行也不能炸掉無辜 open
        return None
    if not s:
        return None
    a = os.path.abspath(os.path.expanduser(s))
    for pref in _FORBIDDEN:
        if a.startswith(pref) or a == pref[:-1]:
            return a
    # realpath 再比一次:防「tmp 裡的 symlink 指向 production」這種繞法
    ra = os.path.realpath(a)
    if ra != a:
        for pref in _FORBIDDEN:
            if ra.startswith(pref) or ra == pref[:-1]:
                return ra
    return None


# os.open 走 flags 不走 mode 字串:任何可能產生寫入/建檔的 flag 都算寫
_WRITE_FLAGS = (os.O_WRONLY | os.O_RDWR | os.O_CREAT
                | os.O_TRUNC | os.O_APPEND)


def _audit(event, args):
    if event == "open":
        # args = (path, mode, flags);io.open 給 mode 字串,os.open 給 flags
        path, mode = args[0], args[1]
        flags = args[2] if len(args) > 2 else 0
        if isinstance(mode, str):
            writing = any(c in mode for c in "wax+")
        else:
            writing = bool(flags & _WRITE_FLAGS)
        if not writing:
            return               # 唯讀放行:state.db 紅線本來就是「只准讀」
        hit = _forbidden_hit(path)
        if hit:
            raise ProductionWriteBlocked(
                f"測試試圖以寫模式開 production 路徑:{hit}"
                "(把它導到 tmp —— 見 tests/_isolation.py)")
    elif event == "sqlite3.connect":
        db = args[0] if args else None
        if not isinstance(db, (str, bytes, os.PathLike)):
            return
        s = os.fsdecode(os.fspath(db))
        if not s or s == ":memory:":
            return
        if s.startswith("file:"):
            if "mode=ro" in s:
                return           # 明確唯讀 URI:紅線允許(harness 讀法)
            s = s[5:].split("?", 1)[0]
            if s.startswith("//"):
                s = s[1:] if not s.startswith("///") else s[2:]
        # 非 ro 的 sqlite 連線一律當寫看待:光 connect+journal 就可能動到檔案,
        # 而 2026-08-15 事故正是「建構子 connect 就建表」。
        hit = _forbidden_hit(s)
        if hit:
            raise ProductionWriteBlocked(
                f"測試試圖對 production DB 開非唯讀 sqlite 連線:{hit}"
                "(導到 tmp,或明確用 file:...?mode=ro —— 見 tests/_isolation.py)")


sys.addaudithook(_audit)
