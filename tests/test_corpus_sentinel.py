"""語料下限哨兵的**自我測試** —— 故意弄壞 glob,哨兵必須紅。

## 為什麼閘門自己需要測試

`scripts/sentinel/corpus_sentinel.py` 的工作是「讓掃描式守門不能因為掃不到
東西而報綠」。但它自己也只是一支程式:**如果哨兵本身壞了(或被某次重構
拿掉了),所有守門會安靜地退回『掃到 0 個 ⇒ 綠』的狀態** —— 而那正是它
存在的理由。所以抄 deepseek-harness 的 `scripts/oxlint-contract.spec.ts`
那條紀律(研究報告 §5.3):

  > **A guard only guards if the regression fails it.**

這一檔就是在問那句話:**我把 glob 弄壞,它真的會紅嗎?**

每個「弄壞」的實驗都跑在子程序裡(`sys.executable`),理由是
patch 掉真守門的模組狀態之後沒辦法乾淨還原 —— 而一支會污染其他測試的
自我測試,比沒有自我測試更糟。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import os
import subprocess
import sys
import textwrap
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

from sentinel import corpus_sentinel  # noqa: E402


def _run_probe(body: str) -> subprocess.CompletedProcess:
    """在子程序裡跑一段探針。探針自己印 `PROBE_OK` 並 exit 0 才算成立。"""
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {_ROOT!r})
        sys.path.insert(0, os.path.join({_ROOT!r}, "tests"))
        sys.path.insert(0, os.path.join({_ROOT!r}, "scripts"))
    """) + textwrap.dedent(body)
    return subprocess.run([sys.executable, "-c", script], cwd=_ROOT,
                          capture_output=True, text=True, timeout=180)


