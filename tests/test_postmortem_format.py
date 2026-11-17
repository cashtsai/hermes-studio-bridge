"""`docs/POSTMORTEMS/` 的格式閘。

## 為什麼格式要有閘門

事後檢討制度最常見的死法不是「沒人寫」,是**寫成流水帳**:
章節缺一半、「防線為什麼沒攔到」只列兩道就跳過、
`Guardrails added` 寫成「下次注意」。那種文件會讓下一個人學會不讀它,
然後制度就只剩一個資料夾。

所以照 deepseek-harness 的作法(研究報告 §5.6 的
`scripts/verify-agent-note-format.ts`),把**結構**做成機械閘:

  · 章節齊不齊、順序對不對、`Status:` 行在不在 —— 機器驗;
  · 「防線為什麼沒攔到」有沒有逐道列 —— 機器數得出條數;
  · `Guardrails added` 的每一條有沒有指到**已 commit 的東西**
    (路徑或 commit sha)—— 機器認得出來;
  · 內容誠不誠實 —— 機器驗不了,所以 CONTRIBUTING 規定了 review 要看什麼。

格式規範本體在 `docs/POSTMORTEMS/README.md`,模板在 `TEMPLATE.md`。
這一檔是那兩份文件的執法面 —— **文件與閘門同一份真相**:
章節清單在這裡是常數,`test_template_matches_the_required_sections` 確認
模板沒有漂走。
"""
import _isolation  # noqa: F401  # 測試隔離閂:必須是第一個 import
import os
import re
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

from sentinel import corpus_sentinel  # noqa: E402

DIR = os.path.join(_ROOT, "docs", "POSTMORTEMS")

#: 必要章節,**順序就是這個順序**。
#: 與 docs/POSTMORTEMS/README.md 的表格、TEMPLATE.md 的骨架是同一份真相。
REQUIRED_SECTIONS = (
    "## Executive summary",
    "## Impact",
    "## Timeline",
    "## Why every safety net missed it",
    "## Root cause",
    "## Guardrails added",
    "## Lessons",
)

#: 向前看的 spec 語言。postmortem 是向後看的記錄,要提案就另開文件。
#: (抄 deepseek 的 `implemented/` 禁 spec-speak:
#:  「A decision recorded without what it beat invites re-litigation」的反面 ——
#:  一份摻了提案的事後檢討,會讓人分不清哪一段是已發生的事實。)
BANNED_SECTIONS = ("## Proposal", "## Plan", "## Migration plan",
                   "## Acceptance criteria", "## Next steps")

#: `Status:` 只准這三個值。`Mitigated` 的意思是「有閘門了但還有已知殘留」,
#: 殘留寫在 `### 還沒做的`。
ALLOWED_STATUS = ("Resolved", "Mitigated", "Open")

#: 至少要對這幾道防線各表態一次。不適用也要寫出「為什麼結構上碰不到」——
#: 那一句本身就是情報(0002 的「開放集合沒辦法掃」就是這樣被寫出來的)。
MIN_SAFETY_NETS = 6

#: 留在 postmortem 裡的待辦標記 = 這份文件還沒寫完。
#: 事後檢討是**已經發生過的事**的記錄,沒有「待補」的欄位。
TODO_MARKS = ("TODO", "TBD", "FIXME", "XXX", "待補", "待填", "<一句", "<填")

