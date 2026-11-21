"""人格「傳檔慣例」提示(2026-11-21,W1-③ A 案配套)。

機主:「要我把這條寫進 agent 的 system prompt 慣例嗎」—— 好。這份測試釘住
_resolve_persona_prompt 每輪都教模型:要傳檔給使用者,存進 {home}/outbox/、
用 [[attach:/路徑]] 明確標記;並確保 outbox 目錄真的被建出來(不然第一次用
就因資料夾不存在寫檔失敗)。
"""
import _isolation  # noqa: F401
import asyncio
import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="persona-attach-hint-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class PersonaAttachHintTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="persona-home-")
        # _save_data_uri 寫真實 UPLOAD_DIR(production 路徑),測試隔離閂會擋下
        # 任何對它的寫入 —— 需要存真附件的那條測試,改導到 tmp。
        self.upload_dir = tempfile.mkdtemp(prefix="persona-uploads-")
        self.enterContext(patch.object(bridge, "UPLOAD_DIR", Path(self.upload_dir)))

    def test_hint_names_outbox_path_and_marker_syntax(self):
        hint = bridge._persona_attach_hint(self.home)
        outbox = os.path.join(self.home, "outbox")
        self.assertIn(outbox, hint)
        self.assertIn("[[attach:", hint)

    def test_hint_creates_outbox_directory(self):
        outbox = os.path.join(self.home, "outbox")
        self.assertFalse(os.path.isdir(outbox))
        bridge._persona_attach_hint(self.home)
        self.assertTrue(os.path.isdir(outbox), "第一次用就該把 outbox 建出來")

    def test_empty_home_yields_no_hint(self):
        self.assertEqual(bridge._persona_attach_hint(""), "")

    def test_resolve_persona_prompt_appends_hint_even_without_attachments(self):
        prompt = asyncio.run(bridge._resolve_persona_prompt(
            [{"role": "user", "content": "你好"}], self.home))
        self.assertTrue(prompt.startswith("你好"))
        self.assertIn("[[attach:", prompt)
        self.assertIn(os.path.join(self.home, "outbox"), prompt)

    def test_resolve_persona_prompt_keeps_existing_attachment_notes(self):
        """原本的「[使用者附件]」段落不能被新提示蓋掉或擠掉。"""
        data_uri = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n").decode()
        prompt = asyncio.run(bridge._resolve_persona_prompt(
            [{"role": "user", "content": [
                {"type": "text", "text": "看這張"},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]}], self.home))
        self.assertIn("[使用者附件]", prompt)
        self.assertIn("[[attach:", prompt)


if __name__ == "__main__":
    unittest.main()
