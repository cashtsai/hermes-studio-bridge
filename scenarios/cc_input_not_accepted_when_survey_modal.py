"""現象:CC 跳出評分問卷的時候,從 Pocket 送訊息一律失敗。

犯案紀錄 2026-11-13 與 11-15(兩天內兩次):Claude Code 的
「How is Claude doing this session? / 0: Dismiss」問卷**佔走輸入框**,bridge
掃不到 composer,於是 `POST /app/v2/sessions/{id}/input` 回 409
CC_INPUT_NOT_ACCEPTED。使用者在手機上只看到紅字「不在可輸入狀態」,
而且完全不知道那台電腦上有個框在等人按一下鍵 —— session 看起來就是死了。

修復(`4c87829`):貼字前偵測到問卷就送**單鍵 0、不帶 Enter**關掉它,
關完重拍畫面走原流程。

base = `4c87829^`

跑法:
    ~/apps/hermes-agent/venvs/hermes-0.22.2/bin/python \
        scenarios/cc_input_not_accepted_when_survey_modal.py
    scripts/prove-scenario.sh cc_input_not_accepted_when_survey_modal 4c87829^
"""
import _scenario
import os
import unittest

SESSION = "shan-ops"
MESSAGE = "整理今天的進度給我三行"


class SurveyModalBlocksInput(unittest.TestCase):
    """使用者視角:問卷擋在前面,我送出的話有沒有真的進到 CC。"""

    def setUp(self):
        self.tmux = _scenario.FakeTmux()
        self.addCleanup(self.tmux.stop)
        workdir = _scenario.tmpdir("ccwork-")
        # 問卷態的現場:上面有一段對話,下面是問卷,**沒有輸入框**。
        self.tmux.add_session(SESSION, survey=True, transcript=[
            "  ✔ 景君宮白牌支線:BuaBue 皮 + 景君宮品牌/神尊/風格",
            "",
            "● 做完了,要我繼續下一段嗎?",
        ])
        _scenario.register_cc_session(SESSION, workdir)
        _scenario.reset_auth_throttle()
        self.http = _scenario.http_client()
        self.addCleanup(self.http.close)

    def test_message_reaches_cc_while_survey_is_on_screen(self):
        res = self.http.post(
            f"/app/v2/sessions/claude_code:{SESSION}/input",
            headers=_scenario.auth_header(), json={"text": MESSAGE})

        self.assertEqual(
            res.status_code, 200,
            "問卷蓋住輸入框時送出被擋掉(使用者看到紅字『不在可輸入狀態』,"
            f"而且看不到有個框在等按鍵):{res.status_code} {res.text[:400]}")
        body = res.json()
        self.assertIn(body.get("delivery"), ("accepted", "queued"), body)

        # 驗行為不驗回傳值:訊息要真的打進那個 pane。
        keys = self.tmux.keys_sent(SESSION)
        self.assertIn("0", keys,
                      "沒有送出關閉問卷的單鍵 0 —— 問卷還在,框還是被佔著")
        self.assertIn("Enter", keys, "沒有按 Enter,訊息不可能送出去")
        self.assertFalse(self.tmux.session(SESSION)["survey"],
                         "問卷還開著")
        self.assertIn(
            MESSAGE.replace(" ", ""),
            self.tmux.pane(SESSION).replace(" ", "").replace(" ", ""),
            "訊息沒有出現在 CC 畫面上 —— 它被吞掉了")


if __name__ == "__main__":
    _scenario.main()
