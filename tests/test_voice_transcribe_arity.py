"""語音附件轉文字:呼叫點不得漏參數(2026-10-10 實害)。

機主對 FLiPER 送一段 1 分 20 秒語音,**連續 7 次 500**:

    TypeError: _transcribe() missing 1 required positional argument: 'home'
    POST /app/v2/sessions/claude_code:FLiPER/input → 500

app 顯示「伺服器暫時有問題,請稍後再試」。音檔其實上傳成功了
(`app_upload_raw_saved voice.m4a 275KB`),炸在轉文字那一步 ——
**語音送進 CC 與 Codex 完全不能用**,而人格那條走 `_transcribe_attachments`
(有好好帶 home)所以是好的,因此這個洞一直沒被發現。

病根是「必填參數 + 兩個只給一個參數的呼叫點」。修法是給 `home` 預設值
(沒有人格的通道本來就該用 HOME_ROOT),連未來新開的通道漏帶也不會炸。
這裡用 inspect 把「單參數可呼叫」釘死,不依賴真的去跑 STT。
"""
import _isolation  # noqa: F401
import inspect
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="voice-arity-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class VoiceTranscribeArityTest(unittest.TestCase):
    def test_transcribe_callable_with_path_only(self):
        """只給 path 必須叫得動 —— CC/Codex 兩個呼叫點就是這樣叫的。"""
        sig = inspect.signature(bridge._transcribe)
        bound = sig.bind("/tmp/voice.m4a")        # 不丟 TypeError 才算過
        bound.apply_defaults()
        self.assertEqual(bound.arguments["home"], "")

    def test_home_defaults_to_home_root(self):
        """省略 home 時要落在預設 Hermes home,不是空字串丟給下游。"""
        seen = {}

        def fake(home, path, locale=""):
            seen["home"] = home
            return {"success": True, "transcript": "哈囉"}

        orig = bridge.hermes_media.transcribe_audio
        bridge.hermes_media.transcribe_audio = fake
        try:
            out = bridge._transcribe("/tmp/voice.m4a")
        finally:
            bridge.hermes_media.transcribe_audio = orig
        self.assertEqual(out, "哈囉")
        self.assertEqual(seen["home"], bridge.HOME_ROOT)

    def test_explicit_home_still_wins(self):
        """人格那條會明確帶 persona home —— 不可被預設值蓋掉。"""
        seen = {}

        def fake(home, path, locale=""):
            seen["home"] = home
            return {"success": True, "transcript": "x"}

        orig = bridge.hermes_media.transcribe_audio
        bridge.hermes_media.transcribe_audio = fake
        try:
            bridge._transcribe("/tmp/voice.m4a", "/some/persona/home")
        finally:
            bridge.hermes_media.transcribe_audio = orig
        self.assertEqual(seen["home"], "/some/persona/home")

    def test_every_call_site_is_bindable(self):
        """靜態掃一遍:原始碼裡每個 `_transcribe(` 呼叫都要綁得起來。

        這條才是真正防再犯的 —— 改簽章時會當場紅在這裡,而不是等使用者
        送語音吃 500。
        """
        import ast
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "bridge.py"), encoding="utf-8").read()
        tree = ast.parse(src)
        sig = inspect.signature(bridge._transcribe)
        calls = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Name) and fn.id == "_transcribe"):
                continue
            calls += 1
            # asyncio.to_thread(_transcribe, path, ...) 的形式在下面另外數
            sig.bind(*["x"] * len(node.args),
                     **{k.arg: "x" for k in node.keywords if k.arg})
        # to_thread(_transcribe, ...) 這種「把函式當參數傳」的呼叫
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id == "_transcribe":
                calls += 1
                sig.bind(*["x"] * (len(node.args) - 1))
        self.assertGreaterEqual(calls, 2, "至少該掃到 CC 與 Codex 兩個呼叫點")


if __name__ == "__main__":
    unittest.main()
