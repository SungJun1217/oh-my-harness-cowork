**English** · [한국어](status.ko.md) · [← README](../README.md)

# What `omhc status` checks

`omhc status` gives every row one of three labels: **PASS** or **FAIL** for
checks that were actually judged and can gate the exit code (adapters,
archive, instruction files, `ledger rejects`, and adapter health rows such as
`codex hook`), and **`----`** for rows that are informational or not
judgeable yet (ledger, off switch, pull rate, watcher) — `----` never gates.
There is no SKIP.

- [ledger rejects](#ledger-rejects)
- [codex hook](#codex-hook)
- [codex root markers](#codex-root-markers)
- [codex agents.md budget](#codex-agentsmd-budget)
- [`<adapter-id>` hooks](#adapter-id-hooks)
- [pull rate](#pull-rate)

## ledger rejects

The ledger appends one JSON line per session start, and each line must fit
in a single `write(2)` call (append-only, so no locking is needed — a
one-syscall write to an `O_APPEND` fd is atomic on POSIX regardless of size,
which is unrelated to `PIPE_BUF`; that guarantee is about pipes only). Rows
over that cap are dropped rather than truncated (a truncated `path`/`session`
would silently point at nothing) and the drop itself is recorded so it's not
invisible; a retried session that still can't fit is only recorded once, not
once per `mark`. `ledger rejects` FAILs when this repo had a drop in the last
7 days that still doesn't fit under the current cap, `----` otherwise; `omhc
clear` drops this repo's record of it (e.g. after raising the cap).

## codex hook

Added when the omhc Codex hook is installed. It FAILs when the newest
interactive Codex session for this repo since `hooks.json` last changed never
ran the hook — the untrusted-hook case — and names that session's originator;
Claude→Codex is then not delivered, while Codex→Claude still works through
Claude's `mark` backfill. It shows `----` while it cannot judge yet: no
interactive Codex session here since `hooks.json` changed (the date is shown),
only headless `codex exec` sessions (they never count — open an interactive
`codex` here once), or an unknown error.

## codex root markers

For a repo whose root is marked by `.omhc-root` (not `.git`, and with no `.git`
in any ancestor directory either — Codex's own default already reaches down
from there), this row checks whether `~/.codex/config.toml`'s
`project_root_markers` includes `.omhc-root` — see
[Codex: non-git projects](install.md#codex-non-git-projects). It never gates:
this setting only matters for Path B (the AGENTS.md fallback), so it PASSes
when the marker is there and otherwise shows `----` with the exact line to
add — plus, when the omhc Codex hook isn't installed, an explicit note that
Path B is currently your only channel to Codex. `----` also covers a config
file that's missing, unreadable, or that can't be parsed (never written by
omhc), and a key found only inside a `[section]` (TOML tables scope keys — it
must be at the top level).

## codex agents.md budget

Whenever an omhc-managed block is currently installed in `AGENTS.md`, this
row checks its actual end offset (bytes) against `~/.codex/config.toml`'s
`project_doc_max_bytes` (default 32768 if unset or unreadable). It FAILs (and
gates) when the block ends past that limit — Codex would never see it —
naming the offset and the limit; `----` when no block is installed. Path B
itself never writes a block it already knows would fail this check: it
declines (falling through to the outbox) instead, and logs the reason to
`guard.log`. See [Codex: the AGENTS.md size limit](install.md#codex-the-agentsmd-size-limit).

## `<adapter-id>` hooks

For every detected harness, status also adds a `<adapter-id> hooks` row
(e.g. `claude-code hooks`, `codex-cli hooks`) that checks whether omhc's
SessionStart hooks are actually merged into that harness's own config, not
just that the harness directory exists. Matching is structural (parsed as
argv, not a byte-for-byte string compare), so an absolute path, `~`,
`${HOME}`, a quoted command, or a bare `omhc` found on `PATH` all still
count as installed. It FAILs (and gates) when the config file is missing
(pointing at `omhc hooks install`) or unparseable, when the `mark`/`brief`
commands aren't there in the order and with the flags the shipped fragment
expects (a stale `--wire sdk`, a missing `mark`, `brief` before `mark`, …
also pointing at `omhc hooks install`), or when the hook's binary can't be
found or isn't executable. It PASSes once the installed commands match the
shipped fragment structurally and the binary is executable. A Codex install
in `config.toml` counts too; see
[Codex: hooks in config.toml](install.md#codex-hooks-in-configtoml).

## pull rate

**Pull rate** ("pulled X of N recent injections") is the one number for
judging whether omhc's overhead is worth it: X is how many of the last N
(`PULL_RATE_WINDOW`, 20) delivered sessions for this repo were actually dug
into via `omhc show`, `omhc log`, or `omhc trace` (each session counts once, no
matter how many times it's pulled) — a human running `omhc log` by hand counts too, not
just an agent. The window is over the most recent deliveries in append order
(matched by session id), not the whole history — otherwise a repo used for a
long time would show a rate that keeps drifting down as old, no-longer-pulled
deliveries pile up in a denominator that never shrinks.
