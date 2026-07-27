"""Input accepted cards: 200 OK must be durable without duplicating transcript echo."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carddigest as cd  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


store = cd.SessionCardStore()
accepted = cd.make_input_accepted_card(
    "claude_code",
    "cid-123",
    "幫我看一下附件",
    attachments=[{"filename": "photo.jpg", "mime": "image/jpeg"}],
    typed_text="幫我看一下附件 [附件已存到本機:/tmp/photo.jpg]",
)
store.upsert_card(accepted)

echo = {
    "type": "user",
    "message": {"content": "幫我看一下附件 [附件已存到本機:/tmp/photo.jpg]"},
    "timestamp": 100.0,
}
for card in cd.cc_event_to_cards(echo, "jsonl-1"):
    store.upsert_card(cd.merge_input_accepted_echo(store, card))

cards = store.snapshot(limit=20)["cards"]
users = [c for c in cards if c["role"] == "user"]
check("accepted + transcript echo stay one user card", len(users) == 1)
check("client_id survives echo merge", users[0]["body"].get("client_id") == "cid-123")
check("echo upgrades origin", users[0]["body"].get("origin") == "transcript.echo")
check("rev incremented by merge", users[0]["rev"] == 2)

cx = cd.CodexThreadDigest()
cx.store.upsert_card(cd.make_input_accepted_card("codex", "cx-1", "跑測試"))
cx.seed_turns([{"id": "t1", "items": [
    {"id": "u1", "type": "userMessage", "content": [{"type": "text", "text": "跑測試"}]},
]}])
cx_users = [c for c in cx.store.snapshot(limit=20)["cards"] if c["role"] == "user"]
check("codex seed merges accepted user card", len(cx_users) == 1)
check("codex client_id survives", cx_users[0]["body"].get("client_id") == "cx-1")

print()
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
