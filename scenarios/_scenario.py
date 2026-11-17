"""scenario 共用底座 —— 「base 失敗 / head 通過」證明制的執行層。

一個 scenario 不是單元測試:它是**一個使用者會踩到的現象**,寫成可執行的
形式,而且必須在修復前的 commit 上**真的紅**、在修復後的 commit 上**真的綠**。
沒有前半段的 scenario 不算 scenario —— 那只是一條宣稱自己有在驗東西的綠線
(本 repo 2026-10-28 記錄過七次「代理指標說謊」,這份底座就是治那個病的)。

## 三個離開碼(scripts/prove-scenario.sh 靠它們分辨真假)

    0   SCENARIO_PASSED   現象不存在 = 行為正確(head 必須是這個)
    1   SCENARIO_FAILED   現象重現了 = bug 在(base 必須是這個)
    2   SCENARIO_ERROR    跑不起來(import 爛了、替身沒接好)→ **不是觀測結果**
    77  SCENARIO_SKIPPED  這台機器缺東西(沿用 tests/_requires.py 的慣例)

2 與 1 一定要分開。`import bridge` 在舊 commit 上炸掉也是非 0 離開碼,但它
證明的是「scenario 自己壞了」,不是「bug 在」。把兩者壓成同一個碼,等於讓
prove 腳本接受一份假的證據 —— 正是這整套制度要防的事。

## 寫 scenario 的三條紀律

1. **只碰產品對外的介面**:HTTP 路由、CLI。不准 import 產品內部函式來斷言
   (`bridge._cc_survey_overlay` 在 base 上不存在 → AttributeError → 離開碼 2,
   底座會直接把它判成無效證據)。這條紀律不是風格,是**跨 commit 跑得動**的
   前提。
2. **狀態一律由產品自己造**:要有卡片就打 API 讓它長出卡片,不要手刻卡片
   JSON、不要寫 canonical.db。schema 會在 base/head 之間漂移,手刻的狀態
   在 base 上不是「舊行為」而是「無效輸入」。
3. **替身只替外面的世界**:tmux / node / Claude Code 的 transcript 檔
   —— 這些是產品的**外部依賴**,它們的介面(CLI 參數、檔案格式)跨版本穩定。
   產品自己的狀態不准替。唯一的例外是 `bridge.TMUX_BIN`(「tmux 執行檔在
   哪」這一個字串),因為它是模組常數、沒有 env 把手,而 base 上不可能有。

docs/SCENARIOS.md 有完整說明與「怎麼加一個」。
"""
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 測試隔離閂(2026-08-15 事故防線)—— 必須在 import bridge 之前。
sys.path.insert(0, os.path.join(REPO, "tests"))
import _isolation  # noqa: F401,E402

_TMP = tempfile.mkdtemp(prefix="scenario-")
# 刻意自己也設一次(不靠 _isolation 的清單):scenario 會在**舊 commit**
# 上跑,那邊的 _isolation 可能還沒收錄某個把手。setdefault 不搶已有的設定。
os.environ.setdefault("BRIDGE_TOKEN", "scenario-token")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("CCSESS_CONF", os.path.join(_TMP, "ccsess", "sessions.conf"))
os.environ.setdefault("POCKET_MEDIA_DIR", os.path.join(_TMP, "media-artifacts"))
os.environ.setdefault("POCKET_REGISTRY_DB", os.path.join(_TMP, "agent-registry.db"))

sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scenarios"))

SKIP_EXIT_CODE = 77
ERROR_EXIT_CODE = 2

_bridge = None


def bridge():
    """延後 import 產品 —— 失敗要變成 unittest error(離開碼 2)。

    在模組層 `import bridge` 的話,舊 commit 上的 import 失敗會變成一個裸
    traceback + 離開碼 1,長得跟「bug 重現了」一模一樣。那是假證據。
    """
    global _bridge
    if _bridge is None:
        import bridge as _b
        _bridge = _b
    return _bridge


def tmpdir(prefix: str) -> str:
    return tempfile.mkdtemp(prefix=prefix, dir=_TMP)


def auth_header() -> dict:
    """主權杖 —— 讀 `bridge.BRIDGE_TOKEN`(模組常數,import 當下定版)。"""
    return {"Authorization": "Bearer " + bridge().BRIDGE_TOKEN}


def http_client():
    """FastAPI TestClient:走真的 HTTP 語意(狀態碼/標頭),不綁通訊埠。"""
    from fastapi.testclient import TestClient
    return TestClient(bridge().app)


def reset_auth_throttle() -> None:
    """清掉 401 連續失敗節流(超過門檻會回 429 而不是 401)。"""
    b = bridge()
    try:
        with b._AUTH_LOCK:
            b._AUTH_FAILS_BY_CLIENT.clear()
    except Exception:  # noqa: BLE001
        pass


