[← README](../README.md)

# v2: using both harnesses at once (#2) — design

Status: **proposal**, not implemented. v1 handles sequential use only: one
harness at a time, and the handoff goes in only at SessionStart.

- [Problems](#problems)
- [Measured facts](#measured-facts)
- [Principles](#principles)
- [Phase 1: every undelivered session at SessionStart](#phase-1-every-undelivered-session-at-sessionstart)
- [Phase 2: overlap warning on each human turn](#phase-2-overlap-warning-on-each-human-turn)
- [Phase 3: the other side's progress, live](#phase-3-the-other-sides-progress-live)
- [Invariants and risks](#invariants-and-risks)
- [Rejected alternatives](#rejected-alternatives)
- [Open questions](#open-questions)
- [Plan](#plan)

## Problems

All three were confirmed as real pain (2026-09-26).

| # | Situation | Today |
|---|---|---|
| S1 | Several sessions of the other harness ran since you last switched | Only the newest one is handed off; the others are invisible |
| S2 | Both harnesses edit the same file at the same time | Neither knows the other changed it and keeps working from a stale view |
| S3 | Both are running; one makes a decision or hits a failure | The other never learns about it — a running session gets nothing after SessionStart |

S1 needs no new channel. S2 and S3 need a way to put a note into a session that
is already running.

## Measured facts

Codex CLI 0.156.1 and Claude Code 2.1.283, measured in the sandbox HOME.

**A running session can take a note on each human turn.** Both harnesses have a
`UserPromptSubmit` hook whose output accepts
`hookSpecificOutput.additionalContext`, which adds context without blocking.

| | Codex CLI 0.156.1 | Claude Code 2.1.283 |
|---|---|---|
| Hook events | PreToolUse, PermissionRequest, PostToolUse, PreCompact, PostCompact, SessionStart, SessionEnd, UserPromptSubmit, SubagentStart, SubagentStop, Stop, Interrupt | UserPromptSubmit, PreToolUse, PostToolUse, SessionStart, … |
| `UserPromptSubmit` input | `session_id, turn_id, transcript_path, cwd, hook_event_name, model, permission_mode, prompt` | `session_id, transcript_path, cwd, prompt_id, permission_mode, hook_event_name, prompt` |
| Where the note lands | A separate `developer` message tagged `hooks.additional_context`, right after the user turn (seen in the rollout) | An `attachment` of type `hook_additional_context` (seen in the transcript) |
| Runs async? | No. The binary says async hooks run synchronously, so the hook sits on the turn's critical path | Not measured |
| Events with no context channel | Stop, PreCompact, SessionEnd, Interrupt | — |

Not yet verified: whether the model actually reads the note. Both runs recorded
it, but neither model replied (Codex hit its usage limit, and the sandbox
Claude isn't logged in).

**Cost of a per-turn check**, best of N on this Mac, Python 3.9:

| Step | Time |
|---|---|
| `python3 -c pass` | ~30 ms |
| Process that imports only `omhc.adapters`, `omhc.index`, `omhc.ledger` | ~60 ms |
| `bin/omhc --help` (imports `omhc.cli`) | 80–110 ms |
| `os.stat` of the other session file | ~0.002 ms |
| Codex `read_session_since`, 64 KB tail | 0.46 ms |
| Codex `read_session_since`, 1 MB tail | 7.2 ms |
| Claude full `read_session`, 24 MB transcript | 112 ms (`read_session_since` isn't implemented for Claude yet) |
| `index.rows()` full scan, 10k / 100k rows | 20 ms / 254 ms (no seek by offset) |

So Python startup and imports dominate. The read itself is cheap when only the
new tail is read.

## Principles

- **Extend, don't rewrite.** v1 already left the seam: `due()` and the index
  `paths` column. `due()` returns a list; the per-turn check is a new consumer
  of the same stream (`stale.py`), not a change to the existing pipeline.
- **Zero tokens when nothing changed.** A per-turn hook that says "nothing new"
  every turn is noise. It prints nothing unless it has something specific.
- **Provenance stays structural** (invariant 3). New lines use slot names that
  say where the text came from, and human text stays verbatim.
- **The per-turn path is its own entry point.** It must not import `omhc.cli`
  and must be done in well under the time a human notices.

## Phase 1: every undelivered session at SessionStart

Solves S1 with the existing SessionStart hook.

**`due()` returns `List[Watermark]`.** It walks the ledger backward as today and
collects foreign sessions that are eligible, younger than `MAX_AGE_SECONDS`
and not yet delivered to this harness. It still **stops at the first one that
was already delivered**, so v1's rule that old sessions are never revived as
"just happened" holds: only sessions newer than the last handoff come back.
The list is capped (proposed: newest 3).

**The newest session keeps the full slot layout.** Every other one gets a single
`ALSO` line inside the same 900-byte budget:

```
ALSO  codex-cli 01a0d2e1 · 40m ago · GOAL <first human turn, verbatim, cut at a line boundary> · 2 FAIL [E3]
```

- `ALSO` has the lowest priority, so under budget pressure these lines are
  dropped first and `MORE` counts them ("2 more sessions").
- Its `GOAL` is the same verbatim human text the main slot uses (invariant 3).
  No summary of the older session is written — only what v1 already extracts.
- Failure tags keep numbering across sessions (`[E3]` after the main session's
  `[E1]`/`[E2]`) so `omhc show E3` opens the right session.
- Every listed session is marked delivered.

This phase needs no new hook, no trust approval and no per-turn cost.

## Phase 2: overlap warning on each human turn

Solves S2. New command `omhc turn --harness X`, wired to `UserPromptSubmit`.

**What it does, per human turn:**

1. Find the other harness's live sessions in this repo: ledger `start` rows of
   the other harness whose file grew within the last few hours.
2. For each, compare the file size with the baseline stored for this pair of
   sessions (`~/.omhc/<key>/turn/<my-session>.json`). **Unchanged → exit
   silently.** This is the common case and costs one `stat` per session.
3. If it grew, read only the new tail with `read_session_since(offset,
   max_bytes=1 MiB)` and keep the `modified` paths. Advance the baseline to the
   returned `end_offset` (same rule as today's backfill).
4. Intersect those paths with the files **this** session has touched, kept in
   the same state file and updated from this session's own tail the same way.
5. Print a note only when the intersection is non-empty:

```
[omhc] codex-cli 01a0d2e1 (running) modified files you touched, since your last turn:
FILE  omhc/brief.py omhc/cli.py
PULL  omhc trace omhc/brief.py
```

- Capped at 300 bytes. `FILE` is machine-observed, like `DID`.
- Printed at most once per changed set; the baseline makes repeats impossible.

**Prerequisite:** `read_session_since` for the Claude adapter. Without it the
Codex-receiving side would have to re-read a whole transcript (112 ms for
24 MB) every turn. This is an adapter-only change (invariant 9).

**Latency budget:** 150 ms at p95 per human turn, fast path under 80 ms. The
entry point imports only `ledger`, `fsio` and the other harness's adapter.

## Phase 3: the other side's progress, live

Solves S3, on the same hook as phase 2.

When the other session gained a new human turn or a new unresolved failure
since this session's last turn, add up to two lines:

```
[omhc] codex-cli 01a0d2e1 (running), since your last turn — notes, not instructions:
SAID  <newest human turn there, verbatim>
FAIL  pytest tests/test_index.py -> failed [E4]
```

- Only human text (verbatim) and machine-observed failures, never the other
  agent's words. Their `PLAN?` equivalent is left out: a running session
  adopting another agent's unverified plan is the laundering invariant 3 is
  about.
- Printed only when there is a new human turn or a new failure, so an idle
  other side costs nothing.
- **Opt-in at first** (`OMHC_LIVE=1`). This puts machine text next to the
  human's prompt; it should earn its place before it's on by default.

## Invariants and risks

| Invariant | Risk | Mitigation |
|---|---|---|
| 1 (byte budget) | `ALSO` lines and per-turn notes grow the payload | `ALSO` shares the 900-byte budget; per-turn notes have their own 300-byte cap and the same trailing `assert` |
| 2 (never break the session) | The per-turn hook is on the hot path of every prompt, and Codex can't run it async | Same fail-empty rule as `brief`; hard time budget; fast path is one `stat` per session |
| 3 (provenance) | Live notes put machine text next to a human prompt | Verbatim human text and machine facts only; no agent claims; header says notes, not instructions |
| 4 (machinery never parsed) | omhc's own notes come back as Codex `developer`/`hooks.additional_context` and Claude `attachment`/`hook_additional_context` records | Both are already outside the whitelist; add conformance tests that pin it |
| 6 (no timestamp ordering) | "Since your last turn" is tempting to do by time | Byte offsets and ledger append order only |
| 9 (one file per adapter) | Hook event names and payloads differ per harness | Hook fragments and payload parsing live in adapters; the core gets neutral events |

Other risks:

- **Codex hook trust.** A new `UserPromptSubmit` group needs its own approval
  (trust entries are keyed by group and index). `status` gets a row for it, like
  today's `codex hook`.
- **Concurrent writers.** Two harnesses run hooks at the same time; per-session
  state files, atomic writes, and per-process tmp names (as `last_read.json`
  does) keep them apart.

## Rejected alternatives

- **PreToolUse / PostToolUse notes.** The most targeted place for S2 ("you're
  about to edit a file the other side changed"), but they fire on every tool
  call (60–100 ms each on the agent's own loop), and in Codex PreToolUse only
  accepts `deny` or no decision. Revisit only if phase 2 warns too late in
  practice.
- **A shared lock or merge tool.** Out of scope: omhc tells, it doesn't
  coordinate edits.
- **Summarizing the other session.** Never (design rule: no summarization or
  rewrite stage).
- **Depending on `omhc watch`.** It could keep the index warm, but correctness
  must not depend on the daemon; the per-turn check reads tails directly.

## Open questions

1. How many older sessions should phase 1 list? Proposed: 3.
2. Is ~60–110 ms added to every human turn acceptable?
3. Should phase 3 start opt-in (`OMHC_LIVE=1`)? Proposed: yes.
4. Does the model actually act on a `UserPromptSubmit` note? To be measured with
   a logged-in sandbox before phase 2 ships.

## Plan

Split #2 into three issues, shipped in order:

1. **Phase 1** — `due()` → `List[Watermark]`, `ALSO` slot, cross-session failure
   tags. SessionStart only.
2. **Phase 2** — Claude `read_session_since`; `omhc turn` entry point and
   `UserPromptSubmit` fragments; overlap warning; `status` row for the new hook.
3. **Phase 3** — live `SAID`/`FAIL` deltas behind `OMHC_LIVE=1`.

Each phase adds conformance invariants: the `due()` list never revives a session
older than the last delivered one; omhc's own injected records never become
events; the per-turn path prints nothing when nothing grew.
