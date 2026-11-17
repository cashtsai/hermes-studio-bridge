"""CC/cc2(以及其他 provider)的卡片要帶可點附件,不是裸路徑(2026-11-17)。

機主實錘:「CC 給我影片/檔案在 Pocket 看不到,要開 Claude 才看得到;CX 有時
點得到」。查證:「本機檔案 → 可點附件」這條管線**只接在 codex seed**
(`_cx_seed_card_digest` 尾端的 `attach_captured_media`)。CC(tmux)把檔案收
進了媒體庫卻把擷取結果丟掉;cc2 連收都沒收(只有 base64 圖那半條)。實測
claude_code:FLiPER 最近 12 張卡 attachments 全 0。

本檔釘住修法的五個面:掃卡片找路徑(白名單/去重/效能)、路徑逃逸防護、
`attachment` 卡補 download_url、live 重發 card.upsert、既有 codex 不回歸。
"""
import _isolation  # noqa: F401
import os
import sys
import tempfile
import unittest
import unittest.mock

_TMP = tempfile.mkdtemp(prefix="cc-media-attach-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carddigest  # noqa: E402
import media_artifacts  # noqa: E402


def _mkfile(path: str, data: bytes = b"x") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


class AttachableVetTest(unittest.TestCase):
    """`vet_attachable_path` = 安全門 + 白名單。四道門少一道都不算過。"""

    def setUp(self):
        self.cwd = tempfile.mkdtemp(prefix="vet-cwd-", dir=_TMP)
        self.outside = tempfile.mkdtemp(prefix="vet-out-", dir=_TMP)
        self.roots = (os.path.realpath(self.cwd),)

    def test_media_inside_cwd_passes(self):
        mp4 = _mkfile(os.path.join(self.cwd, "out", "demo.mp4"))
        self.assertEqual(media_artifacts.vet_attachable_path(mp4, self.roots),
                         os.path.realpath(mp4))

    def test_source_code_is_not_attachable(self):
        """白名單:`.py`/`.swift` 不掛 —— 否則 code agent 每張卡都一排垃圾。"""
        for name in ("main.py", "App.swift", "notes.md", "data.json",
                     "build.log", "conf.yaml"):
            path = _mkfile(os.path.join(self.cwd, name))
            self.assertIsNone(
                media_artifacts.vet_attachable_path(path, self.roots),
                f"{name} 不該被掛成附件")

    def test_attachable_kinds_cover_media_doc_archive(self):
        for name in ("a.mp4", "b.mov", "c.png", "d.jpg", "e.pdf", "f.m4a",
                     "g.xlsx", "h.docx", "i.zip", "j.pptx"):
            path = _mkfile(os.path.join(self.cwd, name))
            self.assertIsNotNone(
                media_artifacts.vet_attachable_path(path, self.roots),
                f"{name} 應該可以掛")

    def test_path_outside_roots_rejected(self):
        outside = _mkfile(os.path.join(self.outside, "secret.pdf"))
        self.assertIsNone(media_artifacts.vet_attachable_path(outside, self.roots))

    def test_symlink_escape_rejected(self):
        """工作目錄裡的 symlink 指到外面 → realpath 之後比對,過不了。"""
        target = _mkfile(os.path.join(self.outside, "leak.pdf"))
        link = os.path.join(self.cwd, "innocent.pdf")
        os.symlink(target, link)
        self.assertIsNone(media_artifacts.vet_attachable_path(link, self.roots))

    def test_traversal_escape_rejected(self):
        target = _mkfile(os.path.join(self.outside, "leak.pdf"))
        sneaky = os.path.join(self.cwd, "..",
                              os.path.basename(self.outside), "leak.pdf")
        self.assertTrue(os.path.isfile(target))
        self.assertIsNone(media_artifacts.vet_attachable_path(sneaky, self.roots))

    def test_missing_file_is_retry_later_not_permanent(self):
        """三態的要害:檔案還沒出現 → `False`(待會再看),**不是** `None`。

        code agent 的卡片先出現、檔案後產生(`ffmpeg … out.mp4` 的工具卡在
        1 秒內落地,檔案 30 秒後才寫完)。把這種 miss 當永久失敗記起來,那
        支影片整個 session 就再也掛不上 —— 正是本功能要治的病。
        """
        verdict = media_artifacts.vet_attachable_path(
            os.path.join(self.cwd, "ghost.mp4"), self.roots)
        self.assertIs(verdict, False)

    def test_permanent_rejections_are_none(self):
        """永久不合回 `None`,呼叫端可以記下來永遠不必再 stat。"""
        outside = _mkfile(os.path.join(self.outside, "x.pdf"))
        src = _mkfile(os.path.join(self.cwd, "main.py"))
        for ref in (outside, src, "relative/x.pdf", "https://e.com/a.mp4", ""):
            self.assertIsNone(
                media_artifacts.vet_attachable_path(ref, self.roots), ref)

    def test_wrong_extension_costs_no_stat(self):
        """白名單在 isfile 之前擋掉 → 永久性拒絕一次 syscall 都不花。"""
        calls = []
        real = os.path.isfile

        def counting(path):
            calls.append(path)
            return real(path)

        with unittest.mock.patch.object(os.path, "isfile", counting):
            media_artifacts.vet_attachable_path(
                os.path.join(self.cwd, "never.py"), self.roots)
        self.assertEqual(calls, [])

    def test_relative_and_url_rejected(self):
        for ref in ("out/demo.mp4", "https://example.com/a.mp4", "", "demo.mp4"):
            self.assertIsNone(media_artifacts.vet_attachable_path(ref, self.roots))

    def test_empty_roots_rejects_everything(self):
        mp4 = _mkfile(os.path.join(self.cwd, "demo.mp4"))
        self.assertIsNone(media_artifacts.vet_attachable_path(mp4, ()))

    def test_attach_roots_narrower_than_home(self):
        """附件根目錄 = cwd + 暫存區,**不含**整個家目錄(媒體庫才含)。"""
        roots = media_artifacts.attach_roots([self.cwd])
        home = os.path.realpath(os.path.expanduser("~"))
        self.assertIn(os.path.realpath(self.cwd), roots)
        self.assertNotIn(home, roots)
        self.assertEqual(
            media_artifacts.ATTACHABLE_KINDS,
            frozenset({"image", "video", "audio", "pdf", "document",
                       "archive"}))

    def test_attach_roots_rejects_too_broad_cwd(self):
        """cwd = 家目錄時不准把整個家目錄變成附件根目錄。

        實害(2026-11-17 複查):cc2 建 session 沒給 workdir 時
        `CCSdkSession.__init__` 會把 cwd 設成 `expanduser("~")` → 推導出
        「整個家目錄可下載」,agent 只要在逐字稿裡提到 `~/Downloads/合約.pdf`
        那份 PDF 就會被複製進 artifact 庫並長出 Bearer 可取的網址。
        工作目錄是「專案資料夾」的意思,家目錄不是專案資料夾。
        """
        home = os.path.realpath(os.path.expanduser("~"))
        for broad in ("~", home, "/", "/Users", "/home", "/var", "/etc"):
            roots = media_artifacts.attach_roots([broad])
            self.assertNotIn(os.path.realpath(os.path.expanduser(broad)),
                             roots, broad)
        # 擋掉太大的目錄之後仍保有 /tmp(agent 產出物的慣例位置)。
        self.assertIn(os.path.realpath("/tmp"),
                      media_artifacts.attach_roots([home]))

    def test_home_file_not_attachable_when_cwd_is_home(self):
        home = os.path.realpath(os.path.expanduser("~"))
        roots = media_artifacts.attach_roots([home])
        self.assertIsNone(media_artifacts.vet_attachable_path(
            os.path.join(home, "Downloads", "contract.pdf"), roots))


class CardScanTest(unittest.TestCase):
    """`media_refs_in_cards`:掃哪些欄位、怎麼去重、vet 幾次。"""

    def setUp(self):
        self.store = carddigest.SessionCardStore()
        self.cwd = tempfile.mkdtemp(prefix="scan-cwd-", dir=_TMP)
        self.roots = media_artifacts.attach_roots([self.cwd])
        self.calls = []

    def _vet(self, ref):
        self.calls.append(ref)
        return media_artifacts.vet_attachable_path(ref, self.roots)

    def _text_card(self, cid, text, kind="markdown", **body):
        self.store.upsert_card(carddigest.make_card(
            cid, "t1", "assistant", kind,
            {"text": text, "fallback_text": text, **body}))

    def test_finds_path_in_markdown_text(self):
        mp4 = _mkfile(os.path.join(self.cwd, "demo.mp4"))
        # 註:`references_in_text` 的 `(?<![:/])` 前瞻會略過緊貼冒號的路徑
        # (那是為了不把 `http://…` 當本機路徑),所以用空白分隔的真實形狀。
        self._text_card("c1", f"做好了 {mp4}")
        self.assertEqual(carddigest.media_refs_in_cards(self.store, self._vet),
                         [mp4])

    def test_finds_path_in_tool_call_summary(self):
        """CC 的 `Write` 工具卡把檔名放在 body.summary,不在 text。"""
        pdf = _mkfile(os.path.join(self.cwd, "report.pdf"))
        self.store.upsert_card(carddigest.make_card(
            "c1", "t1", "assistant", "tool_call",
            {"tool": "Write", "summary": pdf, "fallback_text": "› 🔧 Write"}))
        self.assertEqual(carddigest.media_refs_in_cards(self.store, self._vet),
                         [pdf])

    def test_finds_structured_path_on_attachment_card(self):
        """CC/cc2 的「agent 看圖」卡只有 body.path,文字裡沒有路徑。"""
        png = _mkfile(os.path.join(self.cwd, "shot.png"))
        self.store.upsert_card(carddigest.make_card(
            "c1", "t1", "assistant", "attachment",
            {"filename": "shot.png", "path": png, "mime": "image/png",
             "fallback_text": "🖼 shot.png"}))
        self.assertEqual(carddigest.media_refs_in_cards(self.store, self._vet),
                         [png])

    def test_dedupe_same_path_across_cards(self):
        mp4 = _mkfile(os.path.join(self.cwd, "demo.mp4"))
        self._text_card("c1", f"產出 {mp4}")
        self._text_card("c2", f"再提一次 {mp4}")
        self._text_card("c3", f"第三次 {mp4}")
        self.assertEqual(carddigest.media_refs_in_cards(self.store, self._vet),
                         [mp4])
        self.assertEqual(self.calls.count(mp4), 1, "同一路徑只准 stat 一次")

    def test_seen_set_makes_rescan_cheap(self):
        """跨回合重掃:已看過的路徑不再 vet(長 session 的效能保證)。"""
        mp4 = _mkfile(os.path.join(self.cwd, "demo.mp4"))
        self._text_card("c1", f"產出 {mp4}")
        seen = set()
        self.assertEqual(
            carddigest.media_refs_in_cards(self.store, self._vet, seen), [mp4])
        self.calls.clear()
        self.assertEqual(
            carddigest.media_refs_in_cards(self.store, self._vet, seen), [])
        self.assertEqual(self.calls, [], "重掃不該再 stat 同一批路徑")

    def test_scanned_cards_set_skips_unchanged_cards(self):
        """`scanned_cards` 讓重掃變成 O(新卡):沒變的卡不再組字串、不再 vet。

        這支掛在 `_v2_card_store` 上 = 每次 cards/events/input 請求都跑,沒有
        這層就是每次請求重掃整個卡片庫。
        """
        mp4 = _mkfile(os.path.join(self.cwd, "demo.mp4"))
        self._text_card("c1", f"產出 {mp4}")
        scanned, seen = set(), set()
        self.assertEqual(carddigest.media_refs_in_cards(
            self.store, self._vet, seen, scanned), [mp4])
        self.assertIn("c1:1", scanned)

        # 新卡提到新檔 → 只掃新卡(舊卡在 scanned 裡被跳過)。
        pdf = _mkfile(os.path.join(self.cwd, "report.pdf"))
        self._text_card("c2", f"還有 {pdf}")
        self.calls.clear()
        self.assertEqual(carddigest.media_refs_in_cards(
            self.store, self._vet, seen, scanned), [pdf])
        self.assertEqual(self.calls, [pdf], "只准 vet 新卡帶來的新路徑")

    def test_file_created_after_being_mentioned_is_still_attached(self):
        """最嚴重的那隻(2026-11-17 複查三位獨立審閱都抓到):

        CC 的 `ffmpeg -i a.mov /work/out.mp4` 工具卡在 1 秒內落地、檔案 30
        秒後才寫完。早期版本在 vet 之前就把路徑記進 `seen`,於是那支 mp4
        **整個 session 再也不會被掛上** —— 後面的 tool_result 卡、最終摘要卡
        提到同一條路徑全被擋掉。三態 vet + 「有待重試就不標記這張卡掃過」
        就是為了這個。
        """
        mp4 = os.path.join(self.cwd, "out.mp4")      # 還沒產生
        self.store.upsert_card(carddigest.make_card(
            "tc1", "t1", "assistant", "tool_call",
            {"tool": "Bash", "summary": f"ffmpeg -i a.mov {mp4}",
             "fallback_text": "› 🔧 Bash"}))
        seen, scanned = set(), set()
        self.assertEqual(carddigest.media_refs_in_cards(
            self.store, self._vet, seen, scanned), [])
        self.assertNotIn(mp4, seen, "尚未產生的檔案不准記進永久黑名單")
        self.assertNotIn("tc1:1", scanned, "有待重試的卡不准標記為掃過")

        _mkfile(mp4)                                  # ffmpeg 寫完了
        self.assertEqual(carddigest.media_refs_in_cards(
            self.store, self._vet, seen, scanned), [mp4],
            "同一張卡在檔案出現後必須補掛得上")

    def test_permanent_reject_is_remembered(self):
        """永久不合(副檔名不對)記進 seen → 不會每輪重問。"""
        src = _mkfile(os.path.join(self.cwd, "main.py"))
        self._text_card("c1", f"改了 {src}")
        seen, scanned = set(), set()
        carddigest.media_refs_in_cards(self.store, self._vet, seen, scanned)
        self.assertIn("c1:1", scanned)

    def test_scan_is_newest_first(self):
        """卡片由新到舊掃:額度優先給最近的卡(使用者要的是剛剛那支)。"""
        old = _mkfile(os.path.join(self.cwd, "old.pdf"))
        new = _mkfile(os.path.join(self.cwd, "new.pdf"))
        self._text_card("c1", f"舊的 {old}")
        self._text_card("c2", f"新的 {new}")
        self.assertEqual(
            carddigest.media_refs_in_cards(self.store, self._vet), [new, old])

    def test_scanned_cards_rescans_after_rev_bump(self):
        """卡片改過(rev+1)就要重掃 —— 串流卡的文字是後來才補齊的。"""
        mp4 = _mkfile(os.path.join(self.cwd, "demo.mp4"))
        self._text_card("c1", "還在跑…")
        scanned, seen = set(), set()
        self.assertEqual(carddigest.media_refs_in_cards(
            self.store, self._vet, seen, scanned), [])
        self._text_card("c1", f"做好了 {mp4}")          # rev 2
        self.assertEqual(carddigest.media_refs_in_cards(
            self.store, self._vet, seen, scanned), [mp4])

    def test_scanned_cards_bounded(self):
        scanned = {f"c{i}:1" for i in range(20_001)}
        carddigest.media_refs_in_cards(self.store, self._vet, set(), scanned)
        self.assertEqual(len(scanned), 0)

    def test_skips_cards_that_already_have_attachments(self):
        mp4 = _mkfile(os.path.join(self.cwd, "demo.mp4"))
        self._text_card("c1", f"產出 {mp4}",
                        attachments=[{"media_id": "med_live"}])
        self.assertEqual(carddigest.media_refs_in_cards(self.store, self._vet),
                         [])

    def test_skips_attachment_card_that_already_downloads(self):
        png = _mkfile(os.path.join(self.cwd, "shot.png"))
        self.store.upsert_card(carddigest.make_card(
            "c1", "t1", "assistant", "attachment",
            {"filename": "shot.png", "path": png,
             "download_url": "/app/v2/artifacts/med_b64",
             "fallback_text": "🖼 shot.png"}))
        self.assertEqual(carddigest.media_refs_in_cards(self.store, self._vet),
                         [])

    def test_source_code_paths_never_scanned_in(self):
        py = _mkfile(os.path.join(self.cwd, "main.py"))
        self._text_card("c1", f"改了 {py}")
        self.assertEqual(carddigest.media_refs_in_cards(self.store, self._vet),
                         [])

    def test_ref_cap_bounds_the_scan(self):
        for i in range(carddigest._MEDIA_SCAN_MAX_REFS + 25):
            path = _mkfile(os.path.join(self.cwd, f"f{i}.pdf"))
            self._text_card(f"c{i}", f"產出 {path}")
        refs = carddigest.media_refs_in_cards(self.store, self._vet)
        self.assertEqual(len(refs), carddigest._MEDIA_SCAN_MAX_REFS)


class AttachBackToCardsTest(unittest.TestCase):
    """`attach_captured_media`:掛回去、live 重發、既有 codex 行為不變。"""

    def setUp(self):
        self.store = carddigest.SessionCardStore()
        self.path = "/tmp/pocket-ccmedia/demo.mp4"
        self.cap = [{"media_id": "med_1", "available": True,
                     "source_ref": self.path, "filename": "demo.mp4",
                     "mime": "video/mp4", "kind": "video", "byte_size": 42,
                     "source_kind": "path",
                     "download_url": "/app/v2/artifacts/med_1"}]

    def _text_card(self, cid, text, kind="markdown", **body):
        self.store.upsert_card(carddigest.make_card(
            cid, "t1", "assistant", kind,
            {"text": text, "fallback_text": text, **body}))

    def test_text_card_gets_attachment(self):
        self._text_card("c1", f"影片在 {self.path}")
        self.assertEqual(
            carddigest.attach_captured_media(self.store, self.cap), 1)
        atts = self.store.cards["c1"]["body"]["attachments"]
        self.assertEqual(atts[0]["download_url"], "/app/v2/artifacts/med_1")

    def test_tool_call_summary_card_gets_attachment(self):
        self.store.upsert_card(carddigest.make_card(
            "c1", "t1", "assistant", "tool_call",
            {"tool": "Write", "summary": self.path,
             "fallback_text": "› 🔧 Write"}))
        self.assertEqual(
            carddigest.attach_captured_media(self.store, self.cap), 1)
        self.assertEqual(
            self.store.cards["c1"]["body"]["attachments"][0]["media_id"],
            "med_1")

    def test_attachment_card_path_gets_download_url(self):
        """這是 CC/cc2 的核心病灶:看得到檔名、點不開。"""
        self.store.upsert_card(carddigest.make_card(
            "c1", "t1", "assistant", "attachment",
            {"filename": "demo.mp4", "path": self.path,
             "fallback_text": "🖼 demo.mp4"}))
        self.assertEqual(
            carddigest.attach_captured_media(self.store, self.cap), 1)
        body = self.store.cards["c1"]["body"]
        self.assertEqual(body["download_url"], "/app/v2/artifacts/med_1")
        self.assertEqual(body["media_id"], "med_1")
        self.assertEqual(body["byte_size"], 42)
        self.assertTrue(body["available"])
        # 原有欄位不動(app 已經在渲染 filename/path)。
        self.assertEqual(body["filename"], "demo.mp4")
        self.assertEqual(body["path"], self.path)
        # 單一附件卡不該同時長出 attachments 陣列(契約 §6:同組欄位)。
        self.assertNotIn("attachments", body)

    def test_rendered_kind_wins_over_tool_cards(self):
        """契約 §6:只有 text/markdown/attachment 會渲染 attachments。

        實測的 CC 形狀是「先宣告、再動手」:markdown「我把報告寫到 /x.pdf」
        → tool_call Write → tool_result「File created at /x.pdf」。純粹由後
        往前掃的話 tool_result 先搶到,而「一支檔只掛一張卡」又讓那張**會
        渲染的** markdown 卡拿不到 → 使用者看到的還是裸路徑,log 卻報成功。
        """
        self._text_card("m1", f"我把報告寫到 {self.path}")
        self.store.upsert_card(carddigest.make_card(
            "tc1", "t1", "assistant", "tool_call",
            {"tool": "Write", "summary": self.path, "fallback_text": "› 🔧"}))
        self.store.upsert_card(carddigest.make_card(
            "tr1", "t1", "assistant", "tool_result",
            {"text": f"File created at {self.path}", "fallback_text": "↳"}))

        self.assertEqual(
            carddigest.attach_captured_media(self.store, self.cap), 1)
        self.assertTrue(self.store.cards["m1"]["body"].get("attachments"))
        self.assertNotIn("attachments", self.store.cards["tc1"]["body"])
        self.assertNotIn("attachments", self.store.cards["tr1"]["body"])

    def test_tool_card_is_fallback_when_no_rendered_card(self):
        """只有工具卡提到 → 還是掛上去(additive,總比裸路徑好)。"""
        self.store.upsert_card(carddigest.make_card(
            "tr1", "t1", "assistant", "tool_result",
            {"text": f"wrote {self.path}", "fallback_text": "↳"}))
        self.assertEqual(
            carddigest.attach_captured_media(self.store, self.cap), 1)
        self.assertTrue(self.store.cards["tr1"]["body"]["attachments"])

    def test_dedupe_is_per_turn_not_per_store(self):
        """跨回合刻意**不**去重。

        codex 常在第 3 回合與第 18 回合引用同一份報告;只給最後一次的話
        使用者往上捲就又看到裸文字 —— 那正是 test_cx_file_citation_attach
        要擋的病,只是換到捲回去才看得到。
        """
        self.store.upsert_card(carddigest.make_card(
            "t3", "turn-3", "assistant", "markdown",
            {"text": f"報告在 {self.path}", "fallback_text": "x"}))
        self.store.upsert_card(carddigest.make_card(
            "t18", "turn-18", "assistant", "markdown",
            {"text": f"還是那份 {self.path}", "fallback_text": "x"}))
        self.assertEqual(
            carddigest.attach_captured_media(self.store, self.cap), 2)
        for cid in ("t3", "t18"):
            self.assertEqual(
                self.store.cards[cid]["body"]["attachments"][0]["media_id"],
                "med_1", cid)

    def test_one_card_per_file_within_a_turn(self):
        self.store.upsert_card(carddigest.make_card(
            "a", "turn-1", "assistant", "markdown",
            {"text": f"要寫到 {self.path}", "fallback_text": "x"}))
        self.store.upsert_card(carddigest.make_card(
            "b", "turn-1", "assistant", "markdown",
            {"text": f"寫好了 {self.path}", "fallback_text": "x"}))
        self.assertEqual(
            carddigest.attach_captured_media(self.store, self.cap), 1)
        self.assertNotIn("attachments", self.store.cards["a"]["body"])
        self.assertTrue(self.store.cards["b"]["body"]["attachments"])

    def test_attachment_card_falls_back_to_text_match(self):
        """body.path 對不上(相對寫法)時退回內文比對 —— 舊版的退路不准拆。"""
        self.store.upsert_card(carddigest.make_card(
            "c1", "t1", "assistant", "attachment",
            {"filename": "demo.mp4", "path": "./demo.mp4",
             "fallback_text": f"🖼 demo.mp4 {self.path}"}))
        self.assertEqual(
            carddigest.attach_captured_media(self.store, self.cap), 1)
        self.assertEqual(self.store.cards["c1"]["body"]["download_url"],
                         "/app/v2/artifacts/med_1")

    def test_url_items_never_attached(self):
        """媒體擷取也會收內文網址(source_kind=url),掛上去是點不開的空殼。"""
        self._text_card("c1", f"看 {self.path}")
        bad = [dict(self.cap[0], source_kind="url", download_url=None)]
        self.assertEqual(carddigest.attach_captured_media(self.store, bad), 0)

    def test_emit_false_does_not_touch_ring(self):
        self._text_card("c1", f"影片在 {self.path}")
        before = self.store.seq
        carddigest.attach_captured_media(self.store, self.cap)
        self.assertEqual(self.store.seq, before, "冷載不該灌 ring")

    def test_emit_true_pushes_card_upsert(self):
        self._text_card("c1", f"影片在 {self.path}")
        before_seq, before_rev = self.store.seq, self.store.cards["c1"]["rev"]
        self.assertEqual(
            carddigest.attach_captured_media(self.store, self.cap, emit=True), 1)
        self.assertGreater(self.store.seq, before_seq)
        self.assertGreater(self.store.cards["c1"]["rev"], before_rev)
        evs = [e for e in self.store.events if e["type"] == "card.upsert"]
        self.assertTrue(evs[-1]["data"]["card"]["body"]["attachments"])

    def test_idempotent_second_pass_is_noop(self):
        """掛完就有 attachments → 下一輪掃描跳過,不會每回合 rev+1 無限長。"""
        self._text_card("c1", f"影片在 {self.path}")
        carddigest.attach_captured_media(self.store, self.cap, emit=True)
        seq = self.store.seq
        self.assertEqual(
            carddigest.attach_captured_media(self.store, self.cap, emit=True), 0)
        self.assertEqual(self.store.seq, seq)

    def test_ts_not_bumped_by_emit(self):
        """重發不准把歷史卡的時間拉到現在(卡片是照 ts 排序的)。"""
        self.store.upsert_card(carddigest.make_card(
            "c1", "t1", "assistant", "markdown",
            {"text": f"影片在 {self.path}", "fallback_text": "x"}, 1000.0))
        carddigest.attach_captured_media(self.store, self.cap, emit=True)
        self.assertEqual(self.store.cards["c1"]["ts"], 1000.0)

    def test_empty_inputs(self):
        self._text_card("c1", f"影片在 {self.path}")
        self.assertEqual(carddigest.attach_captured_media(self.store, []), 0)
        self.assertEqual(carddigest.attach_captured_media(self.store, None), 0)


class CcCardShapesProduceRefsTest(unittest.TestCase):
    """真實 provider 卡片形狀 → 掃得到路徑(cc tmux 與 cc2 各一條)。"""

    def setUp(self):
        self.cwd = tempfile.mkdtemp(prefix="shape-cwd-", dir=_TMP)
        self.roots = media_artifacts.attach_roots([self.cwd])

    def _vet(self, ref):
        return media_artifacts.vet_attachable_path(ref, self.roots)

    def test_cc_tmux_jsonl_write_tool(self):
        """`cc_event_to_cards` 吃一行真實 jsonl → 掃得到那支 mp4。"""
        mp4 = _mkfile(os.path.join(self.cwd, "render.mp4"))
        store = carddigest.SessionCardStore()
        event = {"type": "assistant", "timestamp": 1700000000,
                 "message": {"content": [
                     {"type": "tool_use", "id": "tu1", "name": "Bash",
                      "input": {"command": f"ffmpeg -i a.mov {mp4}"}},
                 ]}}
        for card in carddigest.cc_event_to_cards(event, "uid1"):
            store.upsert_card(card)
        self.assertIn(mp4, carddigest.media_refs_in_cards(store, self._vet))

    def test_cc_tmux_image_attachment_card(self):
        png = _mkfile(os.path.join(self.cwd, "shot.png"))
        store = carddigest.SessionCardStore()
        event = {"type": "assistant", "timestamp": 1700000000,
                 "message": {"content": [
                     {"type": "tool_use", "id": "tu1", "name": "Read",
                      "input": {"file_path": png}},
                 ]}}
        cards = list(carddigest.cc_event_to_cards(event, "uid1"))
        for card in cards:
            store.upsert_card(card)
        self.assertTrue(any(c["kind"] == "attachment" for c in cards))
        self.assertIn(png, carddigest.media_refs_in_cards(store, self._vet))

    def test_cc2_digest_tool_card(self):
        """cc2 的 CC2Digest 走同一組卡片形狀 → 同一支掃描器掃得到。"""
        import cc_sdk
        pdf = _mkfile(os.path.join(self.cwd, "out.pdf"))
        d = cc_sdk.CC2Digest("unit")
        d.turn_begin("turn1")
        d.tool_card("Write", pdf, use_id="tu1", inp={"file_path": pdf})
        d.assistant_final(f"寫好了,檔案在 {pdf}")
        refs = carddigest.media_refs_in_cards(d.store, self._vet)
        self.assertEqual(refs, [pdf])

    def test_cc2_turn_end_fires_media_attach_hook(self):
        """回合收尾一定要呼叫 MEDIA_ATTACH —— 否則 live 那條還是斷的。"""
        import cc_sdk
        seen = []
        prev = cc_sdk.MEDIA_ATTACH
        cc_sdk.MEDIA_ATTACH = lambda store, sid: seen.append((store, sid))
        try:
            d = cc_sdk.CC2Digest("unit2")
            d.turn_begin("turn1")
            d.turn_end()
        finally:
            cc_sdk.MEDIA_ATTACH = prev
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], "cc2:unit2")
        self.assertIs(seen[0][0], d.store)

    def test_cc2_turn_end_survives_broken_hook(self):
        import cc_sdk

        def boom(store, sid):
            raise RuntimeError("nope")

        prev = cc_sdk.MEDIA_ATTACH
        cc_sdk.MEDIA_ATTACH = boom
        try:
            d = cc_sdk.CC2Digest("unit3")
            d.turn_begin("turn1")
            d.turn_end()                  # 不准丟出去
        finally:
            cc_sdk.MEDIA_ATTACH = prev
        self.assertFalse(d.busy)


if __name__ == "__main__":
    unittest.main()
