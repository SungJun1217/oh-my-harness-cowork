# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@AGENTS.md

## Claude Code tooling in this repo

- `/temper <task>` — one unit of work on `feature/<slug>` off `develop`: `omhc-analyst` (opus, read-only) → `omhc-implementer` (sonnet, edits + tests, no commit) → `omhc-reviewer` (opus, read-only) → `omhc-tester` for runtime changes → commit → `--no-ff` merge into `develop`. Never pushes, never touches `main`.
- `omhc-tester` (opus, no repo writes) — exercises a change end-to-end with the real harnesses under the `sandbox/` HOME and reports observed behavior. `/temper` runs it for runtime changes; spawn it directly for ad-hoc checks.
- The agents in `.claude/agents/` carry the full invariant list; keep them in sync with `AGENTS.md` if an invariant changes.
- Project hooks (`.claude/settings.json`): every `.py` edit is parsed against the 3.9 grammar regardless of the local `python3`, and `git commit` is blocked unless both suites pass. Treat a block as a bug to fix, not something to bypass.
