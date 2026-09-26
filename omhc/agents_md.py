from __future__ import annotations

import os
import re
import time
from typing import Optional

from . import managed_block
from .adapter import InstallReceipt, NoInjectionChannel

FILE_NAME = "AGENTS.md"
CLAUDE_FILE_NAME = "CLAUDE.md"
EXCLUDE_REL = os.path.join(".git", "info", "exclude")
EXCLUDE_MARK = "# omhc: file holding the Codex handoff-managed block"
OUTBOX_EXCLUDE_MARK = "# omhc: state/outbox directory (not for commit)"
OUTBOX_DIR_LINE = ".omhc/"

# Instruction file candidates Claude Code reads. All repo_root-relative.
_CLAUDE_CANDIDATES = (
    "CLAUDE.md",
    os.path.join(".claude", "CLAUDE.md"),
    "CLAUDE.local.md",
)

# Scan range for finding @AGENTS.md imports in CLAUDE.md-like files. These are
# short instruction files, not logs, so a few KB is enough to not miss an
# import line.
_CLAUDE_SCAN_BYTES = 64 * 1024

# Catches `@AGENTS.md`, `@./AGENTS.md`, `@../AGENTS.md` inside `.claude/CLAUDE.md`,
# and `@AGENTS.md.` with trailing sentence punctuation. Doesn't verify the
# relative path actually resolves to this repo's AGENTS.md — a false positive
# fails safe via outbox, so a regex-level approximation is enough.
_IMPORT_RE = re.compile(r"(?<![\w@])@(?:\.{1,2}/)*AGENTS\.md\b")


def shared_with_claude(repo_root: str) -> Optional[str]:
    """Why AGENTS.md is also wired to leak into Claude Code, or None if it isn't.

    All failures collapse to 'not shared' — this judgment is called on the
    hook path (install → deliver), so raising would break session start.
    """
    agents_path = path_for(repo_root)
    try:
        if os.path.islink(agents_path):
            return "AGENTS.md is a symlink"
    except OSError:
        pass

    for rel in _CLAUDE_CANDIDATES:
        claude_path = os.path.join(repo_root, rel)
        try:
            if (os.path.islink(claude_path)
                    and os.path.realpath(claude_path) == os.path.realpath(agents_path)):
                return "{} is a symlink to AGENTS.md".format(rel)
        except OSError:
            pass
        try:
            # Hard link: not a symlink but the same inode. os.path.samefile
            # requires both to exist, which a hard link satisfies by definition.
            if (os.path.exists(claude_path) and os.path.exists(agents_path)
                    and os.path.samefile(claude_path, agents_path)):
                return "{} is hard-linked to AGENTS.md".format(rel)
        except OSError:
            pass
        try:
            with open(claude_path, encoding="utf-8", errors="replace") as fh:
                head = fh.read(_CLAUDE_SCAN_BYTES)
        except OSError:
            continue
        if _IMPORT_RE.search(head):
            return "{} imports @AGENTS.md".format(rel)
    return None


# Don't write a top-of-file explanatory comment. If a file omhc created keeps
# even one line of non-omhc content, the file lingers as a leftover even
# after the block collapses. The begin marker and the body's first line
# ("[omhc] … not instructions") are already self-explanatory.
FILE_HEADER = ""


def path_for(repo_root: str) -> str:
    return os.path.join(repo_root, FILE_NAME)


def _is_tracked(repo_root: str) -> bool:
    """Is git already tracking this. If tracked, exclude is moot and must not be touched."""
    # subprocess pulls in select/selectors/threading, costing ~4ms to import.
    # This function is only called on Codex Path B, so most of the hook path doesn't pay it.
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", repo_root, "ls-files", "--error-unmatch", FILE_NAME],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def _register_exclude_line(repo_root: str, mark: str, line: str) -> bool:
    """Registers one `line` in `.git/info/exclude` (idempotent).

    Uses info/exclude, not .gitignore, because: this exclusion is a user
    decision that only applies to this clone, not something to commit and
    force onto other people.
    """
    path = os.path.join(repo_root, EXCLUDE_REL)
    if not os.path.isdir(os.path.dirname(path)):
        return False
    existing = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            existing = fh.read()
    for existing_line in existing.splitlines():
        if existing_line.strip() == line:
            return True
    if existing and not existing.endswith("\n"):
        existing += "\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{}{}\n{}\n".format(existing, mark, line))
    return True


