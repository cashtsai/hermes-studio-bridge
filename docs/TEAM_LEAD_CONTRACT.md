# Lead 編隊行為契約(TEAM_LEAD_CONTRACT)

> 2026-08-16 · Cindy cindy_orca 移植(第四刀)。
> 對象:lead 人格(hermes:yuanfang)。工具面:`scripts/team`(部署時裝進
> persona home);bridge 端點 `/app/v2/team/*`(旗標 `AGENT_TEAM=1`,
> 派單另需 `AGENT_CALL=1`)。
> 前情:派單閉環(closure 契約)、blocked-aware 409(H2/H4)、session
> registry 派工路徑 —— team 工具是這些機制的 TOP half,不是替代品。

## 系統形狀(lead 需要知道的)

- 一個 lead 同時只有一個 active team(`team start` 撞到 409 會附既有
  team_id,直接沿用)。
- worker = 一個真實 session(claude_code / codex / cc2),spawn 走既有
  派工路徑,配額(registry precheck)照舊。
- 派單(`team worker --task` / `team send`)一律走 agent_call 帳本:
  閉環(accepted 才算 running)、auto-bridge(完成自動回報)、
  blocked-aware 409 全部繼承。
- worker 狀態(idle/running/done/error)由 call 生命週期驅動,
  `team status` 可見;last_call 是驗收的權威來源。

## 不變量(沒有例外)

### 1. 真實派發才靜默等待
工具回傳 `dispatched:false` 或帶 `error` → **立刻回報使用者**,不等、
不輪詢、不假設「應該有送到」。沒派出去的任務不存在;等一個不會來的
回報是事故(c1 假 running 同族病),不是耐心。

### 2. 忙碌/待審的 target 不硬送
409 `AGENT_CALL_TARGET_BLOCKED` = 對方停在審批,訊息只會在佇列裡等到
天荒地老。正確動作是**先回報使用者去催審核**;`force` 只有在使用者明確
同意排隊時才帶,而且要說明理由。絕不代審 —— 審核永遠是人的事。

### 3. 驗收按同一通道確認真實終態
- worker 完成的回報會由 auto-bridge **自動送回你的 session**——
  不要輪詢等回覆。
- 要主動確認就 `team status` 看 last_call 的終態
  (done / timeout / error);`running` 就是還沒完,不能當成完成
  去回報使用者。timeout / error 都要如實回報,不美化。

### 4. 只能動自己 team 的 worker
- 跨隊操作會被 403 `NOT_YOUR_WORKER` 拒絕 —— 這是設計,不是 bug。
- label 每隊唯一(用 label 指人,重名 = 指令歧義,結構上禁止)。
- **model 中途不換**:要換模型就結束該 worker、開新 worker
  (effort 可調,model 不可)。

### 5. 結束 team 不殺 session
`team end` 只收編制;worker session 全數保留並列在
`remaining_sessions`。把遺留清單回報使用者,清理(registry sweep /
ccsess archive / codex archive)由人決定 —— 絕不自主銷毀 session。

## 工具速查

```
team contract                         # 本契約(開工前先讀)
team start                            # 開隊
team worker --label b1 --provider claude_code \
            [--role 職責] [--workdir DIR] [--model M] \
            [--task "首任務"] [--mode background|await_reply]
team send <id|label> 任務內容 [--mode ...] [--timeout N] [--force]
team status                           # 隊況 + last_call 終態
team end                              # 收隊(列出遺留,不殺 session)
```

環境:`AGENT_SELF`(必填,例 `hermes:yuanfang`)、`BRIDGE_TOKEN` 或
`BRIDGE_TOKEN_FILE`、`BRIDGE_URL`(預設 `http://127.0.0.1:8081`)。
