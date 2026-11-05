# T3Code Round 2 Implementation

日期：2026-11-05
分支：`feat/t3code-completion-round2`
狀態：可測試的 additive release candidate；尚未直接合併 `main`

## 已落地

### 1. AppThread / ProviderThread

Bridge 在 canonical DB 新增獨立表，不改既有 `/app/v1`、`/app/v2` wire shape：

- `POST /app/v3/threads`
- `GET /app/v3/threads/{thread_id}`
- `POST /app/v3/threads/{thread_id}/providers`

Pocket 對應的 Swift client 在 `StudioBridge`，AppThread 是穩定的 Pocket id，ProviderThread 才保存 Codex、Claude Code、Gemini 等原生 id。原生 session 被替換或 archived 時，AppThread 不會跟著消失。

### 2. ContextTransfer v1 dry-run

`POST /app/v3/threads/{thread_id}/context-transfers` 只建立可審查 envelope，不會建立 target provider session，也不會自動執行 pending approval。摘要、決策、artifact reference、pending 都有限長度；遇到 token、cookie、API key、private key 等欄位直接拒絕。相同 `transfer_id` 與內容重送是冪等，不同內容會回 400。

### 3. Checkpoint dry-run

`POST /app/v3/threads/{thread_id}/checkpoints` 只讀 worktree、HEAD、dirty summary 與指定 artifact hash，不 commit、不建立 git ref、不改工作樹。相同 checkpoint id 與內容重送是冪等；不同內容會拒絕。真正 rollback 仍必須另開 approval/destructive action。

### 4. Durable snapshot / cursor

- `GET /app/v3/sessions/{session_id}/snapshot?limit=100`
- `GET /app/v3/sessions/{session_id}/events?after_seq=N`

snapshot 回 `snapshot_seq`；事件使用既有 durable `event_log` 全域遞增序號。這是給新 client/replay harness 的相容層；目前 Pocket 生產卡片仍沿用已驗證的 v2 snapshot + SSE 路徑，避免在同一輪改動 UI 同步核心。

### 5. Typed orchestrator facade

- `GET /app/v3/orchestrator/providers/capabilities`
- `POST /app/v3/orchestrator/delegations`
- `GET /app/v3/orchestrator/delegations/{id}`
- `POST /app/v3/orchestrator/delegations/{id}/await`

create facade 轉接既有 durable delegation registry；await 最多等待 30 秒，只回傳狀態，不暴露 shell、bridge token 或 provider credential。現有 policy、cwd allowlist、depth、parallel quota 與 approval guard 仍是唯一限制來源。

### 6. Usage / Live Activity

有 provider 真實送出 `tokenUsage` 時才寫入 `usage_samples`，以 dedupe key 防止重播重複計算；`/app/v1/usage/summary` 會合併 token、cache、total 與估算成本，沒有來源時維持 null/未同步，不假造數字。

ActivityKit token 可由 Pocket 註冊到 bridge。Codex turn 開始與既有 turn end push 會更新 Live Activity；APNs 未配置、舊 bridge 或 token 過期都只降級，不阻塞普通訊息流。

## 驗證

```bash
PYTHONPYCACHEPREFIX=/tmp/pocket-pycache python3 -m py_compile bridge.py t3code_runtime.py
python3 -m unittest -v tests/test_t3code_runtime.py
TIMEOUT=300 scripts/run-tests.sh
```

完整 bridge suite 需要允許 UNIX socket/本機服務測試；若在受限 sandbox 執行，應用 elevated test runner 重跑，不要把 sandbox 拒絕誤判成產品失敗。

Pocket iOS compile smoke：

```bash
xcodebuild -project pocket.xcodeproj -scheme PocketIOS \
  -sdk iphonesimulator -configuration Debug build \
  CODE_SIGNING_ALLOWED=NO
```

## 尚未自動化的項目

- AppThread/ContextTransfer 尚未取代既有 v2 卡片流，需 replay fixture 與手機端真機驗收後才切主路徑。
- APNs Live Activity 需要 release build 的正確 bundle entitlement 與 APNs key；本地 compile 不等於鎖屏推播已完成。
- macOS release workflow 的 GitHub Release 發佈是手動 input，預設 false；TestFlight 仍需 Apple/GitHub secrets 與人工審核。
- `PR #258` 與 `PR #268` 應在同一個下一輪 release branch 合併驗證，不要各自單獨出貨。
