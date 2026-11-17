#!/bin/sh
# prove-scenario.sh —— 「base 失敗 / head 通過」證明制。
#
#   scripts/prove-scenario.sh <scenario> <base_sha>
#
# 它做兩件事,兩件都對才印 PROVEN:
#
#   1. 在一個**暫存 worktree** checkout `<base_sha>`(= 修復前的世界),把
#      head 的 `scenarios/` 複製進去,跑那個 scenario —— **必須失敗**。
#      沒失敗代表這個 scenario 沒有真的捕捉到那個 bug:它可能在驗一條
#      恆真的事、可能繞過了出問題的那條路、也可能 base 根本選錯。無論哪種,
#      它都是一條會永遠綠著的線,也就是我們最怕的那種「代理指標說謊」
#      (2026-10-28 記錄過七次)。這時腳本報錯,不印 PROVEN。
#   2. 回到 head 跑同一個 scenario —— **必須通過**。
#
# 為什麼不能只看離開碼:舊 commit 上 `import bridge` 炸掉也是非 0,長得跟
# 「bug 重現了」一模一樣。所以 scenario 底座(scenarios/_scenario.py)把
# 「觀測到現象」(1 + SCENARIO_FAILED)與「自己跑不起來」(2 + SCENARIO_ERROR)
# 分成兩個碼,這裡**兩個都查**:base 一定要是前者。
#
# env:
#   PYTHON               跑 scenario 的 python(預設照 run-tests.sh 的順序找)
#   PROVE_SCENARIO_DIR   要複製進 worktree 的 scenarios 目錄(預設 $ROOT/scenarios)
#   PROVE_HEAD_REF       head 那一步也用 worktree 跑這個 ref(預設直接用工作樹,
#                        這樣未 commit 的修復也證得起來)。本腳本的自我測試用它。
#   PROVE_KEEP           非空 = 不刪暫存 worktree(查現場用)
#
# 離開碼:0 = PROVEN;1 = 證明失敗(含 base 沒失敗);2 = 用法錯誤。

set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

usage() {
    echo "用法: scripts/prove-scenario.sh <scenario> <base_sha>" >&2
    echo "  <scenario>  scenarios/ 底下的檔名(可省略 .py)" >&2
    echo "  <base_sha>  修復前的 commit(例:4c87829^)" >&2
    exit 2
}

