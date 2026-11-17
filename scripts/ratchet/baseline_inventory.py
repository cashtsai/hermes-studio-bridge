#!/usr/bin/env python3
"""違規棘輪引擎 —— **舊債凍結(不擋),新債擋死。**

## 為什麼要有這支

本 repo 有一批「明知道不該這樣寫、但現存一堆」的模式:

  · `except …: pass` —— 吞掉例外,安靜地什麼都不做(90 處)
  · 裸 `subprocess.*` —— 不過共用包裝、沒有逾時/錯誤歸因(17 處)
  · JS 的 `.slice(0, N)` 裸截斷 —— 按 UTF-16 code unit 切,中文/emoji 會斷半個字

兩種常見處置都是錯的:

  (a) **一刀全禁**(lint rule / `#![deny(...)]`)—— 現存的全部變紅,
      於是這條規則永遠不會被打開,或者被加上一百個 `# noqa` 豁免。
  (b) **寫進文件說「以後別這樣」** —— 沒有任何後果,所以債只會變多。

棘輪是第三條路,抄 deepseek-harness 的 `scripts/baseline-inventory.ts`
(研究報告 §5.2 (B)):把**現存的違規逐一記進 baseline 當凍結清單**,
之後只擋「不在 baseline 裡的那些」。效果是

  · 今天不用清一行舊債,CI 就能開始擋新債;
  · 舊債的數量變成一個**可量化、只能遞減**的數字(baseline 檔的大小);
  · 清掉一筆舊債之後,baseline 自己收斂(閘門會提示你跑 `--update`),
    而且**那一筆不能再回來** —— 這就是「棘輪」的意思:只能往一個方向轉。

## 違規的身分 = 內容雜湊,不是行號

`identity = sha256(rule \\0 正規化路徑 \\0 正規化的違規內容)`

正規化:去掉行尾註解、把連續空白壓成一個、去頭尾空白。
(deepseek 用的是 TS 的 syntax token 序列;我們沒有 Python/JS 的共用 parser,
所以退一步用「正規化後的行內容」—— 行為上抓到同一件事:
**換行縮排/重排版不算新違規,改動程式碼本身就算。**)

這個選擇的三個後果,寫清楚免得下一個人誤會:

  1. **移動行、重新縮排、調整空白 → 身分不變**,baseline 不用改。這是重點:
     不然任何 reformat 都會讓整份 baseline 失效,制度三天就被放棄。
  2. **搬檔 → 身分改變**(路徑在雜湊裡)。搬檔要在**同一個 change** 裡
     跑 `--update` 把 key 換掉 —— 這是刻意的:搬檔是重新審視那筆債的時機。
  3. **同一個檔裡有兩行一模一樣的違規 → 身分相同**,所以 baseline 記
     `count`;新增第三行同樣的違規會讓 count 超過而**紅**。
     不用行號當身分,是為了第 1 點。

## 怎麼跑

    scripts/ratchet/check.py            # 閘門:有新違規 → exit 1
    scripts/ratchet/check.py --update   # 把現況寫回 baseline(要在 PR 裡被 review)
    scripts/ratchet/check.py --list py-silent-except   # 看某條規則的現存債

`--update` **不是**修 bug 的方法。它只有兩個正當用途:
(a) 搬檔之後換 key;(b) 清掉舊債之後讓 baseline 收斂。
拿它來「把新違規變成舊債」是繞過閘門 —— review 的時候看 baseline 的
條數有沒有變多就知道(變多一定要在 PR 說明裡有一句話)。
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
REPO_ROOT = os.path.dirname(_SCRIPTS)
BASELINE_PATH = os.path.join(_HERE, "baseline.json")

sys.path.insert(0, _SCRIPTS)
from sentinel import corpus_sentinel  # noqa: E402

#: baseline 的 schema。改格式要動這個數字,`check.py` 會拒絕舊格式而不是
#: 默默地把全部舊債當成新債(那會瞬間產生幾百條紅線,然後有人會刪掉 baseline)。
SCHEMA_VERSION = 1

#: 雜湊配方寫進 baseline 檔,讓「我換了身分演算法」這件事看得見。
HASH_RECIPE = "sha256(rule \\0 path \\0 normalized-line)"


# ── 違規與規則 ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Violation:
    rule: str
    path: str
    line: int
    snippet: str          # 給人看的原文(已截斷)
    identity: str         # 身分雜湊


@dataclass(frozen=True)
class Rule:
    name: str
    #: 一句話:為什麼這是債。會印在閘門的失敗訊息裡,所以寫給「2am 被它擋住的人」看。
    why: str
    #: 新違規該怎麼辦。閘門會把這句印出來 —— 不給出路的閘門會被繞過。
    fix: str
    #: 收哪些檔(相對路徑 → bool)
    selects: object
    #: 一行一行判(line, prev_lines, next_lines) → bool
    matches: object
    #: 這條規則的語料下限哨兵名稱(見 scripts/sentinel/corpus_sentinel.py)
    corpus: str


def _normalize(line: str, *, comment_marks: tuple[str, ...]) -> str:
    """違規內容的正規化形式(身分雜湊的原料)。

    去行尾註解 → 空白壓成一個 → 去頭尾。
    **刻意不做更聰明的事**:不解析語法、不管字串裡的 `#`。
    代價是「字串裡剛好有註解符號」的那行會被切掉尾巴,
    好處是這支不依賴任何 parser、兩種語言共用一份、行為一眼看得懂。
    被切過頭的後果只是「身分比預期粗」,不會漏掉違規(違規與否由 matches 決定,
    不由正規化決定)。
    """
    out = line
    for mark in comment_marks:
        idx = out.find(mark)
        if idx >= 0:
            out = out[:idx]
    return re.sub(r"\s+", " ", out).strip()


def _identity(rule: str, path: str, normalized: str) -> str:
    payload = f"{rule}\0{path}\0{normalized}".encode()
    return hashlib.sha256(payload).hexdigest()[:32]


# ── 三條規則 ─────────────────────────────────────────────────────────────────

_PY_PRODUCT_EXCLUDE = ("tests/", "scenarios/")


def _is_product_py(path: str) -> bool:
    return path.endswith(".py") and not path.startswith(_PY_PRODUCT_EXCLUDE)


def _is_js(path: str) -> bool:
    return path.endswith((".js", ".cjs", ".mjs"))


_RE_SUBPROCESS = re.compile(
    r"\bsubprocess\.(?:run|Popen|call|check_call|check_output)\s*\(")
_RE_EXCEPT_HEAD = re.compile(r"^\s*except\b.*:\s*$")
_RE_ONLY_PASS = re.compile(r"^\s*pass\s*$")
_RE_NAKED_TRUNC = re.compile(r"\.(?:slice|substring|substr)\(\s*0\s*,\s*\d+\s*\)")


def _match_silent_except(line: str, prev: list[str], nxt: list[str]) -> bool:
    return bool(_RE_EXCEPT_HEAD.match(line)
                and nxt and _RE_ONLY_PASS.match(nxt[0]))


def _match_bare_subprocess(line: str, prev: list[str], nxt: list[str]) -> bool:
    return bool(_RE_SUBPROCESS.search(line))


def _match_naked_truncation(line: str, prev: list[str], nxt: list[str]) -> bool:
    return bool(_RE_NAKED_TRUNC.search(line))


RULES: tuple[Rule, ...] = (
    Rule(
        name="py-silent-except",
        why="`except …: pass` 把失敗變成「什麼都沒發生」。本 repo 被這個形狀咬過"
            "太多次 —— 2026-11-17 的媒體附件就有一隻:鎖跨 event loop 重用時"
            "丟 RuntimeError,被 `except Exception` 吞掉,整條管線安靜停擺"
            "(docs/POSTMORTEMS/0003)。靜默失敗是本 repo 的最大病原。",
        fix="至少留痕:`_log_exc(site, exc, expected=True)` 或 `_log_event(...)`。"
            "真的要吞,就吞得具體(`except FileNotFoundError:`)並在旁邊寫一句"
            "為什麼這個失敗是預期的。",
        selects=_is_product_py,
        matches=_match_silent_except,
        corpus="ratchet.product_py_sources",
    ),
    Rule(
        name="py-bare-subprocess",
        why="裸 `subprocess.*` 沒有逾時、沒有統一的錯誤歸因,也不會出現在任何"
            "「這台機器上跑了什麼」的紀錄裡。這是一個會執行工具的 agent 閘道,"
            "子程序是它最危險的一面。",
        fix="走共用包裝(帶 timeout + 失敗歸因 + 事件留痕)。這條規則刻意是"
            "**一刀切**(不看有沒有 `timeout=`)—— 因為 `Popen` 這種長命子程序"
            "本來就沒有 timeout 可補,一旦開始分辨「夠不夠好」,規則就會變成"
            "一組可以被辯論的條件,然後新債會從辯論的縫裡進來。"
            "現存的 20 處已經凍結在 baseline;真的必須新增裸呼,"
            "在 PR 說明裡講清楚為什麼不能走包裝,再 `--update`。",
        selects=_is_product_py,
        matches=_match_bare_subprocess,
        corpus="ratchet.product_py_sources",
    ),
    Rule(
        name="js-naked-truncation",
        why="`.slice(0, N)` 按 UTF-16 code unit 切:中文算一格還好,"
            "emoji 與 surrogate pair 會被切成半個字(畫面出現 ``),"
            "而且沒有省略號 —— 使用者看不出來是被截斷還是真的只有這麼多。"
            "這個 repo 的 UI 文案是中文,所以這條不是理論風險。",
        fix="走共用的截斷 helper(以 code point / grapheme 為單位 + 省略號)。"
            "純內部用途(log key、error fingerprint)可以留,但要留在 baseline 裡"
            "而不是新增。",
        selects=_is_js,
        matches=_match_naked_truncation,
        corpus="ratchet.js_sources",
    ),
)

_COMMENT_MARKS = {"py": ("#",), "js": ("//",)}


def _marks_for(path: str) -> tuple[str, ...]:
    return _COMMENT_MARKS["py"] if path.endswith(".py") else _COMMENT_MARKS["js"]


# ── 掃描 ─────────────────────────────────────────────────────────────────────

def list_files(root: str) -> list[str]:
    """repo 的檔案清單(相對路徑)。

    有 `.git` 就用 `git ls-files`(跟著 .gitignore,不會掃到 venv/建置產物);
    沒有就退回 os.walk —— 自我測試用的假 repo 走這條。

    ⚠️ 判準是 `os.path.exists` 而**不是** `os.path.isdir`:
    git worktree 的 `.git` 是一個**檔案**(裡面一行 `gitdir: …`),
    而本 repo 的規矩是「功能開發一律開 worktree」(CLAUDE.md)——
    用 isdir 判的話,**每一次開發都會走到 os.walk 那條**,
    把未追蹤的檔案、快取、venv 全部掃進語料,
    於是 baseline 在 worktree 裡產生、在 main 上比對,兩邊對不起來。
    """
    if os.path.exists(os.path.join(root, ".git")):
        out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True,
                             text=True, check=True).stdout
        return sorted(p for p in out.splitlines() if p)
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
        for fn in filenames:
            found.append(os.path.relpath(os.path.join(dirpath, fn), root))
    return sorted(found)


@dataclass
class ScanResult:
    violations: list[Violation] = field(default_factory=list)
    #: rule name -> 掃到的檔案數(語料)
    corpus: dict = field(default_factory=dict)


def scan(root: str = REPO_ROOT, *, enforce_corpus: bool = True,
         rules: tuple = RULES) -> ScanResult:
    """掃出全部違規。

    `enforce_corpus=True` 時每條規則都先過語料下限哨兵 ——
    **棘輪自己也是掃描式守門,一樣會因為掃不到檔而報綠**
    (0 個檔 ⇒ 0 個新違規 ⇒ 「沒有新債」)。自我測試用的假 repo 關掉它。
    """
    files = list_files(root)
    result = ScanResult()
    cache: dict = {}
    for rule in rules:
        selected = [p for p in files if rule.selects(p)]
        result.corpus[rule.name] = len(selected)
        if enforce_corpus:
            corpus_sentinel.require(
                rule.corpus, len(selected),
                detail=f"(棘輪規則 `{rule.name}` 掃到的檔案數。"
                       f"掃不到檔 ⇒ 0 個新違規 ⇒ 這支會「沒有新債」地綠)")
        marks = None
        for rel in selected:
            if rel not in cache:
                try:
                    with open(os.path.join(root, rel), encoding="utf-8") as f:
                        cache[rel] = f.read().splitlines()
                except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
                    cache[rel] = []
            lines = cache[rel]
            marks = _marks_for(rel)
            for i, line in enumerate(lines):
                if not rule.matches(line, lines[max(0, i - 2):i], lines[i + 1:i + 3]):
                    continue
                normalized = _normalize(line, comment_marks=marks)
                result.violations.append(Violation(
                    rule=rule.name, path=rel, line=i + 1,
                    snippet=line.strip()[:120],
                    identity=_identity(rule.name, rel, normalized)))
    return result


# ── baseline 比對 ────────────────────────────────────────────────────────────

@dataclass
class Verdict:
    #: 不在 baseline(或超出 baseline count)的違規 —— **紅**
    new: list[Violation] = field(default_factory=list)
    #: baseline 有、現在掃不到的 —— 不紅,提示可以收斂
    retired: list[tuple] = field(default_factory=list)   # (rule, identity, meta)
    #: 掃到的總數 / baseline 的總數(給報告用)
    scanned: int = 0
    frozen: int = 0
    corpus: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.new


def _counted(violations: list) -> dict:
    """identity -> (count, 第一個 Violation)"""
    out: dict = {}
    for v in violations:
        count, first = out.get(v.identity, (0, v))
        out[v.identity] = (count + 1, first)
    return out


def compare(result: ScanResult, baseline: dict) -> Verdict:
    """掃描結果 vs baseline → 判決。

    新違規的判準有兩種:
      · identity 不在 baseline;
      · identity 在,但**出現次數超過** baseline 記的 count
        (同一個檔裡多貼了一行一模一樣的違規)。
    """
    verdict = Verdict(scanned=len(result.violations), corpus=dict(result.corpus))
    frozen = baseline.get("rules") or {}
    verdict.frozen = sum(int(e.get("count", 1))
                         for rules in frozen.values() for e in rules.values())

    by_rule: dict = {}
    for v in result.violations:
        by_rule.setdefault(v.rule, []).append(v)

    for rule_name, violations in by_rule.items():
        rule_baseline = frozen.get(rule_name) or {}
        for identity, (count, first) in _counted(violations).items():
            allowed = int((rule_baseline.get(identity) or {}).get("count", 0))
            for _ in range(max(0, count - allowed)):
                verdict.new.append(first)

    seen = {v.identity for v in result.violations}
    for rule_name, rule_baseline in frozen.items():
        for identity, meta in rule_baseline.items():
            if identity not in seen:
                verdict.retired.append((rule_name, identity, meta))
    return verdict


def to_baseline(result: ScanResult) -> dict:
    """掃描結果 → baseline 檔的內容(路徑/行號只是給人看的註記)。"""
    rules: dict = {}
    for v in result.violations:
        bucket = rules.setdefault(v.rule, {})
        entry = bucket.get(v.identity)
        if entry is None:
            bucket[v.identity] = {"count": 1, "path": v.path,
                                  "line": v.line, "snippet": v.snippet}
        else:
            entry["count"] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "hash_recipe": HASH_RECIPE,
        "note": ("凍結清單:這裡面的違規不擋,不在這裡面的擋死。"
                 "條數只准往下走 —— 變多一定要在 PR 說明裡有一句話。"
                 "見 scripts/ratchet/baseline_inventory.py 的檔頭。"),
        "rules": {name: dict(sorted(bucket.items()))
                  for name, bucket in sorted(rules.items())},
    }
