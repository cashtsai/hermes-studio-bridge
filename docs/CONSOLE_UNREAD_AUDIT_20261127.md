# Console Ordering and Persona Unread

Status: candidate feature branch, not deployed. Production worktree untouched.

## Findings and Changes

1. Same-rank sidebar rows were sorted by ID, so older UUID sessions could stay
   above recent work. Capture recency at entry and explicit refresh, preserving
   pin/rank priority and the existing no-live-shuffle behavior.
2. Remote Console subscribed only to roster changes; it had neither the global
   message/read stream nor read receipt permission. Add content-free persona
   read metadata to existing session/roster payloads and a scoped receipt route.
3. Read metadata must update monotonically and immediately after the receipt,
   not depend solely on a subsequent SSE delivery.
4. Report reader overlays must not silently mark the underlying conversation
   read. Existing focus, bottom-follow and snapshot-validity guards remain.

The new read route requires a current per-session grant. It binds session and
device identity server-side and rejects future timestamps/ring seq. No global
events/read endpoint was opened to remote tokens. No live database was edited.

iOS companion branch: `fix/persona-unread-sync-20261127`, rebased on current
main (`9aeb657`), not the original app worktree's old integration branch. It
repairs duplicate counting, timestamp/global cursor handling, legacy fallback
reconciliation, receipt echoes and visibility in both persona v1/v2 routes.

## Verification

- 258 Python tests and 26 subtests passed across Console, sync engine, ratchet
  and private-infrastructure guards.
- 12 keyed-list tests and 8 delivery/order tests passed.
- 18 Chrome fixture tests cover remote reload, scoped receipt, stale snapshots, owner
  read tracking, provider labels, responsive layouts, and live event rendering.
  Opening a report suppresses underlying reads; closing it resumes bottom reads.
- The companion iOS candidate passed 60 simulator tests with 0 failures/skips.
- Legacy browser text assertions were updated to include existing status glyphs;
  the assertions remain exact, not skipped or relaxed.
- Tests used synthetic local data. No live persona messages or read cursors were
  mutated. Production rollout and phone acceptance are separate steps.
- Live candidate smoke was deliberately not run: the feature is not deployed,
  and a read receipt would mutate real conversation state. Production `/health`
  alone cannot validate unpublished code; isolated API and browser tests cover it.

## Acceptance After Approved Release

1. Open Console with recent and old sessions; verify provider/pin/rank priority,
   then recency. New streaming text must not shuffle same-rank rows. Refresh
   explicitly to recapture activity order.
2. Deliver a persona reply while both clients are away from that conversation.
   Verify unread, then read at bottom on Console and check the iPhone clears.
3. Reverse the read direction (phone to Console), then reload both clients.
4. Keep the phone in background, scrolled into history, or showing a report.
   A new reply must remain unread until the latest conversation is visible.

CC/CX task-completion indicators are not changed into server-backed persona
unread counts by this patch. No installer release is required for verification;
deployment of the bridge candidate and an updated iOS build are separate gates.
