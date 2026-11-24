# LINE Release Gate Repairs

Status: fixes implemented on a repair branch; full-suite and remote CI verification
must finish before merging/deploying. A prepared IPA is not an uploaded release.

## Failures And Repairs

- Console CJK: the shortcuts component bypassed the shared mono font stack.
  Restore the shared stack and increment the service-worker asset cache.
- Media matrix: Hermes already resolves its dedicated outbox. Remove the stale
  missing-cwd claim and pin that hook. Unknown personas get no attachment root,
  never another persona's outbox.
- Infrastructure scanner: replace ambiguous sample paths with explicit redacted
  placeholders. Validate public IPv4 candidates with ipaddress; invalid octets
  and reserved numeric library versions are not public addresses. Tests still
  reject genuine public addresses, user directories and tailnet addresses.
- Exception handling: attachment diagnostic failures log type/site with redacted
  messages and existing throttling, without failing the attachment pipeline.
- Subprocess handling: captured commands kill and reap on timeout/cancellation;
  git uses the same path. Five maintenance archive calls use a single bounded
  runner. Failed copy or verification leaves local artifacts intact.

## Audited Primitive

The ratchet is NOT blanket-reset to accept the failing calls. Its only new
entry is process_runner's unavoidable Popen boundary, with an explicit review
reason. That boundary enforces finite timeouts, a dedicated process group,
kill/reap, and outcome logging without argv/stdin/stdout/stderr. Behavioral tests
cover success, nonzero exit, timeout, spawn failure, cancellation, exit races,
and preservation of local artifacts after failed remote checks. One retired
exception entry is removed; no existing violation count is increased.

## Deployment Incident

The smoke check also confused cumulative latest_seq with ring occupancy. A
large sequence does not imply a client cursor has fallen behind retained events.
Validate the cursor shape instead; do not claim buffer headroom from an endpoint
that does not expose it. Missing/malformed snapshots fail rather than silently
passing, and synthetic tests cover high valid sequences and bad responses.

The previous attempt restarted the bridge, then stopped at the read-only
canonical DB smoke check. Pocket ID and build 246 upload were not reached.
The one-shot job was removed; its original state/logs/lock remain for audit.

The system Python could open the file but SQLite queries failed. The bridge's
runtime could read the same DB and pass the dry-run check with unchanged counts.
This establishes a runtime-specific failure, not proven database corruption or
a conclusively identified upstream SQLite bug. Release checks must use the
bridge runtime and run BEFORE any restart/deployment. Keep mode=ro; do not hide
the problem with immutable reads that can omit live WAL data.

Use an explicit launchd plist with KeepAlive=false for any follow-up one-shot
release. The prior launchctl submit job retried its process after failure; its
exclusive lock prevented any repeated deployment, but process retries were
unnecessary. Never remove a previous attempt's lock to replay it blindly.

Other agents' uncommitted iOS changes remain untouched. The signed iOS artifact
does not need rebuilding for these bridge-only fixes. Release remains stopped
until all required checks pass; no model call or live LINE prompt is used as
an unattended smoke test.