def register_cc_session(name: str, workdir: str) -> None:
    """把一條 CC session 放進**產品自己的常駐名單檔**(ccsess sessions.conf)。

    這個檔是使用者手寫的純文字名單、由 `host_discovery.parse_conf_rows` 解析,
    格式跨版本穩定,而且 `tests/_isolation.py` 已經把 `CCSESS_CONF` 導到 tmp
    (真檔在 `~/.config/ccsess`,是 audit hook 的紅線之一,寫不進去)。

    這是紀律 2 的**唯一明文例外**:它是環境設定,不是產品狀態 —— 卡片、附件、
    事件、送出收據一律還是要打 API 讓產品自己長出來。
    """
    conf = os.environ["CCSESS_CONF"]
    os.makedirs(os.path.dirname(conf), exist_ok=True)
    with open(conf, "a", encoding="utf-8") as f:
        f.write(f"{name}|{workdir}|1\n")


class FakeTmux:
    """安裝一隻真的假 tmux 執行檔,並把 `bridge.TMUX_BIN` 指過去。

    `TMUX_BIN` 是模組常數(`/opt/homebrew/bin/tmux` 存在就用絕對路徑),所以
    光改 PATH 在有 homebrew 的機器上沒用,而加 env 把手又只會存在於 head。
    改這一個字串是紀律 3 允許的唯一例外:它說的是「外面那支執行檔在哪」,
    不是任何產品狀態,而且 base 與 head 的語意完全相同。
    """

    def __init__(self):
        import _faketmux
        self.mod = _faketmux
        self.dir = tmpdir("faketmux-")
        self.state_path = os.path.join(self.dir, "state.json")
        self.mod.save({"sessions": {}, "buffers": {}, "log": []},
                      self.state_path)
        self.bin = os.path.join(self.dir, "tmux")
        with open(self.bin, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexec %s %s \"$@\"\n" % (
                sys.executable, os.path.join(REPO, "scenarios", "_faketmux.py")))
        os.chmod(self.bin, 0o755)
        os.environ["PA_FAKE_TMUX_STATE"] = self.state_path
        b = bridge()
        self._saved = b.TMUX_BIN
        b.TMUX_BIN = self.bin
        b._PANE_CACHE.clear()

    def stop(self) -> None:
        bridge().TMUX_BIN = self._saved
        bridge()._PANE_CACHE.clear()

    # ── 狀態 ──
    def add_session(self, name: str, *, transcript=None, survey: bool = False) -> None:
        state = self.mod.load(self.state_path)
        state["sessions"][name] = self.mod.new_session(transcript, survey)
        self.mod.save(state, self.state_path)

    def session(self, name: str) -> dict:
        return self.mod.load(self.state_path)["sessions"][name]

    def pane(self, name: str) -> str:
        return self.mod.render(self.session(name))

    def keys_sent(self, name: str) -> list:
        """所有打進這個 pane 的按鍵(扁平化)—— 「真的按了 0 / Enter」要看這個。"""
        keys = []
        for entry in self.mod.load(self.state_path).get("log", []):
            if entry[0] != "send-keys" or name not in entry:
                continue
            rest = entry[1:]
            keys += [a for i, a in enumerate(rest)
                     if not a.startswith("-") and rest[i - 1] not in ("-t", "-N")]
        return keys


# ─────────────────────────── 離開碼對映 ────────────────────────────────────

def _headline(tb: str) -> str:
    """從 traceback 撈出一行人看得懂的判決。

    不能只取最後一行:多行的 assert 訊息(或裡面夾著 node 的輸出)會讓最後
    一行變成 `}` 這種無意義的碎片,prove 腳本印出來的證據就廢了。
    """
    import re
    lines = [ln for ln in tb.strip().splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if re.match(r"^\w*(Error|Exception|Failure)\b", ln):
            return " ".join(x.strip() for x in lines[i:i + 3])[:400]
    return lines[-1].strip()[:400] if lines else ""


def main(verbosity: int = 2) -> None:
    """跑呼叫端模組裡的 scenario,把 unittest 結果轉成上面那四個離開碼。

    failures(assert 不成立)= 觀測到現象 → 1
    errors(例外)          = 跑不起來   → 2
    """
    module = sys.modules["__main__"]
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    result = unittest.TextTestRunner(verbosity=verbosity, stream=sys.stderr).run(suite)

    if result.errors:
        print("SCENARIO_ERROR: scenario 自己跑不起來 —— 這不是觀測結果,"
              "不能當成「bug 重現」", flush=True)
        for test, tb in result.errors:
            print(f"SCENARIO_ERROR:   {test}: {_headline(tb)}", flush=True)
        raise SystemExit(ERROR_EXIT_CODE)
    if result.failures:
        print("SCENARIO_FAILED: 使用者會踩到的現象重現了", flush=True)
        for test, tb in result.failures:
            print(f"SCENARIO_FAILED:   {test}: {_headline(tb)}", flush=True)
        raise SystemExit(1)
    if result.skipped and not result.testsRun - len(result.skipped):
        print("SCENARIO_SKIPPED: 這台機器缺了這個 scenario 需要的東西", flush=True)
        for test, why in result.skipped:
            print(f"SCENARIO_SKIPPED:   {test}: {why}", flush=True)
        raise SystemExit(SKIP_EXIT_CODE)
    if not result.testsRun:
        print("SCENARIO_ERROR: 這個檔裡沒有任何 scenario", flush=True)
        raise SystemExit(ERROR_EXIT_CODE)
    print("SCENARIO_PASSED: 現象不存在,行為正確", flush=True)
    raise SystemExit(0)
