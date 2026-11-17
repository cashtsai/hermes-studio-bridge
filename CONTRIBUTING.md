# Contributing

Thanks for looking. Two things to know before you spend time on a change.

## 1. The comments in this codebase are in Traditional Chinese

Code, identifiers, API shapes, log keys and this file are English. **The
explanatory comments — which is where the reasoning lives — are in zh-Hant.**
There are a lot of them, and they are not decoration: most of them record a
specific incident ("this guard exists because on 2026-07-28 the service ran an
unmerged commit for an afternoon"). Removing or translating them wholesale would
destroy the most useful thing in the repo, so that is not going to happen.

This is the single biggest barrier to contributing here, and it is fair to call
it a cost. If you hit a comment you need and cannot read it, say so in the issue
or PR — translating a specific block on request is easy and welcome.

## 2. This is a remote-control surface, not a web app

The bridge executes tools on the machine it runs on: it drives Claude Code and
Codex sessions in tmux, serves a terminal over WebSocket, browses files, and
relays approval decisions. A bridge token is equivalent to shell access on that
host. Read [SECURITY.md](SECURITY.md) before touching anything in the auth,
pairing, or console paths — and report vulnerabilities by email, not as a public
issue.

Two hard rules that come out of that:

- **Never commit a credential, and never commit private infrastructure
  identifiers** — tailnet names, personal hostnames, `/Users/<you>` paths. The
  test `tests/test_no_private_infra_leak.py` enforces this by shape, so it will
  also catch *your* tailnet if you paste debug output. Use the placeholders the
  repo already uses (`<your-tailnet>`, `/Users/you`, `example.com`).
- **Operational paths use `$HOME`, not placeholders.** A past cleanup replaced a
  real venv path in `scripts/run-tests.sh` with `/Users/you/...` and turned 98
  test files red at once. Placeholders are for documentation; `$HOME` is for
  things a shell actually runs.

## Running the tests

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
./scripts/run-tests.sh                 # whole suite, one process per file
./scripts/run-tests.sh console         # only files whose name contains "console"
```

`run-tests.sh` runs **one process per test file** on purpose. The suite has two
lineages — ordinary `unittest` files and script-style acceptance files that run
at import and call `sys.exit()` — and the second kind quietly rots under
`unittest discover`. The header comment in that script explains it in full.

Every test file imports `tests/_isolation.py` first. That redirects state paths
to a temp directory and installs an audit hook that **raises** on any write to a
production path. If a new test tries to write to `~/.pocket`, you will get a loud
failure rather than a corrupted local install. Keep that import first.

### Scenarios (behaviour proofs)

`scenarios/` holds a second, much smaller family: one file per **symptom a user
actually hit**, driven only through the product's public HTTP/CLI surface. Each
one must fail on the commit *before* its fix and pass after — otherwise it is a
line that can never go red, which is the exact failure mode this repo keeps
getting bitten by.

```sh
scripts/prove-scenario.sh cc_input_not_accepted_when_survey_modal 4c87829^
```

The script checks the base commit out into a throwaway worktree, runs the
scenario there (**must fail**), then runs it on head (**must pass**), and only
then prints `PROVEN`. Read [`docs/SCENARIOS.md`](docs/SCENARIOS.md) before
adding one — the three rules there are what make a scenario runnable across
commits at all.

### Where to start

Two tasks with a clear boundary, both genuinely useful:

- **Add a provider** — one agent backend the phone can talk to. Additive, four worked
  examples, cannot reach the security-sensitive parts of the bridge.
  See [`docs/ADDING_A_PROVIDER.md`](docs/ADDING_A_PROVIDER.md).
- **Un-skip a test** — see the list below.

CI runs the same script on Ubuntu. Six files are skipped there because they need
things that live on a maintainer's machine (a Hermes agent install, a real
`codex` binary, macOS's zsh) — see `tests/ci-skip.txt`. **That list is a to-do
list, not an exemption**: the right fix is for those files to `SkipTest`
themselves when the dependency is absent. A PR that converts one of them is a
genuinely useful first contribution.

## Post-mortems

[`docs/POSTMORTEMS/`](docs/POSTMORTEMS/) holds a deliberately small set of
backward-looking failure records: what broke, the mechanism, **why every existing
safety net missed it**, and the guardrails that were committed so the same class
of bug fails loudly next time. The format spec is
[`docs/POSTMORTEMS/README.md`](docs/POSTMORTEMS/README.md); copy
[`TEMPLATE.md`](docs/POSTMORTEMS/TEMPLATE.md) and fill in every section.
`tests/test_postmortem_format.py` enforces the structure in CI.

### When to write one

Only when all three conditions hold **at the same time**:

1. **Subtle** — the mechanism is not obvious; a careful engineer would hit it again.
2. **Systemic** — it escaped because of a gap in tests/tooling/conventions, not a
   one-off typo. Test: "if it were a different person and a different module,
   would the same path still be open?" Yes → qualifies.
3. **Costly to rediscover** — hours or days to diagnose, or it lived quietly in
   production for a while.

**If one of the three fails, do not write one.** A folder of flat incident logs is
worse than three documents people actually read. Anything below the bar belongs in
the commit message (long commit messages are normal here) or in the zh-Hant comment
next to the code.

### Who writes it

Whoever fixed the bug, **in the same PR as the fix** — not later. The fix is what
supplies the one thing the document cannot be written without: the list of
guardrails that are already committed. A post-mortem promised for "after we ship"
never gets its guardrails section.

### What review must check

Four things, in this order. The first two are what the machine cannot check.

1. **Does `## Why every safety net missed it` list every line of defence, one per
   line?** Unit tests, scenarios, scan-style gates, types/contracts, post-deploy
   smoke, manual use. A defence that structurally cannot see this bug still gets a
   line saying *why* — that sentence is usually the most valuable one in the
   document. A missing line is almost always the defence the author did not want to
   think about.
2. **Is every entry under `## Guardrails added` a committed gate?** Ask the one
   hard question: *"if I revert the fix, does this one go red?"* If the answer is
   not a plain yes, it is not a guardrail. "Will be careful next time", "added to
   the backlog" and "documented it" do not count — a doc only counts if a
   mechanical gate verifies it, and then you cite the gate.
   `scripts/prove-scenario.sh <name> <base>` mechanises exactly this question; use
   it when the guardrail is a scenario.
3. **Is the root cause structural rather than human?** "Someone forgot" is a
   symptom. Push one layer down: *what made forgetting possible, and invisible to
   every mechanism we have?* Reject a root cause that ends at a person.
4. **Is anything in it forward-looking?** Proposals, plans and acceptance criteria
   belong in a separate document that this one links to. A post-mortem that mixes in
   a proposal makes it impossible to tell which paragraphs are facts.

Adding a post-mortem also means checking whether an existing one is superseded, and
adding your row to the index table in `docs/POSTMORTEMS/README.md`.

## Engineering gates you have to keep green

Beyond the test suite, two mechanisms will block a PR. Both exist because a gate
that can silently stop working is worse than no gate at all.

### Corpus sentinels — a gate must not pass by scanning nothing

`scripts/sentinel/corpus_sentinel.py`. Every scan-style gate here (private
identifier leak scan, event taxonomy registry, the session-ops and media-hook
alignment matrices, replay fixture coverage, the post-mortem format gate, the
ratchet itself) first asserts a **minimum corpus size**. A gate's green has two
possible causes — "scanned everything, found nothing wrong" and "scanned almost
nothing, so found nothing wrong" — and the second happens whenever a glob, a regex
or a `git ls-files` call quietly stops matching.

If you add a scan-style gate, register its corpus in `MINIMUMS` with a floor of
**90% of the value you measured**. Run `python scripts/sentinel/corpus_sentinel.py`
to see current values and headroom. **Lowering a floor requires a reason written on
that line** — "the test went red so I made the number smaller" is the exact move the
table exists to prevent. `tests/test_corpus_sentinel.py` proves the sentinels work by
breaking each gate's discovery on purpose and requiring it to go red.

### The ratchet — old debt is frozen, new debt is blocked

`scripts/ratchet/check.py` (engine and rationale in `baseline_inventory.py`). Three
patterns are known debt in this repo and recorded in `scripts/ratchet/baseline.json`:
silent `except …: pass`, bare `subprocess.*` in product code, and naked
`.slice(0, N)` text truncation in JS. The existing occurrences block nothing;
**a new one does.**

```sh
scripts/ratchet/check.py                      # the gate
scripts/ratchet/check.py --rules              # why each pattern is debt, and the way out
scripts/ratchet/check.py --list py-silent-except
```

A violation's identity is a content hash (`sha256(rule \0 path \0 normalized line)`),
so reformatting and re-indenting are free while changing the code is not. Moving a
file changes the key on purpose — that is the moment to look at the debt again.

`--update` is **not** how you fix a failure. It has two legitimate uses: rewriting
keys after a file move, and letting the baseline converge after you actually removed
debt. Using it to launder a new violation into old debt is visible in review — the
baseline gets longer, and a longer baseline needs a sentence in the PR description.

## Submitting a change

- Branch from `main`. Feature branches are named `feat/*`, `fix/*`, `chore/*`,
  `docs/*`.
- Keep commits focused, and **write the commit message for the next person
  debugging this at 2am**: what was wrong, what you changed, and why the obvious
  alternative was worse. Long messages are normal here, not a faux pas.
- Say what you actually verified. "Tests pass" is fine if they do; "I did not
  test the APNs path, no device" is far more useful than silence.
- New behaviour needs a test. If it cannot be tested, say why in the PR.

## Licence

This repository is **AGPL-3.0**. Contributions are accepted under the same
licence. Note what §13 means in practice: if you modify this bridge and let other
people use it over a network, you have to offer them your modified source.
Running your own copy privately carries no such obligation.

If you are building something commercial on top and that is a problem, open an
issue and say so rather than quietly forking — a different arrangement may be
possible.
