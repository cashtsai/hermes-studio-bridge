#!/bin/sh
# 逐檔跑 tests/*.py,最後印一份彙總。
#
# 為什麼需要這支:本 repo 的測試檔分兩種血統 ——
#   (a) 正規 unittest 檔:`unittest discover` 收得到,CI/全套跑都會驗到。
#   (b) 腳本式驗收檔:0 個 TestCase、模組層直接跑 + `sys.exit()`,靠
#       `python tests/x.py` 單跑才有意義。這種檔在 discover 底下只會「被 import
#       就執行」,製造 ERROR 雜訊與 env/monkeypatch 污染,卻貢獻 0 個 case。
#
# 這些腳本式檔現在加了 `if __name__ != "__main__": raise SkipTest(...)`,
# discover 底下乾淨地顯示為 skipped。但那也代表**沒有任何東西會再跑到它們** ——
# 其中 test_a3_approval_card_parity / test_report_reader_api /
# test_tg_outbound_mirror 單跑其實是紅的,會就這樣無聲無息地爛掉。
#
# 所以:discover 負責 (a),這支負責 (b)。兩個都要跑才算跑完整套。
#
# 用法:
#   scripts/run-tests.sh                  # 跑全部 tests/*.py
#   scripts/run-tests.sh test_foo         # 只跑名字含 "test_foo" 的
#   PYTHON=/path/to/python scripts/run-tests.sh
#   TIMEOUT=120 scripts/run-tests.sh      # 單檔逾時秒數(預設 300)
#
# 離開碼:全綠 0,有任何一檔紅則 1。

set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT" || exit 1

PYTHON=${PYTHON:-/Users/xcash/apps/hermes-agent/runtime/venv/bin/python}
[ -x "$PYTHON" ] || PYTHON=$(command -v python3) || {
    echo "找不到可用的 python(設 PYTHON=... 指定)" >&2; exit 1; }

TIMEOUT=${TIMEOUT:-300}
FILTER=${1:-}

LOGDIR=$(mktemp -d "${TMPDIR:-/tmp}/bridge-tests-XXXXXX")

# 測試會寫 ~/.pocket/openclaw.json —— 導到 tmp,別碰使用者真的設定檔。
# (openclaw_provider 的 _CONFIG_FILE 是模組常數,行程啟動就吃這個 env。)
OPENCLAW_CONFIG_FILE=${OPENCLAW_CONFIG_FILE:-$LOGDIR/openclaw.json}
export OPENCLAW_CONFIG_FILE
export PYTHONPATH=.${PYTHONPATH:+:$PYTHONPATH}

pass=0; fail=0; failed_list=''

for f in tests/test_*.py; do
    [ -e "$f" ] || continue
    name=$(basename "$f" .py)
    case "$name" in
        *"$FILTER"*) ;;
        *) continue ;;
    esac

    log="$LOGDIR/$name.log"
    # 沒有 GNU timeout 也要能跑:用背景 + 輪詢守著,逾時就殺掉。
    "$PYTHON" "$f" >"$log" 2>&1 &
    pid=$!
    waited=0
    while kill -0 "$pid" 2>/dev/null; do
        [ "$waited" -ge "$TIMEOUT" ] && { kill -9 "$pid" 2>/dev/null; echo "TIMEOUT after ${TIMEOUT}s" >>"$log"; break; }
        sleep 1
        waited=$((waited + 1))
    done
    wait "$pid" 2>/dev/null
    rc=$?

    if [ "$rc" -eq 0 ]; then
        pass=$((pass + 1))
        printf 'PASS  %s\n' "$name"
    else
        fail=$((fail + 1))
        failed_list="$failed_list $name"
        printf 'FAIL  %s  (rc=%s)\n' "$name" "$rc"
    fi
done

echo
echo "──────────────────────────────────────────"
echo "逐檔結果:$pass 綠 / $fail 紅  (共 $((pass + fail)) 檔)"
if [ "$fail" -gt 0 ]; then
    echo "紅的:"
    for n in $failed_list; do echo "  - $n    → $LOGDIR/$n.log"; done
    echo
    echo "完整輸出:$LOGDIR"
    exit 1
fi
echo "全綠。輸出:$LOGDIR"
exit 0
