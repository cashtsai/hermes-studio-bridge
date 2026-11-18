# Console CC/CX integration

Date: 2026-11-18. User authorized integration, merge, restart and release.

## Integrated scope

- CC QR-first bridge login: `55b9a27`; local recovery remains available.
- CC P0 capabilities/approvals: `450ce4a`, `e7a442c`.
- CX W2 operations: `71caeec`, `6d4747b`.
- CX W4 mobile/accessibility/errors: `5a99886`.
- CX W3 status/previews/read events: `82bb0f3`.
- Bridge PR #134 merged as `42e4dc9`; integration branch `integ/console-20261118`.
- Pocket ID PR #7 merged as `0e5aa11`: common QR entry and same-phone deep link.
- iOS PR #294 merged as `1800fa0`: dashboard scanner, Console settings order,
  removal of obsolete common-entry six-digit display. Pocket 1.0.0 (234) was
  archived, exported and successfully uploaded to Apple; processing is pending.

The CC iOS working diff was copied into an isolated integration worktree, reviewed
and committed as `0f88d30` / `f5f4f23`. The original `pa-dash-scan` tree is preserved.

## Merge decisions

- Session rows retain W4 native button semantics. W2 overflow controls are siblings,
  never nested buttons. W3 preview and unread indicators are retained.
- Message/read deltas update in place. Approval resolution reconciles immediately.
- W2 history fallback and W4 persistent error feedback coexist.
- Ops modal keyboard isolation, IME guards, mobile viewport handling and focus
  restoration remain intact. Service worker cache is v17 and includes `ops.js`.
- No watchdog, transport, canonical history or account database changes.

## Verification

- Combined browser/Node suite: 47 passed, 1 optional installed-PWA test skipped.
- Full isolated backend runner: 214 passed / 4 failed, 218 files total.
- Three failures were sandbox Unix-socket restrictions. Isolated elevated reruns
  passed all 7 Codex files plus `test_cx_transport_upgrade`.
- Remaining `test_persona_description` is the unchanged baseline failure explicitly
  documented in both task briefs. It was not suppressed or rewritten.
- Ratchet gate, Python compile, JS syntax and diff whitespace checks passed.
- iOS: 53 XCTest + 1237 Swift Testing cases passed; localization gate clean.
- Pocket ID: 479 tests passed. Public HTML/JS/CSS SHA-256 matches local files.
- Clean Chrome on public common entry: QR loaded, deep link visible, obsolete form
  absent, no page errors or horizontal overflow at 390x844.

The read-cursor browser test previously allowed only 150ms beyond the 400ms debounce.
It now waits for the actual HTTP receipt; duplicate/background negative assertions
remain unchanged. Temporary diagnostics were removed.

## Deployment and remaining limits

Pocket ID deployment replaces only `static/console-entry/{index.html,entry.js,entry.css}`.
Backup: `/opt/pocket-id/static/console-entry.backup-20261118-0940` on its host.
No Pocket ID service restart or billing/database migration was needed.

Bridge `42e4dc9` was activated through launchd on 2026-11-18. The process changed
from PID 23110 to 19667 and `/health` returned healthy. No other CC/CX turn was
active before restart; only this integration session was running.

Live acceptance after restart:

- `/health`, `/capabilities`, `/app/v2/sessions`: HTTP 200.
- Provider capabilities expose compact, read cursor and editable model config.
- Invalid bearer token is rejected with HTTP 401.
- Message dry-run returns completion without model execution or persistence.
- Live `app.js`, `ops.js`, `style.css` SHA-256 matches production checkout.
- Service worker cache is v17.
- This integration CX session remains `running` / provider `active`, with no error;
  its updated timestamp was approximately five seconds old during the final check.
- Public Relay `/console` returns the expected unauthenticated QR-login challenge.
- Post-deploy smoke passes after warm-up: usage/status, latest timestamps and card
  ordering are valid. First pass compared two changing snapshots of this active
  session; a 60/5/60/5 read sequence then returned the same latest card and seq 6466
  on all four reads, and the subsequent standard smoke passed.
- One legacy smoke warning remains: it compares cumulative `latest_seq` (6503)
  to ring capacity (8000). That alone does not establish client cursor lag or
  recurring HTTP 410. No event-ring/runtime change was made to silence it.

Build 234 initially stalled in Xcode's `IDEArchiveManager.refreshArchives`, opening
the default Archives symlink into `/Volumes/WorkSSD/Build/Xcode/Archives`. Only our
stalled xcodebuild process was terminated. The recovery invocation uses
`-IDECustomDistributionArchivesLocation=/Users/xcash/BuildLocal/pocketagent/XcodeArchives`
for archive/export, without changing global Xcode preferences or moving old archives.
The original archive log is retained; recovery has separate build/export/upload logs.
Release guards remain enabled: clean/current main, localization, bundle/build identity,
both extensions, and empty developer-host defaults.
Apple accepted the upload with zero errors and one future SDK warning: the archive
uses iOS 26.5 SDK; Apple's uploader requires iOS 27 SDK for uploads starting April
2027. This is not a current rejection. No SDK migration was mixed into this release.

Not included: incomplete iOS motion branch (`fa87142`, unused lazy UI-test injection),
failed iPad navigation experiment, older already-replaced branches, unrelated dirty
persona/admin trees, and unmerged subsidy/account-claim policy changes. Their original
work remains intact. Public privacy-policy wording flagged by CC still needs its
separate content/legal review. No legal text was silently changed.

Real provider approvals were not submitted during QA. Installed standalone PWA,
physical iPhone camera scanning and final TestFlight installation remain device checks.

## Device acceptance

1. Install Pocket 1.0.0 (234), then open the dashboard scanner and scan the common
   Console entry QR from a desktop browser. Verify the intended host is selected.
2. On a phone browser, verify the same-phone deep link opens Pocket for approval.
3. In Console, switch persona/CC/CX/Gemini sessions, inspect status and unread
   previews, then use the session overflow menu. Verify that opening a menu does
   not select the wrong session or submit an action.
4. On a narrow viewport, open/close the software keyboard and operations dialog;
   verify the composer, error feedback and focus remain usable.
5. Run a real approval flow only on an intended task. Browser fixtures verify
   dispatch and error handling, not the consequences of approving production work.
