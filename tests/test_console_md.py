"""Console Markdown 渲染器(static/console/md.js)的轉換與 XSS 紅線。

為什麼用 node 跑:md.js 是瀏覽器端唯一把「訊息文字」變成 innerHTML 的地方,
樣本必須跑在**真正出貨的那份 JS** 上,Python 重寫一份等於測心安。機器沒有
node 時退而求其次:對 md.js 做靜態斷言(escape-first 結構還在、佔位符剝除
還在),至少擋住「整段安全邏輯被刪掉」這種倒退。

XSS 紅線(任何一條破了都是事故):
  ‧ 輸入裡的 HTML 標籤只能以 &lt;…&gt; 形式出現 —— 永遠不會變成元素
  ‧ href 只可能是 http/https/mailto;javascript:/data:/vbscript: 一律降級純文字
  ‧ 事件屬性(onerror/onclick/…)不可能出現在標籤裡
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MD_JS = os.path.join(ROOT, "static", "console", "md.js")

# node 端跑樣本:stdin 收 JSON cases,逐條檢查 contains / not / notRegex,
# 全部結果回 stdout。md.js 路徑由 env 給,不拼進程式碼。
_RUNNER = r"""
const md = require(process.env.MD_JS);
let cases = "";
process.stdin.on("data", (d) => cases += d);
process.stdin.on("end", () => {
  const fails = [];
  for (const c of JSON.parse(cases)) {
    let out;
    try { out = c.api === "text" ? md.renderText(c.src) : md.render(c.src); }
    catch (e) { fails.push({ name: c.name, error: String(e) }); continue; }
    for (const want of c.contains || [])
      if (!out.includes(want)) fails.push({ name: c.name, miss: want, out });
    for (const bad of c.not || [])
      if (out.includes(bad)) fails.push({ name: c.name, bad, out });
    for (const rx of c.notRegex || [])
      if (new RegExp(rx, "i").test(out)) fails.push({ name: c.name, badRegex: rx, out });
  }
  process.stdout.write(JSON.stringify(fails));
  process.exit(fails.length ? 1 : 0);
});
"""

# 每一條 XSS 樣本都自帶全域紅線(見 _redlines);contains/not 是樣本專屬斷言。
XSS_CASES = [
    {"name": "raw html tag", "src": "<img src=x onerror=alert(1)>",
     "contains": ["&lt;img src=x onerror=alert(1)&gt;"], "not": ["<img"]},
    {"name": "script tag", "src": "hi <script>alert(1)</script>",
     "contains": ["&lt;script&gt;"], "not": ["<script"]},
    {"name": "javascript: link", "src": "[x](javascript:alert(1))",
     "not": ["<a ", "href=\"javascript"]},
    {"name": "mixed-case scheme", "src": "[x](JaVaScRiPt:alert(1))", "not": ["<a "]},
    {"name": "tab-split scheme", "src": "[x](java\tscript:alert(1))", "not": ["<a "]},
    {"name": "newline-split scheme", "src": "[x](java\nscript:alert(1))", "not": ["<a "]},
    {"name": "control-char scheme", "src": "[x](javascript:alert(1))", "not": ["<a "]},
    {"name": "data: url", "src": "[x](data:text/html,<script>alert(1)</script>)",
     "not": ["<a ", "<script"]},
    {"name": "vbscript: url", "src": "[x](vbscript:msgbox(1))", "not": ["<a "]},
    {"name": "image syntax no img tag", "src": "![a](https://e.co/x.png)", "not": ["<img"]},
    {"name": "attr breakout via url", "src": '[x](https://e.co/"onmouseover="alert(1))',
     "notRegex": [r"<a[^>]*onmouseover"]},
    {"name": "attr breakout via label", "src": '["><script>alert(1)</script>](https://e.co/)',
     "not": ["<script"]},
    {"name": "quote in autolink", "src": 'see https://e.co/a"onclick="x now',
     "notRegex": [r"<a[^>]*onclick"]},
    {"name": "codeblock breakout", "src": "```\n</pre><script>alert(1)</script>\n```",
     "contains": ["&lt;/pre&gt;"], "not": ["<script"]},
    {"name": "inline code breakout", "src": "`</code><script>x</script>`",
     "not": ["<script"]},
    {"name": "lang label injection", "src": '```"><script>x</script>\nhi\n```',
     "not": ["<script"]},
    {"name": "fake placeholder", "src": "a \x00 0 \x00 b", "contains": ["a"]},
    {"name": "entity smuggling", "src": "[x](javascript&colon;alert(1))",
     "contains": ["&amp;colon;"], "not": ["href=\"javascript"]},
    {"name": "renderText escapes", "api": "text", "src": "<b onclick=x>hey</b>",
     "contains": ["&lt;b onclick=x&gt;"], "not": ["<b"]},
    {"name": "renderText no js autolink", "api": "text", "src": "javascript:alert(1)",
     "not": ["<a "]},
]

FEATURE_CASES = [
    {"name": "bold", "src": "**粗體** 與 __bold__",
     "contains": ["<strong>粗體</strong>", "<strong>bold</strong>"]},
    {"name": "italic", "src": "*斜體* and _em_",
     "contains": ["<em>斜體</em>", "<em>em</em>"]},
    {"name": "snake_case untouched", "src": "foo_bar_baz", "not": ["<em>"]},
    {"name": "strikethrough", "src": "~~刪除~~", "contains": ["<del>刪除</del>"]},
    {"name": "inline code", "src": "run `ls -la` now", "contains": ["<code>ls -la</code>"]},
    {"name": "link", "src": "[官網](https://example.com/a?b=1&c=2)",
     "contains": ['href="https://example.com/a?b=1&amp;c=2"', 'target="_blank"',
                  'rel="noopener noreferrer"', ">官網</a>"]},
    {"name": "autolink with cjk punct", "src": "看 https://example.com/x。",
     "contains": ['href="https://example.com/x"', "。"]},
    {"name": "long url label clipped",
     "src": "https://example.com/" + "a" * 90,
     "contains": ["…</a>"]},
    {"name": "heading clamp", "src": "# 大標\n\n### 小標\n\n###### 最小",
     "contains": ["<h3>大標</h3>", "<h5>小標</h5>", "<h5>最小</h5>"],
     "not": ["<h1", "<h2", "<h6"]},
    {"name": "unordered list", "src": "- a\n- b", "contains": ["<ul><li>a</li><li>b</li></ul>"]},
    {"name": "ordered + nested", "src": "1. one\n2. two\n   - sub",
     "contains": ["<ol><li>one</li><li>two<ul><li>sub</li></ul></li></ol>"]},
    {"name": "blockquote", "src": "> 引用\n> 第二行",
     "contains": ["<blockquote><p>引用<br>第二行</p></blockquote>"]},
    {"name": "hr", "src": "a\n\n---\n\nb", "contains": ["<hr>"]},
    {"name": "table", "src": "| A | B |\n|---|---|\n| 1 | 2 |",
     "contains": ['<div class="md-table"><table>', "<th>A</th>", "<td>2</td>"]},
    {"name": "code block with lang", "src": "```python\nprint('hi')\n```",
     "contains": ['<span class="md-code-lang">python</span>', 'class="md-copy"',
                  "print(&#39;hi&#39;)"]},
    {"name": "code block keeps md literal", "src": "```\n**not bold**\n```",
     "contains": ["**not bold**"], "not": ["<strong>"]},
    {"name": "unclosed fence reaches eof", "src": "```\nabc", "contains": ["abc"]},
    {"name": "paragraph breaks", "src": "line1\nline2\n\npara2",
     "contains": ["<p>line1<br>line2</p>", "<p>para2</p>"]},
    {"name": "renderText autolink + br", "api": "text", "src": "a\nhttps://e.co/x b",
     "contains": ["a<br>", 'href="https://e.co/x"']},
    {"name": "renderText keeps markers", "api": "text", "src": "**raw**",
     "contains": ["**raw**"], "not": ["<strong>"]},
    # 2026-11-21:**url** 以前把尾巴 ** 吃進 href → 404。修後:href 乾淨、外層 ** 包成粗體。
    {"name": "bold-wrapped url keeps clean href", "src": "see **https://e.co/x** ok",
     "contains": ['href="https://e.co/x"', "<strong>"],
     "not": ['href="https://e.co/x**', "x**</a>"]},
    {"name": "italic-wrapped url keeps clean href", "src": "*https://e.co/y* done",
     "contains": ['href="https://e.co/y"'], "not": ['href="https://e.co/y*']},
    # renderText(使用者訊息)同一支 URL_RE,也不能把 * 吃進 href。
    {"name": "renderText bold-wrapped url clean", "api": "text", "src": "**https://e.co/z**",
     "contains": ['href="https://e.co/z"'], "not": ['href="https://e.co/z**']},
    # 2026-11-21:artifact 路徑的圖/檔 → authed 下載 chip(不吐 <img>,不露原始 markdown)。
    {"name": "artifact image → download chip", "src": "![shot.png](/app/v2/artifacts/abc123)",
     "contains": ['data-dl="/app/v2/artifacts/abc123"', "shot.png"],
     "not": ["![", "<img"]},
    {"name": "artifact file link → download chip", "src": "[report.pdf](/app/v2/artifacts/def456)",
     "contains": ['data-dl="/app/v2/artifacts/def456"', "report.pdf"], "not": ["!["]},
    # 本地路徑的圖:載不出來,但要降成乾淨檔名,不是丟一坨原始 markdown 在那。
    {"name": "local-path image → clean label not raw markdown",
     "src": "![screenshot.jpg](/Users/example/screenshot.jpg)",
     "contains": ["screenshot.jpg"], "not": ["![", "/Users/example/screenshot.jpg"]},
]

# ── CJK 樣本(主場是台灣,這些是每天都會打出來的句子)──────────────────────
#
# 兩類題目:
#
# (1) **CJK 粗體**。CommonMark 的 flanking-run 規則會讓 `**注意：**內容` 不變
#     粗體(右側 marker 前面是全角標點、後面是漢字 → 判不出 closing run),
#     dsh 得自己寫 micromark 擴充 cjkFriendlyStrong 來補。我們的 md.js 是
#     自寫的逐段 regex,**不走 flanking run**,所以本來就對 —— 下面這組樣本
#     是把這件事釘住,避免哪天有人「順手改成標準實作」把它弄壞。
#
# (2) **裸網址的中文邊界**。中文不在詞間放空白,所以「空白為界」在中文句子
#     裡失效:`https://x.com/a。下一句` 整句會被吃進 href。修法是把非 ASCII
#     標點當終止符,同時 `/wiki/中文` 的漢字路徑必須完整保留。
CJK_CASES = [
    # (1) 粗體:marker 兩側是「全角標點 + 漢字」的四種組合
    {"name": "cjk strong before fullwidth colon", "src": "**注意：**內容要看清楚",
     "contains": ["<strong>注意：</strong>內容"]},
    {"name": "cjk strong after fullwidth punct", "src": "前文。**重點**後文",
     "contains": ["<strong>重點</strong>"]},
    {"name": "cjk strong glued both sides", "src": "這是**重點**啊",
     "contains": ["這是<strong>重點</strong>啊"]},
    {"name": "cjk strong then ideographic comma", "src": "文字**強調**、下一項",
     "contains": ["<strong>強調</strong>、"]},
    {"name": "cjk strong inside fullwidth parens", "src": "這是（**重點**）喔",
     "contains": ["（<strong>重點</strong>）"]},
    {"name": "cjk strong with trailing fullwidth period", "src": "**結論。**就這樣",
     "contains": ["<strong>結論。</strong>就這樣"]},
    {"name": "japanese strong", "src": "**注意：**ここが大事",
     "contains": ["<strong>注意：</strong>ここが"]},
    {"name": "cjk em glued", "src": "請看*這裡*喔", "contains": ["<em>這裡</em>"]},
    # (2) autolink 邊界
    {"name": "cjk period does not eat the rest of the sentence",
     "src": "看 https://example.com/x。下一句也在這裡",
     "contains": ['href="https://example.com/x"', "。下一句也在這裡</p>"],
     "not": ['href="https://example.com/x。']},
    {"name": "cjk comma terminates url",
     "src": "看 https://example.com/x，然後呢",
     "contains": ['href="https://example.com/x"', "，然後呢"]},
    {"name": "ideographic comma terminates url",
     "src": "見 https://example.com/x、再看別的",
     "contains": ['href="https://example.com/x"', "、再看別的"]},
    {"name": "fullwidth parens terminate url",
     "src": "全形括號 https://example.com/a（註)",
     "contains": ['href="https://example.com/a"', "（註"]},
    {"name": "cjk quote marks terminate url",
     "src": "https://example.com/x「引用」",
     "contains": ['href="https://example.com/x"', "「引用」"]},
    {"name": "han characters in path are kept",
     "src": "參考 https://zh.wikipedia.org/wiki/中文 這篇",
     "contains": ['href="https://zh.wikipedia.org/wiki/中文"']},
    {"name": "han in path with ascii parens kept",
     "src": "見 https://zh.wikipedia.org/wiki/臺灣_(消歧義) 吧",
     "contains": ['href="https://zh.wikipedia.org/wiki/臺灣_(消歧義)"']},
    {"name": "ascii trailing punct still trimmed",
     "src": "see https://example.com/a?b=1.",
     "contains": ['href="https://example.com/a?b=1"']},
    {"name": "ascii wiki parens still balanced",
     "src": "wiki https://en.wikipedia.org/wiki/Foo_(bar) ok",
     "contains": ['href="https://en.wikipedia.org/wiki/Foo_(bar)"']},
    # renderText(使用者訊息/等寬卡片)走同一個 trimUrl,邊界必須一致
    {"name": "renderText cjk url boundary", "api": "text",
     "src": "看 https://example.com/x。下一句",
     "contains": ['href="https://example.com/x"', "。下一句"],
     "not": ['href="https://example.com/x。']},
]


def _redlines(case):
    """全域紅線:套用到每一條樣本,不留白名單例外。"""
    return {
        "notRegex": [
            r"<script", r"<img", r"<iframe", r"<svg", r"<style",
            r"<[a-z]+[^>]*\son\w+=",            # 任何標籤上的事件屬性
            r"href=\"(?!https?:|mailto:)",       # href 只可能是白名單 scheme
        ] + case.get("notRegex", []),
    }


class ConsoleMdNodeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")

    def _run(self, cases):
        if not self.node:
            self.skipTest("node 不在 PATH,改由 ConsoleMdStaticTest 做靜態守門")
        merged = [dict(c, **_redlines(c)) for c in cases]
        p = subprocess.run(
            [self.node, "-e", _RUNNER], input=json.dumps(merged),
            capture_output=True, text=True, timeout=60,
            env=dict(os.environ, MD_JS=MD_JS))
        fails = json.loads(p.stdout or "[]")
        self.assertEqual(p.returncode, 0,
                         "md.js 樣本失敗:\n" + json.dumps(fails, ensure_ascii=False, indent=1))

    def test_xss_corpus(self):
        self._run(XSS_CASES)

    def test_feature_corpus(self):
        self._run(FEATURE_CASES)

    def test_cjk_corpus(self):
        self._run(CJK_CASES)


class ConsoleMdStaticTest(unittest.TestCase):
    """不依賴 node 的結構守門:渲染器的安全骨架與 Console 的接線。"""

    @classmethod
    def setUpClass(cls):
        with open(MD_JS, encoding="utf-8") as f:
            cls.src = f.read()

    def test_escape_first_skeleton_present(self):
        # escape 函式與 scheme 白名單必須存在;白名單裡絕不准出現 javascript
        self.assertIn('replace(/[&<>"\']/g', self.src)
        self.assertRegex(self.src, r"https\?\|mailto")
        self.assertNotRegex(self.src, r"javascript[^)]*\|")
        # 輸入端剝 \x00(佔位符不可偽造)
        self.assertIn('.replace(/\\x00/g, "")', self.src)

    def test_no_dynamic_code_paths(self):
        for needle in ("eval(", "new Function", "innerHTML", "document.write"):
            self.assertNotIn(needle, self.src)

    def test_console_wiring(self):
        html = open(os.path.join(ROOT, "static", "console", "index.html"),
                    encoding="utf-8").read()
        self.assertLess(html.index("/console/static/md.js"),
                        html.index("/console/static/transcript.js"),
                        "md.js 必須先於 transcript.js 載入")
        sw = open(os.path.join(ROOT, "static", "console", "sw.js"),
                  encoding="utf-8").read()
        self.assertIn('"md.js"', sw)
        m = re.search(r"pocket-console-shell-v(\d+)", sw)
        self.assertGreaterEqual(int(m.group(1)), 8, "新增資產必須翻 SW cache 版本")


if __name__ == "__main__":
    unittest.main()
