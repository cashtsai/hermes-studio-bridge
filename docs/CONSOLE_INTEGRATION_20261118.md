# Console CC/CX integration

Date: 2026-11-18. User authorized integration, merge, restart and release.

## Integrated scope

- CC QR-first bridge login: `55b9a27`; local recovery remains available.
- CC P0 capabilities/approvals: `450ce4a`, `e7a442c`.
- CX W2 operations: `71caeec`, `6d4747b`.
- CX W4 mobile/accessibility/errors: `5a99886`.
- CX W3 status/previews/read events: `82bb0f3`.
- Bridge PR #134; integration branch `integ/console-20261118`.
- Pocket ID PR #7 merged as `0e5aa11`: common QR entry and same-phone deep link.
- iOS PR #294 merged as `1800fa0`: dashboard scanner, Console settings order,
  removal of obsolete common-entry six-digit display. TestFlight build 234 queued.

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

Bridge activation and post-restart smoke are the next release step. This document
does not claim that a merge alone constitutes production acceptance.

Not included: incomplete iOS motion branch (`fa87142`, unused lazy UI-test injection),
failed iPad navigation experiment, older already-replaced branches, unrelated dirty
persona/admin trees, and unmerged subsidy/account-claim policy changes. Their original
work remains intact. Public privacy-policy wording flagged by CC still needs its
separate content/legal review. No legal text was silently changed.

Real provider approvals were not submitted during QA. Installed standalone PWA,
physical iPhone camera scanning and final TestFlight installation remain device checks.
