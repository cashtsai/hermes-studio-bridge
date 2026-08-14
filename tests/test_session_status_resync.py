"""會話狀態要能對帳回來 —— 「CC 一直沒事顯示執行中」的 bridge 半邊。

病理(2026-08-14 定位,app+bridge 雙邊確認):
  1. `set_status` 是**邊緣觸發**(有變才發事件)。
  2. snapshot 舊契約只給 `{cards, latest_seq}`,**不帶 status**;而 client 收到
     快照會把 SSE 續傳游標推到 `latest_seq` —— 那是**全域**事件計數,必然涵蓋
     client 從沒套用過的 `session.status` / `turn` 事件。
  3. 於是「回合結束」那顆狀態事件被游標跳過,SSE 補送時被 client 當重複丟掉,
     bridge 又永遠不會再發第二次 → 會話永遠卡在忙碌。
  4. 本來有一條自癒(新訂閱者 → 強制重發狀態),但 `_last_subs` 只在
     `subscribers > 0` 時記錄,**掉到 0 這件事永遠沒被寫下**,所以 1→0→1 的
     重連根本觸發不了它。

這支測的就是 1/4 的修法:snapshot 帶 status、訂閱數無條件記錄。
"""
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="cx-status-resync-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carddigest  # noqa: E402


def _store():
    return carddigest.SessionCardStore()


class TestSnapshotCarriesStatus(unittest.TestCase):
    def test_snapshot_includes_status(self):
        s = _store()
        s.set_status({"busy": True, "phase": "run", "label": "思考中"})
        snap = s.snapshot()
        self.assertIn("status", snap)
        self.assertEqual(snap["status"]["busy"], True)
        self.assertEqual(snap["status"]["phase"], "run")

    def test_snapshot_status_tracks_latest(self):
        """回合結束後拿快照,要看得到 busy:false —— 這正是被游標跳過的那顆。"""
        s = _store()
        s.set_status({"busy": True, "phase": "run"})
        s.set_status({"busy": False, "phase": "idle"})
        self.assertEqual(s.snapshot()["status"]["busy"], False)

    def test_snapshot_status_empty_when_never_polled(self):
        """沒巡邏過 → 空 dict(client 要當『我沒資訊』,不可當閒置)。"""
        self.assertEqual(_store().snapshot()["status"], {})

    def test_snapshot_status_is_a_copy(self):
        """回傳的 status 不能是內部物件的參照,否則呼叫端一改就污染 store,
        `set_status` 的相等比較會失準、之後真的變了反而不發事件。"""
        s = _store()
        s.set_status({"busy": True})
        snap = s.snapshot()
        snap["status"]["busy"] = False
        self.assertEqual(s.status["busy"], True)

    def test_latest_seq_still_present(self):
        s = _store()
        s.set_status({"busy": True})
        self.assertIsInstance(_store().snapshot()["latest_seq"], int)
        self.assertIn("cards", s.snapshot())


class TestSetStatusEdgeTriggered(unittest.TestCase):
    """既有行為的護欄:確認「邊緣觸發」這個前提沒被本次改動動到 ——
    正因為它邊緣觸發,快照才**必須**帶 status。"""

    def test_repeat_status_pushes_nothing(self):
        s = _store()
        self.assertIsNotNone(s.set_status({"busy": True}))
        self.assertIsNone(s.set_status({"busy": True}))

    def test_changed_status_pushes(self):
        s = _store()
        s.set_status({"busy": True})
        self.assertIsNotNone(s.set_status({"busy": False}))

    def test_status_none_forces_next_push(self):
        """自癒手法:把 status 設回 None → 下一次同值也會重發。"""
        s = _store()
        s.set_status({"busy": True})
        s.status = None
        self.assertIsNotNone(s.set_status({"busy": True}))


class TestSubscriberDropIsRecorded(unittest.TestCase):
    """`_last_subs` 的修法用純邏輯重現(不拉起整個 follower 迴圈)。

    舊寫法把記錄放在 `if subscribers > 0:` 裡面 → 掉到 0 沒被記,
    1→0→1 回來時 `1 > 1` 為假,自癒不觸發。
    """

    @staticmethod
    def _old(seq):
        store = {"subs": 0, "_last": 0}
        fired = []
        for n in seq:
            store["subs"] = n
            if store["subs"] > 0:                       # ← 舊:記錄在條件內
                if store["subs"] > store["_last"]:
                    fired.append(n)
                store["_last"] = store["subs"]
        return fired

    @staticmethod
    def _new(seq):
        store = {"subs": 0, "_last": 0}
        fired = []
        for n in seq:
            store["subs"] = n
            was = store["_last"]
            store["_last"] = store["subs"]              # ← 新:無條件記錄
            if store["subs"] > 0:
                if store["subs"] > was:
                    fired.append(n)
        return fired

    def test_old_behaviour_misses_the_reconnect(self):
        # 1 → 0 → 1:重連回來應該要重發,舊寫法沒有。
        self.assertEqual(self._old([1, 0, 1]), [1])

    def test_new_behaviour_fires_on_reconnect(self):
        self.assertEqual(self._new([1, 0, 1]), [1, 1])

    def test_new_behaviour_still_quiet_while_steady(self):
        """訂閱數沒變就不要一直重發(否則每一輪巡邏都在灌 ring)。"""
        self.assertEqual(self._new([1, 1, 1]), [1])

    def test_new_behaviour_no_fire_on_drop(self):
        self.assertEqual(self._new([1, 0]), [1])


if __name__ == "__main__":
    unittest.main()
