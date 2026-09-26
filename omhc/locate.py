from __future__ import annotations

import hashlib
import os
from typing import Optional


# `.git` can be either a file (worktree/submodule) or a directory.
# `.omhc-root` is an explicit marker for non-git projects (#12) — file or
# directory, either is fine.
#
# `AGENTS.md`, `.omhc/`, `CLAUDE.md`, `.claude/`, `.codex/` are never markers:
# the first two are files omhc itself writes into the target repo, so using
# them as markers would make the key shift after the first run; the last
# three also exist under `$HOME` (`~/.omhc`, `~/.claude`, `~/.codex`), and
# accepting them as markers would collapse every directory under a `.git`-less
# home into a single `$HOME`. Also, the core doesn't know vendor names —
# the core knowing `.claude`/`.codex` would itself be a contract violation.
ROOT_MARKERS = (".git", ".omhc-root")


def resolve_repo_root(start: Optional[str] = None) -> str:
    """THE definition of repo root. The first ancestor with one of
    `ROOT_MARKERS`, or realpath(cwd) if none.

    Defining this differently at each call site would silently give a
    session list of 0 in a subdirectory. Codex records the repo root in the
    rollout, so this must pair with the equal-or-descendant judgment.

    Doesn't use `git rev-parse --show-toplevel`. It gets called at least once
    per process on the hook path, and fork/exec is ~2.7ms plus ~3.7ms for the
    subprocess import — together 4% of the budget. Walking upward is a
    handful of stats (measured: 16 levels x 2 markers ≈ 1ms).
    """
    base = os.path.realpath(start or os.getcwd())
    current = base
    while True:
        if any(os.path.exists(os.path.join(current, marker)) for marker in ROOT_MARKERS):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return base
        current = parent


def refused_root(root: str) -> Optional[str]:
    """Why running omhc from this root would be a mistake, or None.

    Not placed inside `resolve_repo_root` itself — mint.relativize and others
    must keep working from any root, so the refusal is a separate predicate
    the caller checks directly. Currently rejects only `/` (owner's call) —
    `$HOME` is not rejected: starting a session from `~` is real usage, and
    people who keep a dotfile repo at home (e.g. `~/.git`) must keep working."""
    if os.path.realpath(root) == "/":
        return "{} is not a project root — run omhc from a project directory " \
            "(or touch .omhc-root there)".format(root)
    return None


def repo_key(repo_root: str) -> str:
    """Human-readable basename + path hash. Distinguishes same-named repos at different paths."""
    digest = hashlib.sha1(repo_root.encode("utf-8")).hexdigest()[:8]
    return "{}-{}".format(os.path.basename(repo_root.rstrip("/")), digest)


def owning_repo_key(cwd: Optional[str]) -> Optional[str]:
    """The key of the repo this cwd actually belongs to. None if there's no cwd.

    The `equal-or-descendant` (is_within) judgment, when called from a
    `.git`-less parent directory, would also pass sessions started in a
    **different** repo beneath it (a child with its own `.git`, e.g. a nested
    worktree/submodule). This function must recompute the candidate's actual
    owning repo key and filter on it — the same interpretation
    cli._backfill_foreign_sessions uses.
    """
    if not cwd:
        return None
    return repo_key(resolve_repo_root(cwd))


def is_within(repo_root: str, candidate: str) -> bool:
    """equal-or-descendant. list_sessions' cwd matching rule."""
    return _within(os.path.realpath(repo_root).rstrip("/"),
                   os.path.realpath(candidate).rstrip("/")) is not None


def _within(root: str, cand: str) -> Optional[str]:
    """Computes a relative path from two already-realpath'd paths. None if outside."""
    if cand == root:
        return "."
    if cand.startswith(root + "/"):
        return cand[len(root) + 1 :]
    return None


def relativize(repo_root: str, path: str) -> Optional[str]:
    """Absolute path → repo-relative POSIX path. None if outside the repo.

    realpath is called only once per path. The earlier implementation called
    it twice inside is_within plus twice in the body, four times total, which
    was 13ms of mint's total 15.6ms (measured, 136 paths). Uses the root as
    passed in, assuming the caller already realpath'd it.
    """
    root = repo_root.rstrip("/")
    rel = _within(root, os.path.realpath(path).rstrip("/"))
    if rel is not None:
        return rel
    # The root passed in might not have been a realpath — try once more.
    resolved = os.path.realpath(repo_root).rstrip("/")
    if resolved == root:
        return None
    return _within(resolved, os.path.realpath(path).rstrip("/"))


# The single definition of state file names. Was redeclared in four modules,
# and any one drifting would split the file brief writes from the file
# status/clear reads, silently making the handoff invisible.
ROOT_NAME = ".omhc"
ARTIFACT_NAME = "omhc.txt"
NOTES_NAME = "notes.txt"


def omhc_root(home: Optional[str] = None) -> str:
    """The root of all omhc state. Real home if home=None.

    Three modules each used to decide this fallback independently — if the
    root ever moved, guard.log and ledger.jsonl would end up somewhere
    different from the per-repo state dir, making the hook path's only
    failure log unfindable.
    """
    return os.path.join(home or os.path.expanduser("~"), ROOT_NAME)


def state_dir(key: str, home: Optional[str] = None) -> str:
    """This repo's state root. Kept under home so it never pollutes the working tree."""
    return os.path.join(omhc_root(home), key)


def artifact_path(state_dir_path: str) -> str:
    return os.path.join(state_dir_path, ARTIFACT_NAME)
