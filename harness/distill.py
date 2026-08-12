"""夜批蒸餾器 —— 把昨晚的軌跡蒸成「提案」,等人審(藍圖 §2)。

    python -m harness.distill --hours 24            # 真的跑一輪(會寫提案)
    python -m harness.distill --hours 24 --dry-run  # 只印,不寫庫

善彰的鐵律:**夜批蒸餾 + 晨報人審,不搞自動自改**。所以這支程式的輸出
**一律是 `state=proposed`**;它不 import `store.approve`,也沒有任何路徑
能讓提案自己生效。最壞情況是「提了一堆爛提案」,而爛提案的代價只是善彰
早上多按幾下 reject。

## 兩種提案來源,刻意分開

| 庫 | 來源 | 為什麼 |
|---|---|---|
| memory / skill / prompt | **LLM 蒸餾** | 需要語意歸納:「這三次失敗其實是同一個坑」 |
| subagent_route | **純統計** | 成功率是數得出來的。讓模型猜路由 = 拿幻覺當路由表 |

route 走統計還有個好處:就算今晚 Ollama 掛了,路由提案照樣產得出來。

## 提示詞餵什麼

每組(節點 × 任務類型)最多 `PER_GROUP` 條軌跡,每條已經過
`trajectory.py` 的遮罩 + 封頂。**秘密不可能進到提示詞**,因為進庫前就被
洗過一次了(縱深防禦:這裡再走一次 `redact_text`)。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time

if __package__ in (None, ""):        # 支援 `python harness/distill.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import model as harness_model      # noqa: E402
from harness import trajectory as traj_mod      # noqa: E402
from harness.store import HarnessStore          # noqa: E402

# 每組餵給模型的軌跡數;超過取「最近的 + 失敗的」(失敗最有蒸餾價值)
PER_GROUP = 12
MAX_GROUPS = 8                # 一輪最多處理幾組(夜批要在早上七點前跑完)
MAX_PER_STORE = {"memory": 5, "skill": 3, "prompt": 1}
ROUTE_MIN_SAMPLES = 4         # 樣本太少不出路由提案(3 次成功不代表什麼)
ROUTE_MIN_RATE = 0.7          # 成功率門檻


# ── 任務分類(v0 純啟發式:看用了哪些工具)──────────────────────────────
_TOOL_KIND = (
    ({"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"}, "coding"),
    ({"Bash", "shell", "run_terminal"}, "shell"),
    ({"Grep", "Glob", "Read", "search"}, "research"),
    ({"WebFetch", "WebSearch"}, "web"),
    ({"Task", "Agent", "agent_call"}, "delegation"),
)


def task_kind(traj: dict) -> str:
    """一條軌跡的任務類型。v0 只看工具組合 —— 刻意不叫模型分類,因為分類
    要穩定(同樣的工作每晚都要落到同一組),啟發式比模型穩。"""
    tools = {str(s.get("tool") or "") for s in traj.get("steps") or []
             if s.get("kind") == "tool"}
    tools.discard("")
    for names, kind in _TOOL_KIND:
        if tools & names:
            return kind
    return "chat" if not tools else "tooling"


def group_trajectories(trajs) -> dict:
    """→ {(scope, task_kind): [traj, ...]}。scope = `node:<session_id>`。"""
    groups: dict = {}
    for t in trajs or []:
        sid = str(t.get("session_id") or "")
        if not sid:
            continue
        groups.setdefault((f"node:{sid}", task_kind(t)), []).append(t)
    return groups


def _pick(trajs: list, n: int = PER_GROUP) -> list:
    """挑要餵的軌跡:失敗的優先(最有學習價值),再補最近的。"""
    fails = [t for t in trajs if not (t.get("result") or {}).get("ok", True)]
    oks = [t for t in trajs if (t.get("result") or {}).get("ok", True)]
    fails.sort(key=lambda t: t.get("ts") or 0, reverse=True)
    oks.sort(key=lambda t: t.get("ts") or 0, reverse=True)
    return (fails + oks)[:n]


# ── 提示詞 ──────────────────────────────────────────────────────────────

_PROMPT_HEAD = """你是一個 agent 系統的「軌跡蒸餾器」。下面是同一個節點在同一類任務上的近期執行軌跡。
你的工作:從重複出現的模式中,提煉出能讓這個節點**下次做得更好**的東西。

規則:
- 只提「重複出現」或「造成明顯失敗」的模式。單次偶發不要提。
- 具體、可執行。不要寫「應該更仔細」這種廢話。
- 沒有值得提的就回空陣列。寧可不提,也不要湊數。
- 全部用繁體中文。
- evidence 必須是下面軌跡的 id(traj-…),不可捏造。

