"""CJK/emoji 安全的字串截斷 —— bridge 的 canonical 截斷器。

## 為什麼需要這個檔

bridge 到處都是 `text[:60] + "…"` 這種截斷(卡片預覽、session 標題、roster
摘要、工具呼叫摘要)。Python 的字串是 **code point** 序列,所以不像 JS 會切出
孤立 surrogate —— 連 CJK Ext-B(U+20000 以上,`𠀋`;台灣人名地名用字就在這裡)
也是單一 code point,`[:n]` 切不壞。**但 `[:n]` 會切壞 grapheme cluster**,而
使用者看到的「一個字」是 cluster,不是 code point:

  ‧ 旗幟 `🇹🇼` = 兩個 regional indicator → 切一半變成落單的 🇹(顯示成字母 T)
  ‧ 膚色 `👍🏽` = 👍 + 膚色修飾符 → 切一半變成沒膚色的 👍,或落單的修飾符方塊
  ‧ 家庭 `👨‍👩‍👧` = 三個 emoji 用 ZWJ 黏起來 → 切一半變成「男人女人」兩個人
  ‧ 表意變體 `茄󠄀` = 漢字 + variation selector(U+E0100 系,日文人名/地名
    的異體字選擇器)→ 切掉基字只剩選擇器,渲染成 tofu
  ‧ 泰文/天城文的組合記號、注音的聲調符號 → 同理

切壞的後果不是例外,是**安靜地**在卡片標題上留一個豆腐方塊或一個錯國旗。

## 這裡**不**做的事

不做視覺寬度摺疊(漢字算 2 欄、拉丁算 1 欄)。那是另一個題目,而且會改動
現有的字數預算語意 —— 要做就得連帶重訂每個呼叫點的上限,不在本次範圍。

## 用法

    from cjk_text import clip

    clip(title, 60)              # → 最多 60 個 grapheme,超過才加 "…"
    clip(text, 180, suffix="")   # → 不加省略號
"""
from __future__ import annotations

import unicodedata

# ── Windows PowerShell 的 CP936/CP950 亂碼 ──────────────────────────────────
#
# Windows PowerShell 5.1(每台 Windows 都內建的那個)預設把 console/OEM
# code page 寫出去,而不是 UTF-8 —— 任何非 ASCII 輸出(中文路徑、中文檔名、
# 中文錯誤訊息)到了這一端就變亂碼。治法是在**第 1 行**先把兩個編碼設成
# 無 BOM 的 UTF-8。
#
# 刻意用 `; ` 擠在同一行、不換行:PowerShell 的錯誤訊息會報行號,多一行
# preamble 就會讓所有行號偏移一行,排查腳本時非常難受。
#
# 現狀(2026-11-17):**bridge 目前沒有任何起 pwsh/powershell 的路徑** ——
# 互動式 shell 一律走 POSIX `pty.openpty()`(見 bridge.py 的 _TerminalSession
# 與 app_v1_terminal),Windows 支援只到「WSL 裡跑 bridge」這一層,自架安裝器
# (deploy/selfhost-install.sh)只是**印出** netsh/wsl 指令給使用者自己貼到
# PowerShell,bridge 自己不執行。
#
# 所以這個常數現在沒有呼叫點,放在這裡是**給未來的那一天**:哪天真的要
# 在 Windows 上起 pwsh,前綴必須是這一行。tests/test_cjk_text_truncation.py
# 的 PowerShellPreambleGateTests 會在「出現了 pwsh 呼叫但沒帶前綴」時紅,
# 所以這不是躺著的死碼,是一顆絆線。
POWERSHELL_UTF8_PREAMBLE = (
    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
    "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
)

# ── grapheme cluster 近似(stdlib 無 UAX #29,下面是涵蓋實務的那幾類)──────
#
# 完整的 UAX #29 要整張 Grapheme_Cluster_Break 表;stdlib 沒有,外部依賴
# (regex / grapheme)不值得為截斷引入。下面這份近似涵蓋**所有會在聊天內容
# 裡出現**的黏著規則,其餘退化成「一個 code point 一個 cluster」—— 也就是
# 舊行為,不會比現在更糟。

ZWJ = "‍"                     # ZERO WIDTH JOINER:emoji 序列的黏著劑
_SKIN_TONES = range(0x1F3FB, 0x1F400)   # EMOJI MODIFIER FITZPATRICK TYPE-1..6
_REGIONAL = range(0x1F1E6, 0x1F200)     # REGIONAL INDICATOR A..Z(旗幟成對)
_VARIATION_SELECTORS = range(0xFE00, 0xFE10)        # VS1..VS16(含 emoji/text 呈現)
_VARIATION_SUPPLEMENT = range(0xE0100, 0xE01F0)     # VS17..VS256(漢字異體字)
_TAGS = range(0xE0020, 0xE0080)         # TAG 字元(🏴󠁧󠁢󠁳󠁣󠁴󠁿 這類次國旗)
_KEYCAP = "⃣"                      # COMBINING ENCLOSING KEYCAP(1️⃣)
_EMOJI_KEYCAP_BASES = set("0123456789#*")