class SentinelContractTest(unittest.TestCase):
    """哨兵本身的契約。"""

    def test_below_minimum_raises(self):
        with self.assertRaises(corpus_sentinel.CorpusTooSmall) as caught:
            corpus_sentinel.require("private_leak.tracked_files", 3)
        msg = str(caught.exception)
        self.assertIn("private_leak.tracked_files", msg)
        self.assertIn("只掃到 3 個", msg)
        # 訊息要把人導向正確的第一個假設:**守門瞎了**,不是語料真的變少。
        self.assertIn("先假設是守門瞎了", msg)

    def test_at_minimum_passes(self):
        minimum = corpus_sentinel.MINIMUMS["private_leak.tracked_files"][0]
        self.assertEqual(corpus_sentinel.require(
            "private_leak.tracked_files", minimum), minimum)

    def test_unregistered_corpus_raises(self):
        """沒登記的語料也要紅 —— 否則「忘了登記」與「語料是空的」又是同一件事。"""
        with self.assertRaises(corpus_sentinel.CorpusTooSmall) as caught:
            corpus_sentinel.require("brand.new.gate", 1200)
        self.assertIn("沒有登記", str(caught.exception))
        self.assertIn("建議值 1080", str(caught.exception))   # 1200 × 0.9

    def test_is_an_assertion_error(self):
        """unittest 要把它當失敗報,不是當 ERROR —— 失敗訊息才會被讀。"""
        self.assertTrue(issubclass(corpus_sentinel.CorpusTooSmall, AssertionError))

    def test_minimums_are_not_quietly_lowered(self):
        """每個下限都該是「記錄當下實際值的 90%」。

        明顯低於那個比例 = 有人把數字手動壓下去了。這條不會阻止那個動作
        (有時候是對的),但會讓它**必須經過改這支測試**,也就是必須被 review 看到。
        """
        suspicious = []
        for name, (minimum, recorded, _what) in corpus_sentinel.MINIMUMS.items():
            if recorded <= 2:
                continue        # 太小的語料,90% 取整後沒有意義
            if minimum < corpus_sentinel.suggested_minimum(recorded) * 0.8:
                suspicious.append((name, minimum, recorded))
        self.assertEqual(
            suspicious, [],
            "這些下限明顯低於『記錄值的 90%』,像是被手動壓低的:"
            f"{suspicious}。壓低一定要有理由,寫在 MINIMUMS 那一行。")

    def test_report_runs_and_sees_the_real_repo(self):
        """`python scripts/sentinel/corpus_sentinel.py` 要能跑,而且現況全部過。"""
        proc = subprocess.run(
            [sys.executable, os.path.join(_ROOT, "scripts", "sentinel",
                                          "corpus_sentinel.py")],
            cwd=_ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0,
                         f"現況有語料低於下限:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("private_leak.tracked_files", proc.stdout)


class BrokenGlobGoesRedTest(unittest.TestCase):
    """**弄壞 glob → 守門必須紅。** 這一組是整檔的重點。"""

    def test_leak_gate_goes_red_when_git_ls_files_returns_almost_nothing(self):
        """洩漏掃描:`git ls-files` 只回三個檔(淺 clone / cwd 跑錯的樣子)。

        在哨兵存在之前,這個情況下那支測試會**完全綠** ——
        它守的是「公開 repo 不准帶私有識別」。
        """
        proc = _run_probe("""
            import test_no_private_infra_leak as gate

            class _Fake:
                stdout = "README.md\\nLICENSE\\nbridge.py\\n"

            gate.subprocess.run = lambda *a, **k: _Fake()
            try:
                gate.tracked_files()
            except AssertionError as exc:
                assert "private_leak.tracked_files" in str(exc), str(exc)
                print("PROBE_OK")
                sys.exit(0)
            print("NOT_RED: 洩漏掃描在只掃到 3 個檔的情況下通過了")
            sys.exit(1)
        """)
        self.assertIn("PROBE_OK", proc.stdout,
                      f"stdout={proc.stdout}\nstderr={proc.stderr}")

    def test_media_matrix_goes_red_when_the_branch_regex_stops_matching(self):
        """媒體掛點矩陣:regex 只配到一個分支(最常見的 regex 腐化形狀)。

        歸零不危險(會變成「全部都缺」而紅);**配到一兩個才危險** ——
        那時候矩陣只剩一兩格在被檢查,其餘安靜地不在守備範圍。
        """
        proc = _run_probe('''
            import test_media_attach_matrix as gate

            gate._BRIDGE = (
                'def _media_session_cwd(session_id):\\n'
                '    if prov == "cc2":\\n'
                '        return ""\\n'
                '\\ndef _next(): pass\\n')
            try:
                gate._cwd_branch_providers()
            except AssertionError as exc:
                assert "media_attach.cwd_branches" in str(exc), str(exc)
                print("PROBE_OK")
                sys.exit(0)
            print("NOT_RED: 只掃到 1 個 provider 分支還是通過了")
            sys.exit(1)
        ''')
        self.assertIn("PROBE_OK", proc.stdout,
                      f"stdout={proc.stdout}\nstderr={proc.stderr}")

    def test_event_taxonomy_goes_red_when_the_source_scan_finds_nothing(self):
        """事件分類登記表:掃源碼的 `_src()` 回一份沒有發射點的假檔。

        原版只有 `assertTrue(types)`(≥1 就過)。這支證明現在連
        「只剩一種型別」都會紅。
        """
        proc = _run_probe('''
            import test_event_taxonomy_b1 as gate

            gate._src = lambda name: 'x = _event_append(s, "turn", {})\\n'
            case = gate.TestRegistryCompleteness("test_event_append_literals_registered")
            try:
                case.test_event_append_literals_registered()
            except AssertionError as exc:
                assert "event_taxonomy.event_append_literals" in str(exc), str(exc)
                print("PROBE_OK")
                sys.exit(0)
            print("NOT_RED: 只掃到 1 種事件型別還是通過了")
            sys.exit(1)
        ''')
        self.assertIn("PROBE_OK", proc.stdout,
                      f"stdout={proc.stdout}\nstderr={proc.stderr}")

    def test_sse_gate_goes_red_when_the_literal_pattern_stops_matching(self):
        """SSE 反緩衝標頭:把字面樣式換成單引號寫法(一條都配不到)。

        2026-10-26 的 tunnel 事故(串流每 36 秒死一次)是漏一條造成的。
        這支守門一旦真空通過,那類事故會整批回來而沒有任何訊號。
        """
        proc = _run_probe('''
            import test_sse_anti_buffering as gate

            case = gate.SSEAntiBufferingTests("test_every_sse_response_carries_the_headers")
            src = open(gate.SRC, encoding="utf-8").read().replace(
                'media_type="text/event-stream"', "media_type=_SSE_MIME")
            import tempfile, os
            tmp = tempfile.mkdtemp()
            gate.SRC = os.path.join(tmp, "bridge.py")
            with open(gate.SRC, "w", encoding="utf-8") as f:
                f.write(src)
            try:
                case.test_every_sse_response_carries_the_headers()
            except AssertionError as exc:
                assert "sse_headers.response_sites" in str(exc), str(exc)
                print("PROBE_OK")
                sys.exit(0)
            print("NOT_RED: 一條 SSE 路由都掃不到,守門還是說「全部都帶了」")
            sys.exit(1)
        ''')
        self.assertIn("PROBE_OK", proc.stdout,
                      f"stdout={proc.stdout}\nstderr={proc.stderr}")

    def test_installer_gate_goes_red_when_the_installer_is_gutted(self):
        """安裝器中立性:安裝器被改成一行 `exec other.sh`。

        本檔全部的斷言都是「不准出現 X」,所以安裝器變短時它們會一起自動通過。
        """
        proc = _run_probe('''
            import tempfile, os
            import test_installer_is_neutral as gate

            tmp = tempfile.mkdtemp()
            thin = os.path.join(tmp, "install-local-bridge.sh")
            with open(thin, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\\nexec \\"$(dirname \\"$0\\")/real-install.sh\\" \\"$@\\"\\n")
            gate.INSTALLER = thin
            try:
                gate.installer_src()
            except AssertionError as exc:
                assert "installer_neutral.installer_lines" in str(exc), str(exc)
                print("PROBE_OK")
                sys.exit(0)
            print("NOT_RED: 兩行的安裝器還是通過了中立性守門")
            sys.exit(1)
        ''')
        self.assertIn("PROBE_OK", proc.stdout,
                      f"stdout={proc.stdout}\nstderr={proc.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
