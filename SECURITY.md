# Security policy

## Reporting a vulnerability

Please don't open a public issue. Use GitHub's **private vulnerability reporting**:
[Security → Report a vulnerability](https://github.com/SungJun1217/oh-my-harness-cowork/security/advisories/new).

Including the steps to reproduce, the affected version (`__version__` in
`omhc/__init__.py`, or a commit hash) and the harness versions (Claude Code / Codex CLI)
speeds things up.

## What omhc reads and writes

omhc handles coding agents' **session records (real conversation content)**. Know what
ends up where.

| What | Where | Notes |
|---|---|---|
| The harness's original session file | `~/.omhc/<repo-key>/` | A **hardlink**, not a copy. The bytes stay here even after the original is deleted |
| Event offset index (TSV) | `~/.omhc/<repo-key>/` | About 115 bytes per event |
| `omhc note` notes, ledger | `~/.omhc/<repo-key>/` | |
| Handoff (≤900 bytes) | The next session's context, the `AGENTS.md` managed block, or `<repo>/.omhc/outbox/` | `.omhc/` is excluded from git |

- There are **no** network or LLM calls. Nothing is sent anywhere.
- To delete the archive, remove the `~/.omhc/<repo-key>/` directory. Because it's a
  hardlink, the bytes only leave the disk once the harness's original is deleted too.
- Test fixtures are real conversations too, so they are never committed (`tests/fixtures/`
  is gitignored). Don't paste raw session content into issues or PRs.
