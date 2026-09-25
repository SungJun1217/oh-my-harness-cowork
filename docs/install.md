**English** · [한국어](install.ko.md) · [← README](../README.md)

# Installing, configuring and removing omhc

- [The install script](#the-install-script)
- [Wiring the hooks](#wiring-the-hooks)
- [Codex: trust the hook](#codex-trust-the-hook)
- [Codex: hooks in config.toml](#codex-hooks-in-configtoml)
- [Codex: non-git projects](#codex-non-git-projects)
- [Codex: the AGENTS.md size limit](#codex-the-agentsmd-size-limit)
- [Delivery fallbacks](#delivery-fallbacks)
- [Repos that share AGENTS.md with Claude Code](#repos-that-share-agentsmd-with-claude-code)
- [Headless sessions](#headless-sessions)
- [Uninstall](#uninstall)

## The install script

```bash
curl -fsSL https://raw.githubusercontent.com/SungJun1217/oh-my-harness-cowork/main/install.sh | sh
```

This unpacks the latest release into `~/.local/share/omhc/<version>` and
symlinks `~/.local/bin/omhc` — no pip, no pipx (zero dependencies, so the
source tree *is* the install). Re-run to update (old versions under
`~/.local/share/omhc` are pruned automatically, keeping only the one
`current` points at); pin a version with `| OMHC_VERSION=v0.1.0 sh`.

For a non-git project, `touch .omhc-root` at its top-level directory — without
either `.git` or `.omhc-root`, every subdirectory you run omhc from becomes
its own project. Codex needs one more setting for such a project; see
[Codex: non-git projects](#codex-non-git-projects).

### From a git checkout instead

```bash
git clone git@github.com:SungJun1217/oh-my-harness-cowork.git
cd oh-my-harness-cowork
ln -s "$PWD/bin/omhc" ~/.local/bin/omhc
```

## Wiring the hooks

```bash
omhc hooks install
```

With no `--harness`, it targets every registered harness that's either
already detected (`~/.claude/projects`, `~/.codex/sessions` exist) or whose
config *directory* exists (`~/.claude`, `~/.codex`) — the latter covers the
common case of installing before either harness has run a first session, so
there's no `projects`/`sessions` directory yet. If neither exists for a
harness (e.g. you haven't installed Codex at all), it isn't targeted by
default; point at it explicitly with `omhc hooks install --harness
claude-code` (or `--harness codex-cli`). If nothing at all is found, the
command prints the registered harness ids and exits 1 instead of silently
doing nothing.

It merges the fragment into that harness's own config
(`~/.claude/settings.json`, `~/.codex/hooks.json`) — it never overwrites the
file, only strips any prior omhc hooks first so re-running (e.g. after a
`hooks/*.json` change) doesn't duplicate them, and leaves an install that
already passes (`omhc status`'s `<adapter-id> hooks` row is PASS) untouched
even if it was hand-merged with extra fields or in a different group order.
It's idempotent: a run with nothing to change writes nothing and makes no
backup — the first change that does write also normalizes the file's JSON
formatting (2-space indent). `omhc hooks uninstall [--harness ID]` removes
only omhc's own `SessionStart` hooks the same way, structurally (parsed as
argv, not `install.sh --uninstall`'s string regex — the two can diverge on
unusual commands; see `omhc/hookconf.py`'s module comment for the measured
cases). Either command backs up the config to `<file>.omhc-bak` first
whenever it's about to change an existing file.

### Merging the fragment files by hand

They live under `~/.local/share/omhc/current/hooks/` (`curl | sh` install)
or `hooks/` in the repo (git checkout). Merge the fragment's `hooks` key into
your own config — don't overwrite it.

| Harness | File | Target |
|---|---|---|
| Claude Code | `claude-settings.fragment.json` | `hooks` in `~/.claude/settings.json` |
| Codex CLI | `codex-hooks.json` | `~/.codex/hooks.json` |

## Codex: trust the hook

> [!WARNING]
> Measured (codex-cli 0.155.1): a hand-dropped `hooks.json` is **not trusted
> by default, and an untrusted hook is silently skipped with no message** —
> neither `mark` nor `brief` ever runs on the Codex side, so nothing gets
> injected into Codex sessions that way (**Claude → Codex**). You must
> approve it once through Codex's own hook trust flow to fix that direction.
> **Codex → Claude** still works without it — Claude's own `mark` backfills
> the Codex session straight from the rollout file.

Both fragments use `--wire claude` — `--wire sdk` (top-level
`additionalContext`) is rejected by codex-cli 0.155.1 with
`hook: SessionStart Failed` and nothing gets injected. `omhc status`'s
[`codex hook` row](status.md#codex-hook) tells you when the hook never ran.

## Codex: hooks in config.toml

Codex also loads hooks from an inline `[hooks]` table in `config.toml`
(same `hooks.<Event>[].hooks[].command` shape as `hooks.json`, just written
as TOML array-of-tables — see the official
[config-advanced docs](https://developers.openai.com/codex/config-advanced#hooks)).
`omhc hooks install` still only ever writes `hooks.json`, but `omhc status`'s
`codex-cli hooks` row and the hook path's `install_handoff` both recognize an
omhc install that lives in `~/.codex/config.toml` instead — if you hand-wrote
one there, you don't need `hooks.json` too. If both exist and both define an
omhc `SessionStart` hook, Codex loads both and warns (per the docs); `status`
shows that as an unjudged `----` row rather than PASS, naming both files.
Project-level `<repo>/.codex/hooks.json` / `<repo>/.codex/config.toml` only
count once that project's `.codex/` layer is trusted (`[projects."<path>"]
trust_level = "trusted"` in `~/.codex/config.toml`) — otherwise omhc ignores
them.

## Codex: non-git projects

> [!IMPORTANT]
> Measured (codex-cli 0.155.1): in a `.omhc-root` project, Codex started in a
> subfolder does **not** read the ancestor `AGENTS.md` with its default
> `project_root_markers = [".git"]` — Path B (the AGENTS.md managed block) is
> then silently ineffective. Add `.omhc-root` to that setting in
> `~/.codex/config.toml` (keep `.git` too):
> ```toml
> project_root_markers = [".git", ".omhc-root"]
> ```

`omhc status`'s [`codex root markers` row](status.md#codex-root-markers)
checks this for you.

## Codex: the AGENTS.md size limit

> [!IMPORTANT]
> Measured (codex-cli 0.156.1): Codex loads `AGENTS.md` head-first, up to
> `project_doc_max_bytes` (default 32768, one total budget across the whole
> chain from the repo root down to cwd), cutting mid-line with no notice.

Path B always writes its managed block at the **top** of `AGENTS.md` (an
existing block found lower down is moved to the top on the next write) so it
survives that cutoff even in a large file. If the block itself would still
end past the configured `project_doc_max_bytes`, omhc declines to claim Path
B and falls through to the outbox instead of writing something Codex can't
see — `omhc status`'s
[`codex agents.md budget` row](status.md#codex-agentsmd-budget) reports it.

## Delivery fallbacks

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../assets/delivery-dark.svg">
  <img src="../assets/delivery-light.svg" width="100%" alt="Delivery fallback chain: if the SessionStart hook is not trusted nothing is delivered; otherwise Path A (install_handoff) is tried, then Path B (AGENTS.md managed block, Codex only, never when shared with Claude Code), then the outbox floor which is never auto-read">
</picture>

`deliver()` tries the adapter's own `install_handoff` (Path A, the hook's
stdout), then the adapter's fallback channels (Codex: the `AGENTS.md` managed
block, Path B), then the universal floor `<repo>/.omhc/outbox/`. All of this
runs inside `brief`, so if the hook never runs, nothing is delivered at all.

`<repo>/.omhc/outbox/` files are transient. `omhc mark` (run by the
SessionStart hook) deletes omhc's own outbox files once they're older than
24 hours; `omhc clear` deletes all of them for the repo immediately. Files
that don't match omhc's own naming and header are never touched. Every file
drop also tries to register `.omhc/` in `.git/info/exclude` (skipped once
it's already registered, or if it's already ignored some other way), the
same per-clone mechanism the AGENTS.md managed block uses.

When Codex's own SessionStart hook succeeds (Path A), omhc also collapses any
leftover AGENTS.md managed block (Path B) from before right away, instead of
waiting the usual 24 hours — so a fresh session never reads a stale block
alongside the fresh hook handoff.

> [!NOTE]
> Measured (codex-cli 0.156.1): Codex reads `AGENTS.md` **before** its own
> SessionStart hooks run — the first turn of a session always sees whatever
> was on disk when the session started, no matter what the hook does to the
> file afterwards. Only later turns of the *same* session (confirmed on
> resume) re-check `AGENTS.md`, and only report a diff: "These AGENTS.md
> instructions replace all previously provided AGENTS.md instructions." plus
> the new text if it changed, "The previously provided AGENTS.md instructions
> no longer apply." if the block is gone, nothing if it's unchanged.

So a block installed before a given Codex session starts is read only by that
session's own `startup` turn (plus, if it's still there, echoed as a diff to
its own later turns) — the *next* Codex session that would otherwise read the
same stale block never gets the chance: `omhc mark` on this session's own
`startup` (never `resume` — the before-hooks read order is only measured for
`startup`; never `compact` — same session, no new turn) collapses a block
whose capture time is already older than this mark call, instead of waiting
the usual 24 hours. This is on top of, not instead of, Path A's own-session
collapse and the 24-hour staleness sweep above. A block a concurrent hook in
*this same* SessionStart just wrote (Codex runs SessionStart hooks in
parallel, measured) is left alone by a small margin on the capture
timestamp, and — since that margin narrows but can't close the window
between judging a block stale and actually removing it — the removal itself
is conditional on the block's capture time still matching what was judged,
so a block written in that gap is never lost.

None of this cleanup runs in a repo where `AGENTS.md` is shared with Claude
Code (see below) — omhc never writes to a shared `AGENTS.md` at all, whether
installing or collapsing.

## Repos that share AGENTS.md with Claude Code

> [!IMPORTANT]
> The recommended layout keeps `AGENTS.md` as the harness-neutral source,
> with `CLAUDE.md` as a real file starting with `@AGENTS.md` followed by
> Claude-specific content (this repo uses exactly that structure). Making
> `CLAUDE.md` a symlink to `AGENTS.md` counts as sharing too — either way,
> `AGENTS.md` itself must never be the symlink.

In such repos, omhc never writes to `AGENTS.md`. Codex's managed block
(Path B) would otherwise be read verbatim in Claude Code sessions too,
leaking the handoff, and writing through the symlink would mutate the
shared, tracked source file. Handoffs to Codex go through Codex's own
SessionStart hook (Path A) instead — if that hook doesn't run, Path B and
the outbox don't step in either, because the `brief` call on the Codex side
never happens. So **Codex does not auto-read the outbox directory**, and you
must install the Codex hook (and trust it through Codex's own flow). The
`instruction files` row in `omhc status` reflects this layout.

## Headless sessions

By default, headless sessions (`claude -p`, `codex exec`, app-server clients) and
Codex subagent threads are never handoff sources. To treat headless sessions as real
ones in a sandbox, export `OMHC_ALLOW_HEADLESS=1` for the receiving launch: eligibility
is judged when the receiving session starts, so it also admits headless sessions that
ran before you set it. Exporting it once for the whole run is simplest. Subagents and
sidechains stay excluded even then.

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/SungJun1217/oh-my-harness-cowork/main/install.sh | sh -s -- --uninstall
```

This removes only omhc's own `SessionStart` hooks from
`~/.claude/settings.json` and `~/.codex/hooks.json` — other hooks in the
same file, or even in the same hook group, are left intact; the JSON is
just re-serialized (2-space indent) in the process. A `<file>.omhc-bak`
backup is written first. It then removes `~/.local/bin/omhc` and
`~/.local/share/omhc`. `~/.omhc` (the archive and ledger) is kept — set
`OMHC_PURGE=1` to remove that too, including through the pipe:
`curl -fsSL .../install.sh | OMHC_PURGE=1 sh -s -- --uninstall`. Safe to
run when nothing is installed, and safe to run twice.

What it doesn't touch:
- **Codex hook trust.** Trust entries are keyed by group/hook index, so
  removing omhc's groups can shift the indices of any other Codex
  `SessionStart` hooks you have — you may need to re-approve those through
  Codex's own trust flow after uninstalling.
- **Per-repo leftovers.** An omhc-managed block in a repo's `AGENTS.md`,
  and `<repo>/.omhc/outbox/`. Run `omhc clear` inside each repo *before*
  uninstalling if you want those cleaned up too.