[ $# -eq 2 ] || usage
SCENARIO=$(basename -- "$1" .py)
BASE_REF=$2

SCEN_DIR=${PROVE_SCENARIO_DIR:-$ROOT/scenarios}
[ -f "$SCEN_DIR/$SCENARIO.py" ] || {
    echo "找不到 scenario: $SCEN_DIR/$SCENARIO.py" >&2
    exit 2
}

PYTHON=${PYTHON:-}
if [ -z "$PYTHON" ]; then
    for cand in "$HOME/apps/hermes-agent/venvs/hermes-0.22.2/bin/python" \
                "$HOME/apps/hermes-agent/runtime/venv/bin/python"; do
        [ -x "$cand" ] && { PYTHON=$cand; break; }
    done
fi
[ -n "$PYTHON" ] || PYTHON=$(command -v python3) || {
    echo "找不到可用的 python(設 PYTHON=... 指定)" >&2; exit 2; }

BASE_SHA=$(git -C "$ROOT" rev-parse --verify "$BASE_REF" 2>/dev/null) || {
    echo "解不出 base commit: $BASE_REF" >&2; exit 2; }
HEAD_SHA=$(git -C "$ROOT" rev-parse --verify "${PROVE_HEAD_REF:-HEAD}")

WORK=$(mktemp -d "${TMPDIR:-/tmp}/prove-scenario-XXXXXX")
BASE_TREE=$WORK/base
LOG_BASE=$WORK/base.log
LOG_HEAD=$WORK/head.log
HEAD_TREE=$ROOT
ADDED_HEAD_TREE=

cleanup() {
    [ -n "${PROVE_KEEP:-}" ] && { echo "(保留現場:$WORK)"; return; }
    git -C "$ROOT" worktree remove --force "$BASE_TREE" >/dev/null 2>&1
    [ -n "$ADDED_HEAD_TREE" ] && \
        git -C "$ROOT" worktree remove --force "$ADDED_HEAD_TREE" >/dev/null 2>&1
    rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

# scenario 的程式碼**永遠來自 head**,只有產品程式碼回到 base —— 這就是整個
# 制度的形狀:同一份行為描述,拿去問兩個世界。
plant() {
    rm -rf "$1/scenarios"
    cp -R "$SCEN_DIR" "$1/scenarios" || exit 2
}

run_scenario() {   # run_scenario <tree> <logfile>
    ( cd "$1" && "$PYTHON" "scenarios/$SCENARIO.py" >"$2" 2>&1 )
}

echo "scenario : $SCENARIO"
echo "base     : $BASE_SHA  $(git -C "$ROOT" log -1 --format=%s "$BASE_SHA" | cut -c1-60)"
echo "head     : $HEAD_SHA  $(git -C "$ROOT" log -1 --format=%s "$HEAD_SHA" | cut -c1-60)"
echo "python   : $PYTHON"
echo

# ── 第一步:base 必須失敗 ────────────────────────────────────────────────
echo "① base($(echo "$BASE_SHA" | cut -c1-9))—— 現象必須重現"
git -C "$ROOT" worktree add --detach "$BASE_TREE" "$BASE_SHA" >/dev/null 2>&1 || {
    echo "   建不起 base worktree" >&2; exit 1; }
plant "$BASE_TREE"
run_scenario "$BASE_TREE" "$LOG_BASE"
RC_BASE=$?
VERDICT_BASE=$(grep -oE 'SCENARIO_(PASSED|FAILED|ERROR|SKIPPED)' "$LOG_BASE" | head -1)
echo "   離開碼 $RC_BASE  判決 ${VERDICT_BASE:-（沒有判決標記）}"

fail_with_log() {
    echo
    echo "✗ NOT PROVEN —— $1"
    echo
    echo "── $2 最後 40 行 ──"
    tail -40 "$3"
    exit 1
}

if [ "$RC_BASE" -eq 0 ]; then
    fail_with_log \
"base 上**沒有失敗**。
  這個 scenario 沒有真的捕捉到那個 bug —— 它在修復前的程式碼上就已經是綠的,
  所以它永遠不會紅,等於一條會說謊的線。可能的原因:
    · 驗的東西跟那個 bug 無關(只驗了存活、沒驗行為)
    · base commit 選錯(修復其實更早就進去了)
    · 替身把出問題的那條路繞過去了
  先把 scenario 改成真的會在 base 上紅,再回來跑這支。" base "$LOG_BASE"
fi
if [ "$RC_BASE" -eq 77 ] || [ "$VERDICT_BASE" = "SCENARIO_SKIPPED" ]; then
    fail_with_log \
"base 上被**跳過**(這台機器缺依賴)。跳過不是證據。" base "$LOG_BASE"
fi
if [ "$VERDICT_BASE" != "SCENARIO_FAILED" ]; then
    fail_with_log \
"base 上是非 0 離開碼,但**不是**『觀測到現象』(沒有 SCENARIO_FAILED)。
  這代表 scenario 自己在舊 commit 上跑不起來(import 爛了、替身沒接上、
  用到了 base 還不存在的內部函式)。那是假證據:它的非 0 來自自己壞掉,
  不是來自產品的 bug。scenario 只准碰產品對外的介面,見 docs/SCENARIOS.md。" \
        base "$LOG_BASE"
fi
echo "   ✓ 現象重現"
echo

# ── 第二步:head 必須通過 ────────────────────────────────────────────────
if [ -n "${PROVE_HEAD_REF:-}" ]; then
    HEAD_TREE=$WORK/head
    ADDED_HEAD_TREE=$HEAD_TREE
    git -C "$ROOT" worktree add --detach "$HEAD_TREE" "$PROVE_HEAD_REF" >/dev/null 2>&1 || {
        echo "   建不起 head worktree($PROVE_HEAD_REF)" >&2; exit 1; }
    plant "$HEAD_TREE"
    echo "② head($PROVE_HEAD_REF)—— 現象必須消失"
else
    echo "② head(工作樹)—— 現象必須消失"
fi
run_scenario "$HEAD_TREE" "$LOG_HEAD"
RC_HEAD=$?
VERDICT_HEAD=$(grep -oE 'SCENARIO_(PASSED|FAILED|ERROR|SKIPPED)' "$LOG_HEAD" | head -1)
echo "   離開碼 $RC_HEAD  判決 ${VERDICT_HEAD:-（沒有判決標記）}"

if [ "$RC_HEAD" -ne 0 ] || [ "$VERDICT_HEAD" != "SCENARIO_PASSED" ]; then
    fail_with_log "head 上**沒有通過** —— 這個現象還在(或 scenario 壞了)。" \
        head "$LOG_HEAD"
fi
echo "   ✓ 現象消失"

echo
echo "══════════════════════════════════════════════════════════════"
echo "PROVEN  $SCENARIO"
echo "  base $(echo "$BASE_SHA" | cut -c1-9)  → 失敗(SCENARIO_FAILED,離開碼 $RC_BASE)"
echo "  head $(echo "$HEAD_SHA" | cut -c1-9)  → 通過(SCENARIO_PASSED,離開碼 $RC_HEAD)"
echo "══════════════════════════════════════════════════════════════"
grep -E '^SCENARIO_FAILED: ' "$LOG_BASE" | sed 's/^/  base ⟩ /'
exit 0
