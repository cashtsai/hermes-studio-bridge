"""現象:CC 產出的影片/報告檔,在 Pocket 上看不到、點不開。

機主 2026-11-17 實錘:「CC 給的影片在 Pocket 看不到,要開 Claude 才拿得到」。
根因雙重 ——
  (1) `carddigest._IMAGE_EXTS` 只認 7 種圖片,`.mp4`/`.pdf`/`.pptx`/`.csv`/`.zip`
      根本不出卡;
  (2) 掛附件的 `attach_captured_media` 靠「內文含絕對路徑」比對,而 attachment
      卡沒有 `body.text` 永遠不命中 —— 且那支函式全 repo 只被 codex 的 seed
      呼叫一次,tmux CC 這條線根本沒接上。

修復(`36bb691`):媒體管線補到 CC/cc2,`GET /app/v2/sessions/{id}/cards` 的卡
帶 `body.attachments[]`(filename/kind/media_id/download_url),Bearer 直接
下載得到位元組。

base = `36bb691^`(= 520deb1)

狀態是這樣造出來的:CC 自己的 transcript jsonl(Claude Code 的檔案格式,
產品的**外部輸入**)+ 工作目錄裡真的有那兩個檔,然後**只打 API** 讀卡片。
沒有手刻任何一張卡、沒有碰 canonical.db —— 所以同一份 scenario 在 base 上
跑的是 base 的媒體管線,不是一份對不上的 fixture。

跑法:
    scripts/prove-scenario.sh cc_media_attachment_missing 36bb691^
"""
import _scenario
import json
import os
import unittest
import uuid

SESSION = "cc-media"


class CcFileOutputHasNoAttachment(unittest.TestCase):
    """使用者視角:CC 說「影片在這」,我在 Pocket 點得開嗎。"""

    def setUp(self):
        self.tmux = _scenario.FakeTmux()
        self.addCleanup(self.tmux.stop)
        self.tmux.add_session(SESSION, transcript=["● 影片算完了。"])

        self.workdir = _scenario.tmpdir("ccmedia-work-")
        _scenario.register_cc_session(SESSION, self.workdir)

        # CC 真的在工作目錄裡產出了兩個檔(非圖片 —— 這正是 bug 的面)。
        self.video = os.path.join(self.workdir, "render.mp4")
        self.report = os.path.join(self.workdir, "report.pdf")
        self.video_bytes = b"\x00\x00\x00\x18ftypmp42" + b"scenario-video"
        with open(self.video, "wb") as f:
            f.write(self.video_bytes)
        with open(self.report, "wb") as f:
            f.write(b"%PDF-1.7\nscenario-report\n%%EOF\n")

        # CC 自己的 transcript(Claude Code 的 jsonl 格式 = 外部輸入)。
        # HOME 導到 tmp,這樣 `~/.claude/projects/...` 落在暫存區。
        home = _scenario.tmpdir("ccmedia-home-")
        self._home = os.environ.get("HOME")
        os.environ["HOME"] = home
        self.addCleanup(lambda: os.environ.__setitem__("HOME", self._home)
                        if self._home else None)
        slug = self.workdir.replace("/", "-")      # Claude Code 的目錄命名慣例
        proj = os.path.join(home, ".claude", "projects", slug)
        os.makedirs(proj, exist_ok=True)
        with open(os.path.join(proj, "session.jsonl"), "w", encoding="utf-8") as f:
            for payload in (
                {"type": "user", "uuid": str(uuid.uuid4()),
                 "timestamp": "2026-11-17T01:00:00.000Z",
                 "message": {"content": "幫我把那段算完並出一份報告"}},
                {"type": "assistant", "uuid": str(uuid.uuid4()),
                 "timestamp": "2026-11-17T01:05:00.000Z",
                 "message": {"content": [{"type": "text", "text":
                                          f"算完了。影片在 {self.video},"
                                          f"報告在 {self.report}"}]}},
            ):
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")

        _scenario.reset_auth_throttle()
        self.http = _scenario.http_client()
        self.addCleanup(self.http.close)

    def _attachments(self) -> list:
        res = self.http.get(f"/app/v2/sessions/claude_code:{SESSION}/cards",
                            headers=_scenario.auth_header())
        self.assertEqual(res.status_code, 200, res.text)
        cards = res.json().get("cards") or []
        self.assertTrue(cards, "卡片庫是空的 —— transcript 根本沒進卡(替身沒接上)")
        out = []
        for card in cards:
            out += (card.get("body") or {}).get("attachments") or []
        return out

    def test_file_output_is_a_tappable_attachment(self):
        atts = self._attachments()
        names = sorted(a.get("filename") for a in atts)
        self.assertEqual(
            names, ["render.mp4", "report.pdf"],
            "CC 產出的影片/報告沒有變成可點附件 —— 在 Pocket 上看不到這些檔,"
            f"使用者只能去開 Claude 自己拿。實際附件:{names}")

        video = next(a for a in atts if a.get("filename") == "render.mp4")
        self.assertTrue(video.get("available"), video)
        self.assertTrue(video.get("download_url"), video)

        # 驗行為:Bearer 真的下載得到位元組(「點得開」不是看欄位有填就算)。
        got = self.http.get(video["download_url"],
                            headers=_scenario.auth_header())
        self.assertEqual(got.status_code, 200, got.text[:300])
        self.assertEqual(got.content, self.video_bytes,
                         "下載到的位元組跟 CC 產出的檔案不一樣")


if __name__ == "__main__":
    _scenario.main()
