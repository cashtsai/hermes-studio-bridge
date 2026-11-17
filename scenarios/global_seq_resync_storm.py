"""現象:一進 session 就卡死在「連線中斷,正在補接」,訊息永遠不出來。

機主 2026-11-10 實錘。根因在 Console(static/console/app.js):per-session
事件流信封上的 `seq` 是**全域** event_log 序號 —— 別的 session 的事件會在中間
佔號,所以本 lane 看到的序號**天生有洞**。當時的 client 寫成「不連號就
resync」+「keepalive ping 的尖端對不上就 resync」,於是:看到洞 → 重同步 →
重同步後又看到洞 → 永遠在補接,畫面上那行紅字永遠不會消失,卡片一張都進不來。

修復(`1cc59e5`):只要序號往前就收(洞是正常的,真正的斷層由 410/resync
協定負責);ping 僅推進重連游標,絕不觸發重同步。

base = `1cc59e5^`

## 序號是真的,不是編的

「有洞」這個前提本身必須是真的,否則整個 scenario 在描述一個不存在的世界。
所以 python 這半邊先用**產品自己的 API** 建兩條 CC session、交錯送訊息,然後
從 `GET /app/v2/sessions/{id}/cards` 讀回各自的 `durable_seq`(= 全域 event_log
id)。那串數字就是餵給 Console 的序號;scenario 會先斷言它**真的有洞**,
沒洞就直接報錯(前提沒了的話,綠色是假的)。

JS 那半邊用 `node --test` + `vm` 把**出貨中的** app.js 裡那段 `subscribeCards`
原始碼切出來跑(同 tests/console_delivery_sort.test.cjs 的既有作法)——
驗的是真的那一份,所以在舊 commit 上會驗到舊的那份邏輯。

跑法:
    scripts/prove-scenario.sh global_seq_resync_storm 1cc59e5^
"""
import _scenario
import json
import os
import shutil
import subprocess
import unittest

LANES = ("lane-a", "lane-b")
ROUNDS = 3


class ConsoleResyncStorm(unittest.TestCase):
    """使用者視角:進 session 看得到訊息,還是卡在「正在補接」。"""

    def setUp(self):
        self.node = shutil.which("node")
        if not self.node:
            raise unittest.SkipTest("這個 scenario 要用 node 跑出貨中的 app.js")
        self.tmux = _scenario.FakeTmux()
        self.addCleanup(self.tmux.stop)
        for name in LANES:
            self.tmux.add_session(name)
            _scenario.register_cc_session(name, _scenario.tmpdir("lane-"))
        _scenario.reset_auth_throttle()
        self.http = _scenario.http_client()
        self.addCleanup(self.http.close)
        self.work = _scenario.tmpdir("seqstorm-")

    def _real_global_seqs(self) -> dict:
        """打產品的 API 讓它自己產生交錯的全域序號,讀回來。"""
        seqs: list[int] = []
        tip = 0
        for i in range(ROUNDS):
            for name in LANES:
                res = self.http.post(
                    f"/app/v2/sessions/claude_code:{name}/input",
                    headers=_scenario.auth_header(),
                    json={"text": f"第{i}則給{name}"})
                self.assertEqual(res.status_code, 200, res.text[:300])
                snap = self.http.get(
                    f"/app/v2/sessions/claude_code:{name}/cards",
                    headers=_scenario.auth_header())
                self.assertEqual(snap.status_code, 200, snap.text[:300])
                durable = int(snap.json().get("durable_seq") or 0)
                tip = max(tip, durable)
                if name == LANES[0]:
                    seqs.append(durable)
        return {"seqs": seqs, "tip": tip + 1}

    def test_gappy_global_seq_does_not_storm_resync(self):
        lane = self._real_global_seqs()
        # 前提查核:序號必須真的不連號。這不是待驗的行為,是 scenario 成立的
        # 條件 —— 用 raise(→ SCENARIO_ERROR / 離開碼 2)而不是 assert,
        # 免得「前提消失」被誤讀成「bug 重現」。
        if len(lane["seqs"]) < 2 or all(
                b - a == 1 for a, b in zip(lane["seqs"], lane["seqs"][1:])):
            raise RuntimeError(
                "產品產出的全域序號沒有洞,這個 scenario 的前提不成立了:"
                f"{lane['seqs']}")

        seq_file = os.path.join(self.work, "lane.json")
        with open(seq_file, "w", encoding="utf-8") as f:
            json.dump(lane, f)

        env = dict(os.environ)
        env["SCENARIO_APP_JS"] = os.path.join(
            _scenario.REPO, "static", "console", "app.js")
        env["SCENARIO_SEQ_FILE"] = seq_file
        proc = subprocess.run(
            [self.node, "--test",
             os.path.join(_scenario.REPO, "scenarios",
                          "global_seq_resync_storm.test.cjs")],
            capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(
            proc.returncode, 0,
            "出貨中的 Console 把全域序號當連號驗 —— 進 session 卡死在"
            f"「連線中斷,正在補接」。本 lane 的真實序號 {lane['seqs']}\n"
            + proc.stdout[-3000:] + proc.stderr[-1500:])


if __name__ == "__main__":
    _scenario.main()
