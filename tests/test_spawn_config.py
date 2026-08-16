"""cc/cx spawn config「全集控制面」測試(設計 §2.1/§2.2)。

裸 assert 風格,仿 test_a3_approval_card_parity.py:設 POCKET_CANON_DB 到 tmp
再 import bridge(import 會跑 _canon_init),可直接
`PYTHONPATH=. python tests/test_spawn_config.py` 執行。

涵蓋:
1. config→flag 翻譯(cc claude flags、cx `codex exec` flags、cx thread params)。
2. enum server-side 驗證(effort/permission_mode/approval_policy/sandbox 未知值 → 400)。
3. api_key 遮罩:redacted/public/log payload 一律不含明文 key;argv 也不含。
4. api_key env 注入:cc→ANTHROPIC_API_KEY、cx→OPENAI_API_KEY,只進該子程序 env。
5. budget 透傳(數值驗證 + --max-budget-usd flag)。
6. 設定讀回:_cc_read_spawn_config round-trip;缺檔 graceful nil。
7. 舊 shape 容忍:config=None / 未知欄位 → 不炸、沿用今天行為。
8. _claude_argv 併 config:permission-mode 覆寫且不重複、其餘 flags 帶上、key 不進 argv。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="spawncfg-canon-")
os.environ["POCKET_CANON_DB"] = os.path.join(_TMP, "canonical.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

FAKE_KEY = "sk-ant-SECRET-donotlog-1234567890"
OPENAI_KEY = "sk-openai-SECRET-xyz-0987654321"

_passed = 0


def ok(cond, msg):
    global _passed
    assert cond, "FAIL: " + msg
    _passed += 1


# ── 1. cc config → claude flags ────────────────────────────────────────────
def test_cc_flags():
    cfg = bridge._spawn_config_validate({
        "model": "opus", "effort": "high", "permission_mode": "plan",
        "max_budget_usd": 2.5, "fallback_model": "sonnet",
        "append_system_prompt": "extra rules", "api_key": FAKE_KEY,
    }, "cc")
    flags = bridge._spawn_cc_flags(cfg)
    ok(flags[:2] == ["--model", "opus"], "cc model flag")
    ok("--effort" in flags and flags[flags.index("--effort") + 1] == "high", "cc effort flag")
    ok("--permission-mode" in flags and flags[flags.index("--permission-mode") + 1] == "plan",
       "cc permission-mode flag")
    ok("--max-budget-usd" in flags and flags[flags.index("--max-budget-usd") + 1] == "2.5",
       "cc budget flag")
    ok("--fallback-model" in flags and flags[flags.index("--fallback-model") + 1] == "sonnet",
       "cc fallback flag")
    ok("--append-system-prompt" in flags, "cc append-system-prompt flag")
    # KEY 絕不進 argv
    ok(FAKE_KEY not in " ".join(flags), "cc api_key NOT in flags")


# ── 2. cx config → codex exec flags + thread params ────────────────────────
def test_cx_flags():
    cfg = bridge._spawn_config_validate({
        "model": "gpt-5.5", "approval_policy": "on-request",
        "sandbox": "workspace-write", "effort": "xhigh", "profile": "prod",
        "api_key": OPENAI_KEY,
    }, "cx")
    flags = bridge._spawn_cx_exec_flags(cfg)
    ok(flags[:2] == ["-m", "gpt-5.5"], "cx model flag")
    ok("-a" in flags and flags[flags.index("-a") + 1] == "on-request", "cx approval flag")
    ok("-s" in flags and flags[flags.index("-s") + 1] == "workspace-write", "cx sandbox flag")
    ok("-c" in flags and flags[flags.index("-c") + 1] == "model_reasoning_effort=xhigh",
       "cx effort config flag")
    ok("-p" in flags and flags[flags.index("-p") + 1] == "prod", "cx profile flag")
    ok(OPENAI_KEY not in " ".join(flags), "cx api_key NOT in flags")
    # thread params(共用 app-server 路徑)
    params = bridge._spawn_cx_thread_params(cfg)
    ok(params.get("model") == "gpt-5.5", "cx thread model")
    ok(params.get("approvalPolicy") == "on-request", "cx thread approvalPolicy")
    ok(params.get("reasoningEffort") == "xhigh", "cx thread reasoningEffort")
    ok(params.get("sandboxMode") == "workspace-write", "cx thread sandboxMode")
    ok("api_key" not in json.dumps(params) and OPENAI_KEY not in json.dumps(params),
       "cx thread params no key")


# ── 3. enum 驗證(未知值 → SpawnConfigError=400)────────────────────────────
def test_enum_validation():
    bad = [
        ("cc", {"effort": "ultra"}),
        ("cc", {"permission_mode": "yolo"}),
        ("cc", {"max_budget_usd": -1}),
        ("cc", {"max_budget_usd": "cheap"}),
        ("cx", {"effort": "max"}),          # max 是 cc 的,不是 cx 的
        ("cx", {"approval_policy": "always"}),
        ("cx", {"sandbox": "open"}),
    ]
    for provider, cfg in bad:
        try:
            bridge._spawn_config_validate(cfg, provider)
            ok(False, f"should reject {provider} {cfg}")
        except bridge.SpawnConfigError as e:
            ok(bool(e.detail), f"reject {provider} {cfg} with zh-TW detail")
    # 合法值全通過
    for e in bridge._SPAWN_CC_EFFORTS:
        ok(bridge._spawn_config_validate({"effort": e}, "cc")["effort"] == e, f"cc effort {e} ok")
    for e in bridge._SPAWN_CX_EFFORTS:
        ok(bridge._spawn_config_validate({"effort": e}, "cx")["effort"] == e, f"cx effort {e} ok")
    for m in bridge._SPAWN_CC_PERMISSION_MODES:
        ok(bridge._spawn_config_validate({"permission_mode": m}, "cc")["permission_mode"] == m,
           f"cc mode {m} ok")
    for p in bridge._CODEX_APPROVAL_POLICIES:
        ok(bridge._spawn_config_validate({"approval_policy": p}, "cx")["approval_policy"] == p,
           f"cx policy {p} ok")


# ── 4. api_key 遮罩(redacted / public / 不進 log)──────────────────────────
def test_api_key_redaction():
    cfg = bridge._spawn_config_validate(
        {"model": "opus", "api_key": FAKE_KEY, "max_budget_usd": 5}, "cc")
    red = bridge._spawn_config_redacted(cfg)
    ok(red["api_key"] == "***redacted***", "redacted marker")
    ok(FAKE_KEY not in json.dumps(red, ensure_ascii=False), "key absent in redacted json")
    pub = bridge._spawn_config_public(cfg)
    ok("api_key" not in pub, "public config has no api_key field")
    ok(pub.get("has_api_key") is True, "public config flags has_api_key")
    ok(FAKE_KEY not in json.dumps(pub, ensure_ascii=False), "key absent in public json")

    # 攔 _log_event:模擬 dispatch 用 **redacted 打 log,斷言 emitted 不含明文。
    captured = []
    orig = bridge._log_event
    bridge._log_event = lambda ev, **f: captured.append((ev, f))
    try:
        bridge._log_event("dispatch_spawn_config", sid="sub-x", tool="claude-code", **red)
    finally:
        bridge._log_event = orig
    ok(captured, "log captured")
    payload = json.dumps(captured, ensure_ascii=False)
    ok(FAKE_KEY not in payload, "raw key NEVER appears in emitted log event")
    ok("***redacted***" in payload, "log carries the redacted marker")


# ── 5. api_key env 注入(只進該子程序 env)──────────────────────────────────
def test_env_injection():
    cc_cfg = bridge._spawn_config_validate({"api_key": FAKE_KEY}, "cc")
    env = bridge._spawn_env(cc_cfg, "cc", base_env={"PATH": "/bin"})
    ok(env["ANTHROPIC_API_KEY"] == FAKE_KEY, "cc key → ANTHROPIC_API_KEY")
    ok("OPENAI_API_KEY" not in env, "cc path leaves OPENAI unset")
    ok(env["PATH"] == "/bin", "base env preserved")

    cx_cfg = bridge._spawn_config_validate({"api_key": OPENAI_KEY}, "cx")
    env2 = bridge._spawn_env(cx_cfg, "cx", base_env={"PATH": "/bin"})
    ok(env2["OPENAI_API_KEY"] == OPENAI_KEY, "cx key → OPENAI_API_KEY")
    ok("ANTHROPIC_API_KEY" not in env2, "cx path leaves ANTHROPIC unset")

    # 無 key → env 原封不動(沿用主機自己的 auth)
    env3 = bridge._spawn_env({}, "cc", base_env={"PATH": "/bin"})
    ok("ANTHROPIC_API_KEY" not in env3, "no key → no injection")


# ── 6. budget 透傳 ─────────────────────────────────────────────────────────
def test_budget_passthrough():
    cfg = bridge._spawn_config_validate({"max_budget_usd": 3}, "cc")
    ok(cfg["max_budget_usd"] == 3.0, "budget normalized to float")
    ok(bridge._spawn_fmt_usd(3.0) == "3", "usd fmt trims .0")
    ok(bridge._spawn_fmt_usd(2.5) == "2.5", "usd fmt keeps decimals")
    ok("--max-budget-usd" in bridge._spawn_cc_flags(cfg), "budget → flag")


# ── 7. cc pin round-trip + graceful nil ────────────────────────────────────
def test_cc_pin_roundtrip():
    import shutil
    name = "spawncfg_test_sess"
    # 缺檔 → nil
    bridge.CCSESS_SPAWN_DIR = os.path.join(_TMP, "spawn")
    bridge.CCSESS_SECRET_DIR = os.path.join(_TMP, "secret")
    ok(bridge._cc_read_spawn_config("nonexistent_" + name) == {}, "missing pins → {}")

    cfg = bridge._spawn_config_validate(
        {"model": "opus", "effort": "high", "permission_mode": "plan",
         "api_key": FAKE_KEY}, "cc")
    red = bridge._cc_write_spawn_pins(name, cfg)
    ok(red.get("api_key") == "***redacted***", "write returns redacted")
    # secret 檔為 0600、內容為明文 key(host-local 慣例),但不在 spawn.json 裡
    spath = os.path.join(bridge.CCSESS_SECRET_DIR, name)
    ok(os.path.exists(spath), "secret file written")
    ok(oct(os.stat(spath).st_mode & 0o777) == "0o600", "secret file 0600")
    jpath = os.path.join(bridge.CCSESS_SPAWN_DIR, name + ".json")
    ok(FAKE_KEY not in open(jpath, encoding="utf-8").read(), "api_key NOT in spawn.json")
    # 讀回:has_api_key 布林、無明文
    back = bridge._cc_read_spawn_config(name)
    ok(back.get("effort") == "high", "read back effort")
    ok(back.get("permission_mode") == "plan", "read back permission_mode")
    ok(back.get("has_api_key") is True, "read back has_api_key flag")
    ok(FAKE_KEY not in json.dumps(back, ensure_ascii=False), "read back has no plaintext key")
    shutil.rmtree(os.path.join(_TMP, "spawn"), ignore_errors=True)
    shutil.rmtree(os.path.join(_TMP, "secret"), ignore_errors=True)


# ── 8. 舊 shape 容忍 + 未知欄位忽略 ────────────────────────────────────────
def test_graceful_old_shape():
    ok(bridge._spawn_config_validate(None, "cc") == {}, "None config → {}")
    ok(bridge._spawn_config_validate({}, "cc") == {}, "empty config → {}")
    # 未知欄位靜默忽略(前向相容)
    cfg = bridge._spawn_config_validate(
        {"model": "opus", "future_knob": "x", "speed": "fast"}, "cc")
    ok(cfg == {"model": "opus"}, "unknown keys ignored")
    ok(bridge._spawn_config_public({}) == {}, "public of empty → {}")
    ok(bridge._spawn_config_redacted({}) == {}, "redacted of empty → {}")
    # 非 dict → 400
    try:
        bridge._spawn_config_validate([1, 2], "cc")
        ok(False, "list config should reject")
    except bridge.SpawnConfigError:
        ok(True, "non-object config rejected")


# ── 9. _claude_argv 併 config(不重複 permission-mode、key 不進 argv)────────
def test_claude_argv_merge():
    cfg = bridge._spawn_config_validate(
        {"model": "opus", "effort": "max", "permission_mode": "acceptEdits",
         "api_key": FAKE_KEY}, "cc")
    argv = bridge._claude_argv("yuanfang", "do a thing", config=cfg)
    ok(argv.count("--permission-mode") == 1, "exactly one --permission-mode")
    ok(argv[argv.index("--permission-mode") + 1] == "acceptEdits",
       "config permission-mode wins over default bypassPermissions")
    ok("--model" in argv and argv[argv.index("--model") + 1] == "opus", "argv model")
    ok("--effort" in argv and argv[argv.index("--effort") + 1] == "max", "argv effort")
    ok(FAKE_KEY not in " ".join(argv), "api_key NEVER in claude argv")
    # 無 config → 沿用今天預設(bypassPermissions),不炸
    argv2 = bridge._claude_argv("yuanfang", "x")
    ok(argv2[argv2.index("--permission-mode") + 1] == "bypassPermissions",
       "no config → default bypassPermissions")


if __name__ == "__main__":
    test_cc_flags()
    test_cx_flags()
    test_enum_validation()
    test_api_key_redaction()
    test_env_injection()
    test_budget_passthrough()
    test_cc_pin_roundtrip()
    test_graceful_old_shape()
    test_claude_argv_merge()
    print(f"OK — {_passed} assertions passed")
