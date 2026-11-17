"""一隻**真的可執行**的假 tmux —— scenario 用來演「CC 畫面上現在長什麼樣」。

為什麼不是 monkeypatch `_tmux_run`:那會把產品自己的子行程路徑、參數組裝、
rc 判讀全部跳過,等於驗了一條不存在的程式。這裡換掉的只有**外面那個執行檔**
(tmux 的 CLI 介面跨版本穩定),bridge 從 `create_subprocess_exec` 往下的
每一行都照跑 —— 而且同一份替身在舊 commit 上一樣接得上。

支援到剛好夠用:
    has-session -t NAME           → rc 0 / 1
    capture-pane [-p|-pe|-e] -t NAME  → 印出畫面
    send-keys -t NAME [-N n] KEY… → 打字(見 _press)
    load-buffer -b BUF -          → 從 stdin 收 buffer
    paste-buffer -t NAME -b BUF [-p] [-d]  → buffer 貼進輸入框
    delete-buffer -b BUF
    display-message -p -t NAME …  → 印一組像樣的 pane_id / tty
    其他                          → rc 0、無輸出

狀態放在一個 JSON 檔(`PA_FAKE_TMUX_STATE`),所以 scenario 看得到按鍵紀錄
—— 「訊息真的被貼進去並按了 Enter」這種斷言要看外面的世界,不是看回傳值。
"""
import json
import os
import sys

BORDER = "─" * 72
SURVEY_LINES = [
    "",
    "● How is Claude doing this session? (optional)",
    "  1: Bad    2: Fine   3: Good   0: Dismiss",
    "",
    BORDER,
]
PROMPT = "❯ "          # ❯ + NBSP,與真 CC 的輸入框一致


# ───────────────────────────── 狀態 ──────────────────────────────────────

def _state_path() -> str:
    path = os.environ.get("PA_FAKE_TMUX_STATE")
    if not path:
        sys.stderr.write("PA_FAKE_TMUX_STATE not set\n")
        raise SystemExit(2)
    return path


def load(path: str | None = None) -> dict:
    with open(path or _state_path(), encoding="utf-8") as f:
        return json.load(f)


def save(state: dict, path: str | None = None) -> None:
    with open(path or _state_path(), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def new_session(transcript=None, survey: bool = False) -> dict:
    return {"transcript": list(transcript or []), "composer": "",
            "survey": bool(survey)}


# ──────────────────────────── 畫面 ───────────────────────────────────────

def render(sess: dict) -> str:
    """把狀態畫成 pane 文字。

    問卷態**刻意不畫輸入框** —— 這就是 2026-11-13/11-15 的現場:框被佔走,
    `_cc_composer_split` 找不到提示符,於是手機端只說「不在可輸入狀態」。
    """
    lines = list(sess.get("transcript") or [])
    if sess.get("survey"):
        return "\n".join(lines + SURVEY_LINES)
    return "\n".join(lines + [
        "",
        BORDER,
        PROMPT + (sess.get("composer") or ""),
        BORDER,
        "   Claude | 7d 79%",
    ])


# ──────────────────────────── 按鍵 ───────────────────────────────────────

def _press(sess: dict, keys: list, repeat: int) -> None:
    for key in keys:
        for _ in range(repeat):
            if key == "0" and sess.get("survey"):
                # 真 CC 實測(2026-11-13):單鍵 0 關得掉、0+Enter 關不掉。
                sess["survey"] = False
            elif key == "Enter":
                text = (sess.get("composer") or "").strip()
                if text:
                    # 送出 = 字離開輸入框、以回顯的形式出現在上方 transcript。
                    sess["transcript"].append("● " + text)
                    sess["composer"] = ""
            elif key == "C-u":
                sess["composer"] = ""
            elif key == "BSpace":
                sess["composer"] = (sess.get("composer") or "")[:-1]
            elif key == "Escape":
                pass
            else:
                sess["composer"] = (sess.get("composer") or "") + key


# ──────────────────────────── CLI ────────────────────────────────────────

def _opt(argv: list, flag: str):
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


def cli(argv: list) -> int:
    if not argv:
        return 0
    cmd, rest = argv[0], argv[1:]
    path = _state_path()
    state = load(path)
    state.setdefault("log", []).append([cmd] + rest)
    sessions = state.setdefault("sessions", {})
    target = _opt(rest, "-t")
    sess = sessions.get(target)

    rc = 0
    out = ""
    if cmd == "has-session":
        rc = 0 if sess is not None else 1
    elif cmd == "capture-pane":
        if sess is None:
            rc, err = 1, f"can't find session: {target}"
            sys.stderr.write(err + "\n")
        else:
            out = render(sess)
    elif cmd == "send-keys":
        if sess is None:
            rc = 1
        else:
            n = _opt(rest, "-N")
            keys = [a for i, a in enumerate(rest)
                    if not a.startswith("-")
                    and rest[i - 1] not in ("-t", "-N")]
            _press(sess, keys, int(n) if n and n.isdigit() else 1)
    elif cmd == "load-buffer":
        buf = _opt(rest, "-b") or "0"
        state.setdefault("buffers", {})[buf] = sys.stdin.buffer.read().decode(
            "utf-8", "replace")
    elif cmd == "paste-buffer":
        buf = _opt(rest, "-b") or "0"
        data = state.setdefault("buffers", {}).get(buf, "")
        if sess is None:
            rc = 1
        else:
            sess["composer"] = (sess.get("composer") or "") + data
            if "-d" in rest:
                state["buffers"].pop(buf, None)
    elif cmd == "delete-buffer":
        state.setdefault("buffers", {}).pop(_opt(rest, "-b") or "0", None)
    elif cmd == "display-message":
        out = "%1 /dev/ttys999"
    elif cmd == "list-sessions":
        out = "\n".join(sessions)

    save(state, path)
    if out:
        sys.stdout.write(out)
    return rc


if __name__ == "__main__":
    raise SystemExit(cli(sys.argv[1:]))
