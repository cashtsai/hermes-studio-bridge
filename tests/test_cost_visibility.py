"""P2-F5 花費/token 可見性 —— bridge 端單元測試。

裸 assert 風格,仿 test_spawn_config.py:設 POCKET_CANON_DB 到 tmp 再
import bridge,可直接 `PYTHONPATH=. python tests/test_cost_visibility.py` 執行。

涵蓋:
1. _model_price / _estimate_cost_usd:已知模型計價、未知模型 → None、
   cache read/write 倍率。
2. _cc_cum_usage_cached:整檔加總、message.id 去重(streamed turn 重複行)、
   增量 append(offset 前進、不重複計)、truncate 重算、半行容忍。
3. _codex_usage_map:向後相容({used,size} 不變)+ 新增累計欄與估算花費;
   未知模型只給 token 不給 $;OpenAI cached 子集語意。
4. CC usage 合併形狀:cum 併 ctx 時 used/size 以 ctx 為準。
"""
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="costvis-canon-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


# ── 1. 單價表 / 估算 ─────────────────────────────────────────────────────

def test_model_price():
    assert bridge._model_price("claude-opus-4-8") == (5.0, 25.0)
    assert bridge._model_price("claude-sonnet-4-6") == (3.0, 15.0)
    assert bridge._model_price("claude-haiku-4-5-20251001") == (1.0, 5.0)
    assert bridge._model_price("claude-fable-5") == (10.0, 50.0)
    assert bridge._model_price("gpt-5-codex") == (1.25, 10.0)
    assert bridge._model_price("gpt-5-mini") == (0.25, 2.0)
    assert bridge._model_price("totally-unknown-model") is None
    assert bridge._model_price(None) is None
    assert bridge._model_price("") is None


def test_estimate_cost():
    # opus: 1M in = $5, 1M out = $25
    c = bridge._estimate_cost_usd("claude-opus-4-8",
                                  input_tokens=1_000_000,
                                  output_tokens=1_000_000)
    assert abs(c - 30.0) < 1e-9, c
    # cache read 0.1x、cache write 1.25x(以 input 價為基準)
    c = bridge._estimate_cost_usd("claude-opus-4-8",
                                  cache_read_tokens=1_000_000,
                                  cache_write_tokens=1_000_000)
    assert abs(c - (0.5 + 6.25)) < 1e-9, c
    assert bridge._estimate_cost_usd("unknown-model", input_tokens=100) is None
    assert bridge._estimate_cost_usd(None, input_tokens=100) is None


# ── 2. CC 累計掃描 ───────────────────────────────────────────────────────

def _assistant_line(mid, model="claude-opus-4-8", inp=100, outp=50,
                    cr=1000, cw=200):
    return json.dumps({
        "type": "assistant",
        "message": {"id": mid, "model": model,
                    "usage": {"input_tokens": inp, "output_tokens": outp,
                              "cache_read_input_tokens": cr,
                              "cache_creation_input_tokens": cw}},
    }, ensure_ascii=False)


def test_cc_cum_basic_and_dedupe():
    path = os.path.join(_TMP, "cum1.jsonl")
    lines = [
        json.dumps({"type": "user", "message": {"content": "hi"}}),
        _assistant_line("msg_a"),
        _assistant_line("msg_a"),   # streamed 重複行 → 只算一次
        _assistant_line("msg_b", inp=10, outp=20, cr=0, cw=0),
    ]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    out = bridge._cc_cum_usage_cached(path)
    assert out is not None
    assert out["input_tokens"] == 110, out
    assert out["output_tokens"] == 70, out
    assert out["cache_read_tokens"] == 1000, out
    assert out["cache_creation_tokens"] == 200, out
    assert out["total_tokens"] == 110 + 70 + 1000 + 200, out
    assert out["model"] == "claude-opus-4-8"
    assert out["cost_is_estimate"] is True
    expected = (110 * 5 + 70 * 25 + 1000 * 5 * 0.1 + 200 * 5 * 1.25) / 1e6
    assert abs(out["cost_usd"] - round(expected, 4)) < 1e-9, out["cost_usd"]


def test_cc_cum_incremental_append():
    path = os.path.join(_TMP, "cum2.jsonl")
    with open(path, "w") as f:
        f.write(_assistant_line("m1", inp=1, outp=1, cr=0, cw=0) + "\n")
    out1 = bridge._cc_cum_usage_cached(path)
    assert out1["input_tokens"] == 1
    st = bridge._cc_cum_cache[path]
    offset_after_first = st["offset"]
    assert offset_after_first == os.path.getsize(path)
    # append 一則 + 一段半行;半行不該被計入
    with open(path, "a") as f:
        f.write(_assistant_line("m2", inp=2, outp=2, cr=0, cw=0) + "\n")
        f.write('{"type":"assistant","half')   # 未完行
    out2 = bridge._cc_cum_usage_cached(path)
    assert out2["input_tokens"] == 3, out2
    assert out2["output_tokens"] == 3, out2
    st = bridge._cc_cum_cache[path]
    assert st["carry"].startswith('{"type":"assistant","half')
    # 半行補完 → 接續解析,不重複之前的
    with open(path, "a") as f:
        f.write('"}\n')   # 補完但沒有 usage → 不影響數字
    out3 = bridge._cc_cum_usage_cached(path)
    assert out3["input_tokens"] == 3, out3


