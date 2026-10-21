"""原生 CC session 管理:沒有 ccsess 的機器也要能用。

## 為什麼

bridge 建立 Claude Code session 原本一律 shell out 到 `~/.local/bin/ccsess`
—— 一支只存在維護者機器上的 1216 行 zsh。照官網流程裝完的人從手機建 session
一律 502,而錯誤訊息只說「ccsess failed」,看不出是**整支程式不存在**。

解法不是把那支腳本收進公開 repo(它的厚度是維護者個人艦隊用的),而是
bridge 自己實作它真正用到的六個動詞,並且**優先沿用既有的 ccsess** ——
維護者機器行為零變化,其他人走原生。

這支釘住的就是那個分叉點,以及原生實作裡**會安靜壞掉**的幾個地方:
 · 旗標映射:壞值只能跳過那一項,不准擋住啟動
 · hooks:必須是合併不是覆寫(那是使用者的設定檔),壞 JSON 一律不碰
 · 同 workdir 兩條 session:必須擋(會互搶 `claude --continue` 的對話歷史)
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import

import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

import bridge


class ResolverPicksExistingCcsess(unittest.TestCase):
    def test_env_pointing_at_missing_file_means_native(self):
        """`CCSESS_BIN` 指到不存在的檔 → None(走原生),而不是退回去猜別的路徑。
        明示就該照明示的算,否則「我明明指定了」會變成最難查的那種問題。"""
        with mock.patch.dict(os.environ, {"CCSESS_BIN": "/nope/ccsess"}):
            self.assertIsNone(bridge._resolve_ccsess())

    def test_env_pointing_at_executable_wins(self):
        with tempfile.TemporaryDirectory() as td:
            fake = os.path.join(td, "ccsess")
            with open(fake, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(fake, 0o755)
            with mock.patch.dict(os.environ, {"CCSESS_BIN": fake}):
                self.assertEqual(bridge._resolve_ccsess(), fake)


class SpawnFlagMapping(unittest.TestCase):
    def _write(self, td, name, cfg):
        os.makedirs(td, exist_ok=True)
        with open(os.path.join(td, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def test_known_values_map_to_flags(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, "s", {"model": "opus", "effort": "high",
                                  "permission_mode": "plan",
                                  "fallback_model": "sonnet"})
            with mock.patch.object(bridge, "CCSESS_SPAWN_DIR", td):
                argv = bridge._cc_spawn_flags("s", model_done=False)
        self.assertIn("--model", argv)
        self.assertIn("opus", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertEqual(argv[argv.index("--fallback-model") + 1], "sonnet")

    def test_model_not_duplicated_when_pin_already_applied(self):
        """model 已由既有 model pin 套用時不可重複下 —— 同一個旗標出現兩次,
        claude 的行為就不是我們能預測的了。"""
        with tempfile.TemporaryDirectory() as td:
            self._write(td, "s", {"model": "opus"})
            with mock.patch.object(bridge, "CCSESS_SPAWN_DIR", td):
                self.assertEqual(bridge._cc_spawn_flags("s", model_done=True), [])

    def test_bad_values_skip_only_that_flag(self):
        """**寬容姿態是刻意的**:一個打錯的 effort 不該讓人連 session 都開不起來。"""
        with tempfile.TemporaryDirectory() as td:
            self._write(td, "s", {"effort": "turbo",          # 無效
                                  "permission_mode": "nope",  # 無效
                                  "max_budget_usd": -3,       # 無效
                                  "model": "opus"})           # 有效
            with mock.patch.object(bridge, "CCSESS_SPAWN_DIR", td):
                argv = bridge._cc_spawn_flags("s", model_done=False)
        self.assertEqual(argv, ["--model", "opus"])

    def test_broken_json_is_ignored_not_fatal(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "s.json"), "w") as f:
                f.write("{not json")
            with mock.patch.object(bridge, "CCSESS_SPAWN_DIR", td):
                self.assertEqual(bridge._cc_spawn_flags("s", model_done=False), [])

    def test_overlong_append_prompt_skipped(self):
        """send-keys 是逐字打進 pane,超長 prompt 不走這條路。"""
        with tempfile.TemporaryDirectory() as td:
            self._write(td, "s", {"append_system_prompt": "x" * 9000})
            with mock.patch.object(bridge, "CCSESS_SPAWN_DIR", td):
                self.assertEqual(bridge._cc_spawn_flags("s", model_done=False), [])


class HooksAreMergedNotOverwritten(unittest.TestCase):
    def test_existing_user_settings_survive(self):
        """那個 settings.json 是**使用者的**。只准加我們的 hook,其餘一字不動。"""
        with tempfile.TemporaryDirectory() as wd:
            os.makedirs(os.path.join(wd, ".claude"))
            path = os.path.join(wd, ".claude", "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"theme": "dark",
                           "hooks": {"Stop": [{"hooks": [{"type": "command",
                                                          "command": "echo mine"}]}]}}, f)
            bridge._cc_native_hooks(wd)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        self.assertEqual(data["theme"], "dark")                      # 沒被動到
        dumped = json.dumps(data, ensure_ascii=False)
        self.assertIn("echo mine", dumped)                           # 使用者的 hook 還在
        self.assertIn("/ccsessions/_hook", dumped)                   # 我們的加進去了
        self.assertIn("UserPromptSubmit", data["hooks"])

    def test_running_twice_does_not_duplicate(self):
        """`ensure` 會反覆跑。少了去重,那個檔案會越疊越長。"""
        with tempfile.TemporaryDirectory() as wd:
            bridge._cc_native_hooks(wd)
            bridge._cc_native_hooks(wd)
            with open(os.path.join(wd, ".claude", "settings.json"), encoding="utf-8") as f:
                data = json.load(f)
        self.assertEqual(len(data["hooks"]["Stop"]), 1)
        self.assertEqual(len(data["hooks"]["UserPromptSubmit"]), 1)

    def test_broken_settings_file_is_left_alone(self):
        """壞 JSON:寧可沒有 hook,也不要把人家的設定檔寫壞。"""
        with tempfile.TemporaryDirectory() as wd:
            os.makedirs(os.path.join(wd, ".claude"))
            path = os.path.join(wd, ".claude", "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{ broken")
            bridge._cc_native_hooks(wd)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "{ broken")


class NativeVerbs(unittest.TestCase):
    def test_same_workdir_twice_is_refused(self):
        """兩條 session 共用一個目錄會因為 `claude --continue` 按 workdir 接續而
        **互搶同一條對話歷史**(2026-07-12 實害)。必須擋。"""
        rows = [("old", "/tmp/projA", "1")]
        with mock.patch.object(bridge, "_cc_conf_rows", lambda: rows):
            with self.assertRaises(Exception) as cm:
                asyncio.run(bridge._cc_native("register", "new", "/tmp/projA"))
        self.assertIn("WORKDIR_TAKEN", str(getattr(cm.exception, "code", "")))

    def test_unsupported_verb_says_so_clearly(self):
        """原生只做六個動詞。遇到別的要講清楚是『沒實作』,而不是假裝成
        tmux 失敗 —— 後者會害人往完全錯的方向查。"""
        with self.assertRaises(Exception) as cm:
            asyncio.run(bridge._cc_native("compress", "x"))
        self.assertEqual(getattr(cm.exception, "code", None), "CCSESS_VERB_UNSUPPORTED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
