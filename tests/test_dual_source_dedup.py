"""雙源(canonical×state.db)壓重模糊比對(fix/persona-tg-fuzzy-dedup)測試。

素材 = 2026-08-04 生產實抓:同一則回覆兩份落稿,措辭微漂+開頭空白,
舊的字面相等壓重必漏 → app 兩顆氣泡(「人格常回覆重複內容」病根)。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bridge  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


N = bridge._dedup_norm
D = bridge._dual_source_dup

# 生產實例(xcash 07/25):canonical 開頭多兩換行、「對話上下文」vs「上下文」
canon = "\n\n善彰,「封存那 5 條」我這邊看不出明確指的是哪 5 個項目——目前上下文(Bridge 健康警報/復原、Build 81 出貨、開發晨報)裡沒有清楚對應到"
tg    = "善彰,「封存那 5 條」我這邊看不出明確指的是哪 5 個項目——目前對話上下文(Bridge 健康警報/復原、Build 81 出貨、開發晨報)裡沒有清楚對應到"
recent = [(1000.0, "assistant", N(canon))]
check("實例:措辭微漂+空白 → 壓掉", D(N(tg), "assistant", 1030.0, recent))

# 完全相等快路
check("完全相等 → 壓掉", D(N(canon), "assistant", 1000.0, recent))

# 附錄漂移:canonical 帶〈🔧 執行步驟〉details 附錄
canon2 = tg + "\n\n<details><summary>🔧 執行步驟</summary>\n一堆工具紀錄\n</details>"
check("附錄剝除後同文 → 壓掉", D(N(tg), "assistant", 1030.0,
                               [(1000.0, "assistant", N(canon2))]))

# 不同 role 不壓
check("role 不同 → 保留", not D(N(tg), "user", 1030.0, recent))

# 時間窗外不壓(相隔 15 分鐘的同文 = 兩次真回覆)
check("超出 600s 窗 → 保留", not D(N(tg), "assistant", 1700.0, recent))

# 真正不同的回覆不壓(相似度低)
other = "善彰,晨報已經整理好,重點在最後一哩:build 105 已出貨,等你驗收。"
check("不同內容 → 保留", not D(N(other), "assistant", 1030.0, recent))

# 長度差 >20% 短路不壓(長回覆 vs 短摘要不是同一則)
short = "善彰,「封存那 5 條」我看不出指哪 5 個。"
check("長度差過大 → 保留", not D(N(short), "assistant", 1030.0, recent))

# 空字串防呆
check("空正文 → 不壓", not D("", "assistant", 1000.0, recent))

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("ALL PASS")
