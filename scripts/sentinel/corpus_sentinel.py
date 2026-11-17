#!/usr/bin/env python3
"""語料下限哨兵 —— 讓掃描式守門不能因為「什麼都沒掃到」而報綠。

## 為什麼要有這支

本 repo 有一批**掃描式守門**:它們去掃源碼/檔案樹,找出「應該被登記卻沒登記
的東西」,然後紅。例如

  · `tests/test_no_private_infra_leak.py` —— 掃全部 git 追蹤檔,找私有識別
  · `tests/test_event_taxonomy_b1.py` —— 掃發射點字面型別,比對登記表
  · `tests/test_v2_session_ops.py` —— 掃對齊矩陣的每個 (op, provider) 格子
  · `tests/test_media_attach_matrix.py` —— 掃媒體掛點的 provider 分支

這類守門有一個共同的致命弱點:**它們的綠色有兩種來源** ——
「掃過了,沒有違規」與「根本沒掃到東西,所以沒有違規」。
而第二種在 glob 寫壞、函式改名、regex 被重構掉、檔案搬家的時候隨時會發生,
**而且完全安靜**。一個只掃到 3 個檔的洩漏守門會興高采烈地報綠。

抄 deepseek-harness 的作法(研究報告 §5.3 / §5.2):

  · `scripts/verify-client-ui-i18n.ts` 裡那個 `MINIMUM_CLIENT_UI_SOURCES = 450`
  · `scripts/AGENTS.md` 的制度原話:「Source-ownership gates use syntax-aware
    discovery, **guard against an empty or narrowed corpus**, and test every
    admitted/excluded form that changes their detection boundary.」

也就是:**閘門必須防止自己因為語料變空/變窄而假通過。**

## 怎麼用

守門裡掃完語料之後,在做任何違規斷言**之前**先報數:

    from sentinel import corpus_sentinel
    files = tracked_files()
    corpus_sentinel.require("private_leak.tracked_files", len(files))

數量低於下限 → 直接 `AssertionError`(unittest 當失敗),訊息講清楚
「這不是你的改動違規,是守門自己瞎了」。

## 下限怎麼取(改動這張表的規矩)

**N = 記錄當下實際值的 90%,向下取整。** 留 10% 的餘裕是因為語料本來就會
正常縮小(刪檔、合併重複、把三個分支收成一個)。縮超過 10% 就值得人看一眼。

**調低一個下限必須有理由,而且理由要寫在那一行的註解裡。**
「測試紅了所以把數字改小」不是理由 —— 那正是這張表要防的那個動作。
合格的理由長這樣:「2026-11-20 把 console 的三個 js 合併成一個,語料真的少了 2」。

語料長大時可以順手把下限往上調(跑 `python scripts/sentinel/corpus_sentinel.py`
看現值與餘裕),但**不調也不會錯** —— 下限只是地板,不是目標。
"""
from __future__ import annotations

import math
import os
import subprocess
import sys

