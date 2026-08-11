"""Continual Harness — 軌跡蒸餾累積層(藍圖 AGENT_INTEROP §2 / 子程序設計 §0)。

Prime Agent(Prime Intellect,Opus 5 + harness 在 ARC-AGI-3 拿 95.5%,模型
未重訓)的洞見:贏在**累積層**,不在執行層。每回合的軌跡回寫四庫
(Prompt / Memory / Skill / Subagent),節點下次開工就站在上次的肩膀上。

善彰的鐵律(藍圖 §2 節奏):**夜批蒸餾 + 晨報人審,不搞自動自改**。
本套件只會「提案」(state=proposed),永遠不會自己套用;approve 一定經人手。

模組分工(刻意做成 `agent_call.py` / `agent_registry.py` 那種純模組:
不 import bridge、可單獨 import、好測):

- `trajectory.py` — 軌跡正規化 + 秘密遮罩(純函式,無 IO)
- `store.py`      — 四庫的 sqlite(自己的 DB,env `HARNESS_DB`)
- `model.py`      — 蒸餾用的一發式模型呼叫(沿用 bridge 既有的本機 Ollama 線)
- `distill.py`    — 夜批蒸餾器 + CLI(`python -m harness.distill`)

紅線:**production 的 canonical.db / state.db 一律唯讀**(`mode=ro` URI),
harness 只寫自己的 `~/.pocket/harness.db`。蒸餾掛掉最多是少一晚提案,
聊天資料零風險。
"""
from __future__ import annotations

__all__ = ["trajectory", "store", "model", "distill"]