_NAME_RE = re.compile(r"^(\d{4})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
#: Guardrails 的每一條都要有證據:反引號路徑、或 7+ 位 hex 的 commit sha。
_EVIDENCE_RE = re.compile(r"`[^`]*[./][^`]*`|`[0-9a-f]{7,40}`|\(`[0-9a-f]{7,40}`\)")
#: 「逐道列」的形狀:`- **<防線>** — <為什麼沒攔到>`
_NET_RE = re.compile(r"^\s*-\s+\*\*.+?\*\*\s+—")


def postmortems() -> list:
    """`NNNN-*.md` 的清單(排除 README / TEMPLATE)。

    語料下限哨兵:這一檔全部的斷言都是「對每一份 postmortem 檢查 X」,
    所以 **glob 掃不到檔的時候它會完全綠** —— 一個 0 份文件的格式閘
    會很高興地報告「全部合格」。編號不回收、不刪檔,所以下限就是現值。
    """
    names = sorted(n for n in os.listdir(DIR) if _NAME_RE.match(n))
    corpus_sentinel.require("postmortems.files", len(names),
                            detail="(docs/POSTMORTEMS/ 下的 NNNN-*.md。"
                                   "編號不回收也不刪檔,所以這個數字只會長大 ——"
                                   "變少代表檔名規則改了或檔被搬走)")
    return names


def read(name: str) -> str:
    with open(os.path.join(DIR, name), encoding="utf-8") as f:
        return f.read()


class StructureTest(unittest.TestCase):
    """骨架:標題、Status、章節齊全且順序正確。"""

    def test_title_line_matches_the_filename_number(self):
        for name in postmortems():
            lines = read(name).splitlines()
            num = _NAME_RE.match(name).group(1)
            self.assertRegex(
                lines[0], rf"^# Postmortem {num}: \S",
                f"{name} 第一行要是 `# Postmortem {num}: <一句話>`")
            self.assertEqual(lines[1].strip(), "",
                             f"{name} 第二行要空白(Status 在第三行)")

    def test_status_line_is_third_and_declares_a_known_state(self):
        for name in postmortems():
            lines = read(name).splitlines()
            self.assertTrue(
                lines[2].startswith("Status: "),
                f"{name} 第三行要是 `Status: <{'/'.join(ALLOWED_STATUS)}> — <一句>`")
            state = lines[2][len("Status: "):].split()[0].strip("—-,。")
            self.assertIn(state, ALLOWED_STATUS,
                          f"{name} 的 Status `{state}` 不在允許值裡")
            self.assertIn("—", lines[2],
                          f"{name} 的 Status 要附一句話說明現在是什麼狀態 —— "
                          "光一個字沒有資訊")

    def test_required_sections_exist_in_order(self):
        for name in postmortems():
            text = read(name)
            positions = []
            for section in REQUIRED_SECTIONS:
                idx = text.find(f"\n{section}\n")
                self.assertGreater(
                    idx, 0, f"{name} 缺章節 `{section}` —— "
                            "照 docs/POSTMORTEMS/TEMPLATE.md 逐欄填,不准跳")
                positions.append(idx)
            self.assertEqual(positions, sorted(positions),
                             f"{name} 的章節順序與 TEMPLATE.md 不一致")

    def test_no_forward_looking_spec_sections(self):
        for name in postmortems():
            text = read(name)
            found = [s for s in BANNED_SECTIONS if f"\n{s}\n" in text]
            self.assertEqual(
                found, [],
                f"{name} 出現向前看的 spec 章節 {found}。postmortem 是**向後看**的"
                "記錄;要提案就另開文件,從這裡連過去"
                "(docs/POSTMORTEMS/README.md「這不是 Agent Note、也不是 spec」)")

    def test_no_unfinished_markers(self):
        for name in postmortems():
            text = read(name)
            found = [m for m in TODO_MARKS if m in text]
            self.assertEqual(
                found, [],
                f"{name} 還留著待辦標記 {found} —— 事後檢討是已經發生過的事的"
                "記錄,沒有「待補」這一欄。真的有還沒做的閘門,"
                "寫進 `### 還沒做的` 並把 Status 改成 Mitigated")


class SafetyNetsAreEnumeratedTest(unittest.TestCase):
    """**本檔最重要的一組**:「防線為什麼沒攔到」必須逐道列,不准跳過。

    這一節是 postmortem 裡唯一無法用別的東西代替的部分 ——
    bug 的機制會過期,「五道防線為什麼同時沒出聲」不會。
    """

    def _section(self, text: str, heading: str) -> str:
        start = text.index(f"\n{heading}\n") + len(heading) + 2
        nxt = text.find("\n## ", start)
        return text[start:nxt if nxt > 0 else len(text)]

    @staticmethod
    def _bullets(body: str) -> list:
        """逐道防線的條目,**含換行續接的部分**。

        一條防線的理由常常兩三行(這是好事),所以不能一行一行判 ——
        只看第一行會把「換行處剛好很短」誤判成「寫得太短」。
        """
        bullets, current = [], None
        for line in body.splitlines():
            if _NET_RE.match(line):
                if current is not None:
                    bullets.append(current)
                current = line
            elif current is not None:
                if not line.strip():
                    bullets.append(current)
                    current = None
                else:
                    current += " " + line.strip()
        if current is not None:
            bullets.append(current)
        return bullets

    def test_each_safety_net_gets_its_own_line(self):
        for name in postmortems():
            body = self._section(read(name), "## Why every safety net missed it")
            nets = self._bullets(body)
            self.assertGreaterEqual(
                len(nets), MIN_SAFETY_NETS,
                f"{name} 的「防線為什麼沒攔到」只列了 {len(nets)} 道"
                f"(至少 {MIN_SAFETY_NETS})。形狀是 `- **<防線>** — <為什麼>`。\n"
                "  → 不適用的那道**也要寫**:「為什麼這道結構上碰不到」"
                "那一句本身就是情報。\n"
                "  → 清單見 docs/POSTMORTEMS/TEMPLATE.md 的規則一")

    def test_each_enumerated_net_actually_explains_itself(self):
        """每一道後面要有實質內容 —— 「- **單元測試** — 沒攔到」不算答案。"""
        for name in postmortems():
            body = self._section(read(name), "## Why every safety net missed it")
            thin = []
            for bullet in self._bullets(body):
                reason = bullet.split("—", 1)[1].strip()
                if len(reason) < 40:
                    thin.append(bullet.strip()[:60])
            self.assertEqual(thin, [], f"{name} 這幾道防線的理由太短:{thin}")

    def test_scenarios_and_scan_gates_are_both_addressed(self):
        """兩道最容易被跳過的防線必須出現:scenario 與掃描式守門。

        它們正是本 repo 用來守「接縫」的兩套制度(見 0003 的共同結論),
        所以每一份 postmortem 都該對它們表態。
        """
        for name in postmortems():
            body = self._section(read(name), "## Why every safety net missed it")
            self.assertIn("cenario", body, f"{name} 沒有對 scenario 這道防線表態")
            self.assertRegex(body, r"掃描式|登記表|對齊矩陣",
                             f"{name} 沒有對掃描式守門這道防線表態")


class GuardrailsAreCommittedGatesTest(unittest.TestCase):
    """`Guardrails added` 必須是閘門,不是感想。"""

    def _section(self, text: str) -> str:
        start = text.index("\n## Guardrails added\n") + len("## Guardrails added") + 2
        nxt = text.find("\n## ", start)
        return text[start:nxt if nxt > 0 else len(text)]

    def test_every_guardrail_cites_something_committed(self):
        """每一條編號項都要有反引號路徑或 commit sha。

        判準很硬,因為「下次注意」是這一節唯一的失敗模式。
        """
        for name in postmortems():
            body = self._section(read(name))
            items, current = [], None
            for line in body.splitlines():
                if re.match(r"^\s*\d+\.\s+\*\*", line):
                    if current:
                        items.append(current)
                    current = line
                elif current is not None and line.strip() and not line.startswith("###"):
                    current += "\n" + line
                elif line.startswith("###"):
                    break
            if current:
                items.append(current)
            self.assertTrue(items, f"{name} 的 Guardrails added 沒有任何編號條目")
            naked = [it.splitlines()[0].strip()[:70] for it in items
                     if not _EVIDENCE_RE.search(it)]
            self.assertEqual(
                naked, [],
                f"{name} 這幾條 guardrail 沒有指到任何已 commit 的東西"
                f"(路徑或 commit sha):{naked}。\n"
                "  → 判準:「把修復 revert 掉,這條會紅嗎?」答不出「會」"
                "就不是 guardrail,刪掉它。\n"
                "  → 「下次注意」「加進 backlog」一律不算"
                "(docs/POSTMORTEMS/README.md 規則二)")

    def test_soft_promises_are_not_allowed_in_guardrails(self):
        soft = ("下次注意", "以後記得", "加進 backlog", "會更小心", "提醒自己")
        for name in postmortems():
            body = self._section(read(name))
            found = [s for s in soft if s in body]
            self.assertEqual(found, [],
                             f"{name} 的 Guardrails added 出現軟性承諾 {found} —— "
                             "那不是閘門。要留這句話就搬到 Lessons")

    def test_mitigated_status_needs_an_open_items_subsection(self):
        """Status 是 Mitigated 就必須有 `### 還沒做的`,反之亦然要說得通。"""
        for name in postmortems():
            text = read(name)
            status = text.splitlines()[2]
            if status.startswith("Status: Mitigated"):
                self.assertIn("### 還沒做的", text,
                              f"{name} 標 Mitigated 卻沒寫還沒做什麼")


class DocsAndGateShareOneTruthTest(unittest.TestCase):
    """文件與閘門不准漂移 —— 一份真相。"""

    def test_template_matches_the_required_sections(self):
        template = read("TEMPLATE.md")
        for section in REQUIRED_SECTIONS:
            self.assertIn(f"\n{section}\n", template,
                          f"TEMPLATE.md 少了 `{section}` —— "
                          "照模板填的人會直接缺那一節")
        positions = [template.index(f"\n{s}\n") for s in REQUIRED_SECTIONS]
        self.assertEqual(positions, sorted(positions),
                         "TEMPLATE.md 的章節順序與閘門不一致")

    def test_readme_states_the_three_qualifying_conditions(self):
        readme = read("README.md")
        for word in ("Subtle", "Systemic", "Costly"):
            self.assertIn(word, readme,
                          f"README.md 少了合格條件 `{word}` —— "
                          "三條缺一條,postmortem 就會氾濫成流水帳")
        self.assertIn("同時", readme, "README.md 要寫明三條必須**同時**成立")

    def test_readme_index_lists_every_postmortem(self):
        readme = read("README.md")
        for name in postmortems():
            self.assertIn(f"]({name})", readme,
                          f"{name} 沒進 README.md 的索引表 —— "
                          "沒被索引的 postmortem 等於不存在")

    def test_contributing_points_at_the_process(self):
        with open(os.path.join(_ROOT, "CONTRIBUTING.md"), encoding="utf-8") as f:
            contributing = f.read()
        self.assertIn("docs/POSTMORTEMS", contributing,
                      "CONTRIBUTING.md 要有一節講什麼時候寫 postmortem、"
                      "誰寫、review 要檢查什麼")


if __name__ == "__main__":
    unittest.main(verbosity=2)
