"""蒸餾用的一發式模型呼叫 —— **沿用 bridge 既有的那條線,不新增依賴**。

盤點過 bridge 現有的「送文字給模型、拿文字回來」路徑,只有三條:

| 路徑 | 位置 | 適不適合蒸餾 |
|---|---|---|
| 本機 Ollama(會議逐字稿清稿) | `bridge._polish_transcript` | ✅ 真一發式、無 session 狀態、無金鑰、本機免費 |
| `bridge.acp_full(model, prompt)` | ACP persona pool | ❌ **不是無狀態**:綁使用者真正的 Telegram canonical session,蒸餾提示詞會噴到善彰手機上,還會卡住真人回合(`self._lock`) |
| `bridge.run_hermes(model, prompt)` | hermes 子行程 | ❌ 同上,刻意打同一個 canonical session |
| headless `claude -p` dispatch | `_run_dispatch` | ❌ 會建 SUBSESSIONS/worktree/registry 配額,層級完全不對 |

所以蒸餾走**第一條**:同一個 `http://127.0.0.1:11434/api/chat`、同一組
逾時/keep_alive/fail-soft 慣例。`requirements.txt` 不用動(httpx 早在),
也不會有雲端金鑰進到 harness 這一層。

為了避免同一段 Ollama 呼叫在 repo 裡出現兩份,這個模組是**唯一實作**:
`bridge._polish_transcript` 也改成呼叫這裡的 `ollama_text()`(語意逐字保持
原樣 —— 逾時/例外仍由 bridge 端自己 catch 並回原稿)。

測試一律 mock `ollama_text`,**不打真的模型**。
"""
from __future__ import annotations

import asyncio
import os

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"


def ollama_url() -> str:
    return (os.environ.get("OLLAMA_URL", "").strip()
            or DEFAULT_OLLAMA_URL).rstrip("/")


def distill_model() -> str:
    """蒸餾模型。預設沿用清稿那顆(mistral-small3.2:實測快、保留繁體、
    不亂改語意),善彰要換更強的設 `HARNESS_MODEL` 即可。"""
    return (os.environ.get("HARNESS_MODEL", "").strip()
            or os.environ.get("MEETING_POLISH_MODEL", "").strip()
            or "mistral-small3.2:latest")


async def ollama_text(prompt: str, *, model: str | None = None,
                      num_ctx: int | None = None, timeout: float = 90.0,
                      temperature: float = 0.2, keep_alive: str = "15m",
                      fmt: str | None = None) -> str:
    """送一段提示詞給本機 Ollama,回傳純文字。

    **會拋**(`asyncio.TimeoutError` / httpx 例外)—— 由呼叫端決定 fail-soft
    策略。這是刻意的:清稿那條路要「失敗回原稿」、蒸餾這條路要「失敗記一筆
    跑批錯誤、今晚不出提案」,兩種語意不該由這裡替人決定。

    `fmt="json"` 會請 Ollama 走 JSON mode(蒸餾用,回傳必為合法 JSON)。
    """
    import httpx
    payload: dict = {
        "model": model or distill_model(),
        "stream": False,
        "keep_alive": keep_alive,
        "messages": [{"role": "user", "content": prompt}],
        "options": {"temperature": temperature},
    }
    if num_ctx:
        payload["options"]["num_ctx"] = int(num_ctx)
    if fmt:
        payload["format"] = fmt

    async def _run() -> str:
        async with httpx.AsyncClient(timeout=timeout + 30) as client:
            r = await client.post(ollama_url() + "/api/chat", json=payload)
            r.raise_for_status()
            return ((r.json().get("message") or {}).get("content") or "").strip()

    return await asyncio.wait_for(_run(), timeout=timeout)
