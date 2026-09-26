from __future__ import annotations

import glob
import os
from typing import NamedTuple, Optional

class PinResult(NamedTuple):
    """Pin result. Silently returning None would let a broken archive go
    unnoticed.

    `omhc status` reports this as PASS/FAIL.
    """

    path: Optional[str]
    linked: bool
    sidecars: int
    error: str = ""

    def __bool__(self) -> bool:
        return self.linked


def pinned_dir(state_dir: str, session_id: str) -> str:
    return os.path.join(state_dir, "pinned", session_id)


def pin_session(state_dir: str, ref) -> Optional[str]:
    """Pins the original session bytes via hardlink.

    Never re-serializes. Same inode, so
      - zero extra disk bytes
      - appends from a still-running session remain visible
      - the bytes survive even if the original directory entry is rm'd or
        /clear'd.
    A destructive format change downstream still can't break the pointer.
    """
    return pin_session_result(state_dir, ref).path


def pin_session_result(state_dir: str, ref) -> PinResult:
    """Observable form of pin_session. Returns the failure reason too."""
    if not os.path.exists(ref.source_path):
        return PinResult(None, False, 0, "source missing: {}".format(ref.source_path))
    target_dir = pinned_dir(state_dir, ref.session_id)
    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError as exc:
        return PinResult(None, False, 0, "mkdir failed: {}".format(exc))
    target = os.path.join(target_dir, "source.jsonl")

    linked = _link_or_none(ref.source_path, target)
    if linked is None:
        try:
            same = os.stat(ref.source_path).st_dev == os.stat(target_dir).st_dev
        except OSError:
            same = False
        reason = (
            "hardlink refused by filesystem"
            if same
            else "cross-device: source and state dir are on different filesystems"
        )
        return PinResult(None, False, 0, reason)
    sidecars = _pin_sidecars(ref, target_dir)
    return PinResult(target, True, sidecars, "")


def _link_or_none(src: str, dst: str) -> Optional[str]:
    if os.path.exists(dst):
        try:
            if os.stat(src).st_ino == os.stat(dst).st_ino:
                return dst
        except OSError:
            return dst
        # Replace it if a different session file has taken the same slot.
        try:
            os.unlink(dst)
        except OSError:
            return dst
    try:
        os.link(src, dst)
        return dst
    except OSError:
        # Different device, or a filesystem that refuses hardlinks. Don't
        # fall back to copying — this file keeps growing, so a copy would go
        # stale immediately. Better to have nothing.
        return None


def _pin_sidecars(ref, target_dir: str) -> int:
    """Pins externalized tool output alongside the session.

    Claude Code replaces large tool_result blocks with a <persisted-output>
    stub and moves the content out to <session>/tool-results/*.txt. Without
    pinning the sidecar, the stub never resolves and tier (b) is only half
    there.
    """
    base = os.path.dirname(ref.source_path)
    sidecar_root = os.path.join(base, ref.session_id, "tool-results")
    if not os.path.isdir(sidecar_root):
        return 0
    mirror = os.path.join(target_dir, "tool-results")
    os.makedirs(mirror, exist_ok=True)
    count = 0
    for path in glob.glob(os.path.join(sidecar_root, "*")):
        if not os.path.isfile(path):
            continue
        if _link_or_none(path, os.path.join(mirror, os.path.basename(path))):
            count += 1
    return count
