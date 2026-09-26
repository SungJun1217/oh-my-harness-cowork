from __future__ import annotations

import errno
import os
import signal
import time
from typing import Dict, List, Optional

from . import adapters, fsio, index, locate, pin

LOCK_NAME = "watch.lock"
POLL_SECONDS = 5.0

# Idle auto-exit. Ends itself once this much time has passed with no session
# growing — a process the user didn't ask for shouldn't stay alive forever.
IDLE_EXIT_SECONDS = 30 * 60


class LockBusy(Exception):
    """Another watcher is already watching this repo."""


def _lock_path(state_dir: str) -> str:
    return os.path.join(state_dir, LOCK_NAME)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def read_lock(state_dir: str) -> Optional[int]:
    """pid of a live watcher, or None. Cleans up a dead lock."""
    path = _lock_path(state_dir)
    try:
        with open(path, encoding="utf-8") as fh:
            pid = int(fh.read().strip() or "0")
    except (OSError, ValueError):
        return None
    if pid and _pid_alive(pid):
        return pid
    try:
        os.unlink(path)
    except OSError:
        pass
    return None


def acquire(state_dir: str) -> None:
    if read_lock(state_dir) is not None:
        raise LockBusy("watcher already running")
    if not fsio.claim_exclusive(_lock_path(state_dir), str(os.getpid())):
        raise LockBusy("watcher already running")


def release(state_dir: str) -> None:
    try:
        os.unlink(_lock_path(state_dir))
    except OSError:
        pass


# {source_path: (st_mtime, st_size)} — kept across sweeps. Exists only to
# avoid re-parsing an unchanged file; losing it has no effect on correctness
# (it just gets re-read).
_SEEN: Dict[str, tuple] = {}


def forget(path: Optional[str] = None) -> None:
    """For tests and diagnostics. Forgets just this path, or everything if
    none is given."""
    if path is None:
        _SEEN.clear()
    else:
        _SEEN.pop(path, None)


def sweep(repo_root: str, state_dir: str, *, home: Optional[str] = None) -> int:
    """One sweep to catch the index up. Returns the number of rows written.

    **Not responsible for correctness.** If this never runs, brief still
    produces the same result via inline parsing — it just gets slower. So the
    daemon dying doesn't change the result; the only benefit is latency —
    "don't parse a big transcript inside the hook at the moment you switch."
    """
    written = 0
    # detect and read need to see the same home. Without passing home to
    # present(), detection would see the real $HOME while reads see the given
    # home, so a daemon pointed at an alternate home would find nothing or
    # index the wrong place.
    homes = {aid: home for aid in adapters.REGISTRY} if home else None
    for adapter_id in adapters.present(homes=homes, now=time.time):
        try:
            adapter = adapters.get(adapter_id, home=home)
            refs = adapter.list_sessions(repo_root)
        except Exception:
            continue
        for ref in refs:
            try:
                # Don't read if the file is unchanged. A 5-second polling
                # daemon re-parsing a 3.2MB transcript every sweep in steady
                # state would burn 4 CPU-minutes/hour and re-read 28GB, for
                # zero new events.
                stamp = (ref.epoch, ref.size)
                if _SEEN.get(ref.source_path) == stamp:
                    continue
                idx = os.path.join(state_dir, "index", ref.session_id + ".idx")
                read = adapter.read_session(ref)
                fresh = index.append_new(idx, read.events)
                if fresh:
                    written += fresh
                    pin.pin_session(state_dir, ref)
                _SEEN[ref.source_path] = stamp
            except Exception:
                continue
    return written


def lag(state_dir: str) -> List[Dict[str, object]]:
    """Bytes remaining after the last indexed event, per session.

    This does **not** mean "the daemon fell behind." A session file's tail
    holds records that never become Events (Codex's world_state/turn_context,
    Claude's attachment, etc.), so this is nonzero in steady state too. The
    useful signal isn't the absolute value but **whether it stays the same
    after another sweep** — that's when the machine is actually dead.
    """
    out = []
    directory = os.path.join(state_dir, "index")
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".idx"):
            continue
        session = name[: -len(".idx")]
        watermark = index.watermark(os.path.join(directory, name))
        source = os.path.join(pin.pinned_dir(state_dir, session), "source.jsonl")
        # "pinned" means pin_session actually made the hardlink. Without this,
        # a session that got a briefing sent but failed to pin would also
        # show size=0/lag_bytes=0, making status's archive row a false PASS
        # (review defect).
        pinned = os.path.exists(source)
        try:
            size = os.path.getsize(source) if pinned else 0
        except OSError:
            size = 0
        out.append({"session": session, "watermark": watermark, "size": size,
                    "lag_bytes": size - watermark, "pinned": pinned})
    return out


def run(
    repo_root: Optional[str] = None,
    *,
    home: Optional[str] = None,
    poll: float = POLL_SECONDS,
    idle_exit: float = IDLE_EXIT_SECONDS,
    max_sweeps: Optional[int] = None,
    now=time.time,
) -> int:
    """Accelerator loop. Exits itself once the idle timeout is exceeded."""
    root = locate.resolve_repo_root(repo_root)
    state = locate.state_dir(locate.repo_key(root), home=home)
    acquire(state)
    stop = {"flag": False}

    def _handle(_signum, _frame):
        stop["flag"] = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            pass

    last_change = now()
    sweeps = 0
    try:
        while not stop["flag"]:
            if sweep(root, state, home=home):
                last_change = now()
            sweeps += 1
            if max_sweeps is not None and sweeps >= max_sweeps:
                break
            if now() - last_change > idle_exit:
                break
            time.sleep(poll)
    finally:
        release(state)
    return 0
