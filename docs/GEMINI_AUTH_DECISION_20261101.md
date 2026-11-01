# Gemini 認證路線決策(2026-11-01)

> 背景:機主要「Gemini 跟 CC/CX 訂閱仔一樣,Google 登入就有免費額度」。
> 2026-11-01 實測發現 gemini CLI 的個人 OAuth 被 Google 關閉,觸發這份研究。
> 全部事實附來源,**未驗證點明確標註**。

## 0. 觸發事件(實測)

機主 Google 登入後,bridge 握手回:
> This client is no longer supported for Gemini Code Assist for individuals.
> To continue using Gemini, please migrate to the Antigravity suite.

查證:Google 2026-05-19 宣布整併到 **Antigravity** 品牌,**2026-06-18** 停掉
gemini CLI 對**所有個人帳號**(免費層 + AI Pro + AI Ultra)的服務。
企業授權與 **API key 不受影響**。

## 1. 四個方案

### A. 換底層到 Antigravity ACP server ⭐ 主線建議

Google 把 gemini CLI 的個人功能**搬到了 Antigravity**(`agy`)。關鍵:它的
ACP server 握手形狀**與 `gemini --acp` 幾乎一樣**:

| | gemini --acp(死) | Antigravity ACP server |
|---|---|---|
| 握手 | initialize→authenticate→session/new→session/prompt | **完全相同** |
| 個人 Google 登入 | ❌ 被拒 | ✅ **可用,含 AI Pro/Ultra 訂閱** |
| 登入持久性 | 差 | ✅ 跨 session 持久 |
| bidirectional(mid-turn 權限) | ✅ | ✅(我們的核准卡相容) |
| authMethods | oauth-personal / api-key / vertex / gateway | personal Google / enterprise / **API key** / EAP |

**對我們的意義**:`gemini_provider.py` 的 ACP client 邏輯(on_update 分流、
核准、排隊、逐字稿)**大部分可重用** —— 換的是 spawn 的 binary、authMethods
id、headless flag。這幾乎是 drop-in。

**這條直接實現機主的願景**:個人 Google 登入 = 免費額度 + 訂閱額度,跟 CC/CX
的訂閱仔同一個模式。

#### ✅ 2026-11-01 SG 龍蝦(207.148.69.149,agy 1.4.12)實測 —— 四點全釘

1. **安裝**:官方 installer → `~/.local/bin/agy`(服務 PATH 常沒這段,
   provider 的 `agy_bin()` 明確摸這個位置)。
2. **wire 形狀:不是 ACP,是 stream-json**(上表「握手完全相同」來自外部
   文章,實測推翻 —— agy 1.4.12 沒有 ACP server):
   - spawn:`agy --input-format stream-json --output-format stream-json
     --dangerously-skip-permissions [--conversation <id>]`
   - stdin:`{"event":"user","message":{"role":"user","content":[{"type":
     "text","text":"…"}]}}`(Claude 式 `{"type":"user"}` 被拒)
   - stdout:`init`(conversation_id、tools)→ `step_update`* →
     `result`(status/response/usage)。**沒有逐字流**,全文在 result。
   - 同行程連發 context 延續(1+2=3 → 再加10=13);跨行程用
     `--conversation`,對話落 `~/.gemini/antigravity-cli/` 跨重啟持久。
3. **無頭登入**:bare `agy` TUI → OAuth URL + 貼回授權碼。⚠️ URL 會被
   TUI 框折行咬壞(機主實踩 400 invalid_request)—— 程式整合時要從
   輸出撈完整 URL,不能叫人從終端機複製。登入跨 session 持久。
4. **個人帳號被服務**:✅ 兩回合真跑 SUCCESS。**而且 models 不只 Gemini**:
   gemini-3.8-flash(high/med/low)、claude-sonnet-4-6、
   claude-opus-4-6-thinking、gpt-oss-120b —— 一個 Google 登入全有。
   註:trivial prompt 的 usage.input_tokens ≈ 6.7k(agent harness 前導),
   額度估算要算這筆。

### B. gemini CLI + 付費 API key(現狀能動)

- 不受 Google 政策影響,**今天就能用**(程式碼已支援,auto 模式會退到這)
- 但這**不是訂閱**,是按量計費:
  - Gemini 3.8 Flash:$0.75/$3.75 每百萬 token(2026 底前優惠,之後 $1.50/$7.50)
  - Flash-Lite:$0.10/$0.40
  - 免費層:Flash/Flash-Lite 每天 **20 次**(機主今天就爆了)—— 免費層不實用
