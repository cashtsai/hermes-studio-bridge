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

CI runs the same script on Ubuntu. Six files are skipped there because they need
things that live on a maintainer's machine (a Hermes agent install, a real
`codex` binary, macOS's zsh) — see `tests/ci-skip.txt`. **That list is a to-do
list, not an exemption**: the right fix is for those files to `SkipTest`
themselves when the dependency is absent. A PR that converts one of them is a
genuinely useful first contribution.

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
