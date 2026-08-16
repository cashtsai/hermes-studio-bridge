#!/usr/bin/env python3
"""cc_detect — Claude Code TUI 狀態偵測(資料驅動 manifest 引擎)。

為什麼要有這個模組:bridge 過去用 5 處 inline 的
`_CC_BUSY_RE.search(pane) or "esc to interrupt" in pane` 判忙碌 —— 硬編碼
regex 一漂移就全壞(b139 錨點修到 v2.1 那類事故)。herdr(github.com/herdrdev/
herdr,Apache-2.0)證明了另一條路:**狀態規則寫成資料(manifest),引擎只負責
評估** —— 規則有優先序、有作用區(region)、可解釋(explain 吐命中規則與證據)、
可本地覆寫熱更新。本模組把 herdr 的 claude manifest 忠實移植成 Python + JSON
(stdlib only,不引 TOML),並疊上 bridge 既有的兩個忙碌訊號讓行為是舊判讀的
嚴格超集。

語意(照 herdr):
  - 狀態:working / blocked / idle / unknown。
  - 規則依 priority 由高至低評估,第一個命中即勝出;同分保留 manifest 順序。
  - 全部規則都沒命中 → idle(已知 agent 的 fallback,herdr 同款)。
  - matcher:contains(小寫比對,全部都要在)、regex(區域全文 search)、
    line_regex(區域內「存在某一行」match);邏輯門 all / any / not 可巢狀。
  - region 限定規則只看畫面的哪一塊:whole_recent、bottom_lines(N)、
    bottom_non_empty_lines(N)、top_non_empty_lines(N)、prompt_box_body
    (❯ 輸入框內文)、above_prompt_box、after_last_horizontal_rule;另外
    **osc_title 是獨立訊號源**(claude 的忙碌 spinner —— braille/半圓字元 ——
    其實寫在終端標題裡,比 pane 內容穩定得多),來自 pane_title 參數而非 pane。

覆寫:env `CC_DETECT_MANIFEST` 指到一個 JSON 檔即可換規則,不用改碼重啟。
每次呼叫用 mtime 檢查熱更新(一個 stat,夠便宜);檔案壞掉 → 記一次 log、
退回內建 manifest,**偵測永不因此 crash**(這是輪詢路徑,壞了要靜默降級)。

覆寫檔格式(JSON)::

    {
      "id": "claude",
      "replace": false,          # true = rules 整份取代內建;false(預設)= 按 id 合併
      "rules": [
        {"id": "osc_title_working", ...},      # 同 id → 整條取代內建那條
        {"id": "my_new_rule", ...},            # 新 id → 追加
        {"id": "legacy_no_prompt_blocker", "remove": true}   # 刪掉內建那條
      ]
    }
"""
from __future__ import annotations

import json
import os
import re
import time

STATES = ("working", "blocked", "idle", "unknown")

# 沒有任何規則命中時的 fallback 規則名(herdr 同名常數)。
DEFAULT_IDLE_FALLBACK = "default_known_agent_idle_fallback"

