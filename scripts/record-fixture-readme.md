# Replay fixture 真錄製(`POCKET_RECORD_FIXTURE`)

replay fixture 的制度見 `tests/fixtures/replay/README.md` 與
`tests/test_replay_fixtures.py` 檔頭:fixture = `{method, params}` 幀序列,
重放進真 digest 管線,「fixture 全綠 = 端到端通」。本檔講**怎麼錄真的**。

錄製器有兩種模式,由同一個 env `POCKET_RECORD_FIXTURE` 的指向決定:

| 指向 | 模式 | 鉤的位置 | 產出 |
| --- | --- | --- | --- |
| **檔案路徑**(可不存在) | wire 模式(舊,T3Code #3) | Codex app-server `_dispatch_wire_message`(所有 inbound 幀,混 thread) | 單一 jsonl |
| **既存目錄** | digest 模式(新) | 各 provider 的 digest 入口(`_cx_cards_feed` / `_oc_events_feed` / `_gm_on_update`) | `<目錄>/<thread_id>.jsonl`,一 thread 一檔 |

兩種模式互斥(目錄模式下 wire recorder 自動讓位)。兩者都走
`replay_fixture.append_frame`:object key 正規化、secret-like 幀**整幀拒收**
(事件 log 只留型別,內容絕不落盤)、輸出 0600 owner-only、吞錯不影響熱路徑。

## 開錄

```sh
mkdir -p /private/tmp/pocket-fixture-rec        # 目錄要先存在(這就是開關)
POCKET_RECORD_FIXTURE=/private/tmp/pocket-fixture-rec \
  BRIDGE_TOKEN=... python3 bridge.py            # 開發用 bridge;別動 production
```

production 常駐(launchd)要錄的話,得把 env 加進 plist 再 kickstart ——
**除非當輪任務明示,不要碰**(repo CLAUDE.md 紅線)。

然後用 app/curl 跑一次真會話。每條 thread/session 會各自長出一份
`<thread_id>.jsonl`(檔名中的非 `[A-Za-z0-9._-]` 字元一律換成 `_`,
例:OpenClaw 的 `agent:main:pocket` → `agent_main_pocket.jsonl`)。

錄到的幀(與 digest 入口看到的完全同形):

- **Codex(CX,必有)**:app-server 通知原樣,`method` 如
  `turn/started` / `item/agentMessage/delta` / `turn/completed`,
  `params` 含 `threadId`。
- **OpenClaw**:gateway 事件,`method` = 事件名(`chat` / `agent`),
  `params` = 事件 payload。
- **Gemini**:`method` 固定 `session/update`,`params` = ACP update 原樣。

## 錄完怎麼進 `tests/test_replay_fixtures.py`

1. **審閱並裁剪**。raw 錄音是 local-only,絕不直接 commit —— 刪掉私人
   prompt/路徑/工具輸出,縮成最小可重現場景(照 `cx-simple.jsonl` 的尺寸)。
2. 放進 `tests/fixtures/replay/<provider>-<scenario>.jsonl`。
3. 餵 runner:
   - **codex**:直接吃 —— `replay()` 把每幀打進
     `CodexThreadDigest.handle(method, params)`。
   - **openclaw**:runner 的 `replay_openclaw()` 吃 `{event, payload}` 鍵名,
     把錄音換個鍵名即可:
     ```sh
     jq -c '{event: .method, payload: .params}' agent_main_pocket.jsonl \
        > tests/fixtures/replay/openclaw-<scenario>.jsonl
     ```
   - **gemini**:runner 的 `replay_persona()` 吃的是正規化 turn 契約
     (`turn_begin`/`turn_delta`/`turn_end`…),raw `session/update` 錄音要
     照 `_gm_on_update` 的分支手工正規化(`agent_message_chunk` →
     `turn_delta` 等),對照 `gemini-simple.jsonl`。
4. 在 `tests/fixtures/replay/manifest.json` 登記 provider/scenario 與
   digest。digest 算法:
   ```python
   from replay_fixture import fixture_digest, load_jsonl
   print(fixture_digest(load_jsonl("tests/fixtures/replay/cx-foo.jsonl")))
   ```
5. 驗:`python3 scripts/validate-replay-fixtures.py` 綠、
   `python3 tests/test_replay_fixtures.py` 綠,然後在該檔加對應場景的斷言
   (投影結果,不是只有「跑不炸」)。

## 守門(錄不進去時先看這裡)

- 目錄不存在 → 錄製靜默關閉(這是開關語意,不是 bug)。
- 幀含 secret-like key/value(`token`/`api_key`/`sk-…`/private key 區塊)→
  整幀拒收,事件 log 留 `digest_fixture_frame_rejected`(只有型別)。
- 單幀 > 512 KiB → 拒收。
- 錄製失敗永不打斷 provider 熱路徑,只留 `digest_fixture_record_failed`。
