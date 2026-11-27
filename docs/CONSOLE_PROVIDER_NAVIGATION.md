# Console Provider Navigation

Date: 2026-11-27. Status: feature candidate, not deployed.
Baseline: `a42ccce` (includes the report reader and OpenClaw push isolation).

## Problem and Design

The previous sidebar used small CC/CX headings that scrolled out of view.
Identically named sessions had no row-level provider identity.

- Opaque, sticky group headings show the provider's full name and the count
  of currently visible sessions. Each heading stops at its own group boundary.
- Every session keeps a compact provider code beside its existing status.
  CC2 remains in the Claude Code group. Unknown providers retain their name.
- Group separators, stronger text, and white/dark surface bands establish
  hierarchy without adding decorative cards or broad color fills.
- Identity accents follow [Pocket CIS colors](https://design.pocketone.app/foundations/colors):
  Claude Code `#D47345`, Codex `#338C80`, OpenClaw `#C41E30`, Gemini `#4285F4`.
  These are small static edges, not execution-state lamps. Text carries identity
  without relying on color. Selection keeps Pocket Red and accentSurface.
- Follow the existing [Web platform rules](https://design.pocketone.app/platforms)
  and [state language](https://design.pocketone.app/state-language).

No new polling, scroll listeners, API, authentication, history or database
behavior. Stable keyed rows, pins, ranking and provider ordering are preserved.
The mobile breakpoint remains unchanged: the sidebar is hidden at 760px and
below, where the existing home and quick switcher remain the navigation path.
Shell asset cache version advances to v29.

## Verification

All UI data is synthetic, in isolated browser profiles with no production access.

- List and delivery unit tests: 19 passed.
- Existing Python Console suite: 20 files passed.
- Ratchet guard: 18 tests passed.
- Private-infrastructure leak guard: 6 tests passed.
- Provider browser suite: 4 scenarios passed, including pocket/white/dark,
  same-name CC/CX sessions, CC2 grouping, sticky transitions, stable roster
  updates, keyboard focus, filtering/counts, and escaped unknown providers.
- Widths checked: 1366, 1024, 820, 768, 390 and 320px; no page/list overflow.
- Report reader browser regression: 3 passed.
- Session-button selectors in handoff/workstation tests now explicitly target
  `.sess`, not the row container and pin button sharing its data-id.

### Browser Gate Follow-up

The untouched baseline produced 10 failures in handoff/workstation, with one
standalone-PWA test skipped. The session-button selector fixes remove four
failures. Six handoff failures also reproduced on the untouched baseline:

1. Borrowed scope fixture records roster events as a session operation.
2. Home search selector matches both home and sidebar search inputs.
3. Lost-acknowledgement test cannot find its expected manual receipt control.
4. Live-card/reconnect fixture does not reach the expected reply.
5. Expired-stream fixture returns multiple cards where it expects only one.
6. Multi-step approval test cannot find its expected option button.

Release triage found stale test fixtures rather than a sidebar regression:
the roster SSE now has its own fixture route; the home search is selected by
its accessible name; receipt recovery waits for the shipped 2s/8s/20s checks;
approval labels omit nonnumeric internal keys; running status includes the
shipped phase glyph. No production behavior was changed for these fixes, and
no failed checks were disabled. Physical-device and production validation
remain separate from the synthetic browser gate.

Existing production smoke: health/capabilities/LINE targets responded, invalid
token was denied, and the message dry-run finished with no canonical count
change and no row for its client ID. This checks the unchanged running service,
not deployment of the sidebar candidate. Candidate bridge syntax also passed.

## Release Boundary

Feature branch only. No production checkout changes, service restarts,
installer releases or live data mutations are part of this change.
After explicit merge/deployment approval, verify the real remote Console with
CC/CX sessions, filtering, a long scroll and a live update. Synthetic screenshots
are design evidence, not proof of a production deployment.
