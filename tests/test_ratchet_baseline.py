"""違規棘輪的**自我測試** —— 新債必須紅、舊債必須收斂、掃不到檔必須紅。

## 為什麼閘門自己需要測試

`scripts/ratchet/` 的承諾只有一句:**舊債凍結(不擋),新債擋死。**
那句話有三個可以安靜地失效的方式:

  1. 新違規沒被判成新 → 閘門形同不存在;
  2. 舊債被判成新 → 整份 baseline 變紅,制度三天內被放棄;
  3. 掃不到檔 → 0 個新違規 → **「沒有新債」地綠**(這是掃描式守門的通病,
     所以棘輪自己也掛了語料下限哨兵)。

抄的紀律同 `tests/test_corpus_sentinel.py`:
**A guard only guards if the regression fails it.**(研究報告 §5.3)

## 怎麼測

大部分測試跑在**暫存的假 repo** 上(沒有 `.git`,引擎會退回 `os.walk`),
這樣可以隨意新增/移除違規而不碰真 repo。
最後一組跑在**真 repo** 上,確認 baseline 與現況一致 ——
那一組才是 CI 真正要守的那條線:有人往 repo 裡加新債,它會紅。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

from ratchet import baseline_inventory as engine  # noqa: E402
from ratchet import check as gate  # noqa: E402
from sentinel import corpus_sentinel  # noqa: E402


_CLEAN_PY = """\
from shellout import run_tracked


def run_it():
    try:
        run_tracked(["true"], timeout=5)
    except OSError as exc:
        log(exc)
"""

_DEBT_PY = """\
import subprocess


def swallow():
    try:
        risky()
    except Exception:
        pass


def spawn():
    subprocess.run(["echo", "hi"])
