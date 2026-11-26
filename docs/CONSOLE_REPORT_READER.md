# Console Report Reader

Status: candidate branch; production merge/restart requires explicit owner approval.

## User Flow

- Open the report icon in a session header to select an archived Markdown report.
- Click a Markdown report link or attachment in a message to open it directly.
- Read headings, tables, lists and code; use the desktop table of contents.
- Switch between rendered Markdown and source, or download the original bytes.
- Closing, changing sessions, going home or logging out clears the reader.

## Contract and Security

`GET /app/v2/sessions/{id}/reports` returns `items` and `max_preview_bytes`.
Each item contains `media_id`, `title`, `filename`, `byte_size`, `sha256`,
`refs` (matching delivered links), and a session-scoped `download_url`.
`GET /app/v2/sessions/{id}/reports?media_id=...` returns archived bytes.

Both operations require authentication. A borrowed Console requires the live
session's `replay` grant. Each download revalidates that grant and membership.
Responses use `Cache-Control: no-store`; bytes use `nosniff` and an attachment
disposition. No changes to `/app/v1`, database schema or existing media grants.

Only explicit Markdown links, `[[attach:...]]` markers, and Markdown attachments
in retained assistant/agent prose cards count as deliveries. Tool output,
reasoning, user messages, code examples and bare paths do not authorize access.
Only already archived `.md`/`.markdown` artifacts belonging to this session are
resolved. No arbitrary filesystem access, external fetch, basename matching,
capture-on-demand, general `/media` inventory or global artifact grant is added.

The reader uses the existing escape-first Markdown renderer. Raw HTML never
executes. It fetches only on demand, caps previews at 512 KiB including chunked
responses, aborts stale reads, and does not persist report text in browser storage.
Downloads do not reuse the general attachment blob cache.

## Limits

- Documents outside retained card history are not listed even if raw media still
  exists. Ask the original session to attach them again; do not widen file roots.
- An unarchived local link shows an explicit unavailable state, not a dead label.
- HTML, PDF, Office documents and relative repository links are not new preview
  formats in this patch. Their existing attachment behavior is unchanged.
- A report inside the reader cannot grant access to additional files by linking
  them. Only reports already delivered in the conversation can be opened.
- This patch displays reports; it does not automatically inject report contents
  into a model, long-term memory or the separate knowledge-base project.

## Verification

- 8 session-report extraction/API/auth tests.
- 205 existing Console tests, including Markdown and remote scope regression.
- 31 media regression tests.
- Three Playwright scenarios: preview/source/TOC/download; responsive screenshots
  at 1366, 768, 390 and 320 pixels; missing/oversized/aborted/revoked report states.
- Existing live service health/capabilities/invalid-token/messages dry-run passed;
  canonical counts unchanged. New endpoints are tested in isolation, not deployed.

Run with the repository's Python environment:

```sh
python -m unittest discover -s tests -p 'test_session_reports.py'
python -m unittest discover -s tests -p 'test_console*.py'
python -m unittest discover -s tests -p 'test_media*.py'
node --test tests/browser/console-reports.cjs
```

Playwright must be available to Node (use `NODE_PATH` for an existing external
installation). Screenshots default to `/tmp/pocket-report-qa`; override with
`POCKET_QA_SCREENSHOTS`. Tests use synthetic data and isolated browser profiles.

## Deployment

Merge only with owner approval and restart from outside the bridge process after
active provider turns have finished. Frontend and backend must ship together;
service-worker shell is v28. Verify a QR-authenticated remote browser can list and
read an actual delivered report, and guessed/cross-session artifact IDs fail.
Do not treat passing local tests or a feature-branch push as production release.
