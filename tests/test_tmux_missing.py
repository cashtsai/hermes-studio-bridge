"""缺 tmux 的行為(乾淨 Mac 預設沒有 tmux)。

背景:原生 CC session 管理(不依賴 ccsess)整個靠 tmux。以前缺 tmux 時
`create_subprocess_exec` 丟 FileNotFoundError、沒人接 —— 不只「建立 session」失敗,
所有碰 tmux 的端點(CC 列表、儀表板)都跟著炸,而且錯誤不透明。官網 FAQ 已經老實寫
「要裝 tmux」,但桌面端與安裝器都不檢查,所以 bridge 至少要給出明確、可行動的錯誤。

釘住的契約:
  1. _tmux_run / _tmux_run_stdin 找不到 tmux → 回 rc=127,**不丟例外**。
  2. 沒有自己接例外的呼叫者(如 _tmux_capture_cached)因此降級成空字串,而不是炸掉。
  3. 建立 session 缺 tmux → 424 TMUX_MISSING,hint 含可直接照做的指令。
  4. **而且在動設定檔之前就擋下**(否則名單已登記、session 起不來,之後每次 ensure
     都在失敗重試)。
  5. ensure 缺 tmux 時不炸、也不把整批卡死。
  6. 「tmux 沒裝」的事件只記一次,不隨每次輪詢刷 log。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(見 tests/_isolation.py)
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

MISSING = "/nonexistent/path/to/tmux"


class TmuxMissingBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bridge._TMUX_MISSING_LOGGED[0] = False
        # 每個測試自己的工作目錄。`_cc_native` 在檢查 tmux **之前**會先做「同工作目錄防呆」
        # (WORKDIR_TAKEN 409);session 名單檔在整個測試行程內共用,若各測試共用同一個
        # 目錄,先跑的測試佔住它,後面的測試就會撞 409 而不是我們要測的 TMUX_MISSING。
        self.wd = tempfile.mkdtemp(prefix="tmux-missing-wd-")
        self._p = patch.object(bridge, "TMUX_BIN", MISSING)
        self._p.start()
        self.addCleanup(self._p.stop)


class TmuxRunDegradesTest(TmuxMissingBase):
    async def test_run_returns_127_instead_of_raising(self):
        rc, out, err = await bridge._tmux_run("list-sessions")
        self.assertEqual(rc, 127)
        self.assertEqual(out, "")
        self.assertIn("tmux", err)

    async def test_run_stdin_returns_127_instead_of_raising(self):
        res = await bridge._tmux_run_stdin(["load-buffer", "-"], b"x")
        self.assertEqual(res[0], 127)

    async def test_uncaught_caller_degrades_instead_of_crashing(self):
        """_tmux_capture_cached 自己沒接例外 —— 修之前缺 tmux 會直接 FileNotFoundError
        (有 try/except 的 _tmux_alive 不行拿來測:它修之前也會回 False,沒有鑑別力)。"""
        bridge._PANE_CACHE.pop("no-such-session-xyz", None)
        self.assertEqual(await bridge._tmux_capture_cached("no-such-session-xyz"), "")

    async def test_missing_event_logged_only_once(self):
        events = []
        with patch.object(bridge, "_log_event", lambda e, **f: events.append(e)):
            for _ in range(5):
                await bridge._tmux_run("list-sessions")
        self.assertEqual(events.count("tmux_missing"), 1)

    def test_installed_probe(self):
        self.assertFalse(bridge._tmux_installed())
        with patch.object(bridge, "TMUX_BIN", "/bin/ls"):   # 任何存在且可執行的絕對路徑
            self.assertTrue(bridge._tmux_installed())


class CreateSessionTest(TmuxMissingBase):
    def _rows(self):
        return [n for n, _wd, _en in bridge._cc_conf_rows()]

    async def test_new_reports_tmux_missing_with_actionable_hint(self):
        with self.assertRaises(bridge.BridgeError) as cm:
            await bridge._cc_native("new", "probe-session", self.wd)
        e = cm.exception
        self.assertEqual(e.status_code, 424)
        self.assertEqual(e.code, "TMUX_MISSING")
        self.assertIn("brew install tmux", e.detail)   # 要講「下一步」,不是只重述失敗

    async def test_new_refuses_before_touching_the_session_list(self):
        before = self._rows()
        with self.assertRaises(bridge.BridgeError) as cm:
            await bridge._cc_native("new", "probe-session", self.wd)
        # 必須是 TMUX_MISSING 本身 —— 只斷言「丟了某個 BridgeError」會讓別種錯誤(例如
        # 409 WORKDIR_TAKEN)空洞地通過,測不到我們要釘的行為。
        self.assertEqual(cm.exception.code, "TMUX_MISSING")
        self.assertEqual(self._rows(), before)
        self.assertNotIn("probe-session", self._rows())

    async def test_register_does_not_need_tmux(self):
        """register 只登記、不啟動 —— 沒有 tmux 也該成功(之後裝好再 ensure)。"""
        await bridge._cc_native("register", "reg-only", self.wd)
        self.assertIn("reg-only", self._rows())

    async def test_ensure_survives_missing_tmux(self):
        await bridge._cc_native("register", "ens-probe", self.wd)
        started = await bridge._cc_native("ensure")
        self.assertEqual(started, "")   # 一條都起不來,但不炸、不卡死


if __name__ == "__main__":
    unittest.main()
