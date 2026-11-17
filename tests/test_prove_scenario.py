"""釘住 prove 腳本自己 —— 尤其是「base 沒失敗就要報錯」那一條。

這支測的不是任何 scenario,是**證明制本身**。整套制度的價值全壓在一句話上:
「base 上沒紅的 scenario 不算證據」。那句話只要失效(腳本把離開碼判錯、把
scenario 自己壞掉當成 bug 重現),prove 就退化成一台蓋 PROVEN 章的機器,
我們等於用一個更精緻的方式重複 2026-10-28 那七次「代理指標說謊」。

三個案例:
  ① 永遠綠的假 scenario      → 必須擋下來(base 沒失敗)
  ② 在 base 上自己炸掉的      → 必須擋下來(非 0,但不是觀測結果)
  ③ 真的 base 紅 / head 綠    → 必須印 PROVEN
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "prove-scenario.sh")

# ③ 用的真實 commit:`1cc59e5` 把 Console「不連號就 resync」那行拿掉。
FIX_SHA = "1cc59e5"
BUGGY_LINE = "if (seq !== state.latestSeq + 1)"

ALWAYS_PASSES = '''\
import _scenario
import unittest


class Fake(unittest.TestCase):
    def test_nothing_real(self):
        self.assertTrue(True)


if __name__ == "__main__":
    _scenario.main()
'''

BLOWS_UP = '''\
import _scenario
import unittest


class Fake(unittest.TestCase):
    def setUp(self):
        raise RuntimeError("替身沒接上")

    def test_never_runs(self):
        self.fail("跑不到這裡")


if __name__ == "__main__":
    _scenario.main()
'''

SOURCE_LEVEL = '''\
import _scenario
import os
import unittest

BUGGY_LINE = %r


class Fake(unittest.TestCase):
    def test_console_does_not_demand_contiguous_seq(self):
        path = os.path.join(_scenario.REPO, "static", "console", "app.js")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn(BUGGY_LINE, src, "Console 還在要求序號連號")


if __name__ == "__main__":
    _scenario.main()
''' % BUGGY_LINE


def _missing() -> str:
    """這台機器/這份 clone 缺了什麼。沿用 exit 77 的慣例,**不假裝綠**。"""
    if not shutil.which("git"):
        return "要有 git 才建得起暫存 worktree"
    if subprocess.run(["git", "-C", REPO, "rev-parse", "--verify", FIX_SHA],
                      capture_output=True).returncode:
        return f"這份 clone 裡沒有 {FIX_SHA}(淺 clone?)—— 證明制驗不起來"
    return ""


_MISSING = _missing()
if _MISSING:
    if __name__ == "__main__":
        print("SKIP: " + _MISSING)
        raise SystemExit(77)
    raise unittest.SkipTest(_MISSING)


class ProveScenarioGate(unittest.TestCase):
    def _run(self, body: str, base: str, head: str):
        """把一個假 scenario 放進暫存 scenarios 目錄,跑 prove 腳本。"""
        work = tempfile.mkdtemp(prefix="prove-selftest-")
        self.addCleanup(shutil.rmtree, work, True)
        scen = os.path.join(work, "scenarios")
        os.makedirs(scen)
        for support in ("_scenario.py", "_faketmux.py"):
            shutil.copy(os.path.join(REPO, "scenarios", support), scen)
        with open(os.path.join(scen, "selftest.py"), "w", encoding="utf-8") as f:
            f.write(body)
        env = dict(os.environ, PROVE_SCENARIO_DIR=scen, PROVE_HEAD_REF=head,
                   PYTHON=sys.executable)
        return subprocess.run([SCRIPT, "selftest", base], cwd=REPO, env=env,
                              capture_output=True, text=True, timeout=300)

    def test_scenario_that_passes_on_base_is_rejected(self):
        """**本檔的主角**:base 沒失敗 = 這個 scenario 沒捕捉到任何 bug。"""
        res = self._run(ALWAYS_PASSES, FIX_SHA + "^", FIX_SHA)
        self.assertNotEqual(res.returncode, 0,
                            "永遠綠的假 scenario 居然被接受了\n" + res.stdout)
        self.assertNotIn("PROVEN  selftest", res.stdout)
        self.assertIn("NOT PROVEN", res.stdout)
        self.assertIn("沒有失敗", res.stdout, res.stdout)
        # 第二步根本不該開跑 —— base 沒紅就沒有「證明」可談。
        self.assertNotIn("② head", res.stdout, res.stdout)

    def test_scenario_that_errors_on_base_is_rejected(self):
        """非 0 不等於證據:自己炸掉的 scenario 不准算 bug 重現。"""
        res = self._run(BLOWS_UP, FIX_SHA + "^", FIX_SHA)
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertNotIn("PROVEN  selftest", res.stdout)
        self.assertIn("NOT PROVEN", res.stdout)
        self.assertIn("SCENARIO_ERROR", res.stdout, res.stdout)

    def test_real_base_fails_head_passes_is_proven(self):
        """正路要走得通,否則上面兩條只是「什麼都擋」。"""
        res = self._run(SOURCE_LEVEL, FIX_SHA + "^", FIX_SHA)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("PROVEN  selftest", res.stdout)

    def test_missing_scenario_is_a_usage_error(self):
        res = subprocess.run([SCRIPT, "no-such-scenario", "HEAD"], cwd=REPO,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)


if __name__ == "__main__":
    unittest.main()