def _is_extending(ch: str) -> bool:
    """這個字元會不會黏在前一個字元上(不自己起一個 cluster)。"""
    cp = ord(ch)
    if cp in _VARIATION_SELECTORS or cp in _VARIATION_SUPPLEMENT or cp in _TAGS:
        return True
    if cp in _SKIN_TONES:
        return True
    if ch == _KEYCAP:
        return True
    # Mn 非間距記號 / Me 圍繞記號 / Mc 間距組合記號 —— 注音聲調、越南語
    # 聲調、泰文母音、天城文 matra 都在這裡。
    return unicodedata.category(ch) in ("Mn", "Me", "Mc")


def graphemes(text: str) -> list[str]:
    """把字串切成「使用者看到的一個字」的清單。

    規則(依序判斷):
      1. ZWJ 之後的那個字元一定黏進同一個 cluster(emoji ZWJ 序列)。
      2. extending 字元(組合記號/變體選擇器/膚色/keycap/tag)黏進前一個。
      3. regional indicator **成對**為一個 cluster(旗幟);落單自成一個。
      4. CRLF 當一個 cluster(不要把 \\r\\n 切開)。
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # (4) CRLF
        if ch == "\r" and i + 1 < n and text[i + 1] == "\n":
            out.append("\r\n")
            i += 2
            continue
        # (3) 旗幟:兩個 regional indicator 綁一起
        if ord(ch) in _REGIONAL:
            if i + 1 < n and ord(text[i + 1]) in _REGIONAL:
                cluster = text[i:i + 2]
                i += 2
            else:
                cluster = ch
                i += 1
            out.append(cluster)
            continue
        cluster = ch
        i += 1
        # (1)(2) 往後吃 extending 與 ZWJ+下一字
        while i < n:
            nxt = text[i]
            if nxt == ZWJ:
                # ZWJ 必須真的接著一個字元才算黏著;落在字串尾就自成一段
                if i + 1 < n:
                    cluster += nxt + text[i + 1]
                    i += 2
                    continue
                cluster += nxt
                i += 1
                continue
            if _is_extending(nxt):
                # keycap 只黏在 0-9/#/* 後面(否則 1️⃣ 的規則會誤抓)
                if nxt == _KEYCAP and cluster[-1] not in _EMOJI_KEYCAP_BASES \
                        and ord(cluster[-1]) not in _VARIATION_SELECTORS:
                    break
                cluster += nxt
                i += 1
                continue
            break
        out.append(cluster)
    return out


def grapheme_len(text: str) -> int:
    """使用者感知的字數(🇹🇼 = 1、👨‍👩‍👧 = 1、`臺` = 1)。"""
    return len(graphemes(str(text or "")))


def clip(text, limit: int, *, suffix: str = "…") -> str:
    """截到最多 `limit` 個 grapheme,**絕不切進一個字的中間**。

    `limit` 以 grapheme 計(≥ 舊的 code-point 計法,所以預算只會變寬鬆一點,
    不會把原本放得下的內容擠掉)。沒超過就原樣回傳 —— 不加省略號、不做
    strip,避免無聲改掉沒被截斷的內容。
    """
    s = "" if text is None else str(text)
    if limit <= 0:
        return ""
    # 快篩:code point 數 ≤ limit 時 grapheme 數必定 ≤ limit,免掉逐字掃描。
    if len(s) <= limit:
        return s
    parts = graphemes(s)
    if len(parts) <= limit:
        return s
    return "".join(parts[:limit]) + suffix


def clip_tail(text, limit: int) -> str:
    """保留**尾端**最多 `limit` 個 grapheme(串流輸出的滾動視窗用)。

    `text[-n:]` 會從前端切進一個字的中間 —— 滾動視窗每收一塊就重算一次,
    於是視窗頭部長期掛著一個落單的變體選擇器/膚色修飾符。
    """
    s = "" if text is None else str(text)
    if limit <= 0:
        return ""
    if len(s) <= limit:
        return s
    parts = graphemes(s)
    if len(parts) <= limit:
        return s
    return "".join(parts[len(parts) - limit:])


def snap_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """把任意 code-point 區間推到最近的 grapheme 邊界(只會往外擴)。

    給「在命中位置前後取片段」這類用途:`text[i-80:i+80]` 的兩端都可能落在
    一個字的中間,往外擴到整個 cluster 才不會留半個 emoji / 落單的變體選擇器。
    """
    s = str(text or "")
    n = len(s)
    start = max(0, min(start, n))
    end = max(start, min(end, n))
    lo, hi = start, end
    pos = 0
    for part in graphemes(s):
        nxt = pos + len(part)
        if pos < start < nxt:
            lo = pos                  # start 落在這個 cluster 中間 → 往前吃整塊
        if pos < end < nxt:
            hi = nxt                  # end 落在中間 → 往後吃完整塊
        if pos >= end:
            break
        pos = nxt
    return (lo, hi)


def clip_marked(text, limit: int, *, mark: str) -> tuple[str, bool]:
    """`clip` 的「要知道有沒有截斷」版本 —— 回 (結果, 是否截斷)。

    截斷時接的是呼叫端自己的提示字(例如「\\n…(上下文過長,已截斷)」),
    所以 mark 必填。
    """
    s = "" if text is None else str(text)
    if limit <= 0:
        return ("", bool(s))
    if len(s) <= limit:
        return (s, False)
    parts = graphemes(s)
    if len(parts) <= limit:
        return (s, False)
    return ("".join(parts[:limit]) + mark, True)
