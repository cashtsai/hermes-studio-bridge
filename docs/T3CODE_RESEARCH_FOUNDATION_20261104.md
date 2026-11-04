# T3Code Research Foundation

日期：2026-11-04  
狀態：research lane，尚未接 production 端點

這份研究把 T3Code 剩餘項目拆成可驗收的契約，不直接把研究概念塞進現有 session schema。現有 delegation API、event log、provider capability 與 replay fixture 是基礎；新模型必須能在其上增量落地。

## 1. AppThread 與 ProviderThread 分層

### 問題

目前 `delegations` 已有 `provider_session_id`、`codex_thread_id`、`cc_session_name`，但 Pocket 的 app 對話、provider 原生 thread、一次委派工作仍容易被當成同一個 id。provider 重啟、CC session 被 archive 或 Codex thread 換代時，app 不應整條對話消失。

### 第一版模型

```text
AppThread
  id: app_<opaque-id>       # Pocket 穩定 id，永不等於 provider id
  title, owner, status
  created_at, updated_at

ProviderThread
  id: provider_<opaque-id>  # bridge 內部 opaque id
  app_thread_id
  provider: codex|claude_code|gemini|hermes|openclaw
  native_id                 # 僅存 provider 原生識別，不拿來當 app id
  status, last_seen_at
  capabilities
```

同一個 AppThread 可以先連 Codex，再透過 ContextTransfer 接續到 Claude Code；舊 ProviderThread 改成 `detached`，歷史仍掛在 AppThread 底下。現有 delegation row 可作 migration source，但不立即改表。

## 2. ContextTransfer v1

ContextTransfer 是「轉交上下文」而不是「偽造另一個 provider 的 session」。第一版只允許摘要、決策、artifact reference 與明確選取的訊息，禁止把 token、cookie、完整 private prompt 或未授權檔案內容默默搬過去。

```json
{
  "version": 1,
  "transfer_id": "ctx_<opaque-id>",
  "source": {"app_thread_id": "app_…", "provider": "codex", "provider_thread_id": "provider_…"},
  "target": {"provider": "claude_code", "provider_thread_id": null},
  "summary": "人類可讀、可審查的目前工作摘要",
  "decisions": [{"key": "…", "value": "…", "source": "user|agent"}],
  "artifacts": [{"media_id": "med_…", "role": "input|output", "sha256": "…"}],
  "pending": [],
  "created_at": 0,
  "expires_at": 0,
  "integrity": {"algorithm": "sha256", "digest": "…"}
}
```

不變式：同一 `transfer_id` 冪等；target 建立失敗不影響 source；pending approval 不自動跨 provider 執行，必須標成 `not_resumable` 或要求重新確認；任何不可攜資料必須留下原因而不是靜默省略。

## 3. Checkpoints / git refs

Checkpoint 先做「可讀、可比較」而非自動回滾：

```text
refs/pocket/checkpoints/<app-thread-id>/<monotonic-seq>
```

每個 checkpoint 應記錄 provider thread、worktree、HEAD、dirty summary、artifact manifest hash。建立 checkpoint 不能偷偷 commit 使用者分支，也不能在未授權時修改工作樹。回滾是獨立的 destructive action，必須經 approval hub。

## 4. Orchestrator MCP

現有 `scripts/agent-call-mcp.py` 與 `/app/v2/agent_call` 可作 MCP 傳輸層，但正式編排仍應走 `/app/v1/delegations`，因為它已有 work order、parent ownership、provider takeover 與 durable registry。

第一階段只暴露四個高階工具：

```text
create_delegation(parent, provider, objective, cwd)
get_delegation(work_order)
send_delegation_input(work_order, message)
await_delegation(work_order, timeout)
```

工具不得直接暴露任意 shell、BRIDGE_TOKEN、provider credential 或未限制的跨人格讀取。深度、並行數、cwd allowlist、approval policy 沿用現有 bridge guard，MCP 只是 typed facade。

## 5. 開發順序與驗收

1. **契約與純函式**：加入 schema/validator、idempotency 與 secret-redaction tests。
2. **唯讀投影**：從現有 delegation rows 投影 AppThread/ProviderThread，不改既有 app wire shape。
3. **ContextTransfer dry-run**：只產生 transfer envelope 與差異報告，不建立 target session。
4. **Checkpoint dry-run**：只讀 git/worktree 狀態，輸出 manifest，不更新 refs。
5. **MCP facade**：接既有 delegation API，先只允許 create/get，後續才開 input/await。

完成前三階段前，不碰 A4 approval cleanup，也不把研究模型放進 production 的 `/app/v2/sessions` 主回應。每階段都要有 replay fixture，才能證明重啟、provider 換代與重連不會讓 AppThread 消失。
