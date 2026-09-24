# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@AGENTS.md

## Claude Code tooling in this repo

- `/temper <task>` — one unit of work: `omhc-analyst` (opus, read-only) → `omhc-implementer` (sonnet, edits + tests, no commit) → `omhc-reviewer` (opus, read-only) → commit. Never pushes.
- The agents in `.claude/agents/` carry the full invariant list; keep them in sync with `AGENTS.md` if an invariant changes.
- Project hooks (`.claude/settings.json`): every `.py` edit is parsed against the 3.9 grammar regardless of the local `python3`, and `git commit` is blocked unless both suites pass. Treat a block as a bug to fix, not something to bypass.
