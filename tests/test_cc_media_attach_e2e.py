"""端到端:卡片裡的本機產出物 → 可點附件 → Bearer 真的下載得到(2026-11-17)。

`test_cc_media_attachments.py` 釘的是各零件;這一檔釘的是**整條線**:
bridge 掃卡 → `capture_path` 入庫 → `attach_captured_media` 掛回 →
`GET /app/v2/artifacts/{media_id}` 吐出位元組。另外釘兩條紅線:
  · 掃描的允許根目錄比媒體庫的 `safe_roots` **窄** —— cwd 以外的檔案就算
    媒體庫收得下也不准變成可下載的附件(路徑逃逸)。
  · artifact 下載一律要 Bearer。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_TEST_ROOT = tempfile.mkdtemp(prefix="cc-media-e2e-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TEST_ROOT, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")

import bridge  # noqa: E402
import carddigest  # noqa: E402
from media_artifacts import MediaArtifactStore  # noqa: E402
from starlette.requests import Request  # noqa: E402


def _request(token: str | None = None) -> Request:
    token = bridge.BRIDGE_TOKEN if token is None else token
    return Request({
        "type": "http", "method": "GET", "path": "/",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    })


class MediaAttachEndToEndTests(unittest.IsolatedAsyncioTestCase):
    SESSION = "claude_code:unit"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.cwd = self.base / "workdir"
        self.outside = self.base / "elsewhere"
        self.cwd.mkdir()
        self.outside.mkdir()
        # 媒體庫的 safe_roots 刻意開得**比掃描根目錄寬**(整個 base),這樣
        # 「逃逸被擋下來」才是掃描那一層的功勞,不是媒體庫順手擋掉的。
        self.store = MediaArtifactStore(
            self.base / "store", safe_roots=[str(self.base)])
        self.cards = carddigest.SessionCardStore()
        self.cards.media_session_id = self.SESSION

    def tearDown(self):
        self.temp.cleanup()

    def _card(self, cid: str, text: str):
        self.cards.upsert_card(carddigest.make_card(
            cid, "t1", "assistant", "markdown",
            {"text": text, "fallback_text": text}))

    async def _attach(self, *, temp_roots=None) -> int:
        """`temp_roots=()` = 只允許 cwd —— 觀察 cwd 邊界時必須這樣。

        測試的 tmpdir 本身就住在暫存區底下,不關掉 `temp_roots` 的話
        「cwd 以外」在測試環境裡仍然落在允許根目錄內,邊界就看不出來。
        """
        patches = [
            patch.object(bridge, "_MEDIA_ARTIFACT_STORE", self.store),
            patch.object(bridge, "_media_session_cwd", lambda sid: str(self.cwd)),
        ]
        if temp_roots is not None:
            patches.append(patch.object(bridge.media_artifacts, "temp_roots",
                                        lambda: tuple(temp_roots)))
        for p in patches:
            p.start()
        try:
            return await bridge._media_attach_cards(
                self.cards, self.SESSION)
        finally:
            for p in reversed(patches):
                p.stop()

    async def test_video_in_workdir_becomes_downloadable(self):
        video = self.cwd / "render.mp4"
        video.write_bytes(b"\x00\x00\x00\x18ftypmp42-unit")
        self._card("c1", f"算完了,影片在 {video}")

        self.assertEqual(await self._attach(), 1)

        atts = self.cards.cards["c1"]["body"]["attachments"]
        self.assertEqual(len(atts), 1)
        self.assertEqual(atts[0]["filename"], "render.mp4")
        self.assertEqual(atts[0]["kind"], "video")
        self.assertTrue(atts[0]["available"])
        media_id = atts[0]["media_id"]
        self.assertEqual(atts[0]["download_url"],
                         f"/app/v2/artifacts/{media_id}")

        # Bearer 取得位元組 —— 這就是機主要的「在 Pocket 點得開」。
        with patch.object(bridge, "_MEDIA_ARTIFACT_STORE", self.store):
            res = await bridge.v2_artifact_download(media_id, _request())
        self.assertEqual(Path(res.path).read_bytes(), b"\x00\x00\x00\x18ftypmp42-unit")

        # 快照(app 冷載讀的東西)真的帶著附件。
        snap = self.cards.snapshot(limit=10)
        got = [c for c in snap["cards"] if c["id"] == "c1"][0]
        self.assertEqual(got["body"]["attachments"][0]["media_id"], media_id)

    async def test_download_requires_bearer(self):
        video = self.cwd / "render.mp4"
        video.write_bytes(b"bytes")
        self._card("c1", f"影片在 {video}")
        await self._attach()
        media_id = self.cards.cards["c1"]["body"]["attachments"][0]["media_id"]
        with patch.object(bridge, "_MEDIA_ARTIFACT_STORE", self.store):
            with self.assertRaises(Exception):
                await bridge.v2_artifact_download(media_id, _request("wrong"))

    async def test_path_outside_workdir_not_attached(self):
        """路徑逃逸:媒體庫收得下,但掃描層不准它變成附件。"""
        leak = self.outside / "private.pdf"
        leak.write_bytes(b"%PDF-private")
        self._card("c1", f"參考 {leak}")

        self.assertEqual(await self._attach(temp_roots=()), 0)
        self.assertNotIn("attachments", self.cards.cards["c1"]["body"])
        # 也不准偷偷進媒體庫索引(= 不會從 /media 冒出一條可下載的列)。
        self.assertEqual(self.store.list_session(self.SESSION)["items"], [])

    async def test_symlink_in_workdir_cannot_escape(self):
        leak = self.outside / "private.pdf"
        leak.write_bytes(b"%PDF-private")
        link = self.cwd / "looks-fine.pdf"
        os.symlink(leak, link)
        self._card("c1", f"檔案 {link}")

        self.assertEqual(await self._attach(temp_roots=()), 0)
        self.assertEqual(self.store.list_session(self.SESSION)["items"], [])

    def test_var_folders_is_not_an_attach_root(self):
        """macOS 的 per-user TMPDIR 不在允許根目錄內(住著別的 app 的快取)。

        媒體庫的 `safe_roots` 仍然收它 —— 那是「可以封存」的範圍,不是
        「可以掛成下載連結」的範圍。兩者刻意不同。
        """
        per_user_tmp = os.path.realpath("/var/folders")
        roots = bridge.media_artifacts.attach_roots(["/Users/you/apps/repo"])
        self.assertNotIn(per_user_tmp, roots)
        self.assertIn(per_user_tmp, bridge.media_artifacts.default_safe_roots())
        # /tmp 是 agent 放產出物的慣例位置,刻意保留。
        self.assertIn(os.path.realpath("/tmp"), roots)
        self.assertIn(os.path.realpath("/Users/you/apps/repo"), roots)

    async def test_source_code_not_attached(self):
        src = self.cwd / "pipeline.py"
        src.write_text("print('hi')\n")
        self._card("c1", f"改好了 {src}")
        self.assertEqual(await self._attach(), 0)

    async def test_live_emit_pushes_event_and_is_idempotent(self):
        video = self.cwd / "render.mp4"
        video.write_bytes(b"v")
        self._card("c1", f"影片在 {video}")
        before = self.cards.seq

        self.assertEqual(await self._attach(), 1)
        self.assertGreater(self.cards.seq, before)
        evs = [e for e in self.cards.events if e["type"] == "card.upsert"]
        self.assertTrue(evs[-1]["data"]["card"]["body"]["attachments"])

        # 第二次(下一回合收尾)不該再掛、不該再發事件、也不該再 stat。
        seq = self.cards.seq
        self.assertEqual(await self._attach(), 0)
        self.assertEqual(self.cards.seq, seq)

    async def test_one_file_attaches_to_one_card_only(self):
        """同一支檔被多張卡提到 → 只掛在**最後**那張(不然同圖連續出現三遍)。

        實測來源:amulet-hunter 的 vid-B.png 同時出現在 tool_call 的 summary、
        tool_result 的輸出、以及專屬的 attachment 卡。
        """
        video = self.cwd / "render.mp4"
        video.write_bytes(b"v")
        self._card("c1", f"在跑 {video}")
        self._card("c2", f"做好了 {video} 請看")
        self.assertEqual(await self._attach(), 1)
        self.assertNotIn("attachments", self.cards.cards["c1"]["body"])
        self.assertEqual(
            len(self.cards.cards["c2"]["body"]["attachments"]), 1)
        # 媒體庫只有一列(同 session 同路徑冪等)。
        self.assertEqual(len(self.store.list_session(self.SESSION)["items"]), 1)

    async def test_attachment_card_wins_over_text_cards(self):
        """有專屬 attachment 卡時,提到同一支檔的其他卡一律不再掛。"""
        png = self.cwd / "shot.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n")
        self.cards.upsert_card(carddigest.make_card(
            "tc1", "t1", "assistant", "tool_call",
            {"tool": "Read", "summary": str(png), "fallback_text": "› 🔧 Read"}))
        self.cards.upsert_card(carddigest.make_card(
            "tc1-img", "t1", "assistant", "attachment",
            {"filename": "shot.png", "path": str(png),
             "fallback_text": "🖼 shot.png"}))
        self._card("tr1", f"↳ 結果 {png}")

        self.assertEqual(await self._attach(), 1)
        self.assertTrue(self.cards.cards["tc1-img"]["body"]["download_url"])
        self.assertNotIn("attachments", self.cards.cards["tc1"]["body"])
        self.assertNotIn("attachments", self.cards.cards["tr1"]["body"])

    async def test_oversized_file_indexed_without_blob(self):
        """超過上限只留 metadata(不複製),所以不會被掛成可下載附件。"""
        small_store = MediaArtifactStore(
            self.base / "store2", safe_roots=[str(self.base)], max_bytes=8)
        video = self.cwd / "huge.mp4"
        video.write_bytes(b"0123456789abcdef")
        self._card("c1", f"影片在 {video}")
        with patch.object(bridge, "_MEDIA_ARTIFACT_STORE", small_store), \
             patch.object(bridge, "_media_session_cwd", lambda sid: str(self.cwd)):
            self.assertEqual(await bridge._media_attach_cards(
                self.cards, self.SESSION), 0)
        row = small_store.list_session(self.SESSION)["items"][0]
        self.assertFalse(row["available"])
        self.assertEqual(row["unavailable_reason"], "too_large")
        self.assertNotIn("attachments", self.cards.cards["c1"]["body"])

    async def test_concurrent_attaches_do_not_lose_attachments(self):
        """併發掃描不准互相吃掉彼此的工(2026-11-17 實測抓到的競態)。

        CC 冷載會同時觸發兩條掃描(`_cc_digest_lines` 的背景任務 +
        `_v2_card_store` 入口)。「看過了」的記憶是共用的,沒有鎖的話後到的
        那條會掃出 0 筆,而先到的那條**還沒寫回附件**就返回 —— 實測
        amulet-hunter 的附件數在 1 / 3 / 7 之間跳。有鎖之後:兩條都 await
        完成時,附件一定已經在卡片上。
        """
        import asyncio
        for i in range(6):
            f = self.cwd / f"out{i}.pdf"
            f.write_bytes(b"%PDF-" + bytes([48 + i]))
            self._card(f"c{i}", f"產出 {f}")

        # 併發時不能用 self._attach:它自己 start/stop patch,四條交錯會互相
        # 拆掉對方的 patch。這裡一次 patch 好,直接併發呼叫受測函式。
        with patch.object(bridge, "_MEDIA_ARTIFACT_STORE", self.store), \
             patch.object(bridge, "_media_session_cwd",
                          lambda sid: str(self.cwd)):
            results = await asyncio.gather(*[
                bridge._media_attach_cards(self.cards, self.SESSION)
                for _ in range(4)])

        attached = [c for c in self.cards.cards.values()
                    if (c.get("body") or {}).get("attachments")]
        self.assertEqual(len(attached), 6,
                         f"掛上 {len(attached)}/6,results={results}")
        self.assertEqual(sum(results), 6, "總工作量不該重複計算")

    async def test_every_pass_emits_so_live_clients_see_it(self):
        """任何一次掃描都要發 `card.upsert` —— 不准有「不發事件」的那種。

        原本冷載走 emit=False(想省掉灌 ring),但兩條路共用「看過了」記憶,
        所以**先跑到的那條把工作吃掉**:媒體抽屜輪詢 / 第二台裝置冷載 /
        guest 頁刷新只要搶先,附件就只寫進 store、沒有事件;稍後 live 那條
        掃到 0 筆什麼都不發。已連線的 app 的 rev 永遠不前進 → 使用者還是
        看不到附件。這一條釘住「不會再有沉默的那一半」。
        """
        video = self.cwd / "render.mp4"
        video.write_bytes(b"v")
        self._card("c1", f"影片在 {video}")

        # 第一次掃描(模擬冷載/抽屜輪詢搶先)就必須發事件。
        before = self.cards.seq
        self.assertEqual(await self._attach(), 1)
        pushed = [e for e in self.cards.events
                  if e["type"] == "card.upsert" and e["seq"] > before]
        self.assertTrue(pushed, "第一次掃描沒發事件 → 在線的 app 永遠看不到")
        self.assertTrue(pushed[-1]["data"]["card"]["body"]["attachments"])
        self.assertEqual(pushed[-1]["data"]["card"]["id"], "c1")

    def test_lock_is_scoped_to_the_running_loop(self):
        """store 常駐、loop 會換 → 鎖必須跟著 loop 換。

        `asyncio.Lock` 在**有競爭**時才會把等待者掛到當下的 loop 上;一把
        跨 loop 重用的鎖會在那一刻丟 `RuntimeError: bound to a different
        event loop`,而 `_media_attach_cards` 外層是 `except Exception`,
        所以結果是整個附件功能**安靜地變成 no-op**,只留一行沒人在看的 log。
        無競爭時不會炸,所以這裡釘的是**不變式**(鎖與 loop 同生命週期),
        不是症狀 —— 症狀只在負載下出現,測不到。
        """
        import asyncio
        video = self.cwd / "render.mp4"
        video.write_bytes(b"v")
        self._card("c1", f"影片在 {video}")
        self.assertEqual(asyncio.run(self._attach()), 1)     # loop #1
        first = getattr(self.cards, "media_attach_lock", None)
        self.assertIsInstance(first, tuple, "鎖必須連 loop 一起記")

        second = self.cwd / "second.pdf"
        second.write_bytes(b"%PDF-2")
        self._card("c2", f"還有 {second}")
        self.assertEqual(asyncio.run(self._attach()), 1)     # loop #2
        later = getattr(self.cards, "media_attach_lock", None)
        self.assertIsNot(later[1], first[1],
                         "換了 loop 卻重用舊鎖 → 有競爭時會安靜地停擺")
        self.assertIsNot(later[0], first[0])

    async def test_scan_state_kept_on_store(self):
        video = self.cwd / "render.mp4"
        video.write_bytes(b"v")
        self._card("c1", f"影片在 {video}")
        await self._attach()
        self.assertIn(str(video), getattr(self.cards, "media_scanned_refs"))

    async def test_attach_is_failsafe(self):
        self._card("c1", "沒有附件的一般回覆")
        with patch.object(bridge, "_media_session_cwd",
                          side_effect=RuntimeError("boom")):
            self.assertEqual(
                await bridge._media_attach_cards(self.cards, self.SESSION), 0)

    async def test_no_session_id_is_noop(self):
        self.assertEqual(await bridge._media_attach_cards(self.cards, ""), 0)
        self.assertEqual(await bridge._media_attach_cards(None, self.SESSION), 0)


class MediaSessionCwdTests(unittest.TestCase):
    """`_media_session_cwd`:各 provider 的工作目錄解析(查不到一律 "")。"""

    def test_claude_code_prefix_reads_sessions_conf(self):
        with patch.object(bridge, "_cc_conf_rows",
                          lambda: [("flip", "/Users/you/apps/fliper")]):
            self.assertEqual(bridge._media_session_cwd("claude_code:flip"),
                             "/Users/you/apps/fliper")
            self.assertEqual(bridge._media_session_cwd("flip"),
                             "/Users/you/apps/fliper")
            self.assertEqual(bridge._media_session_cwd("claude_code:nope"), "")

    def test_cc2_reads_registry_workdir(self):
        class _S:
            workdir = "/Users/you/apps/pocket-cis"

        class _R:
            def get(self, name):
                return _S() if name == "live" else None

        with patch.object(bridge.cc_sdk, "registry", lambda: _R()):
            self.assertEqual(bridge._media_session_cwd("cc2:live"),
                             "/Users/you/apps/pocket-cis")
            self.assertEqual(bridge._media_session_cwd("cc2:ghost"), "")

    def test_acp_providers_read_session_row(self):
        for prov, module in (("gemini", bridge.gemini_provider),
                             ("grok", bridge.grok_sdk),
                             ("opencode", bridge.opencode_sdk)):
            with patch.object(module, "get_session",
                              lambda key: {"cwd": f"/w/{key}"}):
                self.assertEqual(bridge._media_session_cwd(f"{prov}:k1"),
                                 "/w/k1")

    def test_codex_uses_the_thread_cwd_cache(self):
        """codex 的 cwd 要靠旁觀快取 —— `thread/read` 是 async RPC,而附件掃描
        跑在 worker thread 裡沒辦法 await 它。沒快取過就退化成只掃暫存區
        (不會掛錯東西,只是掛不到),所以「有快取就要用」必須釘住。"""
        bridge._CX_THREAD_CWD.pop("tid-1", None)
        self.assertEqual(bridge._media_session_cwd("codex:tid-1"), "")
        bridge._cx_note_thread_cwd("tid-1", "/Users/you/apps/foo")
        try:
            self.assertEqual(bridge._media_session_cwd("codex:tid-1"),
                             "/Users/you/apps/foo")
        finally:
            bridge._CX_THREAD_CWD.pop("tid-1", None)

    def test_thread_cwd_cache_is_bounded(self):
        bridge._CX_THREAD_CWD.clear()
        for i in range(2100):
            bridge._cx_note_thread_cwd(f"t{i}", f"/w/{i}")
        self.assertLessEqual(len(bridge._CX_THREAD_CWD), 2100)
        self.assertLess(len(bridge._CX_THREAD_CWD), 2500)
        bridge._CX_THREAD_CWD.clear()

    def test_unknown_providers_have_no_cwd(self):
        # openclaw 仍無附件 root;hermes 改成有專屬 outbox(2026-11-21 W1-③ A 案),
        # 見 test_persona_sessions_resolve_to_outbox。
        self.assertEqual(bridge._media_session_cwd("openclaw:agent:main:main"), "")

    def test_persona_sessions_resolve_to_outbox(self):
        # 人格的附件 root = 人格 home 下的 outbox 子目錄(不是整個 home)。
        cwd = bridge._media_session_cwd("hermes:cash")
        self.assertTrue(cwd.endswith(os.sep + "outbox"), cwd)
        self.assertNotEqual(cwd, "")

    def test_resolution_is_failsafe(self):
        with patch.object(bridge, "_cc_conf_rows",
                          side_effect=RuntimeError("boom")):
            self.assertEqual(bridge._media_session_cwd("claude_code:x"), "")


if __name__ == "__main__":
    unittest.main()
