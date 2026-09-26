from __future__ import annotations

import os
import tempfile
from typing import Optional

# PIPE_BUF is pipe-only (POSIX, 512 on macOS), so it's not the basis for
# anything here (#22). POSIX guarantees a **single write(2) call** to an fd
# opened O_APPEND on a regular file is one atomic "seek to end + write"
# operation — with no size limit. append_line calling write() exactly once,
# every time, is itself how that guarantee is upheld; every line-appended
# file here (ledger, delivered.tsv, index) depends on it. The constant below
# is unrelated to that guarantee — it's just a generously sized "amount that
# comfortably finishes in one write() call".
PIPE_BUF_SAFE = 4096


def _ensure_parent(path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)


def write_atomic(path: str, text: str, *, fsync: bool = True,
                 suffix: str = ".tmp") -> None:
    """Writes to a tmp file, fsyncs, then swaps in via os.replace.

    A file a human is editing (AGENTS.md) or a file a hook will read
    (omhc.txt) must never be left half-written. This rule used to be
    hand-duplicated in six places, and two of them were missing fsync — this
    consolidates them into one place and removes that gap.
    """
    _ensure_parent(path)
    tmp = path + suffix
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        if fsync:
            os.fsync(fh.fileno())
    os.replace(tmp, path)


def replace_preserving(path: str, text: str) -> None:
    """Atomically swaps a hand-maintained config file (hooks.json/settings.json).
    If `path` is a symlink, the symlink itself is left in place and only the
    real file is replaced — install.sh's uninstall path already used the
    same rule (#7, hookconf.merge/strip).

    Permissions are inherited from realpath's current mode. If the file
    doesn't exist yet (e.g. Codex has no hooks.json to begin with), it's
    created with 0644 — leaving mkstemp's default 0600 would leave a freshly
    created config file with uniquely restrictive access.
    """
    real_target = os.path.realpath(path)
    directory = os.path.dirname(real_target) or "."
    os.makedirs(directory, exist_ok=True)
    try:
        mode = os.stat(real_target).st_mode & 0o777
    except OSError:
        mode = 0o644
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".omhc-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, real_target)
    except Exception:
        unlink_quiet(tmp_path)
        raise


def append_line(path: str, line: str, *, mode: int = 0o600) -> None:
    """Appends one line via a single O_APPEND write(2).

    No partial records occur even with multiple sessions writing
    concurrently — because POSIX guarantees atomicity for a single write(2)
    call to an O_APPEND fd (see the module comment above, #22 review), and
    PIPE_BUF is unrelated (pipe-only). All the caller is responsible for is
    keeping `line` (with its newline) sendable in **one write() call** — the
    real-world cap isn't PIPE_BUF but whatever size the kernel handles in one
    write() (something like ledger.MAX_LINE, set far smaller, exists for a
    different reason than atomicity).
    """
    _ensure_parent(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
    try:
        os.write(fd, (line.rstrip("\n") + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def append_blob(path: str, blob: str, *, mode: int = 0o600) -> None:
    """Appends multiple lines at once. For batch writes like the index."""
    _ensure_parent(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
    try:
        os.write(fd, blob.encode("utf-8"))
    finally:
        os.close(fd)


def read_text(path: str, default: str = "") -> str:
    """Returns a default value instead of raising on read failure. For the hook path."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return default


def size_of(path: str, default: int = 0) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return default


def line_aligned_size(path: str, size: int, window: int = 65536,
                      fallback: Optional[int] = None) -> int:
    """Snaps `size` (usually `os.stat().st_size`) back to the end of the last
    complete line before it. If a process writing a JSONL append-only log
    happens to be mid-write on one long record at that exact moment, `size`
    itself can land mid-record (#22 review) — using that as-is for a
    baseline and reading from there once that record finishes being written
    would make an offset-based reader's (`read_session_since` and friends)
    line-snapping logic skip that entire record.

    Scans backward from the end of the file, at most `window` bytes, for the
    last newline — a JSONL line longer than that is rare. If not found (or
    the file can't be read), returns `fallback` — if none is given (default
    `None`), returns `size` as-is (the old behavior, accepting the
    mid-record risk; applies only to the very rare case of a single record
    genuinely longer than `window`). If the caller passes a previously known
    baseline as `fallback`, that's where it stays when not found — the next
    judgment just looks at that span again rather than skipping the record.
    Never pass `0` when there's no prior baseline: the next judgment would
    read from the start, mistake the original human turn for a new one, and
    pass old content along again (reproduced in the #22 review).
    """
    if size <= 0:
        return 0
    start = max(0, size - window)
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            chunk = fh.read(size - start)
    except OSError:
        return size if fallback is None else fallback
    idx = chunk.rfind(b"\n")
    if idx == -1:
        return size if fallback is None else fallback
    return start + idx + 1


def unlink_quiet(path: str) -> bool:
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def claim_exclusive(path: str, contents: str = "", *, mode: int = 0o600) -> bool:
    """Claims exclusivity via O_CREAT|O_EXCL. Atomic within the same filesystem.

    Called from the hook path, so it **never raises** — failure is False,
    including when the parent directory can't be created.
    """
    try:
        _ensure_parent(path)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    except FileExistsError:
        return False
    except OSError:
        return False
    try:
        if contents:
            os.write(fd, contents.encode("utf-8"))
    finally:
        os.close(fd)
    return True


def listdir_suffix(directory: str, suffix: str) -> list:
    """Sorted list of full paths. Empty list if the directory doesn't exist."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [os.path.join(directory, n) for n in names if n.endswith(suffix)]


def same_inode(a: str, b: str) -> Optional[bool]:
    try:
        return os.stat(a).st_ino == os.stat(b).st_ino
    except OSError:
        return None