- 定位尷尬:要嘛使用者自備 key(「哪有那麼多人有」),要嘛我們代付(燒錢)

### C. OpenRouter(機主本來就要的 fallback)

- **獨立於 Google 政策風險** —— 這是它最大的價值
- BYO OpenRouter key:前 **100 萬請求/月免費額度**,之後 5% 加成
- Gemini 3 Flash 經 OpenRouter:$0.50/$3;Flash-Lite:$0.25/$1.50
- 沒有免費 Gemini 模型(2026-09 查證),但有 18 個**其他**免費模型(20 req/min)
- OpenClaw 原生支援 → 綁 OpenRouter 最省事

### D. 維持現狀(不可接受)

auto 模式退到付費 API key,但那把 key 免費額度爆了 → Gemini 實質不能用。
不做 A/B/C 就是這個。

## 2. 建議:A 主線 + C 退路,B 淘汰

```
主要:Antigravity ACP(個人 Google 登入 = 免費/訂閱)—— 救願景
退路:OpenRouter(BYO key,獨立於 Google 風險)—— 機主本來就要的
淘汰:純 API key 當主路(免費層 20/天不實用、代付燒錢)
```

**OpenClaw 綁定**:主綁 Antigravity Gemini(訂閱額度),fallback OpenRouter ——
正是機主原話的結構,只是「訂閱 Gemini」的底層從死掉的 gemini CLI overlay
換成 Antigravity。

**為什麼不是 B 當主線**:機主的核心訴求是「哪有那麼多人有 API key」。
A 正面解決它,B 繞過它。B 只保留給「已經有付費 key 且不想登入」的少數。

## 3. 下一步(機主已拍板走 A,2026-11-01「直接開做」)

1. ~~龍蝦實測四個未驗證點~~ ✅ 全釘(見上)
2. ~~provider 加 Antigravity 後端~~ ✅ **已實作**:`AntigravityClient`
   (stream-json 引擎,與 `GeminiClient` 完全同門面,`make_client()` 依
   `POCKET_GEMINI_BACKEND=antigravity` 選;bridge 只改一行接工廠)。
   測試:tests/test_antigravity_client.py(wire 契約)+
   test_gemini_auth_plan.py 的門面同形測試。
   已知限制(誠實記):無逐字流(全文一次到)、附件未支援(明說略過)、
   模型旗標未實測(設了模型會 log `antigravity_model_ignored`)、
   `--dangerously-skip-permissions`(stream-json 單向,無 mid-turn 權限
   往返;cwd 釘在工作區)。
3. app 的 Gemini 登入頁:「用 Google 登入」改指向 agy 的流程(待做;
   URL 要由 bridge 撈完整字串回 app,見上面折行坑)
4. 規格 `GEMINI_SUBSCRIPTION_AUTH_20261101.md` 的 gemini CLI 前提作廢,
   指向本文件 ✅(該文件已加頂註)
5. 龍蝦(B 軌)啟用:bridge 服務 env 設 `POCKET_GEMINI_BACKEND=antigravity`
   + 機上 agy 已登入 → 端到端驗一輪(管線存在≠端到端通)

## 4. 不變的教訓

這次又踩到「管線存在≠端到端通」:昨天改規格只驗「CLI 列出 oauth-personal」,
沒驗「Google 握手會不會接受」。走 A 時,ACP 握手 + 認證**必須在真龍蝦上
端到端跑通一次**才改實作,不靠官方文件的「支援」字樣。

## 來源

- [9to5google: Gemini CLI/Code Assist 對消費者關閉](https://9to5google.com/2026/06/17/gemini-cli-code-assist-shutting-down/)
- [gemini-cli issue #28229: AI Pro OAuth 登入失敗](https://github.com/google-gemini/gemini-cli/issues/28229)
- [Antigravity 外部整合(ACP/stream-json/認證)](https://2bab.me/en/blog/2026-10-18-antigravity-external-integration/)
- [agy-agent-acp: Antigravity 的 ACP adapter](https://github.com/dongitran/agy-agent-acp)
- [Gemini API 定價(2026-10)](https://www.morphllm.com/gemini-api-pricing)
- [OpenRouter Google 模型定價](https://openrouter.ai/google)
