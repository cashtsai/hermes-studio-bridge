#!/usr/bin/env python3
"""把舊的媒體成品遷到 cashbox,本機只留最近 N 天。**預設 dry-run。**

## 為什麼需要這支(2026-11-20)

`~/.pocket/media-artifacts` 長到 **31.7 GB / 18,823 個檔**,而整顆磁碟只剩
22 GB —— `media_artifacts.py` 從頭到尾**沒有任何保留或清理機制**,是無上限
成長。這不是「可能會出事」,是倒數計時。

## 為什麼是遷移不是刪除

媒體成品是工作產出(CC/CX 做出來的影片、報告、檔案),不該直接砍。先 rsync
到 cashbox 存檔,**驗證成功之後**才刪本機,並把索引標成「已歸檔」。

## 為什麼可以安全地拿掉本機檔

`media_artifacts` 的 schema 本來就有 `available` 旗標與 `unavailable_reason`
欄位,而 `bridge._media_wire_item()` **只在 `available` 為真時才給
`download_url`**(bridge.py:521-527)。所以標成 unavailable 之後:
卡片照樣看得到檔名與大小,只是沒有下載連結 —— 優雅降級,不會出現死連結,
也不會噴錯。`unavailable_reason` 會寫上 cashbox 的實際路徑,要取回找得到。

## 用法

    ./media-archive-to-cashbox.py                 # dry-run,只report
    ./media-archive-to-cashbox.py --apply         # 真的搬
    ./media-archive-to-cashbox.py --days 14       # 改保留天數(預設 7)

## 安全規矩(刻意的)

  · **預設 dry-run**,`--apply` 才動手。
  · **先傳、驗、才刪**。rsync 失敗或遠端大小對不上 → 本機檔**不動**,
    索引也不改。寧可留著佔空間,不可以把唯一一份弄丟。
  · 一次一個檔地「傳→驗→刪→更新索引」,中途中斷不會留下
    「索引說歸檔了但其實沒傳成功」的狀態。
  · 只動 `available=1` 的列。已經歸檔過的不會再處理(冪等)。
"""
import argparse
import os
import shlex
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from process_runner import run as run_command

ROOT = Path(os.environ.get("POCKET_MEDIA_DIR",
                           os.path.expanduser("~/.pocket/media-artifacts")))
INDEX = ROOT / "index.sqlite3"

NAS_USER = os.environ.get("NAS_USER", "cashtsai")
NAS_HOST = os.environ.get("NAS_HOST", "cashbox.local")
#: 跟 ~/bin/backup-to-cashbox 同一個命名空間,不要自己另開一棵樹。
NAS_BASE = os.environ.get(
    "NAS_BASE", "/share/archive/studio/CashCamp") + "/hermes/media-artifacts"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def ssh(*args: str) -> subprocess.CompletedProcess:
    return run_command(["ssh", f"{NAS_USER}@{NAS_HOST}", *args],
                       operation="archive.ssh", timeout=120)


def candidates(conn: sqlite3.Connection, cutoff: float) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT media_id, stored_relpath, filename, byte_size, created_at"
        "  FROM artifacts"
        " WHERE available = 1 AND stored_relpath IS NOT NULL"
        "   AND created_at < ?"
        " ORDER BY created_at", (cutoff,)).fetchall()


def orphan_blobs(min_age_hours: int = 24) -> list[tuple[Path, int]]:
    """沒有任何索引列引用、且夠舊的 blob。

    **年齡護欄不准拿掉**:store 是先寫 blob 後寫索引列,而 bridge 是 24×7
    在跑 —— 剛落盤還沒入列的檔案長得跟孤兒一模一樣,沒護欄就會刪到
    正在產生的成品。
    """
    with sqlite3.connect(f"file:{INDEX}?mode=ro", uri=True) as conn:
        refs = {r[0] for r in conn.execute(
            "SELECT DISTINCT stored_relpath FROM artifacts"
            " WHERE stored_relpath IS NOT NULL")}
    cutoff = time.time() - min_age_hours * 3600
    out: list[tuple[Path, int]] = []
    for path in (ROOT / "blobs").rglob("*"):
        if not path.is_file() or path.name.startswith(".incoming."):
            continue
        if str(path.relative_to(ROOT)) in refs:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_mtime <= cutoff:
            out.append((path, st.st_size))
    return out


