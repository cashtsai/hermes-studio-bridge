"""`_persona_history` 的 chat 作用域(`feat/telegram-sync-setting` 第 2 項)。

機主 2026-10-12:先把多聊天室的坑標起來,**不改行為**。

## 這支在防什麼

`chat_id=None`(預設)把這個 home 底下所有 `source='telegram'` 訊息壓成一條
時間軸。實測主 home 有 33 條 telegram session 但 `COUNT(DISTINCT chat_id)==1`
—— 它們是**同一個聊天室在 session 輪替下**的紀錄,壓平才是對的。
若哪天有人「修」成按 session 分組,現在這條對話會被切成 33 段。

所以這裡釘兩件事:
  1. **預設行為不變** —— 跨多條 session、同一個 chat 的訊息仍然壓成一條
  2. 給了 `chat_id` 才會縮到單一聊天室(多聊天室時要用的那條路,
     而且**是按 chat_id 分,不是按 session 分**)
"""
import _isolation  # noqa: F401
import os
import sqlite3
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="pershist-")
os.environ.setdefault("POCKET_CANON_DB", os.path.join(_TMP, "canonical.db"))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402

HOME = os.path.join(_TMP, "home")


def _seed():
    """兩個聊天室 × 各兩條 session —— 模擬 session 輪替 + 多聊天室。"""
    os.makedirs(HOME, exist_ok=True)
    db = os.path.join(HOME, "state.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, "
                "chat_id TEXT, chat_type TEXT)")
    con.execute("CREATE TABLE messages (session_id TEXT, role TEXT, "
                "content TEXT, timestamp INTEGER)")
    rows = [
        # (session, chat, role, text, ts) —— chat A 跨兩條 session
        ("s1", "chatA", "user", "A-1", 100),
        ("s2", "chatA", "user", "A-2", 300),
        ("s3", "chatB", "user", "B-1", 200),
        ("s4", "chatB", "user", "B-2", 400),
    ]
    seen = set()
    for sid, chat, role, text, ts in rows:
        if sid not in seen:
            con.execute("INSERT INTO sessions VALUES (?,?,?,?)",
                        (sid, "telegram", chat, "private"))
            seen.add(sid)
        con.execute("INSERT INTO messages VALUES (?,?,?,?)", (sid, role, text, ts))
    con.commit()
    con.close()


class PersonaHistoryScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _seed()

    def _texts(self, **kw):
        return [m["content"] for m in bridge._persona_history(HOME, 50, **kw)]

    def test_default_flattens_all_chats_unchanged(self):
        """預設行為**不變** —— 全撈、按時間排序(今天只有一個聊天室,正確)。"""
        self.assertEqual(self._texts(), ["A-1", "B-1", "A-2", "B-2"])

    def test_chat_id_scopes_to_one_chat(self):
        self.assertEqual(self._texts(chat_id="chatA"), ["A-1", "A-2"])
        self.assertEqual(self._texts(chat_id="chatB"), ["B-1", "B-2"])

    def test_scoping_is_by_chat_not_session(self):
        """chatA 跨 s1/s2 兩條 session —— 縮到 chatA 要拿到**兩條都在**。

        這就是那個坑:按 session 分組會把同一個聊天室切碎(主 home 實測
        33 條 session / 1 個 chat_id)。
        """
        self.assertEqual(len(self._texts(chat_id="chatA")), 2)

    def test_unknown_chat_id_returns_empty(self):
        self.assertEqual(self._texts(chat_id="nope"), [])

    def test_missing_db_returns_empty(self):
        self.assertEqual(bridge._persona_history("/no/such/home"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