# ── 內建 manifest ────────────────────────────────────────────────────────────
# 移植自 herdr src/detect/manifests/claude.toml(version 2026.08.13.1),
# 加上 bridge 既有的兩個忙碌訊號(pane_busy_spinner / pane_esc_to_interrupt)。
# 這兩條要壓在所有 blocked/unknown 規則之上:舊 bridge 的語意是「busy 優先於
# 選單判讀」(_cc_prompt 看到忙碌訊號直接回 None),放 1000+ 才能保持
# 「凡舊判讀為 busy 者,新引擎必為 working」的嚴格超集承諾。
# 注意 Rust regex 的 \x{2800} 在 Python 寫成 ⠀;(?i) 前綴兩邊通用。
DEFAULT_MANIFEST = {
    "id": "claude",
    "version": "2026.08.16.1",
    "rules": [
        {
            # claude 的忙碌 spinner 寫在 OSC 終端標題:braille(<=2.1.227)
            # 或半圓(2.1.228+)字元開頭。比 pane 內容穩定,最高優先。
            "id": "osc_title_working",
            "state": "working",
            "priority": 1100,
            "region": "osc_title",
            "regex": ["^[⠀-⣿◐-◓] "],
        },
        {
            # bridge 既有訊號 1:「· Fermenting… (1m 51s · ↓ 6.5k tokens)」
            # 這種計時行(原 _CC_BUSY_RE,含 re.IGNORECASE → (?i))。
            "id": "pane_busy_spinner",
            "state": "working",
            "priority": 1060,
            "region": "whole_recent",
            "regex": ["(?i)\\((?:\\d+m\\s*)?\\d+(?:\\.\\d+)?s\\s*·.*tokens"],
        },
        {
            # bridge 既有訊號 2:running turn 的底欄提示。
            "id": "pane_esc_to_interrupt",
            "state": "working",
            "priority": 1050,
            "region": "whole_recent",
            "contains": ["esc to interrupt"],
        },
        {
            # ctrl+o 的 transcript 檢視疊層:畫面不反映 agent 真實狀態,
            # herdr 標 unknown + skip_state_update(呼叫端自行決定要不要沿用舊態)。
            "id": "transcript_viewer",
            "state": "unknown",
            "priority": 1000,
            "region": "bottom_non_empty_lines(3)",
            "skip_state_update": True,
            "contains": ["showing detailed transcript"],
            "any": [
                {"contains": ["ctrl+o", "to toggle"]},
                {"contains": ["ctrl+e", "show all"]},
                {"contains": ["ctrl+e", "collapse"]},
                {"contains": ["↑↓ scroll"]},
                {"contains": ["? for shortcuts"]},
            ],
        },
        {
            # 活的確認/選擇表單(最後一條水平線以下):esc to cancel + 確認提示。
            "id": "live_blocked_form",
            "state": "blocked",
            "priority": 980,
            "region": "after_last_horizontal_rule",
            "contains": ["esc to cancel"],
            "any": [
                {"contains": ["enter to confirm"]},
                {"contains": ["enter to select"], "any": [
                    {"contains": ["tab/arrow keys to navigate"]},
                    {"contains": ["arrow keys to navigate"]},
                    {"contains": ["arrows to navigate"]},
                    {"contains": ["↑/↓ to navigate"]},
                    {"contains": ["↑↓ to navigate"]},
                ]},
            ],
        },
        {
            "id": "dynamic_workflow_prompt",
            "state": "blocked",
            "priority": 980,
            "region": "whole_recent",
            "contains": ["run a dynamic workflow?", "esc to cancel"],
        },
        {
            # /btw 背景任務疊層 —— 顯示時 agent 其實還在跑。
            "id": "btw_overlay_working",
            "state": "working",
            "priority": 975,
            "region": "bottom_non_empty_lines(5)",
            "line_regex": ["^\\s*/btw(?:\\s|$)", "(?i)esc to close\\s*$"],
        },
        {
            # ❯ 輸入框內文可見 → 待命。not 門排除「框裡其實是選單」的版面。
            "id": "live_prompt_box",
            "state": "idle",
            "priority": 950,
            "region": "prompt_box_body",
            "line_regex": ["^\\s*❯"],
            "not": [
                {"contains": ["enter to select"]},
                {"contains": ["esc to cancel"]},
                {"contains": ["tab/arrow keys"]},
                {"contains": ["arrow keys to navigate"]},
                {"contains": ["↑/↓ to navigate"]},
            ],
        },
        {
            # /model 選單:人在挑模型,不是 agent 卡住 → unknown + skip。
            "id": "model_picker_menu",
            "state": "unknown",
            "priority": 900,
            "region": "whole_recent",
            "skip_state_update": True,
            "contains": ["select model", "enter to set as default", "esc to cancel"],
            "not": [
                {"contains": ["do you want to proceed?"]},
                {"contains": ["enter to select"]},
            ],
        },
        {
            # Bash 權限審批選單:「Do you want to proceed?」+ ❯ Yes / 1. Yes / 2. No。
            "id": "bash_permission_prompt",
            "state": "blocked",
            "priority": 850,
            "region": "whole_recent",
            "contains": ["do you want to proceed?"],
            "any": [
                {"contains": ["bash command"]},
                {"contains": ["bash("]},
                {"contains": ["contains expansion"]},
                {"contains": ["tab to amend"]},
                {"contains": ["ctrl+e to explain"]},
            ],
            "all": [
                {"any": [
                    {"line_regex": ["(?i)^\\s*❯?\\s*yes\\b"]},
                    {"line_regex": ["(?i)^\\s*1\\.\\s*yes\\b"]},
                    {"line_regex": ["(?i)^\\s*2\\.\\s*no\\b"]},
                ]},
            ],
        },
        {
            "id": "generic_permission_prompt",
            "state": "blocked",
            "priority": 840,
            "region": "after_last_horizontal_rule",
            "contains": ["do you want to proceed?", "esc to cancel"],
            "all": [
                {"any": [
                    {"line_regex": ["(?i)^\\s*❯?\\s*1\\.\\s*yes\\b"]},
                    {"line_regex": ["(?i)^\\s*2\\.\\s*yes\\b"]},
                    {"line_regex": ["(?i)^\\s*2\\.\\s*no\\b"]},
                    {"line_regex": ["(?i)^\\s*3\\.\\s*no\\b"]},
                ]},
            ],
        },
        {
            # 舊版/雜項 blocker 字樣的接底網(空 ❯ 輸入框在場則不算)。
            "id": "legacy_no_prompt_blocker",
            "state": "blocked",
            "priority": 300,
            "region": "whole_recent",
            "any": [
                {"contains": ["do you want to"], "any": [{"contains": ["yes"]}, {"contains": ["❯"]}]},
                {"contains": ["would you like to"], "any": [{"contains": ["yes"]}, {"contains": ["❯"]}]},
                {"contains": ["waiting for permission"]},
                {"contains": ["do you want to allow this connection?"]},
                {"contains": ["tab to amend"]},
                {"contains": ["ctrl+e to explain"]},
                {"contains": ["do you want to proceed?", "esc to cancel"]},
                {"contains": ["review your answers"]},
                {"contains": ["skip interview and plan immediately"]},
            ],
            "not": [
                {"regex": ["(?m)^\\s*❯\\s*$"]},
            ],
        },
        {
            # ✳(U+2733)開頭的標題 = turn 結束後的靜態標記。
            "id": "osc_title_idle",
            "state": "idle",
            "priority": 250,
            "region": "osc_title",
            "regex": ["^✳ "],
        },
        # herdr 還有一條 osc_progress_idle(OSC 9;4 進度序列)—— tmux 路徑
        # 拿不到 progress 訊號源,不移植,免得規則永遠死著誤導 explain。
    ],
}

