[← README](../README.md)

# What `omhc status` checks

`omhc status` gives every row one of three labels: **PASS** or **FAIL** for
checks that were actually judged and can gate the exit code, and **`----`** for
rows that are informational or not judgeable yet — `----` never gates. There
is no SKIP.

| Row | What it shows |
|---|---|
| `adapters` | Which harnesses are detected on this machine. FAIL when none is |
| `ledger` | How many session starts are recorded for this repo (`----`) |
| [`ledger rejects`](#ledger-rejects) | Ledger rows dropped for being too long |
| `archive` | Each archived session with its unarchived tail (`tail=…B`). FAIL when handoffs were delivered but nothing got archived |
| `last read` | What the last session `brief` read yielded: events, unparsed lines, and skipped records by type (#37). Never gates — a session closed with no turn legitimately yields 0 events — but 0 events from a non-empty session points at a harness format change |
| `off switch` | Whether `OMHC_OFF` or the `off` file has turned omhc off (`----`) |
| `instruction files` | Whether `AGENTS.md` is shared with `CLAUDE.md` (Path B then falls to the outbox). FAIL when a stale omhc block would leak into Claude Code |
| [`codex hook`](#codex-hook), [`codex root markers`](#codex-root-markers), [`codex agents.md budget`](#codex-agentsmd-budget) | Codex health rows |
| [`<adapter-id> hooks`](#adapter-id-hooks) | Whether omhc's hooks are merged into that harness's config |
| [`pull rate`](#pull-rate) | How many recent handoffs were actually dug into |
| `watcher (optional)` | Whether the `omhc watch` daemon is running (`----`) |

After the rows, `events` counts indexed events by verb and `artifact` shows the
size of the last minted handoff (`omhc.txt` in the state directory). `--json`
prints the same rows plus the raw numbers.

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
hooks are actually merged into that harness's own config, not just that the
harness directory exists. Matching is structural (parsed as argv, not a
byte-for-byte string compare), so an absolute path, `~`, `${HOME}`, a quoted
command, or a bare `omhc` found on `PATH` all still count as installed. It
FAILs (and gates) when the config file is missing (pointing at `omhc hooks
install`) or unparseable, when the `mark`/`brief` commands aren't there in
the order and with the flags the shipped fragment expects (a stale `--wire
sdk`, a missing `mark`, `brief` before `mark`, … also pointing at `omhc
hooks install`), or when the hook's binary can't be found or isn't
executable. It PASSes once the installed commands match the shipped
fragment structurally and the binary is executable. A Codex install in
`config.toml` counts too; see
[Codex: hooks in config.toml](install.md#codex-hooks-in-configtoml).

This one row also covers the `UserPromptSubmit` group (`omhc turn`, v2
phase 2) — it FAILs the same way if that group is missing entirely from
`hooks.json`, so an install from before phase 2 shipped (`SessionStart`
only) is flagged instead of silently staying PASS; `omhc hooks install`
picks up just the missing group without touching an already-passing
`SessionStart` group's content.

`omhc hooks install` only ever writes `hooks.json` — it never auto-installs
into Codex's inline `config.toml [hooks]` (see
[Codex: hooks in config.toml](install.md#codex-hooks-in-configtoml)). So
when the `SessionStart`/`brief` half is only installed inline, a missing
`hooks.json` turn hook isn't a defect `omhc hooks install` could fix, and
isn't reported as FAIL — it shows as unjudged (`----`) with a hint to add a
`UserPromptSubmit` entry to `config.toml` by hand (`omhc hooks install`
prints the same hint instead of writing `hooks.json` in that case, to avoid
Codex loading both layers and warning).

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
