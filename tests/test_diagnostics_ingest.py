"""feat/diagnostics-ingest 驗收(repo 慣例:python3 tests/test_diagnostics_ingest.py)。

驗證 POST /app/v1/diagnostics:
1. 無 auth → 401;壞 token → 401。
2. 合法 metrickit_diagnostic → 200 + DIAG_DIR 落地一檔,內容含 payload 原文
   與截斷後的中繼資料(server_ts/kind/app_version/build/device/os)。
3. user_report(note + summary)→ 200 落地;note 超長被截到 4000。
4. body 超過 _DIAG_MAX_BYTES → 413;壞 json → 400;kind 不在白名單 → 400。
5. 輪替:壓低 _DIAG_MAX_FILES 後連打,目錄檔數不超過上限(刪最舊)。
"""
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="diag-canon-")
_DIAG = os.path.join(_TMP, "diagnostics")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
os.environ["POCKET_DIAG_DIR"] = _DIAG
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402  (import 即啟動路徑)

from fastapi.testclient import TestClient  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


client = TestClient(bridge.app)
AUTH = {"Authorization": "Bearer " + os.environ["BRIDGE_TOKEN"]}
URL = "/app/v1/diagnostics"


def diag_files():
    if not os.path.isdir(_DIAG):
        return []
    return sorted(f for f in os.listdir(_DIAG) if f.endswith(".json"))


# 1. auth ─────────────────────────────────────────────────────────────────
r = client.post(URL, json={"kind": "user_report", "note": "x"})
check("no auth → 401", r.status_code == 401)
r = client.post(URL, json={"kind": "user_report", "note": "x"},
                headers={"Authorization": "Bearer wrong"})
check("bad token → 401", r.status_code == 401)
check("auth 失敗不落檔", len(diag_files()) == 0)

# 2. metrickit_diagnostic 落地 ─────────────────────────────────────────────
payload = {"crashDiagnostics": [{"callStackTree": {"frames": ["a", "b"]}}]}
r = client.post(URL, headers=AUTH, json={
    "kind": "metrickit_diagnostic",
    "app_version": "1.4" + "x" * 60,       # 超長 → 截 32
    "build": "58",
    "device": "iPhone16,1",
    "os": "iOS 26.0",
    "payload": payload,
})
check("metrickit_diagnostic → 200", r.status_code == 200)
check("回應帶 stored 檔名", r.status_code == 200 and r.json().get("stored", "").endswith(".json"))
files = diag_files()
check("落地一檔", len(files) == 1)
if files:
    entry = json.load(open(os.path.join(_DIAG, files[0]), encoding="utf-8"))
    check("payload 原文保留", entry.get("payload") == payload)
    check("kind 正確", entry.get("kind") == "metrickit_diagnostic")
    check("app_version 截斷到 32", len(entry.get("app_version", "")) == 32)
    check("server_ts 存在", bool(entry.get("server_ts")))
    check("device/os 保留", entry.get("device") == "iPhone16,1" and entry.get("os") == "iOS 26.0")

# 3. user_report ──────────────────────────────────────────────────────────
r = client.post(URL, headers=AUTH, json={
    "kind": "user_report",
    "note": "按鈕沒反應" + "很" * 5000,     # 超長 → 截 4000
    "summary": {"errorlog": ["e1", "e2"], "metrickit": "1 crash"},
    "app_version": "1.4", "build": "58",
})
check("user_report → 200", r.status_code == 200)
files = diag_files()
check("第二檔落地", len(files) == 2)
newest = json.load(open(os.path.join(_DIAG, sorted(files)[-1]), encoding="utf-8"))
by_kind = {}
for f in files:
    e = json.load(open(os.path.join(_DIAG, f), encoding="utf-8"))
    by_kind[e["kind"]] = e
ur = by_kind.get("user_report", {})
check("note 截 4000", len(ur.get("note", "")) == 4000)
check("summary 原樣保留", ur.get("summary") == {"errorlog": ["e1", "e2"], "metrickit": "1 crash"})

# 4. 邊界 ─────────────────────────────────────────────────────────────────
big = b'{"kind": "user_report", "note": "' + b"A" * (bridge._DIAG_MAX_BYTES + 10) + b'"}'
r = client.post(URL, headers={**AUTH, "Content-Type": "application/json"}, content=big)
check("超過大小上限 → 413", r.status_code == 413)
r = client.post(URL, headers={**AUTH, "Content-Type": "application/json"}, content=b"{not json")
check("壞 json → 400", r.status_code == 400)
r = client.post(URL, headers={**AUTH, "Content-Type": "application/json"}, content=b'["list"]')
check("json 非 dict → 400", r.status_code == 400)
r = client.post(URL, headers=AUTH, json={"kind": "evil_kind"})
check("kind 白名單外 → 400", r.status_code == 400)
check("失敗請求不落檔", len(diag_files()) == 2)

# 5. 輪替 ─────────────────────────────────────────────────────────────────
bridge._DIAG_MAX_FILES = 5
for i in range(8):
    r = client.post(URL, headers=AUTH, json={"kind": "metrickit_metric",
                                             "payload": {"i": i}})
    check(f"輪替寫入 {i} → 200", r.status_code == 200)
check("目錄檔數 ≤ 上限(5)", len(diag_files()) <= 5)
kept = [json.load(open(os.path.join(_DIAG, f), encoding="utf-8")) for f in diag_files()]
kept_idx = sorted(e["payload"]["i"] for e in kept if e["kind"] == "metrickit_metric")
check("留下的是最新幾筆", kept_idx == kept_idx and max(kept_idx) == 7)

print()
if fails:
    print(f"✗ {len(fails)} failure(s): " + ", ".join(fails))
    sys.exit(1)
print("✓ all checks passed")