"""

_DEBT_JS = """\
export function label(text) {
  return String(text).slice(0, 40);
}
"""


class _FakeRepo:
    """暫存的假 repo。沒有 `.git`,所以 list_files 走 os.walk。"""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="ratchet-fake-")
        self.baseline = os.path.join(self.root, "baseline.json")

    def write(self, rel: str, text: str):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def scan(self):
        return engine.scan(self.root, enforce_corpus=False)

    def freeze(self):
        with open(self.baseline, "w", encoding="utf-8") as f:
            json.dump(engine.to_baseline(self.scan()), f, ensure_ascii=False)

    def verdict(self):
        with open(self.baseline, encoding="utf-8") as f:
            return engine.compare(self.scan(), json.load(f))

    def destroy(self):
        shutil.rmtree(self.root, ignore_errors=True)


class RatchetTurnsOneWayTest(unittest.TestCase):
    """棘輪的三個方向。"""

    def setUp(self):
        self.repo = _FakeRepo()
        self.addCleanup(self.repo.destroy)
        self.repo.write("product.py", _DEBT_PY)
        self.repo.write("ui.js", _DEBT_JS)
        self.repo.freeze()

    def test_frozen_debt_does_not_block(self):
        """舊債凍結:什麼都沒改 → 綠,而且**不**提示收斂。"""
        verdict = self.repo.verdict()
        self.assertTrue(verdict.ok, [v.snippet for v in verdict.new])
        self.assertEqual(verdict.retired, [])
        self.assertEqual(verdict.scanned, verdict.frozen)

    def test_a_single_new_violation_goes_red(self):
        """新債擋死:多加一行裸 subprocess → 紅,而且指名道姓。"""
        self.repo.write("product.py", _DEBT_PY + '\n\ndef more():\n'
                                                 '    subprocess.Popen(["ls"])\n')
        verdict = self.repo.verdict()
        self.assertFalse(verdict.ok, "新增的裸 subprocess 沒被擋下來")
        self.assertEqual(len(verdict.new), 1, [v.snippet for v in verdict.new])
        self.assertEqual(verdict.new[0].rule, "py-bare-subprocess")
        self.assertIn("Popen", verdict.new[0].snippet)

    def test_new_violation_in_a_brand_new_file_goes_red(self):
        """新檔案裡的違規也要擋 —— 不然「開新檔」就是繞過閘門的捷徑。"""
        self.repo.write("another.js", _DEBT_JS)
        verdict = self.repo.verdict()
        self.assertFalse(verdict.ok)
        self.assertEqual({v.rule for v in verdict.new}, {"js-naked-truncation"})

    def test_duplicating_an_identical_violation_goes_red(self):
        """同一個檔裡多貼一行一模一樣的違規 → 紅(靠 baseline 的 count)。

        這是「身分用內容雜湊而不用行號」必須付的代價,所以必須被測到:
        不處理的話,複製貼上就是免費的新債。
        """
        self.repo.write("product.py", _DEBT_PY + '\n\ndef again():\n'
                                                 '    subprocess.run(["echo", "hi"])\n')
        verdict = self.repo.verdict()
        self.assertFalse(verdict.ok, "重複貼同一行違規沒被擋下來")
        self.assertEqual(len(verdict.new), 1)

    def test_reformatting_a_frozen_violation_is_not_new_debt(self):
        """重排版/改縮排/加註解 → 身分不變,不算新債。

        這條是制度能活下來的前提:任何 reformat 都讓 baseline 失效的話,
        第一次格式化之後就沒人要用了。
        """
        self.repo.write("product.py", _DEBT_PY.replace(
            '    subprocess.run(["echo", "hi"])',
            '    subprocess.run(["echo",    "hi"])      # 暫時留著,等共用包裝'))
        verdict = self.repo.verdict()
        self.assertTrue(verdict.ok,
                        f"重排版被誤判成新債:{[v.snippet for v in verdict.new]}")

    def test_moving_a_file_rewrites_the_key_and_is_loud(self):
        """搬檔 → 身分改變 → 紅。刻意的:搬檔是重新審視那筆債的時機。"""
        os.remove(os.path.join(self.repo.root, "ui.js"))
        self.repo.write("static/ui.js", _DEBT_JS)
        verdict = self.repo.verdict()
        self.assertFalse(verdict.ok, "搬檔之後的違規應該要重新表態")
        self.assertEqual(len(verdict.retired), 1)
        self.assertEqual(verdict.retired[0][0], "js-naked-truncation")

    def test_removing_debt_converges_and_hints(self):
        """清掉舊債 → **不紅**,而且提示 baseline 可以收斂。

        這是棘輪的回報機制:清債的人要看到棘輪往前轉了一格。
        """
        self.repo.write("product.py", _CLEAN_PY)
        verdict = self.repo.verdict()
        self.assertTrue(verdict.ok, [v.snippet for v in verdict.new])
        retired_rules = {r for r, _i, _m in verdict.retired}
        self.assertEqual(retired_rules, {"py-silent-except", "py-bare-subprocess"},
                         f"收斂提示不完整:{verdict.retired}")

    def test_update_after_cleaning_shrinks_the_baseline(self):
        """`--update` 收斂之後,被清掉的那筆**不能再回來**(棘輪只往一邊轉)。"""
        self.repo.write("product.py", _CLEAN_PY)
        self.repo.freeze()                      # = scripts/ratchet/check.py --update
        with open(self.repo.baseline, encoding="utf-8") as f:
            frozen = json.load(f)
        self.assertNotIn("py-silent-except", frozen["rules"],
                         "清掉的舊債還留在 baseline 裡 —— 那它隨時可以回來")

        self.repo.write("product.py", _DEBT_PY)  # 把債加回去
        verdict = self.repo.verdict()
        self.assertFalse(verdict.ok, "收斂之後舊債又被加回來,必須紅")
        self.assertEqual({v.rule for v in verdict.new},
                         {"py-silent-except", "py-bare-subprocess"})


class BrokenGlobGoesRedTest(unittest.TestCase):
    """棘輪自己也是掃描式守門:掃不到檔必須紅,不是「沒有新債」。"""

    def test_empty_corpus_trips_the_sentinel(self):
        empty = tempfile.mkdtemp(prefix="ratchet-empty-")
        self.addCleanup(shutil.rmtree, empty, True)
        with self.assertRaises(corpus_sentinel.CorpusTooSmall) as caught:
            engine.scan(empty, enforce_corpus=True)
        self.assertIn("ratchet.", str(caught.exception))

    def test_corpus_check_off_is_only_for_fake_repos(self):
        """關掉哨兵的那條路要存在(自我測試要用),但**只能**明確地關。

        也就是:預設是開的。這條測試在防「有人把預設改成關」。
        """
        import inspect
        sig = inspect.signature(engine.scan)
        self.assertIs(sig.parameters["enforce_corpus"].default, True,
                      "語料下限哨兵的預設被改成關了 —— "
                      "那棘輪就會在掃不到檔的時候報綠")


class RuleQualityTest(unittest.TestCase):
    """規則本身的品質 —— 一條沒有出路的規則會被繞過,不是被遵守。"""

    def test_every_rule_explains_itself_and_gives_a_way_out(self):
        for rule in engine.RULES:
            self.assertGreater(len(rule.why), 30,
                               f"{rule.name} 沒說清楚為什麼這是債")
            self.assertGreater(len(rule.fix), 20,
                               f"{rule.name} 沒給出路 —— 不給出路的閘門會被繞過")
            self.assertIn(rule.corpus, corpus_sentinel.MINIMUMS,
                          f"{rule.name} 的語料沒登記下限")

    def test_rules_actually_detect_their_own_bad_example(self):
        """每條規則都要在一份刻意寫壞的語料上命中。

        閘門的經典死法是「regex 被重構掉但沒人發現」—— 那時候它掃得到檔
        (語料哨兵過關)卻什麼都配不到。這組斷言守的是這一層。
        """
        repo = _FakeRepo()
        self.addCleanup(repo.destroy)
        repo.write("product.py", _DEBT_PY)
        repo.write("ui.js", _DEBT_JS)
        hits = {v.rule for v in repo.scan().violations}
        self.assertEqual(hits, {r.name for r in engine.RULES},
                         "有規則在刻意寫壞的語料上都配不到 —— regex 死了")

    def test_clean_code_is_not_flagged(self):
        """反面:乾淨的寫法不准被判違規(誤報會讓人去關閘門)。"""
        repo = _FakeRepo()
        self.addCleanup(repo.destroy)
        repo.write("product.py", _CLEAN_PY)
        repo.write("ui.js", "export const ok = (s) => truncate(s, 40);\n")
        self.assertEqual([v.snippet for v in repo.scan().violations], [])

    def test_tests_and_scenarios_are_out_of_scope_for_python_rules(self):
        """python 規則只管產品碼。測試裡的 `except: pass` 常常就是被測的東西。"""
        self.assertFalse(engine._is_product_py("tests/test_x.py"))
        self.assertFalse(engine._is_product_py("scenarios/foo.py"))
        self.assertTrue(engine._is_product_py("bridge.py"))
        self.assertTrue(engine._is_product_py("scripts/post-deploy-smoke.py"))


class RealRepoIsRatchetedTest(unittest.TestCase):
    """**CI 真正守的那條線**:真 repo 的現況不准有新違規。

    有人往 repo 裡加一行 `except: pass` 或裸 subprocess,這一支就紅。
    """

    def test_no_new_violations_in_this_repo(self):
        verdict = engine.compare(engine.scan(_ROOT),
                                 gate.load_baseline())
        self.assertTrue(
            verdict.ok,
            "這些是**新**違規(舊債凍結,新債擋死):\n  "
            + "\n  ".join(f"{v.rule} {v.path}:{v.line}  {v.snippet}"
                          for v in verdict.new)
            + "\n跑 `scripts/ratchet/check.py` 看每條規則的理由與出路。"
              "不要用 --update 把它變成舊債。")

    def test_baseline_file_is_well_formed(self):
        baseline = gate.load_baseline()
        self.assertEqual(baseline["schema_version"], engine.SCHEMA_VERSION)
        self.assertEqual(baseline["hash_recipe"], engine.HASH_RECIPE)
        self.assertTrue(baseline["rules"], "baseline 是空的?那棘輪在擋全部舊債")
        for name in baseline["rules"]:
            self.assertIn(name, {r.name for r in engine.RULES},
                          f"baseline 裡有不存在的規則 `{name}` —— "
                          "規則被刪掉的話那些 key 永遠不會被清")

    def test_cli_gate_exits_zero_on_the_current_tree(self):
        """閘門當 CLI 跑也要通(CI 掛的是這支,不是 import)。"""
        proc = subprocess.run(
            [sys.executable, os.path.join(_ROOT, "scripts", "ratchet", "check.py")],
            cwd=_ROOT, capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0,
                         f"stdout={proc.stdout}\nstderr={proc.stderr}")
        self.assertIn("沒有新違規", proc.stdout)

    def test_cli_gate_reports_corpus_sizes(self):
        """閘門要把語料數印出來 —— 人才看得出「它這次掃了多少東西」。"""
        proc = subprocess.run(
            [sys.executable, os.path.join(_ROOT, "scripts", "ratchet", "check.py")],
            cwd=_ROOT, capture_output=True, text=True, timeout=180)
        for rule in engine.RULES:
            self.assertIn(rule.name, proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