#: `name -> (minimum, 記錄當下的實際值, 這份語料是什麼)`
#:
#: 第二欄不是裝飾:它讓「下限 = 當時的 90%」這個推導**留在檔案裡**,
#: 所以下一個人看得出下限是算出來的還是被人手動壓低的
#: (壓低過的那一行,minimum 會明顯小於 recorded * 0.9)。
MINIMUMS: dict[str, tuple[int, int, str]] = {
    # ── 私有識別洩漏掃描(tests/test_no_private_infra_leak.py)───────────────
    # 這一條是全 repo 最需要哨兵的:它的語料是 `git ls-files`,
    # 而 `git ls-files` 在非 repo 目錄、淺 clone、或 cwd 跑錯的時候回空 ——
    # **回空的時候那支測試會綠**,而它守的是「公開 repo 不准帶私有識別」。
    "private_leak.tracked_files": (345, 384, "git 追蹤的全部檔案(洩漏掃描的語料)"),

    # ── 事件分類登記表完整性(tests/test_event_taxonomy_b1.py)──────────────
    # 三個掃描點各自是一條 regex。regex 配不到東西 = 登記表完整性「真空通過」。
    # 原版只有 `assertTrue(types)`(≥1 就過),所以 regex 退化成只配到一種
    # 型別時仍然綠 —— 那是最常見的 regex 腐化形狀,不是歸零。
    "event_taxonomy.event_append_literals": (6, 7, "_event_append 的字面事件型別"),
    "event_taxonomy.milestone_sink_literals": (2, 3, "里程碑鏡射落地的字面型別"),
    "event_taxonomy.ring_push_literals": (2, 3, "ring 活流 _push 的字面型別"),
    "event_taxonomy.event_log_writes": (1, 1, "event_log 的 INSERT 點(單一 chokepoint)"),

    # ── v2 session 操作對齊矩陣(tests/test_v2_session_ops.py)───────────────
    # 語料 = 矩陣的格子數 = len(ops) × len(providers)。
    # 這張表是「每個格子都要表態」的制度本體;表被改瘦 = 制度安靜地鬆掉。
    "v2_session_ops.cells": (36, 40, "對齊矩陣的 (op, provider) 格子"),
    "v2_session_ops.ops": (4, 5, "_V2_SESSION_OPS 的操作數"),

    # ── 媒體掛點對齊矩陣(tests/test_media_attach_matrix.py)─────────────────
    # 語料是「掃到的 provider 分支/呼叫點」。這兩條 regex 認的是具體寫法
    # (`if prov == "x"` / `_schedule_media_attach(d.store, _store_media_sid(...)`),
    # 重構掉寫法就配不到 —— 正是 postmortem 0003 那個「沒有被呼叫」的盲區。
    "media_attach.cwd_branches": (5, 6, "_media_session_cwd 的 provider 分支"),
    "media_attach.turn_end_sites": (3, 4, "turn-end _schedule_media_attach 的具名呼叫點"),
    "media_attach.cells": (14, 16, "媒體掛點矩陣的 (hook, provider) 格子"),

    # ── Replay fixture 覆蓋(tests/test_replay_fixtures.py)──────────────────
    # manifest 的 fixture_digests 條數。manifest 被改瘦 = 「fixture 全綠 =
    # 端到端通」這個制度安靜地縮水。
    "replay_fixtures.fixture_digests": (8, 9, "manifest 登記的 replay fixture 摘要"),

    # ── 安裝器中立性(tests/test_installer_is_neutral.py)────────────────────
    # 語料只有一個檔,所以哨兵守的是「檔還在、而且還有內容」:
    # 安裝器被改成一行 `exec other.sh` 的時候,所有「不准出現 X」的斷言
    # 都會自動通過。
    "installer_neutral.installer_lines": (459, 510, "install-local-bridge.sh 的行數"),

    # ── SSE 反緩衝標頭掃描(tests/test_sse_anti_buffering.py)───────────────
    # 這支靠一個**字面樣式** `media_type="text/event-stream"` 找路由。
    # 改成單引號、拉成變數、換成別名,它就一條都配不到 ——
    # 然後「每條路由都帶了標頭」。2026-10-26 的 tunnel 事故(串流每 36 秒死
    # 一次)就是漏一條造成的,這條守門不能有真空通過的空間。
    "sse_headers.response_sites": (14, 16, "bridge.py 的 SSE 回應點"),

    # ── 事後檢討格式閘(tests/test_postmortem_format.py)────────────────────
    # 一個 0 份文件的格式閘會很高興地報告「全部合格」。編號不回收、不刪檔,
    # 所以這裡**不留 10% 餘裕** —— 下限 = 現值,變少一定是檔名規則改了或
    # 檔被搬走,兩種都要紅。
    "postmortems.files": (3, 3, "docs/POSTMORTEMS/ 下的 NNNN-*.md"),

    # ── 違規棘輪的語料(scripts/ratchet/)───────────────────────────────────
    # 棘輪自己也是掃描式的,同一個病:掃不到檔 ⇒ 0 個新違規 ⇒ 綠。
    "ratchet.product_py_sources": (47, 53, "棘輪掃的產品 .py 檔(排除 tests/ 與 scenarios/)"),
    "ratchet.js_sources": (18, 21, "棘輪掃的 .js/.cjs/.mjs 檔"),
}

#: 計算下限時用的餘裕。改這個常數等於一次放寬全部下限 —— 別。
HEADROOM = 0.9


class CorpusTooSmall(AssertionError):
    """語料數低於下限。刻意繼承 AssertionError,unittest 直接當失敗報。"""


def suggested_minimum(actual: int) -> int:
    """實際值 → 建議下限(90%,向下取整,但至少 1)。"""
    return max(1, math.floor(actual * HEADROOM))


