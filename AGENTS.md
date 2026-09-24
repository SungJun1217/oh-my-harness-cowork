# AGENTS.md

Guidance for coding agents (Codex CLI, Claude Code, …) working in this repository. Claude-Code-specific tooling is in `CLAUDE.md`.

## What this is

omhc carries working context across coding-agent harnesses (Claude Code ↔ Codex CLI) so switching tools doesn't reset to zero. It is a **personal practical tool**: rough UX and hardcoded paths are acceptable; the only success criterion is "does my day actually get easier". Python ≥ 3.9 stdlib only, no dependencies, **no LLM calls anywhere**.

Talk to the user in Korean. Repo comments and commit messages are Korean (`type(scope): 한국어 요약`, body explains *why*); instruction files meant for models (`AGENTS.md`, `CLAUDE.md`, `.claude/`) are English.

README.md (English) and README.ko.md (Korean) carry the same facts — change both together.

## Commands

```bash
python3 -m unittest discover -s tests -t . -q        # full suite, ~12s, never launches a harness
python3 -m unittest tests.test_mint                   # one module
python3 -m unittest tests.test_mint.TestBudget.test_output_never_exceeds_the_budget   # one test
bash tests/smoke.sh                                   # 7 adversarial inputs: hook path must give empty stdout + exit 0
python3 tests/harvest.py [--force]                    # regenerate fixtures from this machine's real sessions
bin/omhc status                                       # the one human dashboard: every check gets PASS/FAIL/`----` (never SKIP)
```

- ~60 tests skip without fixtures. Fixtures (`tests/fixtures/`) are real conversations and are **never committed**; run `harvest.py` locally to enable them.
- Tests pinned to the original EC2 checkout path skip elsewhere. Tests must not depend on the real `$HOME` (CI runners have no `~/.claude`) — plant a temp home via the adapters' `home=` parameter.
- CI (`.github/workflows/test.yml`) runs both suites on Python 3.9–3.14 (ubuntu) and 3.9/3.14 (macOS).
- Commit only with both suites green, and never push without asking.

## Branches

- `main` holds released states only. `develop` is the integration branch. Each unit of work is a `feature/<slug>` branched from `develop` and merged back with `git merge --no-ff` (one merge commit per unit), then deleted.
- Never commit directly to `main` or `develop`. Release (`develop` → `main`, optionally tagged `v0.x`) only when the user asks.

## Trying omhc for real

Trying omhc for real: don't install it on the host (its SessionStart hook would fire in the sessions developing it, and it writes `AGENTS.md` into the working tree). Use the gitignored `sandbox/` scripts: `bash sandbox/setup.sh` builds an isolated `HOME` at `~/omhc-sandbox`, and `bash sandbox/run.sh claude|codex|omhc …` runs the host binaries under it. Everything omhc and both harnesses touch is under `HOME`.

## Architecture

Entry: `bin/omhc` → `omhc/cli.py` (subcommands `mark`, `brief`, `note`, `log`, `show`, `status`, `watch`, `clear`). Hooks call only `mark` then `brief` at `SessionStart` (see `hooks/*.json` — these are **user install fragments** to merge into `~/.claude/settings.json` / `~/.codex/hooks.json`, unrelated to the dev hooks in `.claude/`).

**Hook path (`brief.emit` → `brief.compute`)**, which must never raise:
1. `due.due()` picks the other harness's latest session for this repo from the ledger (`ledger.py`), skipping same-harness (native resume is strictly better), already-delivered, too-old, or `OMHC_OFF`. This is the **v2 concurrency seam** — v1 is sequential use only.
2. `adapters.get(id).read_session(ref)` parses the raw session into neutral `event.Event`s (verbs `said/inspected/modified/ran/delegated/researched`, author `human|agent|harness`). Whitelist parsing only.
3. `mint.mint()` renders the ≤900-byte handoff (slots `GOAL NEXT NOTE FAIL DID PLAN? MORE PULL`, dropped by priority to fit).
4. `gate.claim()` — at most once per session (SessionStart fired 6× in one measured session). Claimed only after a non-empty body exists.
5. Archive: `pin.py` hardlinks (`os.link`) the original session file into `~/.omhc/<repo-key>/`, and `index.py` appends a ~115 B/event TSV offset index. The archive *is* the original bytes; `omhc show E1` reads them back by offset.
6. `deliver.deliver()` routes: adapter's `install_handoff` → adapter's `fallback_channels` (Codex: `AGENTS.md` managed block via `agents_md.py`/`managed_block.py`) → universal floor `<repo>/.omhc/outbox/`. The router has no vendor strings.

State lives in `~/.omhc/<repo-key>/` (`locate.py`); `fsio.py` owns atomic writes/appends. `watch.py` is an optional accelerator daemon; correctness never depends on it.

**Adapters** (`omhc/adapter.py` contract, `omhc/adapters/`): a new harness = one file implementing `detect`, `list_sessions`, `read_session`, `native_resume_hint`, `install_handoff` with `@_register`, one import line in `adapters/__init__.py`, one fixture. No core changes — needing one is a contract defect. `tests/conformance/test_suite.py` parameterizes 22 invariants over the registry, so every adapter gets them automatically. Read-only adapters are a normal state.

## Invariants (violating any is a bug)

1. `mint()` output ≤ budget in UTF-8 **bytes** (Korean text: bytes ≠ chars). Its last statement is the `assert`; output is rechecked before emit and becomes empty on failure.
2. The hook path never breaks session start: any failure → empty stdout, exit 0.
3. Provenance is in the slot name. `GOAL`/`NEXT` are verbatim `author == human` only; unverified agent claims go to `PLAN?`. A short approval turn ("계속 진행해") must never become `NEXT` — that launders a rejected proposal into an instruction. Sidechain/subagent `type:"user"` records are not human.
4. Harness machinery (`attachment`, `skill_listing`, `<environment_context>`, system-reminders) is never parsed. The guard (`guard.py`) is fail-closed on machine-derived text and drops, never rewrites. Marker lists alone are insufficient (the 29,958-char `skill_listing` has zero markers).
5. No tool-name field in the IR — the two harnesses' tool vocabularies are disjoint.
6. Timestamps are not an ordering source (they go backwards); order is ordinal/byte offset.
7. The on-disk formats are undocumented and change; parse by whitelist, fail open, and report degradation in `status`. Codex rollouts nest `content_item_kinds` under `payload.internal_chat_message_metadata_passthrough`; `user.*` kinds are human.
