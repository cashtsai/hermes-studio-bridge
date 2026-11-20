#!/usr/bin/env python3
"""刪掉沒有任何索引列引用的孤兒 blob。**預設 dry-run。**

## 為什麼有孤兒(2026-11-20 查出)

`media_artifacts.capture_bytes()` 的 upsert:

    ON CONFLICT(session_id, source_ref) DO UPDATE SET
        filename = excluded.filename, mime = ..., updated_at = ...

**`stored_relpath` 與 `sha256` 不在更新清單裡。** 所以同一個
`(session_id, source_ref)` 的內容改了之後重新擷取時:新 blob 已經落盤,
但索引那一列還指著舊 blob → **新 blob 當場變孤兒,永遠沒人引用**。

實測:128,341 列裡 68,753 列被重新擷取過;磁碟 18,837 個 blob 中
**5,060 個(27.42 GB)完全沒有列引用**。這是 86% 的空間。

> 這支只負責掃地。**根因(upsert 不更新 relpath)另外修** —— 不修的話
> 孤兒會繼續長出來,而且使用者下載到的永遠是舊版本內容。

## 為什麼要有 `--min-age-hours`(預設 24)

store 是**先寫 blob、後寫索引列**。剛落盤還沒入列的檔案,在這支眼裡
長得跟孤兒一模一樣 —— bridge 是 24×7 在跑的,沒有年齡護欄就會刪掉
正在產生的成品。這條護欄不准拿掉。

## 用法

    ./media-gc-orphans.py                  # dry-run
    ./media-gc-orphans.py --apply          # 真的刪
    ./media-gc-orphans.py --min-age-hours 48
"""
import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("POCKET_MEDIA_DIR",
                           os.path.expanduser("~/.pocket/media-artifacts")))
INDEX = ROOT / "index.sqlite3"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def referenced() -> set[str]:
    """**所有**列引用到的 relpath —— 不分 available。

    已標 unavailable 的列也算引用:那是「歸檔了但還認得」,blob 可能
    之後要取回。只有「完全沒有列」才算孤兒。
    """
    with sqlite3.connect(f"file:{INDEX}?mode=ro", uri=True) as conn:
        return {r[0] for r in conn.execute(
            "SELECT DISTINCT stored_relpath FROM artifacts"
            " WHERE stored_relpath IS NOT NULL")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--min-age-hours", type=int, default=24,
                    help="只動比這更舊的檔(避開『已落盤、尚未入索引』的競態)")
    args = ap.parse_args()

    if not INDEX.exists():
        print(f"找不到索引:{INDEX}", file=sys.stderr)
        return 1

    refs = referenced()
    cutoff = time.time() - args.min_age_hours * 3600
    orphans, skipped_young, total = [], 0, 0

    for path in (ROOT / "blobs").rglob("*"):
        if not path.is_file() or path.name.startswith(".incoming."):
            continue
        rel = str(path.relative_to(ROOT))
        if rel in refs:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_mtime > cutoff:
            skipped_young += 1
            continue
        orphans.append((path, st.st_size))
        total += st.st_size

    print(f"索引引用 {len(refs)} 個 relpath")
    print(f"孤兒 {len(orphans)} 檔,{human(total)}"
          f"(另有 {skipped_young} 個太新、未達 {args.min_age_hours} 小時,跳過)")

    if not args.apply:
        for p, s in sorted(orphans, key=lambda x: -x[1])[:5]:
            print(f"  最大:{human(s):>10}  {p.relative_to(ROOT)}")
        print("\n(dry-run。加 --apply 才會真的刪)")
        return 0

    # 刪之前**再查一次索引**:dry-run 與 apply 之間可能隔了很久,
    # 這期間 bridge 可能已經把某個 blob 認領走了。
    refs_now = referenced()
    deleted = freed = 0
    for path, size in orphans:
        if str(path.relative_to(ROOT)) in refs_now:
            continue                      # 期間被認領了,放過
        try:
            path.unlink()
        except OSError:
            continue
        deleted += 1
        freed += size

    print(f"\n刪了 {deleted} 檔,釋出 {human(freed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
