[← README](../README.md)

# Understanding a handoff and managing notes

## Why was nothing delivered?

```sh
omhc explain --harness codex-cli
omhc explain --harness claude-code --session <receiving-session-id> --json
```

`--harness` names the **receiving** harness. `explain` observes the same
selection and rendering path as `brief --dry-run`: same-harness/native
resume, already-delivered boundaries, the seven-day age window, adapter
exclusions (headless/subagent), missing or unrecognized source files,
resumes without a new human turn, and older-session read/cap limits. A
receiving `--session` also reports its existing gate state, even when no
source is currently due.

The command reads recorded state only. It never backfills the ledger,
claims a gate, archives, delivers, records a pull, changes configuration, or
writes a failure log. A real SessionStart may first collect foreign state
that this snapshot has not seen. Session files can also grow after the
preview. `ready` means a preview could be built; it does **not** establish
that a hook is trusted or that a model received the context. The global hook-config
check and suggested install command are advisory, and do not run a repair.
If an adapter detects a runnable repo-scoped hook despite missing global
configuration, its result stays unjudged and no duplicate install is suggested.

`--json` includes a reason code, decision events, hook configuration status,
next actions, and the bounded preview. Human/agent provenance in the preview
is exactly the same as a normal handoff. A source read error exits 1; a
legitimate omission exits 0.

## Which recorded sessions are pending?

```sh
omhc sessions --harness codex-cli
omhc sessions --harness claude-code --last 30 --json
```

This read-only list contains the latest start of each recorded session,
newest ledger position first, never sorted by transcript timestamps. It
shows the source harness, unique session prefix, delivery state for the
specified receiver, decision reason, a clipped first human `GOAL` when the
adapter permits reading it, and a native resume hint when available.

The ledger read is bounded by its normal 2,000-row repo window. `--last`
accepts 1–100 and defaults to 20. Unrecorded transcripts are not listed;
`outside_selection_window` means the session was not judged, usually
because it sits behind a delivered/stale boundary or scan limit. It does
not mean that the session is safe to deliver. Adapter exclusions deliberately
remain generic: the core does not guess vendor-specific headless/subagent
metadata.

## Notes with a shorter lifetime

The original add syntax still works:

```sh
omhc note "Keep the CSV header stable"
omhc note --ttl 1d "Use the temporary endpoint today"
omhc note list
omhc note list --json
omhc note delete <id>
omhc note add -- "list"
```

A duration is a positive integer followed by `s`, `m`, `h`, or `d` (for
example `30m`, `12h`, `1d`), at most 7 days. Without `--ttl`, a new note
retains the existing seven-day lifetime. Custom expiry only shortens that
window. Whitespace in a new note is folded to one stamped line.

The words `add`, `list`, and `delete` in the first argument position are
reserved actions. Use `note add -- <text>` to leave a literal note starting
with a reserved word. `--json` applies to `note list` only.

Listing includes active, expired, and legacy entries, with their expiry and
stable content ID; it never removes anything. An ID is derived from the
stored record, so deleting another note does not renumber it. Identical
records (same timestamp and text) are displayed as one group with a `copies`
count and share an ID; deleting that ID removes those copies. A unique
prefix of at least four hex characters is accepted; ambiguous/unknown IDs
are rejected. Append and delete share a lock, and deletion replaces the
file atomically so a concurrent append is not lost. A file with invalid
UTF-8 is neither rewritten by delete nor partially appended by add.

Existing unstamped notes remain eligible for compatibility and show
`legacy`/unknown expiry. They can now be deliberately removed by ID. Default
stamped records keep their old `<epoch>\t<text>` representation. A custom
TTL stores `<epoch>\t!expires=<expiry-epoch>\t<text>`; a new reader enforces
both that expiry and the existing seven-day upper bound. Older omhc
versions do not understand this custom-expiry extension, so use the same
updated installation for writing and reading TTL notes.

## Installation self-test

```sh
omhc doctor
omhc doctor --self-test
omhc doctor --self-test --json
```

Without `--self-test`, `doctor` reuses the existing `status` dashboard. The
opt-in self-test creates disposable homes (including a space in HOME) and
synthetic projects. For each participating source/receiver pair it executes
the shipped SessionStart shell commands through the current omhc launcher,
checks the synthetic human goal and failure, the 900-byte cap, duplicate
suppression, and raw-byte `show E1` after unlinking the synthetic source.

It never starts a harness/model, uses credentials, repairs host settings, or
changes the caller's repository or omhc state. Temporary data is removed on
completion. Existing host off/headless/LIVE switches do not disable the
isolated probes. Failures are `FAIL`; a missing optional adapter sample or
hook channel is `----`, never a fabricated PASS. Exit 0 requires at least
one passing direction and no failed probe.

This proves the synthetic local pipeline, not compatibility with every
future transcript format, hook approval, or real model consumption. Adapter
samples are owned by each adapter's optional `diagnostic_sample(repo_root)`
method so another harness does not require vendor-specific code in the
self-test runner.
