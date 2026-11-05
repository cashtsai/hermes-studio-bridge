# T3Code Phase 2 Provider Contract

更新日期：2026-11-05

## 目的

把目前已存在的 `_V2_SESSION_OPS`、`_PROVIDER_CAPS` 與 replay fixture 收斂成一個可由 CI 驗證的 provider contract。App 和 orchestrator 只能讀能力，不得用 provider 名稱猜按鈕或狀態。

## Provider manifest

每個 provider 的 manifest 需要包含：

```json
{
  "provider": "codex",
  "contract_version": 1,
  "ops": ["archive", "history"],
  "capabilities": {
    "interrupt": true,
    "queued_messages": true,
    "streaming": "live",
    "terminal_status_quality": "strong",
    "approvals": true,
    "approval_response": "live",
    "native_id_strength": "strong"
  },
  "replay_scenarios": [
    "simple_turn",
    "stream_resume",
    "interrupt_or_unsupported",
    "approval_pending_and_resolved",
    "queued_message",
    "error_terminal"
  ]
}
```

`ops` 必須由 `_V2_SESSION_OPS` 推導，不接受手寫第二份。能力不足時要明確回傳 unavailable/unsupported，而不是畫出不可用的控制項。

目前 bridge 已有 per-approval 的 `response_capability`（`live`、`not_resumable`、`none`）。這個欄位比 provider 級的靜態布林值更可靠；Phase 2 只會把它納入 capability contract 的明確文件，不會用一個 provider 級旗標取代每筆審批的即時判定。

## Fixture 規則

Fixture 放在 `tests/fixtures/replay/`，每行一個脫敏 frame。每份 fixture 需要：

- provider 與 scenario 可由檔名或 manifest 唯一判定。
- 有明確 begin/terminal 邊界，除非 scenario 本身測試斷線。
- 不含 bridge token、provider credential、cookie、私人檔案路徑或完整使用者 prompt。
- 能以純本機 replay runner 執行，不啟動 tmux、網路或 provider process。
- 對 card projection、busy 狀態、terminal outcome、重連 cursor 做斷言。

目前 Bridge 的 `replay_fixture.py` 已把錄製與 CI 載入收斂到同一個邊界：寫入前會
驗證 frame、拒絕敏感欄位/常見 secret pattern、排序 JSON object key，並以 owner-only
權限寫入；`load_jsonl()` 會在 CI 再驗證一次。digest 使用
`sha256-canonical-jsonl-v1`，保留 frame 順序但不受 object key 順序影響。被拒絕的
frame 只留下錯誤類型，不把內容寫進 event log；raw transcript 仍只允許留在隔離的
本機暫存路徑，不進 repo 或 PR artifact。

除了 provider 六場景外，`manifest.json` 的 `capability_scenarios` 會登記
`visual_replies` 與 `live_updates` 的獨立 fixture。前者驗證 typed block 與舊版
fallback；後者驗證 duplicate、out-of-order 與 terminal 後遲到事件。共用
`LiveUpdateCursor` 只接受同 session 且 seq 較新的事件，終態一旦收尾不再被後續
事件覆寫。

## CI gate

共用 validator 可用以下命令執行 development gate：

```bash
python scripts/validate-replay-fixtures.py
```

升版或 staging promotion 時改用：

```bash
python scripts/validate-replay-fixtures.py --release
```

`--release` 會要求 manifest 裡的每個 provider 都有六個 release scenario，並
重新以同一個 sanitized loader 驗證檔案與 manifest pin 的 SHA-256 digest；development
gate 允許 provider 先以部分 fixture 增量接入。兩種模式都拒絕路徑穿越、不安全
recording policy 與 fixture digest drift。

新 provider 進入 `_V2_SESSION_OPS` 時，CI 必須同時檢查：

1. 有 capability manifest。
2. `ops` 與對齊矩陣一致。
3. 至少有 `simple_turn`、`error_terminal`、`approval_pending_and_resolved` 三個 fixture。
4. fixture replay 不產生重複 card，且 terminal 後 busy 為 false。
5. `approval_response` 與 approval persistence 的 `response_capability` 語意一致。

完整六場景是 provider 進入 release channel 前的要求；三場景是開發分支最小門檻，讓新 provider 不會因 fixture 還沒全部錄好而完全無法開發。

## 建議

先把現有 CC/CX/Gemini/OpenClaw fixture 補上 manifest，再錄 Claude Code 與 Hermes 的脫敏真實 transcript。不要先把 `TerminalCardStore` 切成 v3；provider contract 綠、真機 replay 綠、舊 bridge fallback 綠，才有資格翻主路徑 feature flag。

## Typed visual replies 與 placement preview

`body.visual` 是 additive capability，規格在 `APP_BRIDGE_CONTRACT.md` §1.1；
不符合 envelope 的 provider output 必須退回 `fallback_text`。多機排程先使用
`POST /app/v3/orchestrator/placement/preview` 取得純計畫，該端點永遠回
`applied:false`，不會搬移 session 或傳遞 credential。active/streaming session
維持 host affinity；只有 explicit failover + durable checkpoint 才可進入下一台。

Persona/provider 若要產生 visual reply，可在 `studio-card` fence 使用
`{"kind":"visual","blocks":[...]}`；digest 會投影成普通 markdown card 的
`body.visual`，同時把 fallback 文字留在母卡與 visual card。非法 block 會被丟棄，
不會阻斷串流或破壞 v2 client。

### Live Update 共用事件

Bridge 的 ActivityKit APNs sender 現在在既有 iOS `content-state` 欄位外，附帶
`version/sessionID/seq/state/terminal` 共用事件 envelope。Android Live Updates
可直接沿用同一組狀態與序號規則；非終態更新仍可節流，`completed`/`failed` 不可被
節流吞掉。這一輪只完成 bridge-side event contract，Android client 與背景 quota
仍需另外驗收。
