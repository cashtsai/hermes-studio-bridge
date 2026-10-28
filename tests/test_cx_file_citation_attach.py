"""codex 的檔案引用要變成可點的附件,不是一行裸文字(2026-10-28 機主回報)。

實害:CX 回覆末尾的 `:codex-file-citation{path="/…/x.pptx"}` 在 app 裡原樣印出來,
點不開也下載不了。查證結果:媒體擷取其實**完全成功**(檔案進倉庫、產得出
media_id 與下載網址、檔名大小都對),但 seed 時 `_media_capture_sync` 的回傳值
整個被丟掉,沒有人寫回卡片的 attachments —— 卡片就只剩那行文字。
"""
import _isolation  # noqa: F401
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="cx-filecite-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carddigest  # noqa: E402


def _card(text):
    return carddigest.make_card("card-x", "t1", "assistant", "markdown",
                                {"text": text, "fallback_text": text})


class CXFileCitationAttachTest(unittest.TestCase):
    def setUp(self):
        self.d = carddigest.CodexThreadDigest()
        self.path = "/tmp/pocket-filecite/報告.pptx"
        self.cap = [{"media_id": "med_1", "available": True, "source_ref": self.path,
                     "filename": "報告.pptx", "byte_size": 100,
                     "download_url": "/app/v2/artifacts/med_1"}]

    def test_citation_card_gets_attachment(self):
        self.d.store.upsert_card(_card(f'完成。\n:codex-file-citation{{path="{self.path}" purpose="output"}}'))
        self.assertEqual(carddigest.attach_captured_media(self.d.store, self.cap), 1)
        atts = self.d.store.cards["card-x"]["body"]["attachments"]
        self.assertEqual(atts[0]["filename"], "報告.pptx")
        self.assertEqual(atts[0]["download_url"], "/app/v2/artifacts/med_1")

    def test_unrelated_card_untouched(self):
        self.d.store.upsert_card(_card("這則沒提到任何檔案"))
        self.assertEqual(carddigest.attach_captured_media(self.d.store, self.cap), 0)
        self.assertIsNone(self.d.store.cards["card-x"]["body"].get("attachments"))

    def test_existing_attachments_win(self):
        c = _card(f'see {self.path}')
        c["body"]["attachments"] = [{"media_id": "live", "filename": "live.png"}]
        self.d.store.upsert_card(c)
        self.assertEqual(carddigest.attach_captured_media(self.d.store, self.cap), 0)
        self.assertEqual(self.d.store.cards["card-x"]["body"]["attachments"][0]["media_id"], "live")

    def test_unavailable_capture_skipped(self):
        self.d.store.upsert_card(_card(f'see {self.path}'))
        bad = [dict(self.cap[0], available=False)]
        self.assertEqual(carddigest.attach_captured_media(self.d.store, bad), 0)

    def test_url_captures_are_not_attached(self):
        """網址被擷取成 source_kind=url、沒有 download_url —— 掛上去是點不開的空殼。"""
        self.d.store.upsert_card(_card(f'see {self.path} 與 https://example.com/en'))
        caps = self.cap + [
            {"media_id": "med_u", "available": True, "source_kind": "url",
             "source_ref": "https://example.com/en", "filename": "en"},
        ]
        self.assertEqual(carddigest.attach_captured_media(self.d.store, caps), 1)
        atts = self.d.store.cards["card-x"]["body"]["attachments"]
        self.assertEqual([a["filename"] for a in atts], ["報告.pptx"],
                         "只能留真檔案,網址片段不得變成附件")

    def test_capture_without_download_url_skipped(self):
        self.d.store.upsert_card(_card(f'see {self.path}'))
        bad = [dict(self.cap[0], download_url=None)]
        self.assertEqual(carddigest.attach_captured_media(self.d.store, bad), 0)

    def test_empty_capture_is_noop(self):
        self.d.store.upsert_card(_card(f'see {self.path}'))
        self.assertEqual(carddigest.attach_captured_media(self.d.store, []), 0)


if __name__ == "__main__":
    unittest.main()