_GATE_KEYS = ("contains", "regex", "line_regex", "all", "any", "not")
_REGION_COUNT_RE = re.compile(
    r"^(bottom_lines|bottom_non_empty_lines|top_non_empty_lines)\((\d+)\)$")


# ── 規則編譯(regex 預編譯 + 依 priority 排序)────────────────────────────────

def _compile_gate(g: dict) -> dict:
    return {
        "contains": [str(n).lower() for n in g.get("contains", [])],
        "regex": [re.compile(p) for p in g.get("regex", [])],
        "line_regex": [re.compile(p) for p in g.get("line_regex", [])],
        "all": [_compile_gate(n) for n in g.get("all", [])],
        "any": [_compile_gate(n) for n in g.get("any", [])],
        "not": [_compile_gate(n) for n in g.get("not", [])],
    }


def compile_manifest(manifest: dict) -> list[dict]:
    """manifest dict → 已編譯規則列(priority 由高至低;同分保留原順序)。
    壞規則(缺 id / 未知 state / 爛 regex)直接 raise —— 給覆寫載入端 catch。"""
    compiled = []
    for rule in manifest.get("rules", []):
        rid = str(rule.get("id") or "").strip()
        if not rid:
            raise ValueError("rule id must not be empty")
        state = rule.get("state", "unknown")
        if state not in STATES:
            raise ValueError(f"rule {rid} has invalid state {state!r}")
        region = str(rule.get("region", "whole_recent")).strip()
        if not _valid_region(region):
            raise ValueError(f"rule {rid} uses invalid region {region!r}")
        compiled.append({
            "id": rid,
            "state": state,
            "priority": int(rule.get("priority", 0)),
            "region": region,
            "skip_state_update": bool(rule.get("skip_state_update", False)),
            "gate": _compile_gate(rule),
        })
    return sorted(compiled, key=lambda r: -r["priority"])   # stable → 同分照原序


def _valid_region(spec: str) -> bool:
    return spec in ("whole_recent", "prompt_box_body", "above_prompt_box",
                    "after_last_horizontal_rule", "osc_title") \
        or bool(_REGION_COUNT_RE.match(spec))


# ── 區域切割(herdr region 函式的 Python 移植;行為對齊 manifest.rs)──────────

def _is_horizontal_rule(line: str) -> bool:
    """整行是 ─ 水平線(前綴 ≥3 個 ─,或 ─ 之後全空白)。"""
    t = line.strip()
    if not t:
        return False
    n = 0
    for ch in t:
        if ch == "─":
            n += 1
        else:
            break
    if n == 0:
        return False
    return not t[n:].strip() or n >= 3