def archive_orphans(args) -> int:
    """孤兒 blob:**先整批傳到 cashbox、對帳、才刪本機。**

    ## 孤兒是什麼(查證過的版本)

    我一開始推測是「upsert 漏更新 `stored_relpath` → 新 blob 變孤兒、索引
    指著舊版」。**查了之後推翻** —— `capture_path` 的 ON CONFLICT 本來就有
    更新 relpath/sha256/byte_size。真相相反:

      列**正確地**換指新 blob,**被取代的那顆舊 blob 從此沒人引用**,
      而 `media_artifacts` 完全沒有 GC。

    所以孤兒 = 使用者檔案的**歷史版本**,不是最新版。

    ## 為什麼還是先搬不直接刪(2026-11-20 機主拍板)

    歷史版本仍是工作產出(CC/CX 做出來的影片、報告、檔案),而 blob 檔名只是
    內容雜湊、沒有任何地方記錄它原本屬於哪個 `(session_id, source_ref)`,
    刪掉就再也認不回來。cashbox 上留一份,日後真要翻舊版找得到。
    """
    items = orphan_blobs()
    total = sum(s for _, s in items)
    dest = f"{NAS_BASE}/orphans/{time.strftime('%Y-%m-%d')}"
    print(f"孤兒 {len(items)} 檔,{human(total)}")
    print(f"目的地 {NAS_USER}@{NAS_HOST}:{dest}/")
    if not args.apply:
        print("\n(dry-run。加 --apply 才會真的搬)")
        return 0
    if not items:
        return 0
    if ssh("true").returncode != 0:
        print(f"連不上 {NAS_HOST} —— 不動任何東西", file=sys.stderr)
        return 1

    # 整批一次 rsync(`--files-from`)。一個檔開一條 ssh 的話,4,600 個檔
    # 光是建連線就跑到天荒地老。
    # 清單寫到暫存目錄,**不要寫進 ROOT** —— 那裡是媒體庫,放雜物進去會讓
    # 別的掃描器(孤兒盤點、容量統計)把它算進來,中途被砍還會留下殘渣。
    import tempfile
    listing = Path(tempfile.gettempdir()) / f"pocket-orphan-archive.{os.getpid()}"
    listing.write_text(
        "\n".join(str(p.relative_to(ROOT)) for p, _ in items) + "\n",
        encoding="utf-8")
    try:
        if ssh(f"mkdir -p {shlex.quote(dest)}").returncode != 0:
            return 1
        send = run_command(
            ["rsync", "-a", "--partial", "--files-from", str(listing),
             str(ROOT), f"{NAS_USER}@{NAS_HOST}:{dest}/"],
            operation="archive.orphans.copy", timeout=7200)
    finally:
        listing.unlink(missing_ok=True)
    if send.returncode != 0:
        print(f"rsync 失敗,本機一個檔都沒動:\n{send.stderr[-500:]}",
              file=sys.stderr)
        return 1

    # **對帳了才刪 —— 逐檔對,不是整個目錄統計。** `dest` 是共用的
    # `orphans/<date>/`,同一天多跑幾次會把累積的檔都算進去,拿整目錄的
    # 檔數/總量跟「這次的 items」比一定對不上(2026-11-20 首跑實測:遠端
    # 目錄 4,783 檔 vs 這次 45 檔)。正解是只驗這次送的每一個檔的大小。
    #
    # 一次把本次要對帳的相對路徑丟上去、遠端逐檔 stat 回傳 "rel size"。
    rels = [str(p.relative_to(ROOT)) for p, _ in items]
    want = {str(p.relative_to(ROOT)): size for p, size in items}
    import shlex as _shlex
    remote_cmd = (f"cd {_shlex.quote(dest)} && while IFS= read -r f; do "
                  f"s=$(stat -c %s \"$f\" 2>/dev/null || echo MISSING); "
                  f"printf '%s\\t%s\\n' \"$f\" \"$s\"; done")
    probe = run_command(
        ["ssh", f"{NAS_USER}@{NAS_HOST}", remote_cmd],
        input="\n".join(rels) + "\n", operation="archive.orphans.verify", timeout=600)
    if probe.returncode != 0:
        print("Remote verification failed; all local files retained", file=sys.stderr)
        return 1
    remote_size = {}
    for line in probe.stdout.splitlines():
        rel, _, s = line.rpartition("\t")
        if rel and s.isdigit():
            remote_size[rel] = int(s)

    with sqlite3.connect(f"file:{INDEX}?mode=ro", uri=True) as conn:
        refs_now = {r[0] for r in conn.execute(
            "SELECT DISTINCT stored_relpath FROM artifacts"
            " WHERE stored_relpath IS NOT NULL")}
    deleted = freed = kept = 0
    for path, size in items:
        rel = str(path.relative_to(ROOT))
        if rel in refs_now:
            kept += 1                      # 傳輸期間被索引認領了 → 放過
            continue
        if remote_size.get(rel) != size:   # 這一個檔沒對上 → 不刪它
            kept += 1
            continue
        try:
            path.unlink()
        except OSError:
            continue
        deleted += 1
        freed += size
    if kept:
        print(f"（{kept} 個未通過逐檔對帳或已被重新認領,保留不刪）")
    print(f"本機刪除 {deleted} 檔,釋出 {human(freed)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7, help="本機保留天數(預設 7)")
    ap.add_argument("--apply", action="store_true", help="真的搬(預設只 report)")
    ap.add_argument("--limit", type=int, default=0, help="最多處理幾個(0=不限)")
    ap.add_argument("--orphans", action="store_true",
                    help="改搬『索引完全沒有引用』的孤兒 blob(見 archive_orphans)")
    args = ap.parse_args()

    if args.orphans:
        return archive_orphans(args)

    if not INDEX.exists():
        print(f"找不到索引:{INDEX}", file=sys.stderr)
        return 1

    cutoff = time.time() - args.days * 86400
    with sqlite3.connect(INDEX) as conn:
        rows = candidates(conn, cutoff)

    if args.limit:
        rows = rows[:args.limit]

    # **依 blob 去重。** blob 是內容定址,多個索引列會共用同一個
    # stored_relpath(光是空內容的 e3b0c442… 就有上千列)。原本逐列跑:第一列
    # 刪了檔,後面指同一 blob 的列 unlink 失敗算「略過」,而且那些列的
    # available 沒改 → 指向已刪的檔 = 新的死連結。2026-11-20 一次跑就製造了
    # 12,553 筆。改成:一個 blob 搬一次、刪一次,收齊所有指它的 media_id,
    # 成功後一起把它們的 available 設 0。
    #
    # 一次 stat 把「存在 + 大小」拿定(bridge 24×7 在跑,讀索引與讀磁碟之間
    # 有 TOCTOU 窗口,OSError 就當它不在、跳過)。
    blobs: dict = {}        # rel -> {"path", "size", "ids": [media_id,...]}
    for r in rows:
        rel = r["stored_relpath"]
        ent = blobs.get(rel)
        if ent is None:
            p = ROOT / rel
            try:
                size = p.stat().st_size
            except OSError:
                continue
            ent = blobs[rel] = {"path": p, "size": size, "ids": []}
        ent["ids"].append(r["media_id"])
    present = [(rel, e["path"], e["size"], e["ids"]) for rel, e in blobs.items()]
    total = sum(e["size"] for e in blobs.values())

    print(f"保留 {args.days} 天 → 可歸檔 {len(present)} 檔,{human(total)}")
    print(f"目的地 {NAS_USER}@{NAS_HOST}:{NAS_BASE}/")
    if not args.apply:
        for rel, p, size, ids in present[:5]:
            print(f"  例:{rel[-16:]:<18} {human(size):>10}  {len(ids)} 列指向")
        print("\n(dry-run。加 --apply 才會真的搬)")
        return 0

    if ssh("true").returncode != 0:
        print(f"連不上 {NAS_HOST} —— 不動任何東西", file=sys.stderr)
        return 1

    # **一次批次傳,不要逐檔開連線。** 原本每個檔開一條 rsync+ssh —— 2026-11-20
    # 實測 21,598 個檔只搬了 1,105 個就花 43 分鐘(全是 SSH 握手),照這速度要
    # 十幾小時。孤兒段早就用 --files-from 批次,7 天段漏抄。rsync 會自己依
    # 相對路徑在遠端建目錄(-R / --files-from 的相對結構),不必逐檔 mkdir。
    import tempfile
    listing = Path(tempfile.gettempdir()) / f"pocket-retention-archive.{os.getpid()}"
    listing.write_text(
        "\n".join(rel for rel, _, _, _ in present) + "\n",
        encoding="utf-8")
    try:
        send = run_command(
            ["rsync", "-a", "--partial", "--files-from", str(listing),
             str(ROOT), f"{NAS_USER}@{NAS_HOST}:{NAS_BASE}/"],
            operation="archive.retention.copy", timeout=14400)
    finally:
        listing.unlink(missing_ok=True)
    if send.returncode != 0:
        print(f"rsync 失敗,本機一個檔都沒動:\n{send.stderr[-500:]}", file=sys.stderr)
        return 1

    # 遠端對帳:一次 ssh 把清單丟上去,批次 stat 回傳每檔大小。
    # **cashbox 是 QNAP / BusyBox**(2026-11-20 實測):`xargs -d`、
    # `stat --printf` 都不支援,但 `stat -c '%n %s'` 支援且一次吃多個檔。
    # blob 路徑是 `blobs/xx/<hex>`,沒有空白,所以空白分隔解析安全。
    # xargs 預設會把 stdin 依空白/換行切成參數、自動分批,BusyBox 也通。
    rels = [rel for rel, _, _, _ in present]
    remote_cmd = (f"cd {shlex.quote(NAS_BASE)} && "
                  f"xargs stat -c '%n %s' 2>/dev/null")
    probe = run_command(
        ["ssh", f"{NAS_USER}@{NAS_HOST}", remote_cmd],
        input="\n".join(rels) + "\n", operation="archive.retention.verify", timeout=1200)
    if probe.returncode != 0:
        print("Remote verification failed; all local files retained", file=sys.stderr)
        return 1
    got = {}
    for line in probe.stdout.splitlines():
        rel, _, s = line.rpartition(" ")
        if rel and s.isdigit():
            got[rel] = int(s)

    # blob 對上就刪一次,並把**所有**指向它的列一起標 unavailable ——
    # 一個 blob 多列共用,漏改任何一列就是一條指向已刪檔的死連結。
    moved = freed = skipped = 0
    now = time.time()
    with sqlite3.connect(INDEX) as conn:
        for rel, path, size, ids in present:
            if got.get(rel) != size:        # 沒對上 → 不刪、不改索引
                skipped += 1
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass                        # 已經不在 → 仍要把索引列標掉
            except OSError:
                skipped += 1
                continue
            conn.executemany(
                "UPDATE artifacts SET available = 0, unavailable_reason = ?,"
                "       updated_at = ? WHERE media_id = ?",
                [(f"archived:cashbox:{NAS_BASE}/{rel}", now, mid) for mid in ids])
            moved += 1
            freed += size

    print(f"\n完成:搬了 {moved} 個 blob,本機釋出 {human(freed)};未對上 {skipped}")
    if skipped:
        print("沒對上的**本機都還在**,索引也沒改 —— 再跑一次即可續傳。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
