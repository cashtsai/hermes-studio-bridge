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

## Fixture 規則

Fixture 放在 `tests/fixtures/replay/`，每行一個脫敏 frame。每份 fixture 需要：

- provider 與 scenario 可由檔名或 manifest 唯一判定。
- 有明確 begin/terminal 邊界，除非 scenario 本身測試斷線。
- 不含 bridge token、provider credential、cookie、私人檔案路徑或完整使用者 prompt。
- 能以純本機 replay runner 執行，不啟動 tmux、網路或 provider process。
- 對 card projection、busy 狀態、terminal outcome、重連 cursor 做斷言。

## CI gate

新 provider 進入 `_V2_SESSION_OPS` 時，CI 必須同時檢查：

1. 有 capability manifest。
2. `ops` 與對齊矩陣一致。
3. 至少有 `simple_turn`、`error_terminal`、`approval_pending_and_resolved` 三個 fixture。
4. fixture replay 不產生重複 card，且 terminal 後 busy 為 false。
5. `approval_response` 與 approval persistence 的 `response_capability` 語意一致。

完整六場景是 provider 進入 release channel 前的要求；三場景是開發分支最小門檻，讓新 provider 不會因 fixture 還沒全部錄好而完全無法開發。

## 建議

先把現有 CC/CX/Gemini/OpenClaw fixture 補上 manifest，再錄 Claude Code 與 Hermes 的脫敏真實 transcript。不要先把 `TerminalCardStore` 切成 v3；provider contract 綠、真機 replay 綠、舊 bridge fallback 綠，才有資格翻主路徑 feature flag。
