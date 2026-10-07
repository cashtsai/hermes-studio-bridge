#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CC_TOKEN_STREAM live probe — 對一個 tmux CC session 高頻擷取 pane,
餵進 bridge.CCPaneStream,印出「逐字到達」時間線。

唯讀:只跑 `tmux capture-pane -p`,不碰 bridge 服務、不碰任何 DB。
用途:在不開 production 旗標的前提下,實測 pane-diff 萃取器對「當前
Claude Code TUI 版面」是否還認得(版面漂移是這條路的頭號風險)。

    python3 scripts/cc-stream-probe.py <tmux-session> \
        [--interval 0.25] [--duration 60] [--json out.json]

輸出每個 tick 一行:
    +12.50s  feed=+23ch   draft=145ch  «…最後 40 字»
    +12.75s  layout=None  (版面認不出 → 跳過)
結尾統計:總 tick / changed tick / layout-miss / 首字延遲 / 最終草稿。
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge  # noqa: E402


def capture(target: str) -> str:
    r = subprocess.run(["tmux", "capture-pane", "-p", "-t", target],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("--interval", type=float, default=0.25)
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    st = bridge.CCPaneStream()
    t0 = time.time()
    ticks = changed_ticks = layout_miss = 0
    first_change = None
    timeline = []
    prev_len = 0

    while time.time() - t0 < args.duration:
        time.sleep(args.interval)
        ticks += 1
        now = time.time() - t0
        pane = capture(args.session)
        content = bridge._cc_stream_content(pane)
        if content is None:
            layout_miss += 1
            print(f"+{now:6.2f}s  layout=None  (認不出版面,跳過)")
            timeline.append({"t": round(now, 2), "layout": None})
            continue
        draft, changed = st.feed(pane)
        if changed:
            changed_ticks += 1
            if first_change is None:
                first_change = now
            delta = len(draft) - prev_len
            prev_len = len(draft)
            tailtxt = draft[-40:].replace("\n", "⏎")
            print(f"+{now:6.2f}s  feed=+{delta}ch  draft={len(draft)}ch  «{tailtxt}»")
            timeline.append({"t": round(now, 2), "delta": delta,
                             "draft_len": len(draft)})
        else:
            timeline.append({"t": round(now, 2), "delta": 0,
                             "draft_len": len(draft)})

    print("\n── 統計 ──")
    print(f"ticks={ticks}  changed={changed_ticks}  layout_miss={layout_miss}")
    print(f"首字延遲={'%.2fs' % first_change if first_change is not None else 'N/A(從未流出)'}")
    print(f"最終草稿({len(st.draft)}ch):\n{st.draft}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"ticks": ticks, "changed": changed_ticks,
                       "layout_miss": layout_miss, "first_change": first_change,
                       "final_draft": st.draft, "timeline": timeline},
                      f, ensure_ascii=False, indent=1)
        print(f"\nJSON → {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