只輸出 JSON,格式如下(三個 key 都必須在,可以是空陣列):
{
  "memory": [{"key": "短標識", "fact": "學到的耐久事實(一句話)", "tags": ["標籤"],
              "rationale": "為什麼值得記", "evidence": ["traj-…"]}],
  "skill":  [{"key": "短標識", "name": "技能名", "when_to_use": "什麼情況用",
              "steps": ["步驟1", "步驟2"], "rationale": "…", "evidence": ["traj-…"]}],
  "prompt": [{"fragment": "要加進這個節點 system prompt 的一段話(≤300字)",
              "rationale": "…", "evidence": ["traj-…"]}]
}
"""


def build_prompt(scope: str, kind: str, trajs: list) -> str:
    """把一組軌跡攤成提示詞。刻意用精簡 JSON 而不是原始卡片流:
    模型要看的是「做了什麼、成不成」,不是渲染細節。"""
    lines = [_PROMPT_HEAD,
             f"\n節點:{scope}\n任務類型:{kind}\n軌跡數:{len(trajs)}\n",
             "軌跡:"]
    for t in trajs:
        res = t.get("result") or {}
        compact = {
            "id": t.get("id"),
            "purpose": t.get("purpose") or "",
            "ok": bool(res.get("ok", True)),
            "duration_s": res.get("duration_s"),
            "error": res.get("error", ""),
            "steps": [{k: v for k, v in s.items() if v not in ("", None)}
                      for s in (t.get("steps") or [])],
        }
        lines.append(traj_mod.redact_text(
            json.dumps(compact, ensure_ascii=False)))
    return "\n".join(lines)


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def parse_proposals(text: str) -> dict:
    """模型輸出 → {store: [dict]}。壞 JSON / 缺欄一律回空,不拋。

    夜批不能因為模型某晚多打了一個逗號就整輪掛掉,所以這裡極度容錯:
    先剝 code fence,再試整段 JSON,再退而求其次抓第一個 {…} 區塊。
    """
    if not text:
        return {}
    s = _FENCE_RE.sub("", text.strip())
    data = None
    for candidate in (s, _first_json_object(s)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
            break
        except (ValueError, TypeError):
            continue
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    for store in ("memory", "skill", "prompt"):
        items = data.get(store)
        if not isinstance(items, list):
            continue
        clean = [it for it in items if isinstance(it, dict)]
        if clean:
            out[store] = clean[:MAX_PER_STORE.get(store, 5)]
    return out


def _first_json_object(s: str) -> str:
    i = s.find("{")
    if i < 0:
        return ""
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                return s[i:j + 1]
    return ""


# ── preview(diff 樣的「會變成什麼」)────────────────────────────────────

def make_preview(store: str, payload: dict, current: str = "") -> str:
    """給人看的 diff-like 預覽。晨報與核准頁顯示的就是這段。"""
    if store == "memory":
        return f"+ 記憶:{payload.get('fact', '')}"
    if store == "skill":
        steps = payload.get("steps") or []
        body = "\n".join(f"+   {i + 1}. {s}" for i, s in enumerate(steps[:8]))
        return (f"+ 技能「{payload.get('name', '')}」\n"
                f"+   時機:{payload.get('when_to_use', '')}\n{body}")
    if store == "prompt":
        node = payload.get("node", "")
        lines = [f"# {node} 的 append_system_prompt"]
        if current:
            lines += ["- " + ln for ln in current.splitlines()]
        else:
            lines.append("- (目前無)")
        lines += ["+ " + ln for ln in str(payload.get("fragment", "")).splitlines()]
        return "\n".join(lines)
    if store == "subagent_route":
        return (f"+ 路由:{payload.get('task_kind', '')} → {payload.get('target', '')}"
                f"(成功 {payload.get('success_n', 0)}/{payload.get('sample_n', 0)})")
    return ""


# ── 統計型路由提案(不經模型)────────────────────────────────────────────

def route_candidates(trajs) -> list[dict]:
    """(task_kind, node) 的成功率統計 → 路由提案候選。

    純數數,沒有模型參與 —— 路由表要能被質疑「你憑什麼這樣派」,而數字
    答得出來,模型答不出來。
    """
    tally: dict = {}
    for t in trajs or []:
        sid = str(t.get("session_id") or "")
        if not sid:
            continue
        k = (task_kind(t), sid)
        ok = bool((t.get("result") or {}).get("ok", True))
        cnt = tally.setdefault(k, {"n": 0, "ok": 0, "provider": t.get("provider") or ""})
        cnt["n"] += 1
        cnt["ok"] += 1 if ok else 0
    best: dict = {}
    for (kind, sid), c in tally.items():
        if c["n"] < ROUTE_MIN_SAMPLES:
            continue
        rate = c["ok"] / c["n"]
        if rate < ROUTE_MIN_RATE:
            continue
        cur = best.get(kind)
        if cur is None or rate > cur["rate"] or (
                rate == cur["rate"] and c["n"] > cur["sample_n"]):
            best[kind] = {"task_kind": kind, "target": sid,
                          "provider": c["provider"], "rate": rate,
                          "success_n": c["ok"], "sample_n": c["n"]}
    return list(best.values())


# ── 主流程 ──────────────────────────────────────────────────────────────

async def run(store: HarnessStore, *, hours: float = 24.0,
              model_call=None, current_prompt=None, now: float | None = None,
              max_trajectories: int = 400, max_groups: int = MAX_GROUPS,
              dry_run: bool = False) -> dict:
    """跑一輪蒸餾。回傳 {run_id, trajectories, groups, proposals, errors}。

    - `model_call`: `async (prompt: str) -> str`。預設走本機 Ollama
      (`harness.model.ollama_text`,JSON mode)。**測試一律注入 mock**。
    - `current_prompt`: `(node: str) -> str`,拿該節點現行的
      append_system_prompt 來做 diff 預覽。bridge 端傳
      `_cc_read_spawn_config`;CLI 沒有就顯示「(目前無)」。
    - `dry_run`: 只算不寫(提案在回傳值裡,庫裡不留痕)。
    """
    now = now if now is not None else time.time()
    since = now - float(hours) * 3600.0
    mc = model_call or _default_model_call
    trajs = store.trajectories_since(since, limit=max_trajectories)
    rid = "" if dry_run else store.run_start(
        hours=hours, model=harness_model.distill_model())
    proposals: list = []
    errors: list = []

    try:
        # 1) 統計型:路由(不需要模型,先做,模型掛了也有產出)
        for cand in route_candidates(trajs):
            payload = {"task_kind": cand["task_kind"], "target": cand["target"],
                       "success_n": cand["success_n"], "sample_n": cand["sample_n"]}
            ev = [t["id"] for t in trajs
                  if t.get("session_id") == cand["target"]
                  and task_kind(t) == cand["task_kind"]][:20]
            rec = {"store": "subagent_route", "key": cand["task_kind"],
                   "scope": "global", "payload": payload,
                   "rationale": (f"近 {hours:.0f} 小時內 {cand['task_kind']} 類任務,"
                                 f"{cand['target']} 成功 {cand['success_n']}/"
                                 f"{cand['sample_n']}(成功率 "
                                 f"{cand['rate'] * 100:.0f}%),為同類最高。"),
                   "evidence": ev,
                   "preview": make_preview("subagent_route", payload),
                   "meta": {"source": "statistics", "rate": cand["rate"]}}
            proposals.append(rec)

        # 2) LLM 蒸餾:memory / skill / prompt(逐組)
        groups = group_trajectories(trajs)
        ordered = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)
        for (scope, kind), items in ordered[:max_groups]:
            picked = _pick(items)
            if len(picked) < 2:          # 一條軌跡蒸不出「重複模式」
                continue
            valid_ids = {t["id"] for t in picked}
            try:
                raw = await mc(build_prompt(scope, kind, picked))
            except Exception as exc:      # noqa: BLE001 — 一組失敗不斷整輪
                errors.append(f"{scope}/{kind}: {type(exc).__name__}: {exc}")
                continue
            parsed = parse_proposals(raw)
            node = scope[5:] if scope.startswith("node:") else scope
            provider = next((str(t.get("provider") or "") for t in picked
                             if t.get("provider")), "")
            for st, items2 in parsed.items():
                for it in items2:
                    rec = _shape(st, it, scope, node, kind, valid_ids,
                                 current_prompt, provider)
                    if rec:
                        proposals.append(rec)

        # 3) 落庫(state=proposed;dry_run 不寫)
        written = []
        if not dry_run:
            for rec in proposals:
                try:
                    row = store.propose(
                        rec["store"], key=rec["key"], scope=rec["scope"],
                        payload=rec["payload"], rationale=rec["rationale"],
                        evidence=rec["evidence"], preview=rec["preview"],
                        meta=rec.get("meta"))
                    written.append(row["id"])
                except Exception as exc:   # noqa: BLE001
                    errors.append(f"propose {rec['store']}/{rec['key']}: {exc}")
            store.mark_distilled([t["id"] for t in trajs])
    finally:
        if rid:
            store.run_finish(rid, trajectories=len(trajs),
                             proposals=len(proposals),
                             error="; ".join(errors)[:500])

    return {"run_id": rid, "trajectories": len(trajs),
            "groups": len(group_trajectories(trajs)),
            "proposals": proposals, "errors": errors,
            "written": [] if dry_run else written, "dry_run": dry_run}


def _shape(st: str, it: dict, scope: str, node: str, kind: str,
           valid_ids: set, current_prompt=None, provider: str = "") -> dict | None:
    """模型吐的一項 → 可入庫的提案 dict。缺必要欄位就丟掉(不腦補)。

    evidence 只留**確實在這組軌跡裡**的 id —— 模型愛捏造引用,捏造的證據
    比沒有證據更糟(它會讓晨報上的東西看起來有憑有據)。
    """
    R = traj_mod.redact_text
    ev = [e for e in (it.get("evidence") or []) if e in valid_ids]
    rationale = R(str(it.get("rationale") or ""))[:800]
    if st == "memory":
        fact = R(str(it.get("fact") or "")).strip()
        if not fact:
            return None
        key = R(str(it.get("key") or fact[:40])).strip()[:80]
        payload = {"fact": fact[:1000],
                   "tags": [R(str(t))[:30] for t in (it.get("tags") or [])][:6]}
        return {"store": st, "key": key, "scope": scope, "payload": payload,
                "rationale": rationale, "evidence": ev,
                "preview": make_preview(st, payload),
                "meta": {"source": "llm", "task_kind": kind}}
    if st == "skill":
        name = R(str(it.get("name") or "")).strip()
        steps = [R(str(s)).strip()[:300] for s in (it.get("steps") or []) if str(s).strip()]
        if not name or not steps:
            return None
        payload = {"name": name[:120],
                   "when_to_use": R(str(it.get("when_to_use") or ""))[:400],
                   "steps": steps[:12]}
        return {"store": st, "key": R(str(it.get("key") or name))[:80],
                "scope": scope, "payload": payload, "rationale": rationale,
                "evidence": ev, "preview": make_preview(st, payload),
                "meta": {"source": "llm", "task_kind": kind}}
    if st == "prompt":
        frag = R(str(it.get("fragment") or "")).strip()
        if not frag:
            return None
        payload = {"node": node, "provider": provider, "fragment": frag[:1200]}
        cur = ""
        if current_prompt is not None:
            try:
                cur = str(current_prompt(node) or "")
            except Exception:  # noqa: BLE001
                cur = ""
        # key = 節點:一個節點同時只該有一份現行片段(新版自動接舊版的版號)
        return {"store": st, "key": node, "scope": scope, "payload": payload,
                "rationale": rationale, "evidence": ev,
                "preview": make_preview(st, payload, cur),
                "meta": {"source": "llm", "task_kind": kind}}
    return None


async def _default_model_call(prompt: str) -> str:
    """預設走本機 Ollama 的 JSON mode。num_ctx 依提示詞長度給(沿用清稿那
    邊的教訓:別吃模型預設的超大 context,冷載入建 KV cache 是耗時大宗)。"""
    return await harness_model.ollama_text(
        prompt, num_ctx=min(40960, max(8192, len(prompt) // 2 + 4096)),
        timeout=float(os.environ.get("HARNESS_MODEL_TIMEOUT", "") or 180.0),
        fmt="json")


# ── CLI ─────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m harness.distill",
        description="夜批軌跡蒸餾:讀近 N 小時軌跡 → 產出提案(state=proposed)。"
                    "提案永遠不會自己生效,一定要人在晨報上核准。")
    ap.add_argument("--hours", type=float, default=24.0, help="回看時數(預設 24)")
    ap.add_argument("--db", default=None, help="harness DB 路徑(預設 $HARNESS_DB)")
    ap.add_argument("--dry-run", action="store_true", help="只印不寫庫")
    ap.add_argument("--max-groups", type=int, default=MAX_GROUPS)
    ap.add_argument("--json", action="store_true", help="輸出 JSON")
    args = ap.parse_args(argv)

    store = HarnessStore(args.db)
    out = asyncio.run(run(store, hours=args.hours, dry_run=args.dry_run,
                          max_groups=args.max_groups))
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    print(f"軌跡 {out['trajectories']} 條 / {out['groups']} 組 "
          f"→ 提案 {len(out['proposals'])} 筆"
          + ("(dry-run,未寫庫)" if out["dry_run"] else ""))
    for p in out["proposals"]:
        print(f"\n[{p['store']}] {p['key']}  scope={p['scope']}")
        print(f"  理由:{p['rationale']}")
        print(f"  證據:{', '.join(p['evidence']) or '(無)'}")
        for ln in p["preview"].splitlines():
            print(f"  {ln}")
    for e in out["errors"]:
        print(f"\n⚠️ {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