def _register_exclude(repo_root: str) -> bool:
    return _register_exclude_line(repo_root, EXCLUDE_MARK, FILE_NAME)


def _exclude_has_line(repo_root: str, line: str) -> bool:
    """Checks `.git/info/exclude` by reading the single file only (no
    subprocess) — since `register_outbox_exclude` is called on every file
    drop (#36 review #1), the common already-registered case ends with this
    cheap check without reaching `_is_ignored`'s git subprocess (measured
    15-37ms)."""
    path = os.path.join(repo_root, EXCLUDE_REL)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            existing = fh.read()
    except OSError:
        return False
    return any(existing_line.strip() == line for existing_line in existing.splitlines())


def _is_ignored(repo_root: str, rel: str) -> bool:
    """Is `rel` already ignored (e.g. by .gitignore) — a git subprocess, so
    `register_outbox_exclude` only calls it when that line isn't yet in
    `.git/info/exclude` (this one cost is paid even on the hook path, for the
    uncommon first file-drop case)."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", repo_root, "check-ignore", "-q", rel],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def register_outbox_exclude(repo_root: str) -> bool:
    """Registers `.omhc/` (including outbox) in `.git/info/exclude` (#36) —
    leaves it alone if it's already registered (judged by a plain file read)
    or already ignored some other way (e.g. .gitignore, judged via git
    subprocess). **Called on every file drop** — trying only right when
    `.omhc/` is created would leave existing users who already had an outbox
    never registered (review #1)."""
    if _exclude_has_line(repo_root, OUTBOX_DIR_LINE):
        return True
    if not os.path.isdir(os.path.join(repo_root, ".git", "info")):
        # A non-git (.omhc-root) root, or a worktree where .git is a file,
        # has nowhere to register — don't waste a git check-ignore on every
        # drop (review).
        return False
    if _is_ignored(repo_root, OUTBOX_DIR_LINE):
        return False
    return _register_exclude_line(repo_root, OUTBOX_EXCLUDE_MARK, OUTBOX_DIR_LINE)


def install(bundle, *, now: Optional[float] = None) -> InstallReceipt:
    """Path B: pushes the handoff into the working tree's AGENTS.md managed block.

    Only called when install_handoff (Path A) fails — typically when
    hooks.json has no omhc hook. **Doesn't remove the need for hook trust**:
    if the hook isn't trusted, the Codex-side brief call never happens and
    this function never runs either. Unlike Path A, this doesn't get read
    once and vanish — AGENTS.md is re-read every session.
    """
    reason = shared_with_claude(bundle.repo_root)
    if reason:
        raise NoInjectionChannel(
            "AGENTS.md is shared with Claude Code ({}); writing would leak a Codex "
            "handoff into Claude sessions and mutate the shared file".format(reason)
        )

    stamp = time.time() if now is None else now
    path = path_for(bundle.repo_root)
    tracked = _is_tracked(bundle.repo_root)

    managed_block.splice(
        path, bundle.body_md, captured_at=stamp,
        file_header="" if tracked else FILE_HEADER,
    )

    excluded = False
    if tracked:
        hint = (
            "AGENTS.md is tracked by git — omhc left it in the working tree and did "
            "NOT touch .git/info/exclude; run `omhc clear` before committing"
        )
    else:
        excluded = _register_exclude(bundle.repo_root)
        hint = "omhc clear (auto-collapses after 24h)"

    return InstallReceipt(
        channel="agents-md",
        paths_written=(path,),
        consumed_on_read=False,
        cleanup_hint=hint if excluded or tracked else hint + " [exclude registration failed]",
    )


def collapse(repo_root: str, *, now: Optional[float] = None, force: bool = False) -> bool:
    """Removes a stale managed block. Called from any omhc invocation."""
    stamp = time.time() if now is None else now
    path = path_for(repo_root)
    if managed_block.installed_captured_at(path) is None:
        return False
    if not force and not managed_block.is_stale(path, now=stamp):
        return False
    return managed_block.strip(path)


def collapse_if_captured(repo_root: str, expected_captured: float) -> bool:
    """A conditional version of `collapse(force=True)` — removes it only if
    the currently installed block's `captured` still equals
    `expected_captured` that the caller already used in its judgment (see
    managed_block.strip_if_captured, #36 review: narrows the check-then-act
    race). Used in places like Codex's `on_session_start_mark`, where another
    process may have written a new block between the judgment and the actual
    delete."""
    return managed_block.strip_if_captured(path_for(repo_root), expected_captured)
