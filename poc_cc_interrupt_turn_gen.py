#!/usr/bin/env python3
"""POC: 驗證 _cc_interrupt_core 的 turn-generation 防護邏輯。

純邏輯模擬,不碰 tmux/正式服務。模擬三個情境:
  A) 正常情況:世代編號在整個 interrupt 過程中不變 → 應該正常送 Escape 並判定
     interrupted=True(pane 在送出後回報不忙碌)。
  B) Race 情況(本次要修的根因):世代編號在「送出 Escape 之後、驗證忙碌狀態
     之前」被 UserPromptSubmit hook 遞增(代表新 turn 已經搶先開始了)→ 修復
     後應該偵測到 stale_turn=True 並停止繼續送下一次 Escape,不把這次結果誤判
     成「成功打中原 turn」。
  C) 已經 idle 情況(連按兩次停止鍵的第二次):hook 新鮮資料且 busy=False →
     完全不送 Escape,直接回報「沒有需要中斷的 turn」。

跑法: python3 poc_cc_interrupt_turn_gen.py
"""
import asyncio


class FakeWorld:
    """模擬 _CC_TURN_GEN / _CC_HOOK_STATE / tmux send-keys / pane 忙碌狀態。"""

    def __init__(self):
        self.gen = {}
        self.hook_state = {}
        self.escape_sent_count = 0
        # 事件腳本:在第 N 次 send-keys 呼叫之後觸發的動作(例如遞增世代)
        self.on_escape_sent = {}   # escape_index(1-based) -> callable
        self.pane_busy_sequence = []  # 每次驗證時回傳的忙碌與否(依序取用)

    def bump_gen(self, name):
        self.gen[name] = self.gen.get(name, 0) + 1

    async def send_keys_escape(self, name):
        self.escape_sent_count += 1
        idx = self.escape_sent_count
        cb = self.on_escape_sent.get(idx)
        if cb:
            cb()
        return 0, "", ""

    def pane_busy_now(self):
        if self.pane_busy_sequence:
            return self.pane_busy_sequence.pop(0)
        return False

    def fresh_hook_state(self, name):
        return self.hook_state.get(name)


async def cc_interrupt_core_sim(world: "FakeWorld", name: str) -> dict:
    """簡化版 _cc_interrupt_core,搬用修復後同樣的世代比對邏輯（不含 tmux I/O）。"""
    fresh = world.fresh_hook_state(name)
    if fresh is not None and fresh.get("busy") is False:
        return {"ok": True, "interrupted": True, "attempts": 0,
                "reason": "already_idle"}

    gen0 = world.gen.get(name, 0)
    attempts = 0
    interrupted = False
    stale = False
    for _ in range(3):
        if world.gen.get(name, 0) != gen0:
            stale = True
            break
        attempts += 1
        rc, _, _ = await world.send_keys_escape(name)
        assert rc == 0
        # 模擬 asyncio.sleep(0.7) 讓出控制權,期間 hook 事件可能觸發世代遞增
        await asyncio.sleep(0)
        if world.gen.get(name, 0) != gen0:
            stale = True
            break
        if not world.pane_busy_now():
            interrupted = True
            break
    return {"ok": True, "interrupted": interrupted, "attempts": attempts,
            "stale_turn": stale}


async def scenario_a_normal():
    """A) 正常情況:無 race,第一次 Escape 就打中,世代全程不變。"""
    world = FakeWorld()
    name = "sess-a"
    world.pane_busy_sequence = [False]   # 送出後立刻確認不忙碌
    res = await cc_interrupt_core_sim(world, name)
    assert res["interrupted"] is True
    assert res["stale_turn"] is False
    assert res["attempts"] == 1
    print("A) 正常情況 PASS:", res)