def _prompt_box_top_border(lines: list[str]):
    """由下往上找第 2 條水平線 = 輸入框上緣(herdr 同款啟發式)。"""
    seen = 0
    for i in range(len(lines) - 1, -1, -1):
        if _is_horizontal_rule(lines[i]):
            seen += 1
            if seen == 2:
                return i
    return None


def _prompt_box_body(pane: str) -> str:
    lines = pane.split("\n")
    top = _prompt_box_top_border(lines)
    if top is None:
        return ""
    end = len(lines)
    for i in range(top + 1, len(lines)):
        if _is_horizontal_rule(lines[i]):
            end = i
            break
    return "\n".join(lines[top + 1:end])


def _above_prompt_box(pane: str) -> str:
    lines = pane.split("\n")
    top = _prompt_box_top_border(lines)
    if top is None:
        return pane
    return "\n".join(lines[:top])


def _after_last_horizontal_rule(pane: str) -> str:
    lines = pane.split("\n")
    last = -1
    for i, line in enumerate(lines):
        if _is_horizontal_rule(line):
            last = i
    return pane if last < 0 else "\n".join(lines[last + 1:])


def _bottom_lines(pane: str, count: int) -> str:
    lines = pane.split("\n")
    return "\n".join(lines[max(0, len(lines) - count):])


def _bottom_non_empty_lines(pane: str, count: int) -> str:
    """由下數第 N 條非空行起、到畫面底(中間的空行保留)。"""
    lines = pane.split("\n")
    seen = 0
    start = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            seen += 1
            start = i
            if seen == count:
                break
    if start is None:
        return ""
    return "\n".join(lines[start:])


def _top_non_empty_lines(pane: str, count: int) -> str:
    lines = pane.split("\n")
    seen = 0
    end = None
    for i, line in enumerate(lines):
        if line.strip():
            seen += 1
            end = i
            if seen == count:
                break
    if end is None:
        return ""
    return "\n".join(lines[:end + 1])


def _region_text(pane: str, title: str, spec: str) -> str:
    if spec == "osc_title":
        return title            # OSC 區域讀專屬訊號源,不看 pane
    if spec == "whole_recent":
        return pane
    if spec == "prompt_box_body":
        return _prompt_box_body(pane)
    if spec == "above_prompt_box":
        return _above_prompt_box(pane)
    if spec == "after_last_horizontal_rule":
        return _after_last_horizontal_rule(pane)
    m = _REGION_COUNT_RE.match(spec)
    if m:
        name, count = m.group(1), int(m.group(2))
        if name == "bottom_lines":
            return _bottom_lines(pane, count)
        if name == "bottom_non_empty_lines":
            return _bottom_non_empty_lines(pane, count)
        return _top_non_empty_lines(pane, count)
    return ""


# ── 評估 ─────────────────────────────────────────────────────────────────────

def _gate_matches(g: dict, text: str, low: str) -> bool:
    if not all(needle in low for needle in g["contains"]):
        return False
    if not all(rx.search(text) for rx in g["regex"]):
        return False
    for rx in g["line_regex"]:
        if not any(rx.search(line) for line in text.split("\n")):
            return False
    if not all(_gate_matches(n, text, low) for n in g["all"]):
        return False
    if g["any"] and not any(_gate_matches(n, text, low) for n in g["any"]):
        return False
    if any(_gate_matches(n, text, low) for n in g["not"]):
        return False
    return True


# ── 覆寫載入(env CC_DETECT_MANIFEST,mtime 熱更新,壞檔退回內建)─────────────

_EMBEDDED_RULES = compile_manifest(DEFAULT_MANIFEST)     # import 時就驗證內建規則
# path → {"mtime": float, "rules": list|None(壞檔), "logged": bool}
_OVERRIDE_CACHE: dict = {}


