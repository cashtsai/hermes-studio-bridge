"""bridge 的 CJK/emoji 安全截斷 + PTY 串流解碼。

兩類 bug,兩類重現:

## 1. 截斷切壞「一個字」

Python 的字串是 code point 序列,所以不像 JS 會切出孤立 surrogate —— 連 CJK
Ext-B(`𠀋`,U+2000B;台灣人名地名用字就在這一區)都是單一 code point,`[:n]`
切不壞。**真正的病是 grapheme cluster**:使用者看到的「一個字」常常由多個
code point 組成,`[:n]` 會從中間切開,留下一個豆腐方塊或一面錯的國旗。

下面每一條都附「舊寫法會長什麼樣」的對照斷言 —— 證明舊行為是錯的,不是
憑空加一層抽象。

## 2. PTY 串流在 chunk 邊界解碼

一次 `os.read` 停在哪完全看 kernel,漢字 UTF-8 是 3 bytes,所以讀取邊界常常
落在字的中間。對每塊 bytes 各自 `decode("utf-8", "replace")` → 被切開的漢字
兩側各吐一個 U+FFFD,終端機裡 `ls` 一個中文檔名就變亂碼。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import asyncio
import codecs
import unittest

import cjk_text


# 樣本:每一個都是「使用者眼中的一個字」,但 code point 數 > 1。
FLAG_TW = "🇹🇼"                      # 2 × REGIONAL INDICATOR
THUMB_TONE = "👍🏽"                   # emoji + 膚色修飾符
FAMILY = "👨‍👩‍👧"                  # 3 emoji 用 ZWJ 串起來
KEYCAP_1 = "1️⃣"                     # "1" + VS16 + COMBINING ENCLOSING KEYCAP
SCOT_FLAG = "🏴󠁧󠁢󠁳󠁣󠁴󠁿"           # 黑旗 + 6 個 TAG 字元
IVS_HAN = "茄\U000E0100"             # 漢字 + 表意變體選擇器(異體字)
EXT_B = "𠀋"                         # CJK Ext-B,BMP 外的漢字(單一 code point)


class GraphemeSegmentationTests(unittest.TestCase):
    def test_user_perceived_length(self):
        for sample, want in ((FLAG_TW, 1), (THUMB_TONE, 1), (FAMILY, 1),
                             (KEYCAP_1, 1), (SCOT_FLAG, 1), (IVS_HAN, 1),
                             (EXT_B, 1), ("臺灣", 2), ("𠀋𡃁", 2)):
            with self.subTest(sample=sample):
                self.assertEqual(cjk_text.grapheme_len(sample), want)

    def test_code_point_length_disagrees(self):
        """確認這些樣本真的會踩到裸 [:n] —— 否則測試是假的。"""
        for sample in (FLAG_TW, THUMB_TONE, FAMILY, KEYCAP_1, SCOT_FLAG, IVS_HAN):
            with self.subTest(sample=sample):
                self.assertGreater(len(sample), 1)

    def test_cjk_ext_b_is_a_single_code_point_in_python(self):
        # 任務書寫的是「surrogate pair」—— 那是 JS 的病。Python 這裡本來就安全,
        # 釘住這個事實,免得下次有人又為它加一層沒必要的保護。
        self.assertEqual(len(EXT_B), 1)
        self.assertEqual(len(EXT_B.encode("utf-16-le")), 4)   # UTF-16 才是 2 個單元

    def test_crlf_is_one_cluster(self):
        self.assertEqual(cjk_text.graphemes("a\r\nb"), ["a", "\r\n", "b"])


class ClipTests(unittest.TestCase):
    def test_flag_is_never_cut_in_half(self):
        text = "台北" + FLAG_TW + "天氣"
        # 舊寫法:text[:3] 把兩個 regional indicator 切開 → 剩一個落單的 🇹,
        # 渲染成一個方框字母,看起來像亂碼。
        self.assertEqual(text[:3], "台北\U0001F1F9")
        self.assertEqual(cjk_text.grapheme_len(text[:3] + "…"), 4)   # 落單那個自成一格
        # 新寫法:整面旗一起留或一起不留。
        self.assertEqual(cjk_text.clip(text, 3), "台北" + FLAG_TW + "…")
        self.assertEqual(cjk_text.clip(text, 2), "台北…")

    def test_zwj_family_is_never_split(self):
        text = FAMILY + "一家人"
        self.assertEqual(text[:2], "👨‍")            # 舊寫法:男人 + 懸空的 ZWJ
        self.assertEqual(cjk_text.clip(text, 1), FAMILY + "…")

    def test_skin_tone_modifier_stays_with_its_base(self):
        text = THUMB_TONE + "讚啦"
        self.assertEqual(text[:1], "👍")                   # 舊寫法:膚色沒了
        self.assertEqual(cjk_text.clip(text, 1), THUMB_TONE + "…")

    def test_ideographic_variation_selector_stays_with_its_han(self):
        text = IVS_HAN + "子料理"
        # 舊寫法切在 1 → 基字留著、選擇器掉了(異體字變成常體,日文姓名會錯)
        self.assertEqual(text[:1], "茄")
        self.assertEqual(cjk_text.clip(text, 1), IVS_HAN + "…")
        # 更糟的方向:切在選擇器之後的下一字時不該把選擇器留成孤兒
        self.assertEqual(cjk_text.clip(text, 2), IVS_HAN + "子…")

    def test_tag_sequence_flag_is_atomic(self):
        text = SCOT_FLAG + "蘇格蘭"
        self.assertEqual(text[:1], "🏴")                   # 舊寫法:黑旗 + 一串隱形 tag
        self.assertEqual(cjk_text.clip(text, 1), SCOT_FLAG + "…")

    def test_keycap_is_atomic(self):
        text = KEYCAP_1 + "號"
        self.assertEqual(cjk_text.clip(text, 1), KEYCAP_1 + "…")

    def test_cjk_ext_b_never_needs_splitting(self):
        text = EXT_B * 5
        self.assertEqual(cjk_text.clip(text, 3), EXT_B * 3 + "…")

    def test_short_text_is_returned_untouched(self):
        for sample in ("", "abc", "臺灣", FLAG_TW, FAMILY):
            with self.subTest(sample=sample):
                self.assertEqual(cjk_text.clip(sample, 60), sample)

    def test_no_ellipsis_when_not_truncated(self):
        self.assertEqual(cjk_text.clip("剛好十個字的標題", 8), "剛好十個字的標題")

    def test_suffix_can_be_suppressed(self):
        self.assertEqual(cjk_text.clip("一二三四", 2, suffix=""), "一二")

    def test_zero_and_negative_limits(self):
        self.assertEqual(cjk_text.clip("一二三", 0), "")
        self.assertEqual(cjk_text.clip("一二三", -1), "")

    def test_none_is_empty(self):
        self.assertEqual(cjk_text.clip(None, 10), "")

    def test_clip_marked_reports_truncation(self):
        self.assertEqual(cjk_text.clip_marked("短", 10, mark="!"), ("短", False))
        body, cut = cjk_text.clip_marked("台北" + FLAG_TW + "天氣", 3, mark="(截斷)")
        self.assertTrue(cut)
        self.assertEqual(body, "台北" + FLAG_TW + "(截斷)")


class ClipTailTests(unittest.TestCase):
    def test_tail_window_does_not_orphan_a_cluster(self):
        text = "前面的輸出" + FLAG_TW + "後面"
        # 舊寫法 text[-3:] 從旗幟中間切 → 視窗頭部掛一個落單的 🇼
        self.assertEqual(text[-3:], "\U0001F1FC後面")
        self.assertEqual(cjk_text.clip_tail(text, 3), FLAG_TW + "後面")

    def test_tail_shorter_than_limit(self):
        self.assertEqual(cjk_text.clip_tail("短", 10), "短")


class SnapBoundsTests(unittest.TestCase):
    def test_both_ends_expand_to_whole_clusters(self):
        text = "aa" + FAMILY + "bb" + FLAG_TW + "cc"
        start, end = cjk_text.snap_bounds(text, 3, len(text) - 3)
        self.assertEqual(start, 2, "start 落在 ZWJ 家庭中間 → 往前吃整塊")
        frag = text[start:end]
        self.assertTrue(frag.startswith(FAMILY))
        # 片段裡不准出現落單的 regional indicator
        self.assertEqual(frag.count("\U0001F1F9"), frag.count(FLAG_TW))

    def test_bounds_are_clamped(self):
        self.assertEqual(cjk_text.snap_bounds("abc", -5, 99), (0, 3))


class CallSiteTests(unittest.TestCase):
    """真正出貨的呼叫點 —— 換了 helper 之後行為要對。"""

    def test_agent_context_clip_uses_grapheme_boundary(self):
        import agent_context
        body, cut = agent_context.clip("台北" + FLAG_TW + "天氣很好", 3)
        self.assertTrue(cut)
        self.assertTrue(body.startswith("台北" + FLAG_TW))
        self.assertIn("已截斷", body)
        self.assertEqual(agent_context.clip("短", 10), ("短", False))

    def test_agent_context_card_line_keeps_emoji_whole(self):
        import agent_context
        line = agent_context.card_line("user", "台北" + FLAG_TW + "天氣", max_len=3)
        self.assertIn(FLAG_TW, line)
        self.assertNotIn("\U0001F1F9天", line)   # 落單的半面旗不准出現

    def test_agent_context_match_snippet_snaps_to_boundaries(self):
        import agent_context
        text = FLAG_TW.join(["填充" * 60, "命中詞", "填充" * 60])
        frag = agent_context.match_snippet(text, "命中詞", width=10)
        self.assertIsNotNone(frag)
        self.assertNotIn("\U0001F1FC", frag.replace(FLAG_TW, ""))

    def test_grok_titles_are_grapheme_safe(self):
        import grok_sdk
        long_title = ("標題" * 40) + FAMILY
        row = grok_sdk.session_v2_row({"id": "x", "title": long_title})
        self.assertEqual(cjk_text.grapheme_len(row["title"]), 48)

    def test_cc_tool_summary_is_grapheme_safe(self):
        import cc_sdk
        cmd = ("echo 台北" * 20) + FLAG_TW
        summary = cc_sdk._tool_summary("Bash", {"command": cmd})
        self.assertLessEqual(cjk_text.grapheme_len(summary), 118)
        self.assertNotIn("\U0001F1F9…", summary)


class PtyStreamDecodeTests(unittest.TestCase):
    """PTY 串流:漢字被 chunk 邊界切開時不准變亂碼。

    `_TerminalSession.decode` 持有 per-socket 的 incremental decoder,所以
    這裡不需要真的開 PTY —— 直接餵切半的 bytes 進去就能重現/驗證。
    """

    @staticmethod
    def _split(text, at):
        raw = text.encode("utf-8")
        return raw[:at], raw[at:]

    def test_old_per_chunk_decode_corrupts_chinese(self):
        """先證明舊寫法是錯的。"""
        head, tail = self._split("中文檔名.txt", 2)      # 「中」是 3 bytes,切在第 2
        broken = head.decode("utf-8", "replace") + tail.decode("utf-8", "replace")
        self.assertIn("�", broken)
        self.assertNotIn("中", broken)

    def test_stateful_decoder_reassembles_across_chunks(self):
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        head, tail = self._split("中文檔名.txt", 2)
        self.assertEqual(dec.decode(head), "", "未完成的序列必須先留著")
        self.assertEqual(dec.decode(head[:0] + tail), "中文檔名.txt")

    def test_terminal_session_decode_is_stateful(self):
        import bridge
        session = bridge._TerminalSession.__new__(bridge._TerminalSession)
        session._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        head, tail = self._split("臺北 101 大樓", 1)       # 「臺」切在第 1 byte
        out = session.decode(head) + session.decode(tail)
        self.assertEqual(out, "臺北 101 大樓")
        self.assertNotIn("�", out)

    def test_decode_handles_every_split_point_of_a_cjk_line(self):
        import bridge
        line = "測試:臺灣𠀋人名 ✅ 🇹🇼\n"
        raw = line.encode("utf-8")
        for at in range(len(raw) + 1):
            with self.subTest(split=at):
                session = bridge._TerminalSession.__new__(bridge._TerminalSession)
                session._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                out = session.decode(raw[:at]) + session.decode(raw[at:])
                self.assertEqual(out, line)

    def test_final_flush_emits_the_dangling_bytes(self):
        import bridge
        session = bridge._TerminalSession.__new__(bridge._TerminalSession)
        session._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        half = "中".encode("utf-8")[:2]
        self.assertEqual(session.decode(half), "")
        # shell 被砍、殘餘永遠補不完 → 收尾時沖出替代字元,不要無聲吞掉
        self.assertEqual(session.decode(b"", final=True), "�")

    def test_writer_loop_does_not_decode_each_chunk_itself(self):
        """結構守門:writer loop 不准繞過 session.decode 自己 decode。

        這一條是防「下次有人為了少一層呼叫改回 item.decode」—— 改回去不會有
        任何測試壞掉的徵兆,只有中文使用者看到亂碼。
        """
        import inspect
        import bridge
        src = inspect.getsource(bridge.terminal_ws)
        self.assertIn("session.decode(item)", src)
        self.assertNotIn('item.decode("utf-8"', src)


class TerminalWsCjkEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """真的跑 `terminal_ws`,用真 pipe 製造「漢字被讀取邊界切開」。

    姊妹路徑 `app_v1_terminal` 早就有 tests/test_terminal_utf8_chunking.py 守著;
    漏的是這一條(Console 終端機分頁走的就是它)。
    """

    async def _run(self, writes, *, legacy_decode=False):
        import os as _os
        import json
        import bridge
        from unittest.mock import Mock, patch

        # `_TerminalSession.start()` 會 close 它拿到的 slave fd,所以給它一份
        # dup —— 寫入端留我們自己這份,關掉它才是 EOF。
        master, slave = _os.pipe()
        slave_for_handler = _os.dup(slave)
        frames = []

        class Socket:
            query_params: dict = {}
            client_state = bridge.WebSocketState.CONNECTED

            async def accept(self):
                pass

            async def send_json(self, payload):
                frames.append(payload)

            async def send_text(self, raw):
                frames.append(json.loads(raw))

            async def receive_text(self):
                await asyncio.Future()        # 永不回 —— 由 EOF 收場

            async def close(self, **kwargs):
                pass

        proc = Mock()
        proc.poll.return_value = 0
        proc.wait.return_value = 0

        async def feed():
            # 每寫一塊就讓出一次事件圈 → add_reader 各自吃到一塊,
            # 分塊點完全由 writes 決定(這就是重現的關鍵)。
            for chunk in writes:
                _os.write(slave, chunk)
                await asyncio.sleep(0.02)
            _os.close(slave)                  # EOF → 收場

        with (patch.object(bridge, "POCKET_TERMINAL_ENABLED", True),
              patch.object(bridge, "_terminal_token_from_ws", return_value="fixture"),
              patch.object(bridge, "_terminal_device_id_for_token", return_value="dev-1"),
              patch.object(bridge.pty, "openpty",
                           return_value=(master, slave_for_handler)),
              patch.object(bridge.subprocess, "Popen", return_value=proc)):
            if legacy_decode:
                # 修改前的那一行:每塊 bytes 各自 decode,無跨 chunk 狀態。
                patcher = patch.object(
                    bridge._TerminalSession, "decode",
                    lambda self, chunk, final=False: chunk.decode("utf-8", "replace"))
                patcher.start()
            try:
                feeder = asyncio.create_task(feed())
                await bridge.terminal_ws(Socket())
                await feeder
            finally:
                if legacy_decode:
                    patcher.stop()
        return "".join(f["data"] for f in frames if f.get("type") == "output")

    async def test_legacy_per_chunk_decode_garbles_the_whole_line(self):
        """重現:把 decode 換回修改前的寫法,同一條中文輸出全變亂碼。"""
        raw = "中文".encode("utf-8")
        out = await self._run([raw[:2], raw[2:]], legacy_decode=True)
        self.assertIn("�", out)
        self.assertNotIn("中", out)

    async def test_han_split_across_reads_is_not_garbled(self):
        raw = "臺北 101：測試𠀋 🇹🇼\n".encode("utf-8")
        # 逐 byte 餵 —— 最惡劣的分塊,每個多位元組字都被切到底
        out = await self._run([bytes([b]) for b in raw])
        self.assertEqual(out, raw.decode("utf-8"))
        self.assertNotIn("�", out)

    async def test_three_byte_han_split_two_one(self):
        raw = "中文".encode("utf-8")
        out = await self._run([raw[:2], raw[2:]])
        self.assertEqual(out, "中文")


class PowerShellPreambleGateTests(unittest.TestCase):
    """絆線:哪天真的在 Windows 上起 pwsh,UTF-8 前綴不准漏。

    現狀是 bridge **沒有**任何起 pwsh/powershell 的路徑(互動式 shell 一律走
    POSIX `pty.openpty()`),所以這裡不是「已修的 bug」,是「下一個人加這條
    路徑時會被擋下來」。理由與前綴內容見 cjk_text.POWERSHELL_UTF8_PREAMBLE。
    """

    #: 真的在跑 pwsh 的樣子(不是字串裡提到、也不是印給使用者看的說明)
    _SPAWN_HINTS = ("powershell.exe", "pwsh.exe", '"pwsh"', "'pwsh'")

    def test_preamble_is_canonical_and_single_line(self):
        pre = cjk_text.POWERSHELL_UTF8_PREAMBLE
        self.assertIn("[Console]::OutputEncoding", pre)
        self.assertIn("$OutputEncoding", pre)
        self.assertIn("UTF8Encoding]::new($false)", pre, "必須是無 BOM 的 UTF-8")
        self.assertNotIn("\n", pre, "前綴不准換行 —— 會讓 PowerShell 的錯誤行號偏移")

    def test_any_pwsh_spawn_must_carry_the_preamble(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for path in sorted(root.glob("*.py")):
            src = path.read_text(encoding="utf-8", errors="replace")
            if any(h in src for h in self._SPAWN_HINTS) \
                    and "POWERSHELL_UTF8_PREAMBLE" not in src:
                offenders.append(path.name)
        self.assertEqual(
            offenders, [],
            "這些檔案起了 pwsh 卻沒帶 cjk_text.POWERSHELL_UTF8_PREAMBLE —— "
            "Windows PowerShell 5.1 會用 OEM code page 寫出去,中文全亂:"
            + str(offenders))

    def test_interactive_shell_is_still_posix_only(self):
        """釘住現狀的前提:互動式 shell 走 PTY,所以今天確實沒有 pwsh 面。

        這條一紅代表「Windows 原生 shell 支援被加進來了」—— 那就該回來把
        前綴接上,而不是把這條測試刪掉。
        """
        import pathlib
        bridge_src = (pathlib.Path(__file__).resolve().parent.parent
                      / "bridge.py").read_text(encoding="utf-8")
        self.assertIn("pty.openpty()", bridge_src)
        for hint in self._SPAWN_HINTS:
            self.assertNotIn(hint, bridge_src)


if __name__ == "__main__":
    unittest.main()