async def scenario_b_race_after_send():
    """B) Race:Escape 送出後、驗證忙碌狀態前,新 turn 搶先開始(世代 +1)。
    修復前的行為：仍然會去看 pane_busy_now()，若剛好也是 False 就誤判
    interrupted=True —— 但那個「不忙碌」量到的其實是新 turn 剛起步瞬間的假象,
    不能信任。修復後：偵測到世代已變就直接標記 stale_turn=True 並停止,
    不再去看 pane、也不再送下一次 Escape。"""
    world = FakeWorld()
    name = "sess-b"

    def new_turn_started():
        # 模擬：使用者的下一條訊息在我們送出 Escape 之後、還沒驗證忙碌狀態前
        # 就被送出去了，觸發 UserPromptSubmit hook，世代遞增。
        world.bump_gen(name)

    world.on_escape_sent[1] = new_turn_started
    # 就算 pane 隨後量到「不忙碌」，也不該被採信（新 turn 剛開始很可能還沒進
    # 入忙碌畫面），所以刻意讓它回 False 來測試修復是否仍正確地不採信它。
    world.pane_busy_sequence = [False]

    res = await cc_interrupt_core_sim(world, name)
    assert res["stale_turn"] is True, f"應偵測到 stale turn，結果卻是 {res}"
    assert res["interrupted"] is False, f"不該採信 race 後的 pane 讀數，結果卻是 {res}"
    assert res["attempts"] == 1, f"不該再送第二次 Escape，結果卻是 {res}"
    print("B) race-after-send 情況 PASS（修復生效，未誤判/未送出多餘 Escape):", res)


async def scenario_b2_race_before_first_send():
    """B2) 更早的 race：呼叫 _cc_interrupt_core 當下記下的 gen0 之後、送出第一
    次 Escape 之前，世代就已經又變了（模擬 interrupt request 在事件迴圈裡排
    隊等了一陣子，新 turn 已經搶先開始）。修復後：連第一次 Escape 都不該送。
    這裡用一個會在特定第 N 次讀取後自動跳號的 dict 子類，驅動真正的
    cc_interrupt_core_sim 執行路徑來驗證行為，而不只是檢查前置條件。"""

    class AutoBumpGenDict(dict):
        """第 bump_after_nth_get 次 .get() 被呼叫「之後」，自動把該 key 的值
        +1 —— 模擬世代在那個時間點被 hook 事件搶先遞增。"""
        def __init__(self, bump_after_nth_get: int):
            super().__init__()
            self._n = 0
            self._bump_after = bump_after_nth_get

        def get(self, key, default=None):
            self._n += 1
            val = super().get(key, default if default is not None else 0)
            if self._n == self._bump_after:
                self[key] = val + 1
            return val

    world = FakeWorld()
    name = "sess-b2"
    # 第 1 次 get 是 gen0 = world.gen.get(name, 0)；第 2 次 get 是迴圈內第一次
    # 檢查（送出 Escape 之前）。讓世代在第 1 次讀取「之後」立刻跳號，模擬搶跑
    # 剛好卡在函式一進來、迴圈檢查前的瞬間。
    world.gen = AutoBumpGenDict(bump_after_nth_get=1)

    res = await cc_interrupt_core_sim(world, name)
    assert res["stale_turn"] is True, f"應在送出第一次 Escape 前就偵測到 stale，結果卻是 {res}"
    assert world.escape_sent_count == 0, f"不該送出任何 Escape，結果卻送了 {world.escape_sent_count} 次"
    print("B2) race-before-first-send PASS（0 次 Escape，正確判定 stale_turn):", res)


async def scenario_c_already_idle():
    """C) 連按兩次停止鍵：第一次已成功中斷並同步 hook busy=False（沿用前一個
    P0 修復），第二次呼叫 interrupt 時應該直接判斷「沒有活躍 turn」，完全不送
    Escape（送出次數應為 0）。"""
    import time
    world = FakeWorld()
    name = "sess-c"
    world.hook_state[name] = {"busy": False, "updated_at": time.time(),
                               "source": "interrupt"}
    res = await cc_interrupt_core_sim(world, name)
    assert res["interrupted"] is True
    assert res["attempts"] == 0
    assert world.escape_sent_count == 0, "已 idle 時不該送任何 Escape"
    print("C) 已 idle（連按兩次停止鍵）情況 PASS（0 次 Escape）:", res)


async def main():
    await scenario_a_normal()
    await scenario_b_race_after_send()
    await scenario_b2_race_before_first_send()
    await scenario_c_already_idle()
    print("\nALL POC SCENARIOS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