def _log(event: str, **fields) -> None:
    """獨立模組不 import bridge(避免循環);格式對齊 _log_event 方便同一套 grep。"""
    payload = {"event": event,
               "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               **fields}
    print("[cc-detect] " + json.dumps(payload, ensure_ascii=False, sort_keys=True),
          flush=True)


def _merge_rules(override: dict) -> dict:
    """覆寫 manifest + 內建 → 合併後 manifest(replace=true 則整份取代)。"""
    if override.get("replace"):
        return override
    by_id = {r["id"]: dict(r) for r in DEFAULT_MANIFEST["rules"]}
    order = [r["id"] for r in DEFAULT_MANIFEST["rules"]]
    for rule in override.get("rules", []):
        rid = str(rule.get("id") or "").strip()
        if not rid:
            raise ValueError("override rule id must not be empty")
        if rule.get("remove"):
            by_id.pop(rid, None)
            continue
        if rid not in by_id:
            order.append(rid)
        by_id[rid] = rule
    merged = dict(override)
    merged["rules"] = [by_id[rid] for rid in order if rid in by_id]
    return merged


def _load_rules() -> tuple[list[dict], str]:
    """回 (已編譯規則列, 來源標籤)。永不 raise:覆寫檔任何問題都退回內建。"""
    path = (os.environ.get("CC_DETECT_MANIFEST") or "").strip()
    if not path:
        return _EMBEDDED_RULES, "embedded"
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        cached = _OVERRIDE_CACHE.get(path)
        if not (cached and cached.get("logged")):
            _log("cc_detect_manifest_unreadable", path=path)
            _OVERRIDE_CACHE[path] = {"mtime": None, "rules": None, "logged": True}
        return _EMBEDDED_RULES, "embedded"
    cached = _OVERRIDE_CACHE.get(path)
    if cached and cached.get("mtime") == mtime:
        if cached["rules"] is not None:
            return cached["rules"], path
        return _EMBEDDED_RULES, "embedded"           # 壞檔已 log 過,靜默退回
    try:
        with open(path, encoding="utf-8") as fh:
            override = json.load(fh)
        rules = compile_manifest(_merge_rules(override))
        _OVERRIDE_CACHE[path] = {"mtime": mtime, "rules": rules, "logged": False}
        _log("cc_detect_manifest_loaded", path=path, rules=len(rules))
        return rules, path
    except Exception as exc:  # noqa: BLE001 — 輪詢路徑,壞檔只能降級不能炸
        _OVERRIDE_CACHE[path] = {"mtime": mtime, "rules": None, "logged": True}
        _log("cc_detect_manifest_error", path=path,
             error=f"{type(exc).__name__}: {exc}"[:200])
        return _EMBEDDED_RULES, "embedded"


# ── 對外 API ─────────────────────────────────────────────────────────────────

def classify(pane_text: str, pane_title: str | None = None) -> dict:
    """pane(+ 可選 OSC title)→ {"state","rule","priority"}。純函式、永不 raise
    (bridge 的輪詢路徑靠它,壞了要回 unknown 而不是把 watcher 炸掉)。"""
    try:
        rules, _src = _load_rules()
        title = pane_title or ""
        pane = pane_text or ""
        for r in rules:
            text = _region_text(pane, title, r["region"])
            if _gate_matches(r["gate"], text, text.lower()):
                return {"state": r["state"], "rule": r["id"],
                        "priority": r["priority"]}
        return {"state": "idle", "rule": DEFAULT_IDLE_FALLBACK, "priority": -1}
    except Exception as exc:  # noqa: BLE001
        _log("cc_detect_classify_error", error=f"{type(exc).__name__}: {exc}"[:200])
        return {"state": "unknown", "rule": "classify_error", "priority": -1}


def explain(pane_text: str, pane_title: str | None = None) -> dict:
    """herdr `agent explain` 的等價物:逐條列出每個規則的命中結果,並附上命中
    規則實際看到的證據行 —— 偵測跑歪時直接看這個,不用腦內重演 regex。"""
    rules, source = _load_rules()
    title = pane_title or ""
    pane = pane_text or ""
    matched = None
    evidence: list[str] = []
    evaluated = []
    for r in rules:
        text = _region_text(pane, title, r["region"])
        ok = _gate_matches(r["gate"], text, text.lower())
        evaluated.append({
            "id": r["id"], "priority": r["priority"], "region": r["region"],
            "state": r["state"], "matched": ok,
            "region_preview": text[:240],
        })
        if ok and matched is None:               # priority 已排序,首個命中即答案
            matched = r
            evidence = [ln for ln in text.split("\n") if ln.strip()][:12]
    result = classify(pane_text, pane_title)
    return {
        **result,
        "manifest_source": source,
        "matched_rule": None if matched is None else {
            "id": matched["id"], "priority": matched["priority"],
            "region": matched["region"], "state": matched["state"],
            "skip_state_update": matched["skip_state_update"],
        },
        "evidence_lines": evidence,
        "evaluated_rules": evaluated,
    }
