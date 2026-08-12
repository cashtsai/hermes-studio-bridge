#!/usr/bin/env python3
"""把共用 `~/.codex` 裡的既有 thread 一次性搬進 bridge 的隔離家目錄。

這支是**選擇性、可重複執行、來源全程唯讀**的。它不刪除也不修改
`~/.codex` 底下任何東西:thread-store 先複製出快照再讀,逐字稿只讀不寫
(macOS/APFS 上用 `cp -c` 做 copy-on-write clone,秒殺且不佔額外空間)。

為什麼需要它:開了 `POCKET_CODEX_ISOLATED=1` 之後,bridge 用的是自己的
thread-store,**舊的 thread 不會自動出現在 Pocket 裡**。搬過來的才會。

用法:
    # 1. 看看有哪些可以搬(唯讀,不動任何東西)
    scripts/migrate-codex-threads.py --list

    # 2. 預演:確認要搬哪幾條、會佔多少空間
    scripts/migrate-codex-threads.py --thread 019f39d3-... --thread 019f01b0-...

    # 3. 真的搬
    scripts/migrate-codex-threads.py --thread 019f39d3-... --apply

    # 或者「最近 N 條」一起搬
    scripts/migrate-codex-threads.py --recent 12 --apply

環境變數:`POCKET_CODEX_HOME`(目標,預設 ~/.pocket/codex-home)、
`CODEX_HOME`(來源,預設 ~/.codex)。也可以用 --source/--target 蓋掉。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import codex_home  # noqa: E402


def human(nbytes: int) -> str:
    size = float(nbytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="codex thread 搬進隔離家目錄")
    ap.add_argument("--thread", action="append", default=[],
                    help="要搬的 thread id(可重複)")
    ap.add_argument("--recent", type=int, default=0,
                    help="改成搬「最近 N 條」")
    ap.add_argument("--list", action="store_true", help="只列出可搬的 thread")
    ap.add_argument("--include-archived", action="store_true",
                    help="連封存的 thread 也算進來")
    ap.add_argument("--source", default=None, help="來源家目錄(預設 ~/.codex)")
    ap.add_argument("--target", default=None,
                    help="目標家目錄(預設 $POCKET_CODEX_HOME)")
    ap.add_argument("--apply", action="store_true",
                    help="真的寫入;不加就是預演")
    args = ap.parse_args(argv)

    source = args.source or codex_home.shared_home()
    target = args.target or codex_home.isolated_home()

    if args.list:
        rows = codex_home.list_threads(
            source, limit=(args.recent or 30),
            include_archived=args.include_archived)
        print(f"來源:{source}(唯讀)")
        for r in rows:
            flag = "" if r["rollout_exists"] else "  ⚠️ 逐字稿不見了,搬不了"
            print(f"  {r['id']}  {(r['name'] or '—'):<10} {r['title']}{flag}")
        print(f"\n共 {len(rows)} 條。要搬:--thread <id> --apply")
        return 0

    if not args.thread and not args.recent:
        ap.error("要嘛 --thread <id>,要嘛 --recent N(或先 --list 看看)")

    result = codex_home.migrate_threads(
        thread_ids=args.thread, recent=args.recent, source=source,
        target=target, apply=args.apply,
        include_archived=args.include_archived)

    print(f"來源:{result['source']}(唯讀)")
    print(f"目標:{result['target']}")
    print("模式:" + ("真的寫入" if args.apply else "預演(加 --apply 才會寫)"))
    for item in result["migrated"]:
        print(f"  ✔ {item['id']}  {item.get('name') or '—'}  "
              f"[{item.get('mode')}] {item.get('rollout')}")
    for item in result["skipped"]:
        print(f"  – {item['id']}  跳過:{item['reason']}")
    print(f"\n搬了 {len(result['migrated'])} 條 / 跳過 {len(result['skipped'])} 條;"
          f"逐字稿共 {human(result['bytes'])}")
    if not args.apply and result["migrated"]:
        print("這只是預演。確認沒問題就重跑一次並加上 --apply。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
