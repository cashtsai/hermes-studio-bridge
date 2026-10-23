# Adding a provider / 新增一個 provider

A **provider** is one agent backend the phone can talk to. Four exist today:
Hermes personas, Claude Code, Codex, OpenClaw — plus Gemini, which is implemented
but hidden in the shipping app.

**This is the contribution with the clearest boundary.** It is additive, it has four
worked examples, and it cannot reach into the parts of the bridge that matter for
security. If you want to contribute and are looking for where to start, start here.

> 繁體中文摘要在最後一節。本文以英文為主,因為它是給外部貢獻者看的。

---

## Before you start: what a provider may and may not do

The bridge executes tools on the machine it runs on. A provider is a **transport
adapter**, not a place to add capability:

| A provider **does** | A provider **must not** |
|---|---|
| Spawn / attach to one agent CLI | Add new HTTP endpoints that bypass `_check_auth` |
| Translate its output into cards | Read or write another provider's state |
| Keep its own session list | Touch `BRIDGE_TOKEN`, pairing, or the console paths |
| Persist its own transcript | Shell out to anything the user did not configure |

If your idea needs something from the right-hand column, open an issue first. It is
probably still doable, just not inside a provider.

## The shape

Two files, plus wiring:

```
<name>_provider.py          the adapter — self-contained, no bridge.py imports
tests/test_<name>_provider.py
bridge.py                   a handful of call sites (see "Wiring" below)
```

Read **`gemini_provider.py` (520 lines)** as the reference. It is the newest and the
most regular. `openclaw_provider.py` (350 lines) is the smaller one — read it second
to see which parts are genuinely required versus Gemini-specific.

### What the module exposes

`gemini_provider.py`'s surface, in the order you will need it:

| | |
|---|---|
| `load_config()` / `save_config()` | Where the user's credentials live. **Never put a key in argv or a log** — see how the secret file is read. |
| `<name>_bin()` | Resolve the CLI. Fail with a clear message when it is absent; do **not** guess a path. |
| `list_sessions()` / `get_session()` / `remember_session()` / `touch_session()` / `forget_session()` | The session registry, persisted as JSON under the provider's own state dir. |
| `transcript_path()` / `transcript_read()` / `transcript_append()` | The conversation, on disk, so the phone can read history offline. |
| `<Name>Client` | The object `bridge.py` holds. Takes `log=` so every event lands in the bridge's event log rather than stdout. |
| `<Name>Error` | One exception type. The bridge maps it to a coded HTTP error. |

### Wiring into bridge.py

Grep for `gemini_provider` — there are only a handful of call sites, and they are the
complete list of what a provider has to hook into: import, client construction,
transcript read/append, and session touch.

Follow the existing error style: `raise http_err(<status>, "<CODE>", "<message>",
"<hint>")`. **The hint is the part users read** — it must name the next action, not
just restate the failure. Compare `CCSESS_MISSING` and `CX_THREAD_LOCKED` for the
tone we want.

## Rules that are not negotiable

1. **No silent failures.** `except Exception: pass` is rejected by
   `tests/test_robustness_pack.py`. If you swallow something, leave a trace with
   `_log_exc(..., expected=True)`.
2. **No private infrastructure identifiers.** `tests/test_no_private_infra_leak.py`
   matches by *shape*, so it catches **your** tailnet name and home directory too.
   Use the placeholders already in the repo.
3. **Tolerant config parsing.** A bad value in one field skips that field and logs
   it; it must never stop the agent from starting. `_cc_spawn_flags` in `bridge.py`
   is the pattern.
4. **Missing dependency ≠ failure.** If your tests need a CLI that not every machine
   has, use `tests/_requires.py` so they skip honestly instead of going red.

## Checklist

- [ ] `<name>_provider.py` imports nothing from `bridge.py`
- [ ] Credentials never appear in argv, logs, or scrollback
- [ ] `./scripts/run-tests.sh` green (skips are fine, reds are not)
- [ ] Errors carry a hint that names the next action
- [ ] No new unauthenticated endpoint

---

## 繁體中文摘要

**provider = 手機可以對話的一個 agent 後端。** 現有四個(Hermes 人格、Claude Code、
Codex、OpenClaw)外加一個實作好但在出貨版隱藏的 Gemini。

**這是邊界最清楚的貢獻題目** —— 純增量、有四個現成範例、而且碰不到 bridge 裡
跟安全有關的部分。想參與又不知從哪開始,就從這裡。

要寫的是兩個檔(`<name>_provider.py` 與對應測試)加上 `bridge.py` 裡少數幾個接點
(`grep gemini_provider` 就是完整清單)。**拿 `gemini_provider.py` 當範本**,它最新
也最規整;`openclaw_provider.py` 比較小,可以對照出哪些是必要的、哪些是 Gemini
自己的需求。

四條不可妥協:**不准靜默失敗**(有守門擋)、**不准帶私有識別**(守門用形狀判斷,
連你自己的 tailnet 也會被擋)、**設定解析要寬容**(壞值只跳過那一項,不准擋住啟動)、
**缺依賴用 `tests/_requires.py` 誠實跳過**而不是讓測試紅著。

錯誤訊息的 hint **要講出下一步**,不是重述失敗 —— 參考 `CCSESS_MISSING` 與
`CX_THREAD_LOCKED` 的寫法。
