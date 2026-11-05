# T3Code Formalization Release

更新日期：2026-11-05

這一輪把 T3Code 從「既有 v1 delegation 外掛資料」整理成可逐步切換的
v3 durable contract。舊 `/app/v1`、`/app/v2` 路徑仍是相容層，沒有在本輪
直接拔掉，避免尚未更新的 Pocket、桌面端或其他 agent 被部署順序卡住。

## 已完成

- 舊 delegation 在 bridge 啟動時投影到 `app_threads` / `provider_threads`。
  投影是 deterministic、可重跑、只新增/更新 v3 projection，不修改舊 registry。
- ContextTransfer 狀態機：
  `dry_run -> approved -> target_created -> applied`；失敗或逾期只能進
  `failed` / `expired`，approved 必須有 `approval_id`。
- Checkpoint 仍先建立 read-only manifest；只有明確 approval 才能建立
  `refs/pocket/checkpoints/<id>`，restore 必須 `confirm=true` 且 worktree clean。
- MCP stdio server 提供 typed T3Code tools：delegation、capabilities、
  context transfer、checkpoint；工具只轉送白名單欄位，不接受 credential/token。
- Pocket 新增 v3 snapshot/events shadow client，缺少 v3 route 時退回 v2。
  既有 `TerminalCardStore` 在實機 replay 驗證前維持 v2 真相路徑。
- Live Activity production plist 與出貨檢查補齊；archive/IPA 都必須同時有
  `Pocket Share.appex` 與 `Pocket Widgets.appex`。
- 用量 summary 接受 provider 真實 token sample；Claude local JSONL fallback
  只呈現 token count，不冒充官方 quota percentage，也不把累計值重複寫成每回合。

## Bridge API

| 用途 | Endpoint |
|---|---|
| 建立/讀取 AppThread | `POST/GET /app/v3/threads` |
| 連結 provider thread | `POST /app/v3/threads/{id}/providers` |
| 建立 transfer | `POST /app/v3/threads/{id}/context-transfers` |
| 查詢 transfer | `GET /app/v3/context-transfers/{id}` |
| 推進 transfer | `POST /app/v3/context-transfers/{id}/transition` |
| 建立 checkpoint | `POST /app/v3/threads/{id}/checkpoints` |
| 復原 checkpoint | `POST /app/v3/checkpoints/{id}/restore` |
| snapshot/cursor | `GET /app/v3/sessions/{id}/snapshot`、`events` |
| orchestrator facade | `/app/v3/orchestrator/providers/capabilities`、`delegations` |

## 部署順序

1. 先部署 bridge；startup projection 失敗只記 log，不阻斷既有服務。
2. 觀察 `t3code_legacy_projection`、`T3CODE_BAD_REQUEST` 與 usage write log。
3. 再裝 Pocket TestFlight；v3 shadow read 失敗會自動回 v2，不需回滾 app。
4. 實機驗證 snapshot、cursor replay、轉移 approval、checkpoint restore、
   Live Activity 被背景凍結後的更新。

## 驗證

```sh
python3 -m py_compile bridge.py t3code_runtime.py scripts/agent-call-mcp.py
PYTHONPATH=tests:. python3 -m unittest -v tests/test_t3code_runtime.py
TIMEOUT=300 scripts/run-tests.sh
```

出貨前另需確認：Apple Developer 的 Widgets App ID/profile 已安裝、APNs
production key 可由 launchd env 讀取、TestFlight build 已達 `VALID`。這三項
是外部狀態，不應把私鑰或 token 寫入 repo、文件或聊天。

## 尚未切換的部分

- v2 `TerminalCardStore` 仍為正式卡片流真相；v3 message envelope 先 shadow
  read，等實機 replay 確認不丟卡、不跳 cursor 後再翻 feature flag。
- Gemini/OpenClaw 沒有可靠的 provider token usage 時保持 null，不造數字。
- APNs Live Activity 仍受 iOS budget 與 bridge push 設定影響；app 行程內的
  start/update/end 路徑仍是必要 fallback。
