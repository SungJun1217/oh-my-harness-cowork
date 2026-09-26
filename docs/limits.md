[← README](../README.md)

# Known limits

- [An untrusted Codex hook turns off Claude to Codex](#an-untrusted-codex-hook-turns-off-claude-to-codex)
- [Codex 0.144 to 0.148 sessions carry no command facts](#codex-0144-to-0148-sessions-carry-no-command-facts)
- [A Claude Code fork needs a turn of its own](#a-claude-code-fork-needs-a-turn-of-its-own)
- [The on-disk formats are not an official contract](#the-on-disk-formats-are-not-an-official-contract)
- [Non-git projects need a marker](#non-git-projects-need-a-marker)
- [Concurrent use is out of scope for v1](#concurrent-use-is-out-of-scope-for-v1)
- [Fixtures are never committed](#fixtures-are-never-committed)
- [Measured facts](#measured-facts)

## An untrusted Codex hook turns off Claude to Codex

Measured (codex-cli 0.155.1): an untrusted `hooks.json` is silently
skipped, and neither `mark` nor `brief` ever runs on the Codex side.
Because `deliver()` (Path B included) only runs inside a `brief` call, an
untrusted hook means Claude → Codex isn't caught by the `AGENTS.md`
managed block or the outbox either — it just never turns on. Path B/outbox
only open when `brief` actually runs but `install_handoff` fails —
typically a missing omhc hook in `~/.codex/hooks.json`, though other
exceptions (e.g. a write failure under `~/.omhc`) take the same path, such
as calling `omhc brief --harness codex-cli` manually.

### Codex to Claude works without that hook

Codex → Claude no longer needs that hook: Claude's own `mark` calls the
Codex adapter's `discover()` and backfills the Codex session's ledger row
(`via:"scan"`) directly from the rollout file whenever it's newer than
anything already ledgered for that harness in this repo, up to 5 sessions
per `mark` call. `omhc status`'s `codex hook` row ignores those `scan` rows
on purpose — counting them would hide the fact that the hook itself never
ran. **Known gap (#22, harmless in the default config):** sessions beyond
those 5 (or beyond `discover()`'s own hook-path time budget) are never
backfilled later either — the next `mark` call's watermark is already the
newest one just picked, so anything older permanently fails the "newer
than what's ledgered" check. This is harmless because `due()` only ever
needs the single newest *eligible* foreign session, and `discover()`
applies the same headless filter (`allow_headless()`) that `brief`'s
eligibility check does — so what gets backfilled and what `due()` wants
are normally the same set. It only breaks if `OMHC_ALLOW_HEADLESS` differs
between the `mark` that ran the backfill and the later `brief` call: an
interactive session sitting behind more than 5 newer headless ones could
then be missing from the ledger entirely. Not fixed — an uncommon
configuration change to hit in practice.

### Detecting a resumed Codex session without its hook

`session_meta.timestamp` is read once from a rollout's first line and
never updated — `codex exec resume` (measured: it appends to the same
rollout, no new `session_meta`) doesn't move it, so a resumed session's
start epoch stays exactly what it was, and neither the first-line start
time nor an `already_delivered` session id can tell the backfill path
that a resume happened. When the Codex hook *is* trusted, that's fine —
SessionStart fires with `source:"resume"`, `mark` records a fresh start
and a `reopen` line in `delivered.tsv` (`source:"compact"` never
reopens). When the hook is **not** trusted (the default, unverified
Codex config), fixing this needs a change-detection signal other than
session-start epoch — invariant 6 still rules out last-record timestamp
or mtime as an *ordering* source, but file **size** only detects "this
file grew", not "when": `mark`'s backfill now also stats the ledger's
last known size per foreign session (no re-scan, no date-dir window, so
it still works for sessions the 14-day `discover()` window can no longer
see) and, if it grew, reads only the new tail via the adapter's optional
`read_session_since(ref, offset)` (Codex: snaps to the next line
boundary) to check whether that tail actually contains a new human
turn — agent-only growth (tool calls, `turn_aborted`, `task_complete`)
just updates the size baseline and does not re-surface the session.
Reading the tail still costs roughly what a full read costs per byte
(measured: ~22µs/KB), so it stops as early as possible: `stop_at_human_turn`
returns the moment a human turn is found (measured: 0.18ms even on a
45MB rollout when the turn is near the read start) and a `max_bytes`
cap bounds the pathological case — a large tail with *no* human turn at
all — to a fixed worst case (measured: ~17ms for a 1MB cap regardless of
how much bigger the actual tail is) well inside the hook budget. A
capped read doesn't advance the size baseline past what it actually
read, so the unread remainder gets picked up on the next `mark` instead
of being silently skipped. The reported `end_offset` (used as the next
baseline, instead of the raw stat size) is always snapped to the last
complete line actually read — including for that very first baseline:
stat'ing a file mid-write can catch it mid-record, and using that raw
byte count as the cutoff would make a subsequent read skip the
remainder of that exact record, permanently losing whatever human turn
was being written at that instant. That snap looks backward at most
64KB for a newline; a single JSONL record longer than that (unmeasured,
believed rare) falls back to the previous known-good baseline when one
exists, so the worst case is re-reading a span. **Known limit:** with no
previous baseline it keeps the raw size — using `0` instead would make the
next read start from byte 0, find the session's *original* human turn and
hand the old content off again (reproduced in review) — so a >64KB human
record caught mid-write on a session's very first observation can be missed. The same reasoning applies to
`stop_at_human_turn`: a line that already parses as a complete human
turn but hasn't had its trailing newline written yet is not counted as
a match — counting it would advance the baseline right up to (but not
past) that line, so the very next `mark`, once the newline lands, would
find "new" growth starting at the same unterminated line and hand off
the same turn a second time.

A resumed session found this way still lands in the ledger through
**append order** (a fresh `start` row, `grew:1`), the same ordering rule
`due()` always used; the row's epoch is `mark`'s own clock, so
`due.MAX_AGE_SECONDS` never filters it out for being old. A session that
loses to a newer one in the same interval isn't blocked forever either,
regardless of how that newer session's start row got into the ledger:
if it came from the backfill scan, the losing session's baseline is
rebaselined past it in that same `mark` call (before anything has had a
chance to grow, minimizing the ambiguous window below); if the newer
session's own trusted hook wrote its start row directly — invisible to
the backfill scan, since that session isn't a stranger to the ledger
anymore — the next reactivation pass does the same rebaseline lazily,
whether or not the losing session happened to grow in that pass. Either
way, growth past that newer row is judged fresh again; it just doesn't
retroactively un-supersede the interval it lost. This also covers a
case that never had a fix before: a live-continue where the human keeps
typing in an already-delivered Codex session A and then switches
straight to a new Claude session, with no Codex SessionStart at all —
the growth check runs from *any* `mark`, including the receiving
harness's, so it needs no hook on A's side either.

**Remaining limit:** the underlying ordering is still ledger append
order, so if a brand-new session and a resumed/continued one both grow
within the *same* interval — between the newer session's start row
landing and the next time the older session is rebaselined (at most one
`mark` call later) — that growth is inherently ambiguous (size alone
can't tell whether it happened before or after) and stays absorbed; the
newer *start* wins for that interval (the older one's next growth,
after that interval, is judged fresh again — see above). And this is
Codex→Claude only, because the growth check only runs for adapters that
implement `read_session_since` — the Claude adapter doesn't yet, so a
live-continue *into a Claude session* (someone keeps typing in an
already-delivered Claude session, then switches to a new Codex one)
isn't detected until it is.

### `reopen` is a hint, not a guarantee (#27)

`codex exec resume <id> ""`
fires `source:"resume"` and leaves a `reopen` line, but the rollout only
gains a user record with `"text": ""` — no human turn. Concurrently, Codex
fires its SessionStart hooks in parallel, so `mark` and `brief` can race:
`brief` can deliver a session in the same second `mark`'s growth check
reactivates it from a turn `brief` had already read, appending `reopen`
*after* the delivery. Either way, `due()` would hand the session back
to `brief` with nothing new to say. So `brief.compute` also records, on
every delivery, the byte offset just past what it read (a 5th
tab-separated column in `delivered.tsv`; readers that only look at the
first two columns are unaffected, and lines without it fall back to
today's unconditional behavior). Before redelivering a reopened session,
it requires at least one `author=="human"`, `verb=="said"` event with a
non-empty `text` at or past that offset — otherwise it returns empty
without touching the gate or `delivered.tsv`, exactly like the "nothing
to send" path. `--dry-run` runs the same check. `mark`'s `reopen` write is
unchanged — it is still the only signal that makes `due()` reconsider a
delivered session; `brief` is what decides whether there is actually
something new to send.

## Codex 0.144 to 0.148 sessions carry no command facts

The Codex mapping is measured against 194 real rollouts (codex-cli
0.141–0.155.1), which fall into three eras. 0.141–0.142 record shell runs as
`exec_command` function calls with plain-text exit status. 0.149 and later
record them as `CommandExecution` items. In 0.144–0.148 the shell call exists
only inside JavaScript source that the adapter deliberately does not parse
(whitelist, fail-closed), so those sessions read with edits but no `ran`
events. Unknown record types are still counted as `unparsed`.

## A Claude Code fork needs a turn of its own

A fork made with `/branch`, `--fork-session` or background `/fork` is not a handoff source until it has a human turn of its own (#34).

A fork starts SessionStart with `source:"fork"` (`"resume"` before
2.1.214), gets a brand-new session id, and its transcript opens with the
parent's current message chain copied in (each copied record keeps its
original `uuid`/`timestamp`/`type`/`message` but gets `sessionId`,
`parentUuid`, `isSidechain:false`, `sessionKind:undefined`, and a new
`forkedFrom:{sessionId, messageUuid}` field; a `{"type":
"history-suppression","cause":"fork_inherit"}` record may be prepended).
Treated as a plain new session, that means if the parent had already been
handed off to the other harness and the fork never gets a human turn of
its own, the next handoff to that harness would deliver the parent's
GOAL/NEXT a second time under the fork's new id — `mark`'s per-session
`reopen`/offset guard (#27) doesn't apply because that id never had a
`delivered.tsv` row to begin with.

Fixed in the Claude adapter's `classify()` (used by `list_sessions`,
`ref_for_path`, and `brief`'s eligibility check): a forked transcript is
eligible only once it has at least one `author=="human"` turn that
**isn't** a copied record (no `forkedFrom` key) — i.e. something typed in
the fork itself. Detection is cheap on the common case: a transcript is
only even considered a fork if `forkedFrom`/`fork_inherit` shows up in
its first few lines, and the scan for "does the fork have its own turn
yet" stops at the first record past the copied run. The 50 ms time cap
runs over the whole scan (including the copied run, which is a cheap
per-line substring check); the 8 MB byte cap only counts bytes **past**
the copied run (the own tail) — counting the copied run against the
byte budget too was a bug caught in review: any fork of a parent bigger
than 8 MB always failed open and got redelivered (repro: this repo's own
6.9 MB session rewritten as a fork, duplicated to 12.6 MB, delivered its
428-byte handoff again). Hitting either cap fails open to eligible (the
old behavior). Measured (synthetic, copy-only i.e. no own turn to find):
6.3 MB ~7 ms, 12.6 MB ~13 ms, 25.1 MB ~26 ms, 30 MB run to EOF ~32 ms; a
realistic 2 MB copied run with a new turn resolves in ~2 ms. Any
unexpected record shape in the own tail (`message` not a dict, a `text`
block whose `text` isn't a string, …) is caught per-line and also fails
open, rather than raising out of `classify()`/`list_sessions()` and
losing every Claude ref for that repo. `ref_for_path` (called by
`brief`) and `brief.eligible`'s own `classify()` call would otherwise
scan the same file twice per brief; a small per-process cache keyed by
`(path, size, mtime_ns)` on the classify result avoids the repeat scan
and self-invalidates the moment the file grows (a new turn arrives).
`mark` doesn't special-case `source:"fork"` — it's a new session id, so a
plain start row is correct as-is; only `resume` reopens a delivered one.

## The on-disk formats are not an official contract

Claude Code's on-disk schema is undocumented and changed in
backward-incompatible ways throughout 2026 (even the official
`SessionStore` declares entries "opaque"). Mitigated with whitelist
parsing, fail-open behavior, and degradation reporting in `status` — but
designed assuming it will break. That's why the archive is a pointer.

## Non-git projects need a marker

The repo root is the nearest ancestor
holding `.git` *or* `.omhc-root`. Without either, every subdirectory you run
omhc from becomes its own project (its own key, its own state under
`~/.omhc/`). If your project isn't a git repo, run `touch .omhc-root` at
its top once. `omhc` itself refuses to run at `/` (`status` shows `FAIL
root`; the hook path stays silent, per invariant 2) — `$HOME` is fine.

## Concurrent use is out of scope for v1

The seam is a single function, `omhc/due.py::due()`. Phase 1 (#41) is
done: `due()` now returns `List[Watermark]` — every eligible, undelivered
session of the other harness since the last handoff, newest first, capped
at `due.MAX_SESSIONS` (3) — and the newest keeps the full slot layout while
each older one becomes one `ALSO` line in the same 900-byte budget. Phases 2
and 3 (a per-turn overlap warning, and live progress from a still-running
session) are still proposals. v1 already recorded the foundation this
needed (an untruncated `paths` column plus byte-offset ordering).
The v2 design (three phases, with measurements) is in
[v2-concurrency.md](v2-concurrency.md) (#2).

## Fixtures are never committed

They contain real conversation content. Generate them on your own
machine with `python3 tests/harvest.py`.

## Measured facts

The measured facts behind the design:

| Fact | Value |
|---|---|
| Share of a session file that is actual conversation | Claude Code 8%, Codex 0.15% |
| Interactive (`entrypoint=cli`) sessions among the top 31 | **1** (the other 30 are `sdk-py`) |
| Real human turns extracted from 798 records | **11** (of 95 `user` records, 67 are `tool_result` and 7 are slash-command envelopes) |
| Times `SessionStart` fired within one session | **6** → a once-per-session gate is required |
| Record index where `cwd` first appears | **3** (not 0, and entirely absent in 222 of 798 records) |
| Points where timestamps go backwards | **254** (up to 52 ms) → cannot be used as an ordering source |
| Size of the `skill_listing` body / markers it contains | 29,958 chars / **0** → marker detection alone cannot catch it |
| Intersection of the two harnesses' tool vocabularies | **empty set** (`Read/Edit/Bash` vs. `shell/apply_patch`) |
