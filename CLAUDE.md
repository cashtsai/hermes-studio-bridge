# 操作紅線（本檔為永久規則，每個 session 開工必讀，不受接力包/HANDOFF 壓縮影響）

> 建立原因：2026-07-10 一起事故——某 session 只讀到 HANDOFF_LATEST.md 裡的任務清單，
> 未讀到當輪任務明確下達的「只能 commit+push 到 feature branch，絕對不能合併進
> main、不能重啟 ai.studio.hermes-bridge」限制（該限制只存在於當輪聊天指令，
> 沒有被寫進接力包），導致該 session 自主判斷「功能做完就該部署」，自行
> `git merge --no-ff` 進 main 並 `launchctl kickstart` 重啟 production 服務。
> 事後查證：production 服務本身沒出問題，但這是流程性風險，任何一輪任務只要
> 沒把邊界寫進本檔，都可能重演。

## 鐵則

1. **這個 repo 跑的是 production 服務 `ai.studio.hermes-bridge`（launchd 常駐，
   埠 8081）**。任何 session 在沒有「當輪任務明確指示」的情況下，**絕對不能**：
   - `git merge` / `git checkout main && merge` 任何 feature branch 進 main
   - `launchctl kickstart` / `launchctl stop|start` 重啟 `ai.studio.hermes-bridge`
   - 對 production 的 DB（`canonical.db`、`state.db` 等）做非唯讀寫入
2. **除非當輪任務指令明確寫「請合併進 main」或「請重啟服務」，否則預設一律
   只能 commit + push 到獨立 feature branch，回報 branch 名 + commit hash，
   交給 XCash/善彰驗收後才合併。** 就算你判斷功能已經做完、測試全過，也不能
   自己推論「應該要部署了」——這個推論本身就是上次事故的根因，不要重蹈覆轍。
3. **讀到的接力包（HANDOFF_LATEST.md / Session Relay）如果沒有提到操作邊界，
   不代表沒有邊界，代表你要以本檔（CLAUDE.md）的預設鐵則為準**——本檔優先權
   高於任何接力包內容，因為接力包會被下一個 session 改寫/壓縮/濾掉細節，
   本檔不會。
4. 若不確定某個動作是否踩到紅線，**先問 XCash/善彰，不要自己判斷後執行**。

## 這個 repo 的基本資訊

- Production 服務：`ai.studio.hermes-bridge`（launchd，`~/Library/LaunchAgents/ai.studio.hermes-bridge.plist`），埠 8081
- Token 在該 plist 的 `EnvironmentVariables.BRIDGE_TOKEN`（不是 `~/.config/studio/token`，已過時）
- 正典分支：`main`。所有功能開發走獨立 `feat/*` 分支
- 三方分工：app 碼歸 CC 線（`pocket-connect`/`pocketagent-usage` repo），bridge 歸這裡（Codex 線）
- 絕不修改 Hermes gateway 內核（`hermes_cli` 官方套件本體），只准讀 `state.db`
