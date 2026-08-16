#!/usr/bin/env python3
"""部署後煙測 —— 每次 bridge 重啟/部署後跑一次,把「顯示與真相不一致」當場抓出來。

## 為什麼要有這支

2026-08-11~16 之間,CX 這條線一口氣修了七個獨立根因:送出佇列、事件緩衝時機、
echo client_id、時間戳四層、用量條爆表、預覽停在開場白、updatedAt 落後 39 天、
黏住的 stalled 錯誤。**它們全都是同一類**:底層一直正常運作,但呈現給使用者的
數字/文字是錯的。

而它們全都是**使用者在手機上踩到之後才被回報**的 —— 部署當下沒有任何檢查會叫。
這支就是那個檢查:同樣的 bug 再發生一次,部署完當場就知道,不必等人踩。

## 設計原則

**預設唯讀、不花錢**:只讀狀態與既有資料,不送訊息、不觸發模型。所以可以掛在
每次重啟後無腦跑。真正要驗端到端才加 --live(需指定一條待命 session)。

**檢查的是「不變式」不是「快照值」**:例如「用量不得超過容量」「idle 的 session
不該掛著錯誤」—— 這些跟資料內容無關,不會因為使用者聊了什麼而誤報。

用法:
    scripts/post-deploy-smoke.py                    # 唯讀,退出碼 0/1
    scripts/post-deploy-smoke.py --live cx:<thread> # 加送一則健康檢查
    scripts/post-deploy-smoke.py --json             # 給監控/晨報吃
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get("POCKET_BRIDGE_URL", "http://127.0.0.1:8081")
PLIST = os.path.expanduser(
    "~/Library/LaunchAgents/ai.studio.hermes-bridge.plist")


# ── 基礎設施 ──────────────────────────────────────────────────────────────
class Result:
    def __init__(self):
        self.checks: list[dict] = []

    def add(self, name: str, ok: bool, detail: str = "", warn: bool = False,
            note: str = ""):
        """detail = **壞掉時**才講的話(為什麼這條重要 / 實際壞在哪);
        note = 不論成敗都想附的補充(例如量到的數字)。分開才不會出現
        「✓ 卡片按時間遞增 — 順序亂了」這種自相矛盾的輸出。"""
        self.checks.append({"check": name, "ok": ok, "warn": warn,
                            "detail": "" if ok else detail, "note": note})
        return ok

    @property
    def failed(self):
        return [c for c in self.checks if not c["ok"] and not c["warn"]]

    @property
    def warned(self):
        return [c for c in self.checks if not c["ok"] and c["warn"]]


def read_token() -> str:
    if os.environ.get("BRIDGE_TOKEN"):
        return os.environ["BRIDGE_TOKEN"]
    try:
        with open(PLIST, "rb") as f:
            return (plistlib.load(f).get("EnvironmentVariables") or {}
                    ).get("BRIDGE_TOKEN", "")
    except OSError:
        return ""


def get(base: str, path: str, token: str, timeout: float = 20.0):
    req = urllib.request.Request(base + path,
                                 headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def post(base: str, path: str, token: str, body: dict, timeout: float = 90.0):
    req = urllib.request.Request(
        base + path, method="POST", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ── 檢查項 ────────────────────────────────────────────────────────────────
def check_health(base, token, res: Result):
    try:
        h = get(base, "/health", token, timeout=10)
    except Exception as exc:  # noqa: BLE001
        return res.add("bridge 可達", False, f"{type(exc).__name__}: {exc}")
    res.add("bridge 可達", bool(h.get("ok")), "")
    caps = h.get("capabilities") or {}
    res.add("capabilities 有回", bool(caps),
            "缺 capabilities → app 無法依能力顯示功能",
            note=",".join(k for k, v in (caps or {}).items() if v))
    # 進行中的回合數是安全重啟的依據,缺了就等於重啟腳本瞎了
    res.add("turns_in_flight 有回", "turns_in_flight" in h, "")
    return caps


def check_codex_sessions(base, token, res: Result):
    """CX 的顯示不變式 —— 這幾條正是 08-11~16 那七個 bug 的形狀。"""
    try:
        data = get(base, "/codexsessions", token, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return res.add("CX 清單可讀", False, f"{type(exc).__name__}: {exc}")
    sessions = data.get("sessions") or []
    res.add("CX 清單可讀", True, note=f"{len(sessions)} 條")

    bad_usage, stale_ts, stuck_err = [], [], []
    now = time.time()
    for s in sessions:
        name = s.get("name") or (s.get("thread_id") or "")[:8]
        u = s.get("usage") or {}
        used, size = u.get("used"), u.get("size")
        # 不變式 1:上下文用量不得超過容量(舊 bug 實測 200,203%)
        if isinstance(used, int) and isinstance(size, int) and size > 0:
            if used > size:
                bad_usage.append(f"{name} {used}/{size}={used / size * 100:.0f}%")
        # 不變式 2:updatedAt 不該比 lastEventAt 舊(舊 bug 實測差 39 天)
        up, le = s.get("updatedAt"), s.get("lastEventAt")
        if isinstance(up, (int, float)) and isinstance(le, (int, float)):
            if le - up > 3600:
                stale_ts.append(f"{name} 落後 {(le - up) / 3600:.1f}h")
        # 不變式 3:provider 說 idle 卻掛著錯誤 = 黏住的錯誤(舊 bug:卡過一次永久紅)
        if s.get("error") and s.get("providerStatus") == "idle" \
                and not s.get("activeTurn"):
            age = (now - (le or now)) / 3600
            stuck_err.append(f"{name} ({age:.1f}h 前) {str(s['error'])[:40]}")

    res.add("CX 用量條不爆表", not bad_usage, "; ".join(bad_usage))
    res.add("CX updatedAt 不落後", not stale_ts, "; ".join(stale_ts))
    res.add("CX 無黏住的錯誤", not stuck_err, "; ".join(stuck_err), warn=True)
    return sessions


def check_card_stream(base, token, sessions, res: Result):
    """卡片流:回得出來、按時間遞增、小 limit 拿的是最新段。"""
    target = next((s for s in sessions if s.get("thread_id")), None)
    if not target:
        return res.add("卡片流可讀", False, "沒有可測的 session", warn=True)
    tid = target["thread_id"]
    try:
        few = get(base, f"/app/v2/sessions/codex:{tid}/cards?limit=5",
                  token, timeout=30)
        many = get(base, f"/app/v2/sessions/codex:{tid}/cards?limit=60",
                   token, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return res.add("卡片流可讀", False, f"{type(exc).__name__}: {exc}")
    fc, mc = few.get("cards") or [], many.get("cards") or []
    res.add("卡片流可讀", bool(mc), "卡片流回不出東西", note=f"{len(mc)} 張")
    if not fc or not mc:
        return False
    ts = [c.get("ts") or 0 for c in mc]
    res.add("卡片按時間遞增", ts == sorted(ts),
            "順序亂了 → app 端會把舊內容顯示成最新")
    # 小 limit 必須給**最新**那段,不是最舊(否則進場只看得到老訊息)
    res.add("小 limit 給最新段",
            (fc[-1].get("ts") or 0) >= (mc[-1].get("ts") or 0) - 1,
            "limit 小的時候回了最舊那段")
    return True


def check_ring_headroom(base, token, sessions, res: Result):
    """事件環餘裕:latest_seq 逼近上限 → 客戶端會頻繁 410 整包重載。"""
    target = next((s for s in sessions if s.get("thread_id")), None)
    if not target:
        return
    tid = target["thread_id"]
    try:
        snap = get(base, f"/app/v2/sessions/codex:{tid}/cards?limit=1",
                   token, timeout=20)
    except Exception:  # noqa: BLE001
        return
    seq = snap.get("latest_seq")
    ring = int(os.environ.get("POCKET_CARD_RING_MAX", "8000"))
    if isinstance(seq, int) and seq > ring * 0.8:
        res.add("事件環有餘裕", False,
                f"latest_seq={seq} 逼近 ring_max={ring} → 客戶端會頻繁 410",
                warn=True)
    else:
        res.add("事件環有餘裕", True, note=f"latest_seq={seq} / ring_max={ring}")


def check_live_roundtrip(base, token, session_id, res: Result):
    """--live:真的送一則進去,驗端到端。只該用在待命/測試用的 session。"""
    kind, _, tid = session_id.partition(":")
    if kind not in ("cx", "codex") or not tid:
        return res.add("端到端往返", False, "只支援 cx:<thread_id>")
    marker = f"SMOKE-{int(time.time())}"
    try:
        out = post(base, f"/codexsessions/{tid}/input", token,
                   {"text": f"健康檢查:只回我一句「{marker}」,不要做任何其他事。",
                    "client_id": marker})
    except Exception as exc:  # noqa: BLE001
        return res.add("端到端往返", False, f"送出失敗 {type(exc).__name__}: {exc}")
    if not out.get("ok"):
        return res.add("端到端往返", False, f"送出未被接受: {out}")
    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(5)
        try:
            st = get(base, f"/codexsessions/{tid}/status", token).get("session", {})
        except Exception:  # noqa: BLE001
            continue
        if not st.get("activeTurn"):
            break
    try:
        hist = get(base, f"/codexsessions/{tid}/history?limit=1", token)
    except Exception as exc:  # noqa: BLE001
        return res.add("端到端往返", False, f"讀歷史失敗: {exc}")
    got = marker in (hist.get("text") or "")
    res.add("端到端往返", got,
            "送出後回覆裡找不到 marker(可能還在跑或真的沒回)" if not got else "")


# ── 主流程 ────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="bridge 部署後煙測")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--live", metavar="cx:<thread_id>",
                    help="額外做一次端到端往返(會真的跑一個回合,只用待命 session)")
    ap.add_argument("--json", action="store_true", help="輸出 JSON 給監控吃")
    args = ap.parse_args()

    token = read_token()
    res = Result()
    if not token:
        res.add("取得 BRIDGE_TOKEN", False, f"env 與 {PLIST} 都沒有")
    else:
        caps = check_health(args.base, token, res)
        if caps is not False:
            sessions = check_codex_sessions(args.base, token, res)
            if isinstance(sessions, list) and sessions:
                check_card_stream(args.base, token, sessions, res)
                check_ring_headroom(args.base, token, sessions, res)
            if args.live:
                check_live_roundtrip(args.base, token, args.live, res)

    if args.json:
        print(json.dumps({"ok": not res.failed, "checks": res.checks},
                         ensure_ascii=False, indent=2))
    else:
        for c in res.checks:
            mark = "✓" if c["ok"] else ("⚠" if c["warn"] else "✗")
            line = f"  {mark} {c['check']}"
            if c.get("note"):
                line += f" ({c['note']})"
            if c["detail"]:
                line += f" — {c['detail']}"
            print(line)
        print()
        if res.failed:
            print(f"✗ 煙測失敗:{len(res.failed)} 項")
        elif res.warned:
            print(f"⚠ 煙測通過,但有 {len(res.warned)} 項警告")
        else:
            print("✓ 煙測全過")
    return 1 if res.failed else 0


if __name__ == "__main__":
    sys.exit(main())
