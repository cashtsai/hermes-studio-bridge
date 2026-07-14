from bridge import _codex_stream_turn_finished


def test_idle_follow_stream_stays_open_before_turn_activity():
    assert not _codex_stream_turn_finished(False, False, 4, 4)


def test_follow_stream_finishes_after_terminal_turn_is_drained():
    assert not _codex_stream_turn_finished(True, True, 4, 4)
    assert not _codex_stream_turn_finished(True, False, 3, 4)
    assert _codex_stream_turn_finished(True, False, 4, 4)
