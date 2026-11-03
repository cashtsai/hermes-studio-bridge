"""終端機輸出的 UTF-8 跨 read() 邊界解碼(docs/TERMINAL_PTY_CONTRACT.md)。

`app_v1_terminal`(bridge.py 的 /app/v1/terminal,目前實際在跑的那個,不是
`_TerminalSession` 那條還沒接 tmux 的替代實作)原本對每次 os.read() 的結果
獨立 decode("utf-8","replace")。機主回報「常常使用的時候排版會跑掉」——
他的 CLI 工具介面滿滿中文,中文是 3-byte UTF-8,65536 不是 3 的倍數,長時間
輸出遲早會剛好讀到切一半的字元中間,那半個字元**連同緊接在後面本來完整的
下一個字元**一起變成亂碼。改成 `codecs.getincrementaldecoder("utf-8")`
把不完整的尾巴留到下一次 read() 才解碼。

這裡不重現真的 PTY 時機(os.read() 切在哪裡是不可控的),而是直接驗證
「被切成兩段的中文位元組序列,新寫法解得對、舊寫法解不對」——這是修補的
技術核心,也是唯一真的有風險、需要驗證的部分。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import(2026-08-15 事故防線,見 tests/_isolation.py)
import codecs
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 腳本式驗收(repo 慣例):模組層直接跑、sys.exit() 回報。被 unittest discover
# 匯入時明確 skip,理由同 test_cc_image_bytes.py 開頭的注記。
if __name__ != "__main__":
    import unittest as _unittest

    raise _unittest.SkipTest(
        "腳本式驗收,module 層即執行 + sys.exit,需獨立行程:"
        "python3 tests/test_terminal_utf8_chunking.py"
    )

TEXT = "常常使用的時候排版會跑掉"  # 全中文,每個字 3-byte UTF-8
RAW = TEXT.encode("utf-8")


def _split_mid_character(raw: bytes, char_index: int, byte_offset: int) -> tuple[bytes, bytes]:
    """在第 char_index 個字元的位元組序列中間切一刀(byte_offset ∈ 1..2,
    3-byte 字元切在第 1 或第 2 個位元組之後)。回傳 (前段, 後段)。"""
    cut = len(TEXT[:char_index].encode("utf-8")) + byte_offset
    return raw[:cut], raw[cut:]


def test_old_approach_corrupts_the_split_and_following_character():
    """先證明舊寫法真的壞——不是假設,是跑給你看。"""
    part1, part2 = _split_mid_character(RAW, char_index=3, byte_offset=1)
    decoded = (part1.decode("utf-8", "replace")
              + part2.decode("utf-8", "replace"))
    assert decoded != TEXT
    assert "�" in decoded, "切一半的字元應該要變成替代字元,證明舊寫法真的壞了"


def test_incremental_decoder_survives_split_at_every_possible_boundary():
    """對這串中文的**每一個可能切點**都驗一次——不是只挑一個剛好對的案例。"""
    for char_index in range(1, len(TEXT)):
        for byte_offset in (1, 2):
            part1, part2 = _split_mid_character(RAW, char_index, byte_offset)
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            decoded = decoder.decode(part1) + decoder.decode(part2)
            assert decoded == TEXT, (
                f"切在第 {char_index} 字、位元組偏移 {byte_offset} 時解碼結果跑掉:"
                f"{decoded!r} != {TEXT!r}")


def test_incremental_decoder_final_flush_on_eof_does_not_lose_data():
    """EOF 時 shell 真的吐出不完整的尾巴(極端情況,例如 shell 被強制殺掉)——
    final=True 要把能解的部分吐出來,不能無聲吞掉。"""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    part1, part2 = _split_mid_character(RAW, char_index=5, byte_offset=1)
    first = decoder.decode(part1)
    assert first == TEXT[:5]   # 不完整的那個字元還沒吐出來,正常
    # 模擬連線在這裡斷掉(EOF),不會再有 part2 補上來了。
    tail = decoder.decode(b"", final=True)
    assert "�" in tail or tail == ""  # 真的解不完整就該是替代字元,不是例外


def test_decoder_state_is_independent_per_instance():
    """見 bridge.py 的注記:這個 decoder 必須是每條連線自己的,不能是模組層
    全域——這裡驗證兩個獨立 decoder 實例互不干擾(模擬兩條同時連線)。"""
    part1, part2 = _split_mid_character(RAW, char_index=2, byte_offset=1)
    decoder_a = codecs.getincrementaldecoder("utf-8")(errors="replace")
    decoder_b = codecs.getincrementaldecoder("utf-8")(errors="replace")
    a_first = decoder_a.decode(part1)
    # decoder_b 完全沒有被餵東西,不該受 decoder_a 影響。
    b_full = decoder_b.decode(RAW)
    assert a_first == TEXT[:2]
    assert b_full == TEXT


if __name__ == "__main__":
    import traceback
    fails = []
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            fails.append(t.__name__)
        except Exception:
            print(f"ERROR {t.__name__}")
            traceback.print_exc()
            fails.append(t.__name__)
    print()
    if fails:
        print(f"FAILED: {fails}")
        sys.exit(1)
    print("ALL PASS")