def require(name: str, count: int, *, detail: str = "") -> int:
    """斷言 `name` 這份語料至少有登記的下限那麼多。回傳 count 方便串接。

    `name` 不在表上 → 也紅。新掃描式守門必須先把自己的語料登記進來,
    不然「忘了登記」與「語料是空的」又變成同一件事。
    """
    entry = MINIMUMS.get(name)
    if entry is None:
        raise CorpusTooSmall(
            f"語料 `{name}` 沒有登記在 scripts/sentinel/corpus_sentinel.py 的 "
            f"MINIMUMS 裡(現值 {count})。新的掃描式守門要先登記下限 —— "
            f"建議值 {suggested_minimum(count)}(= 現值的 90%)。")
    minimum, recorded, what = entry
    if count < minimum:
        raise CorpusTooSmall(
            f"掃描式守門的語料縮到下限以下:`{name}` 只掃到 {count} 個"
            f"(下限 {minimum},登記當下是 {recorded})。{what}。\n"
            f"  → **先假設是守門瞎了,不是語料真的變少**:glob/regex 配不到、"
            f"函式改名、檔案搬家、cwd 跑錯都長這個樣子。\n"
            f"  → 語料真的變小(刪檔/合併)才調下限,"
            f"而且要在 MINIMUMS 那一行寫明理由與日期。"
            + (f"\n  {detail}" if detail else ""))
    return count


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _git_tracked(root: str) -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True,
                         text=True, check=True).stdout
    return [p for p in out.splitlines() if p]


def report() -> int:
    """印出每條語料的現值與餘裕。給人校準下限用,不在 CI 跑。

    只量得出來的那幾條(不想在這裡重新實作每個守門的掃描邏輯)——
    其餘由各守門自己在跑的時候 require()。
    """
    import re

    root = _repo_root()

    def src(name: str) -> str:
        with open(os.path.join(root, name), encoding="utf-8") as f:
            return f.read()

    bridge_src = src("bridge.py")
    card_src = src("carddigest.py")
    i = bridge_src.index("def _media_session_cwd(")
    cwd_body = bridge_src[i:bridge_src.index("\ndef ", i + 10)]

    measured = {
        "private_leak.tracked_files": len(_git_tracked(root)),
        "event_taxonomy.event_append_literals": len(set(re.findall(
            r"_event_append\(\s*[^,]+,\s*\"([a-z_.]+)\"", bridge_src))),
        "event_taxonomy.milestone_sink_literals": len(set(re.findall(
            r"sink\(\s*sid,\s*\"([a-z_.]+)\"", card_src))),
        "event_taxonomy.ring_push_literals": len(set(re.findall(
            r"_push\(\s*\"([a-z_.]+)\"", card_src))),
        "media_attach.cwd_branches": len(re.findall(
            r"prov (?:==|in) (\(.*?\)|\"[a-z_0-9]+\")", cwd_body)),
        "media_attach.turn_end_sites": len(set(re.findall(
            r"_schedule_media_attach\(d\.store, _store_media_sid\("
            r"d\.store, f\"([a-z_0-9]+):", bridge_src))),
        "sse_headers.response_sites": bridge_src.count(
            'media_type="text/event-stream"'),
        "postmortems.files": len([
            n for n in os.listdir(os.path.join(root, "docs", "POSTMORTEMS"))
            if re.match(r"^\d{4}-.*\.md$", n)]),
        "ratchet.product_py_sources": len(
            [p for p in _git_tracked(root)
             if p.endswith(".py") and not p.startswith(("tests/", "scenarios/"))]),
        "ratchet.js_sources": len([p for p in _git_tracked(root)
                                   if p.endswith((".js", ".cjs", ".mjs"))]),
    }

    print(f"{'語料':<44} {'現值':>6} {'下限':>6} {'建議':>6}  餘裕")
    worst = 0
    for name, (minimum, recorded, _what) in MINIMUMS.items():
        actual = measured.get(name)
        if actual is None:
            print(f"{name:<44} {'(守門內量)':>6} {minimum:>6} {'-':>6}")
            continue
        slack = actual - minimum
        worst = min(worst, slack)
        flag = "  ⚠️ 低於下限" if slack < 0 else ""
        print(f"{name:<44} {actual:>6} {minimum:>6} "
              f"{suggested_minimum(actual):>6}  {slack:+d}{flag}")
    print("\n「建議」= 現值 × 0.9。下限只是地板:語料長大時可以往上調,"
          "不調也不會錯;\n**往下調一定要在 MINIMUMS 那一行寫理由與日期。**")
    return 0 if worst >= 0 else 1


if __name__ == "__main__":
    sys.exit(report())
