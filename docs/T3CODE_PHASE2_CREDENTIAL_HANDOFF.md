# T3Code Credential Handoff Contract

更新日期：2026-11-05

## 結論

T3Code 的 context transfer **不是** credential transfer。現行
`create_context_transfer` 只允許摘要、決策、artifacts、pending 等可脫敏內容，
遇到 `token`、`api_key`、`cookie`、`password`、`private_key` 或相近欄位會拒絕。
這個邊界要維持：credential 不得進 event log、SQLite transfer envelope、fixture、
guest link、URL 或一般 bridge log。

因此目前可以正式化的是「交接請求的安全契約」，不是把秘密塞進既有 transfer。

## 建議的 handoff 形狀

未來若要跨主機接手 provider，另開獨立的 credential handoff endpoint，請求只帶
metadata，不帶明文 secret：

```json
{
  "version": 1,
  "handoff_id": "ho_…",
  "source_host_id": "cashcamp",
  "target_host_id": "siege",
  "provider": "codex",
  "audience": "provider-session:…",
  "expires_at": 0,
  "nonce": "…"
}
```

實際 secret 只能由來源主機的 Keychain/secret provider 讀出，透過已驗證的短效
通道交給指定 target；target 收到後立即寫入自己的 Keychain，回傳只包含
`accepted`、`credential_ref` 與狀態，不回傳 secret。

## 必要安全規則

1. `handoff_id`、`nonce`、`source_host_id`、`target_host_id`、provider 與 audience
   必須完全綁定；target 不符直接拒絕。
2. 有效期建議 5 分鐘，nonce 只能消費一次；成功、拒絕、過期都不可重放。
3. audit 只記 `handoff_id`、provider、target、結果與時間，不記 token、JWT、URL
   query、credential value 或完整 request body。
4. handoff 不可透過 guest link、分享 URL、附件、context transfer 或 replay fixture
   傳遞；fixture 僅測 metadata 與拒絕條件。
5. target 寫入 Keychain 失敗時，來源不能把 secret 當 fallback 回傳給 app；只回報
   可行動錯誤，並讓使用者重新發起一次新的 handoff。
6. revoke 要能以 `handoff_id` 或 credential reference 執行，不依賴刪整個 session。

## 落地順序

1. 先把上述 envelope 與拒絕規則做成純驗證器和 fixture，不接真 Keychain。
2. 再在 bridge 建立一次性 server-side state，保留 hash，不保存 secret。
3. 最後接 macOS Keychain / iOS Keychain，加入 target binding、過期、重放與 audit
   redaction 的實機驗收。

在這三步完成前，產品 UI 應該只提供「在目標主機重新登入/貼上 API key」的流程，
不要顯示「已自動轉移憑證」之類的文案。
