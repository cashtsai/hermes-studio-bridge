"""cx 一句多卡(重複泡泡)根治測試 — 2026-08-03 Android cx 回報。

三個病灶,各釘一組:
1. accepted 晚到 race:start_turn await 期間 live userMessage 已出卡,
   accepted 再開一張 → `_cx_feed_input_accepted` 要走 absorb 反向合併。
2. echo 卡第一次合併後 origin 翻成 transcript.echo,同 item 的
   started→completed 重放過不了 merge gate → 另開新卡。
3. thread/turns/list 回補的 item id 是位置型(item-N),live 是 uuid/msg_*,
   id 對不上 → 回補開平行卡(#59 讓回補常駐化後被放大)。

反向保護:使用者刻意重送同文(不同 turn)必須仍是兩顆泡泡;
桌面寫入的 turn(無 live 卡)回補功能不得退化。
"""
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="cx-dup-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carddigest  # noqa: E402


def _user_item(iid: str, text: str) -> dict:
    return {"id": iid, "type": "userMessage",
            "content": [{"type": "text", "text": text}]}


def _agent_item(iid: str, text: str) -> dict:
    return {"id": iid, "type": "agentMessage", "text": text}


def _user_cards(store):
    return [store.cards[c] for c in store.order
            if store.cards.get(c, {}).get("role") == "user"]


def _assistant_cards(store):
    return [store.cards[c] for c in store.order
            if store.cards.get(c, {}).get("role") == "assistant"]


class CxDupScenarioTests(unittest.TestCase):
    def _digest(self):
        return carddigest.CodexThreadDigest()

    def _feed_accepted(self, d, text):
        """比照 bridge._cx_feed_input_accepted(修正後)的餵法。"""
        card = carddigest.make_input_accepted_card("codex", None, text,
                                                   typed_text=text)
        d.store.upsert_card(
            carddigest.absorb_echo_into_accepted(d.store, card))

    def test_full_三來源同句只留一卡(self):
        """實錄場景:accepted → live started/completed → 回補 item-N。"""
        d = self._digest()
        self._feed_accepted(d, "剛剛試了下，無線的判斷好像不行")
        d.handle("turn/started", {"turn": {"id": "turn-A"}})
        item = _user_item("019fc513-uuid", "剛剛試了下，無線的判斷好像不行")
        d.handle("item/started", {"item": item})
        d.handle("item/completed", {"item": item})
        d.seed_turns([{"id": "turn-A",
                       "items": [_user_item("item-382",
                                            "剛剛試了下，無線的判斷好像不行")]}])
        users = _user_cards(d.store)
        self.assertEqual(len(users), 1,
                         [c["id"] for c in users])

    def test_accepted晚到併進live回顯(self):
        d = self._digest()
        d.handle("turn/started", {"turn": {"id": "turn-B"}})
        d.handle("item/completed", {"item": _user_item("uuid-1", "測試訊息")})
        self._feed_accepted(d, "測試訊息")
        users = _user_cards(d.store)
        self.assertEqual(len(users), 1, [c["id"] for c in users])
        # 併進 live 卡的 id,origin 收斂為 transcript.echo
        self.assertEqual(users[0]["id"], "card-cx-uuid-1")
        self.assertEqual(users[0]["body"].get("origin"), "transcript.echo")

    def test_completed重放不再開新卡(self):
        """echo 第一次合併翻 origin 後,同 item 重放仍要收斂回同一張。"""
        d = self._digest()
        self._feed_accepted(d, "同一句話")
        d.handle("turn/started", {"turn": {"id": "turn-C"}})
        item = _user_item("uuid-2", "同一句話")
        d.handle("item/started", {"item": item})
        self.assertEqual(len(_user_cards(d.store)), 1)
        d.handle("item/completed", {"item": item})
        self.assertEqual(len(_user_cards(d.store)), 1,
                         [c["id"] for c in _user_cards(d.store)])

    def test_assistant回補不開平行卡(self):
        d = self._digest()
        d.handle("turn/started", {"turn": {"id": "turn-D"}})
        d.handle("item/completed",
                 {"item": _agent_item("msg_abc", "IP 會動的話,用公網 IP 不穩")})
        d.seed_turns([{"id": "turn-D",
                       "items": [_agent_item("item-383",
                                             "IP 會動的話,用公網 IP 不穩")]}])
        assistants = _assistant_cards(d.store)
        self.assertEqual(len(assistants), 1,
                         [c["id"] for c in assistants])

    def test_刻意重送同文不同turn仍兩卡(self):
        d = self._digest()
        self._feed_accepted(d, "重送這句")
        d.handle("turn/started", {"turn": {"id": "turn-E1"}})
        d.handle("item/completed", {"item": _user_item("uuid-3", "重送這句")})
        d.handle("turn/completed", {"turn": {"id": "turn-E1"}})
        self._feed_accepted(d, "重送這句")
        d.handle("turn/started", {"turn": {"id": "turn-E2"}})
        d.handle("item/completed", {"item": _user_item("uuid-4", "重送這句")})
        users = _user_cards(d.store)
        self.assertEqual(len(users), 2, [c["id"] for c in users])

    def test_桌面turn回補功能不退化(self):
        """desktop 寫入、bridge 沒收過 live 事件的 turn,回補照樣出卡。"""
        d = self._digest()
        d.seed_turns([{"id": "turn-F",
                       "items": [_user_item("item-1", "桌面問的"),
                                 _agent_item("item-2", "桌面答的")]}])
        self.assertEqual(len(_user_cards(d.store)), 1)
        self.assertEqual(len(_assistant_cards(d.store)), 1)

    def test_回補重複輪不灌rev(self):
        """emit_unchanged=False 的重補輪,內容沒變不應重發事件。"""
        d = self._digest()
        turns = [{"id": "turn-G", "items": [_agent_item("item-9", "固定內容")]}]
        d.seed_turns(turns)
        seq_after_first = d.store.seq
        d.seed_turns(turns, emit_unchanged=False)
        self.assertEqual(d.store.seq, seq_after_first)


class BridgeWiringTest(unittest.TestCase):
    """接線層:cc 修過同款 race 但 cx 沒接上,就是這次的病因之一 —
    直接對 bridge._cx_feed_input_accepted 驗,防止再脫鉤。"""

    def test_feed_accepted_absorbs_bare_echo(self):
        import bridge
        tid = "unittest-thread-dup"
        d = bridge._CX_CARD_DIGESTS[tid] = carddigest.CodexThreadDigest()
        try:
            d.handle("turn/started", {"turn": {"id": "turn-W"}})
            d.handle("item/completed",
                     {"item": _user_item("uuid-w", "接線驗證句")})
            bridge._cx_feed_input_accepted(tid, None, "接線驗證句",
                                           typed_text="接線驗證句")
            users = _user_cards(d.store)
            self.assertEqual(len(users), 1, [c["id"] for c in users])
        finally:
            bridge._CX_CARD_DIGESTS.pop(tid, None)


if __name__ == "__main__":
    unittest.main()