def test_cc_cum_truncate_rescan():
    path = os.path.join(_TMP, "cum3.jsonl")
    with open(path, "w") as f:
        f.write(_assistant_line("t1", inp=5, outp=5, cr=0, cw=0) + "\n")
        f.write(_assistant_line("t2", inp=5, outp=5, cr=0, cw=0) + "\n")
    out = bridge._cc_cum_usage_cached(path)
    assert out["input_tokens"] == 10
    # truncate 成單行(模擬 rotate)→ 應整檔重算而不是負數/殘留
    with open(path, "w") as f:
        f.write(_assistant_line("t3", inp=7, outp=0, cr=0, cw=0) + "\n")
    out = bridge._cc_cum_usage_cached(path)
    assert out["input_tokens"] == 7, out


def test_cc_cum_unknown_model_tokens_only():
    path = os.path.join(_TMP, "cum4.jsonl")
    with open(path, "w") as f:
        f.write(_assistant_line("u1", model="mystery-9000",
                                inp=10, outp=10, cr=0, cw=0) + "\n")
    out = bridge._cc_cum_usage_cached(path)
    assert out["input_tokens"] == 10
    assert "cost_usd" not in out, out
    assert "cost_is_estimate" not in out, out


def test_cc_cum_empty_or_missing():
    assert bridge._cc_cum_usage_cached(None) is None
    assert bridge._cc_cum_usage_cached(os.path.join(_TMP, "nope.jsonl")) is None
    path = os.path.join(_TMP, "cum5.jsonl")
    with open(path, "w") as f:
        f.write(json.dumps({"type": "user"}) + "\n")
    assert bridge._cc_cum_usage_cached(path) is None


# ── 3. CX usage map ─────────────────────────────────────────────────────

def test_codex_usage_map_backcompat():
    tu = {"tokenUsage": {"totalTokens": 1234, "modelContextWindow": 272000}}
    out = bridge._codex_usage_map(tu)
    assert out["used"] == 1234
    assert out["size"] == 272000
    # 沒有 input/output 欄 → 不帶新欄
    assert "input_tokens" not in out
    assert "cost_usd" not in out


def test_codex_usage_map_cost():
    tu = {"tokenUsage": {"inputTokens": 100_000, "cachedInputTokens": 60_000,
                         "outputTokens": 10_000,
                         "modelContextWindow": 272000}}
    out = bridge._codex_usage_map(tu, model="gpt-5-codex")
    assert out["used"] == 170_000            # 向後相容:總和當 meter
    assert out["input_tokens"] == 100_000
    assert out["cache_read_tokens"] == 60_000
    assert out["output_tokens"] == 10_000
    assert out["total_tokens"] == 170_000
    assert out["model"] == "gpt-5-codex"
    assert out["cost_is_estimate"] is True
    # OpenAI 語意:input 含 cached → 計價 (100k-60k)*1.25 + 60k*0.125 + 10k*10
    expected = (40_000 * 1.25 + 60_000 * 1.25 * 0.1 + 10_000 * 10.0) / 1e6
    assert abs(out["cost_usd"] - round(expected, 4)) < 1e-9, out["cost_usd"]


def test_codex_usage_map_unknown_model():
    tu = {"inputTokens": 100, "outputTokens": 5}
    out = bridge._codex_usage_map(tu, model="weird-local-llm")
    assert out["input_tokens"] == 100
    assert "cost_usd" not in out
    out = bridge._codex_usage_map(tu)   # 沒 model 也一樣
    assert "cost_usd" not in out


def test_codex_usage_map_total_split():
    # 未來版本若拆 total/last → 取 total 那份
    tu = {"tokenUsage": {"total": {"inputTokens": 50, "outputTokens": 5},
                         "last": {"inputTokens": 1, "outputTokens": 1}}}
    out = bridge._codex_usage_map(tu, model="gpt-5")
    assert out["used"] == 55
    assert out["input_tokens"] == 50


# ── 4. 合併形狀 ─────────────────────────────────────────────────────────

def test_merge_shape_ctx_wins():
    cum = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
           "used": 999999, "size": 1}          # 假設 cum 帶了同名欄也要被壓過
    ctx = {"used": 120_000, "size": 200_000}
    merged = {**cum, **ctx}
    assert merged["used"] == 120_000
    assert merged["size"] == 200_000
    assert merged["input_tokens"] == 10


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
