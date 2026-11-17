#!/usr/bin/env python3
"""違規棘輪閘門 —— 新債擋死,舊債凍結。引擎與制度說明在 baseline_inventory.py。

用法:
    scripts/ratchet/check.py                      # 閘門(有新違規 → exit 1)
    scripts/ratchet/check.py --update             # 把現況寫回 baseline
    scripts/ratchet/check.py --list <rule>        # 列某條規則的現存債
    scripts/ratchet/check.py --rules              # 規則清單與理由
    scripts/ratchet/check.py --json               # 給 CI / 晨報吃

離開碼:0 = 沒有新違規(可能有可收斂的舊債,只提示);1 = 有新違規或 baseline 壞了。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ratchet import baseline_inventory as engine  # noqa: E402


def load_baseline(path: str = engine.BASELINE_PATH) -> dict:
    if not os.path.exists(path):
        # 第一次跑:空 baseline = 全部現存違規都算新債。這會很紅,而且**應該**
        # 很紅 —— 不要用「檔不存在就當全部放行」來迎合第一次跑的人,
        # 那會讓「baseline 被誤刪」變成一件安靜的事。
        return {"schema_version": engine.SCHEMA_VERSION, "rules": {}}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("schema_version") != engine.SCHEMA_VERSION:
        raise SystemExit(
            f"baseline schema 版本不合({data.get('schema_version')} != "
            f"{engine.SCHEMA_VERSION})。改過格式就要同時遷移 baseline —— "
            "直接重跑 --update 會把全部舊債當新債,產生幾百條紅線。")
    if data.get("hash_recipe") not in (None, engine.HASH_RECIPE):
        raise SystemExit(
            f"baseline 的雜湊配方是 `{data.get('hash_recipe')}`,引擎現在用的是 "
            f"`{engine.HASH_RECIPE}` —— 身分演算法換過了,整份 baseline 的 key "
            "都失效。這是一次刻意的遷移,要在同一個 change 裡 --update 並說明。")
    return data


def write_baseline(result: engine.ScanResult, path: str = engine.BASELINE_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(engine.to_baseline(result), f, ensure_ascii=False, indent=2,
                  sort_keys=False)
        f.write("\n")


def _rule(name: str):
    for r in engine.RULES:
        if r.name == name:
            return r
    raise SystemExit(f"沒有這條規則:{name}(可用:"
                     f"{', '.join(r.name for r in engine.RULES)})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="違規棘輪:舊債凍結,新債擋死")
    ap.add_argument("--root", default=engine.REPO_ROOT)
    ap.add_argument("--baseline", default=engine.BASELINE_PATH)
    ap.add_argument("--update", action="store_true",
                    help="把現況寫回 baseline(只為搬檔換 key / 清債收斂而用)")
    ap.add_argument("--list", metavar="RULE", help="列某條規則的現存違規")
    ap.add_argument("--rules", action="store_true", help="規則清單與理由")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-corpus-check", action="store_true",
                    help="關掉語料下限哨兵(只有自我測試用的假 repo 該用)")
    args = ap.parse_args(argv)

    if args.rules:
        for r in engine.RULES:
            print(f"── {r.name}\n   為什麼是債:{r.why}\n   新違規怎麼辦:{r.fix}\n")
        return 0

    result = engine.scan(args.root, enforce_corpus=not args.no_corpus_check)

    if args.list:
        rule = _rule(args.list)
        hits = [v for v in result.violations if v.rule == rule.name]
        print(f"{rule.name}:{len(hits)} 處\n")
        for v in hits:
            print(f"  {v.path}:{v.line}  {v.snippet}")
        return 0

    if args.update:
        write_baseline(result, args.baseline)
        counts = {}
        for v in result.violations:
            counts[v.rule] = counts.get(v.rule, 0) + 1
        print(f"baseline 已更新:{args.baseline}")
        for name, n in sorted(counts.items()):
            print(f"  {name}: {n}")
        print("\n⚠️ `--update` 會把現在的新違規變成「舊債」。review 的時候"
              "請看 baseline 的條數有沒有變多 —— 變多一定要有一句說明。")
        return 0

    verdict = engine.compare(result, load_baseline(args.baseline))

    if args.json:
        print(json.dumps({
            "ok": verdict.ok,
            "scanned": verdict.scanned,
            "frozen": verdict.frozen,
            "corpus": verdict.corpus,
            "new": [{"rule": v.rule, "path": v.path, "line": v.line,
                     "snippet": v.snippet} for v in verdict.new],
            "retired": [{"rule": r, "identity": i, "path": m.get("path"),
                         "snippet": m.get("snippet")}
                        for r, i, m in verdict.retired],
        }, ensure_ascii=False, indent=2))
        return 0 if verdict.ok else 1

    print(f"掃到 {verdict.scanned} 處違規,baseline 凍結 {verdict.frozen} 處。")
    for name, n in sorted(verdict.corpus.items()):
        print(f"  語料 {name}: {n} 個檔")

    if verdict.retired:
        print(f"\n✅ 有 {len(verdict.retired)} 筆舊債已經不見了 —— "
              "棘輪可以往前轉一格:")
        for rule_name, _identity, meta in verdict.retired[:20]:
            print(f"  {rule_name}  {meta.get('path')}:{meta.get('line')}  "
                  f"{meta.get('snippet', '')[:80]}")
        if len(verdict.retired) > 20:
            print(f"  …還有 {len(verdict.retired) - 20} 筆")
        print("  → 跑 `scripts/ratchet/check.py --update` 把 baseline 收斂"
              "(這一步不緊急,但不做的話那些 key 會一直留著當墓碑)")

    if verdict.new:
        print(f"\n❌ {len(verdict.new)} 筆**新**違規 —— 舊債凍結,新債擋死:")
        shown = set()
        for v in verdict.new:
            print(f"  {v.rule}  {v.path}:{v.line}\n      {v.snippet}")
            if v.rule not in shown:
                shown.add(v.rule)
                rule = _rule(v.rule)
                print(f"      ↳ 為什麼是債:{rule.why}")
                print(f"      ↳ 怎麼辦:{rule.fix}")
        print("\n不要用 `--update` 把它變成舊債 —— 那是繞過閘門,"
              "而且 review 看 baseline 條數就看得到。")
        return 1

    print("\n沒有新違規。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
