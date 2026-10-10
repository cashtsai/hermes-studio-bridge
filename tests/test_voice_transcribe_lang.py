"""語音附件轉文字:呼叫點不得漏**語言**(2026-10-10 第二次實害)。

同一條路、同一種病根,但這次不會炸,會**安靜地辨識錯**。

機主用繁體中文對 Claude Code 這條線口述,收到的轉錄結果是同一句英文
重複十幾次:

    "The second part is that we will not be able to put the information
     in our service system. The second part is that we will not be able…"

病根:`hermes_media.transcribe_audio` 把 `locale` 查成 Whisper 的
`language` + 中文提示詞(`_LOCALE_LANGUAGE` / `_LOCALE_PROMPT`)。`locale`
是空字串 → 兩者都空 → **Whisper 自動判語言** → 中文被誤判成英文 → 幻覺迴圈。

為什麼只有人格線沒事:`_transcribe_attachments` 一直有把 `lang` 傳下去
(app 的 `PersonaCardSessionView` 會送 `stt_lang`),而 CC / Codex / 委派
四個呼叫點都只給 `path`。跟 `home` 那次是**完全一樣的形狀** ——
必填語意 + 漏帶參數的呼叫點 —— 只是這次沒有 TypeError 幫忙把問題叫出來。

修法同樣是**在 `_transcribe` 兜底**(`STT_LANG_DEFAULT`,env
`POCKET_STT_LANG`),而不是只補呼叫點;另外把 v2/v1 input 已經收到的
`stt_lang` 接進 CC 與 Codex,讓介面語言能覆寫兜底。

這支測試釘三件事:兜底有效、明確帶的語言不被蓋掉、使用者輸入那兩個
呼叫點必須真的把語言傳下去(靜態掃,改壞會當場紅)。
"""
import _isolation  # noqa: F401
import ast
import inspect
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="voice-lang-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

_BRIDGE_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge.py")


class _CaptureLocale:
    """把 transcribe_audio 換掉,記下實際收到的 locale。"""

    def __init__(self):
        self.seen = {}
        self._orig = None

    def __enter__(self):
        def fake(home, path, locale=""):
            self.seen["locale"] = locale
            return {"success": True, "transcript": "哈囉"}

        self._orig = bridge.hermes_media.transcribe_audio
        bridge.hermes_media.transcribe_audio = fake
        return self

    def __exit__(self, *exc):
        bridge.hermes_media.transcribe_audio = self._orig
        return False


class VoiceTranscribeLangTest(unittest.TestCase):
    def test_omitted_lang_falls_back_to_default(self):
        """省略 lang 時不可以把空字串丟給下游 —— 那就是幻覺迴圈的來源。"""
        with _CaptureLocale() as cap:
            out = bridge._transcribe("/tmp/voice.m4a")
        self.assertEqual(out, "哈囉")
        self.assertEqual(cap.seen["locale"], bridge.STT_LANG_DEFAULT)
        self.assertTrue(bridge.STT_LANG_DEFAULT,
                        "兜底不可以是空字串,否則這個修法等於沒修")

    def test_default_is_a_locale_hermes_media_understands(self):
        """兜底值必須查得到 language/prompt,不然傳下去也等於空。"""
        import hermes_media
        self.assertIn(bridge.STT_LANG_DEFAULT, hermes_media._LOCALE_LANGUAGE)

    def test_explicit_lang_still_wins(self):
        """介面切英文時,語音就該用英文辨識,不可被機主的預設鎖成中文。"""
        with _CaptureLocale() as cap:
            bridge._transcribe("/tmp/voice.m4a", "", "en")
        self.assertEqual(cap.seen["locale"], "en")

    def test_empty_string_lang_is_treated_as_omitted(self):
        """app 沒帶時我們送的是 ""(不是 None),也要落兜底。"""
        with _CaptureLocale() as cap:
            bridge._transcribe("/tmp/voice.m4a", "", "")
        self.assertEqual(cap.seen["locale"], bridge.STT_LANG_DEFAULT)

    def test_user_input_call_sites_pass_a_language(self):
        """靜態掃:使用者輸入那兩條必須把語言傳下去,不能只給 path。

        這條才是真正防再犯的 —— 有人新增 provider 時照抄舊的單參數寫法,
        會當場紅在這裡,而不是等使用者發現語音辨識出別的語言。
        委派那兩個呼叫點刻意不在名單裡:那裡沒有使用者語言,落兜底才對。
        """
        with open(_BRIDGE_PY, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        must_pass_lang = {"_cc_input_core", "_codex_input_items"}
        checked = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in must_pass_lang:
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call) or not inner.args:
                    continue
                first = inner.args[0]
                if not (isinstance(first, ast.Name)
                        and first.id == "_transcribe"):
                    continue
                checked.add(node.name)
                # to_thread(_transcribe, path, home, lang) → 4 個 args
                self.assertEqual(
                    len(inner.args), 4,
                    f"{node.name} 的 _transcribe 呼叫沒把語言傳下去 "
                    f"(args={len(inner.args)},期望 4)")
        self.assertEqual(
            checked, must_pass_lang,
            f"沒掃到預期的呼叫點:缺 {must_pass_lang - checked}")

    def test_codex_input_items_accepts_stt_lang(self):
        """簽名要收得下語言,且帶預設(委派等無語言的呼叫點靠它)。"""
        sig = inspect.signature(bridge._codex_input_items)
        self.assertIn("stt_lang", sig.parameters)
        self.assertEqual(sig.parameters["stt_lang"].default, "")
        sig.bind("text", [])          # 兩參數仍要叫得動


if __name__ == "__main__":
    unittest.main()
