from __future__ import annotations

import os
import time
from typing import List, Optional

from . import adapters, fsio
from .adapter import (
    AdapterUnavailable,
    Capability,
    HandoffBundle,
    InstallReceipt,
    NoInjectionChannel,
)

OMHC_DIR = ".omhc"
OUTBOX_DIR = os.path.join(OMHC_DIR, "outbox")
# So old outbox files don't pile up forever, cmd_mark only deletes ones past
# this age (#36). Shares its value with the AGENTS.md block's
# STALE_AFTER_SECONDS but means something different — that one is about "not
# being read as an instruction", this one is about "not piling up on disk".
OUTBOX_TTL_SECONDS = 24 * 3600
# Only deletes outbox files omhc itself created — recognized by this header.
FILE_DROP_HEADER_PREFIX = "<!-- omhc file drop"


def _iso(epoch: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(epoch))


def _iso_readable(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _safe_header_text(text: str) -> str:
    """Makes a single line safe to embed inside the header HTML comment.

    `why` can carry an exception message or path verbatim, so it's arbitrary
    text — if `-->` sneaks in, the comment ends right there and the
    `captured=`/`captured_utc=` after it leaks into the body (review #3).
    Newlines are also folded so the header is always one line (and therefore
    always the first line) — `_is_own_outbox_file` only looks at the first line."""
    return text.replace("-->", "--&gt;").replace("\n", " ").replace("\r", " ")


def file_drop(bundle: HandoffBundle, why: str, *, now: float) -> InstallReceipt:
    """The universal floor. Even if every injection path is blocked, a
    human-readable file is left behind.

    **This function also never raises.** If the repo root is read-only (CI
    checkout, a root-owned mount, disk full), even the floor collapses, and
    exactly there is where deliver()'s "never raises" contract must not
    break. If it can't write, that fact is carried back in the receipt.
    """
    path = ""
    try:
        directory = os.path.join(bundle.repo_root, OUTBOX_DIR)
        os.makedirs(directory, exist_ok=True)
        # Try to register so `.omhc/` doesn't leak into git status (#36
        # review #1: trying only right when `.omhc/` is created would leave
        # existing users who already had an outbox never registered — try on
        # every file drop, but if register_outbox_exclude is already
        # registered, finish with just one file read (invariant 2: the file
        # drop itself continues even on failure)).
        try:
            from . import agents_md

            agents_md.register_outbox_exclude(bundle.repo_root)
        except Exception:
            pass
        path = os.path.join(
            directory, "{}-to-{}.md".format(_iso(now), bundle.to_adapter_id)
        )
        header = '{}: {} captured="{:.0f}" captured_utc="{}" -->'.format(
            FILE_DROP_HEADER_PREFIX, _safe_header_text(why), now, _iso_readable(now))
        fsio.write_atomic(path, "{}\n{}".format(header, bundle.body_md))
    except OSError as exc:
        return InstallReceipt(
            channel="nowhere",
            paths_written=(),
            consumed_on_read=False,
            cleanup_hint="{}; file drop also failed: {}".format(why, exc),
        )
    return InstallReceipt(
        channel="file-drop",
        paths_written=(path,),
        consumed_on_read=False,
        cleanup_hint="rm {}".format(path),
    )


def deliver(
    bundle: HandoffBundle,
    *,
    home: Optional[str] = None,
    now: Optional[float] = None,
    allow_fallbacks: bool = True,
) -> InstallReceipt:
    """Routes the handoff. **Never raises.**

    Order: adapter's install_handoff → (if Codex) AGENTS.md Path B → universal floor.
    """
    stamp = time.time() if now is None else now
    try:
        adapter = adapters.get(bundle.to_adapter_id, home=home)
    except AdapterUnavailable as exc:
        return file_drop(bundle, "unknown adapter: {}".format(exc), now=stamp)

    if Capability.WRITE not in getattr(adapter, "capabilities", frozenset()):
        return file_drop(bundle, "adapter is read-only", now=stamp)

    # The channel list is an adapter attribute. If the router held even one
    # vendor string, "adding an adapter is one file + one fixture" would be false.
    channels = [adapter.install_handoff]
    if allow_fallbacks:
        try:
            channels.extend(getattr(adapter, "fallback_channels", tuple)() or ())
        except Exception:
            pass

    reasons = []
    for channel in channels:
        try:
            return channel(bundle)
        except NoInjectionChannel as exc:
            reasons.append("no channel: {}".format(exc))
        except Exception as exc:  # an adapter's bad day must not break session start
            reasons.append("{}: {}".format(type(exc).__name__, exc))

    return file_drop(bundle, "; ".join(reasons) or "no channels declared", now=stamp)


def _is_own_outbox_file(path: str) -> bool:
    """Is this a file `file_drop` created. Checks **both** the naming
    convention and the header — going by name alone (e.g. `*-to-*.md`) could
    delete a file a human happened to name the same way."""
    name = os.path.basename(path)
    if not (name.endswith(".md") and "-to-" in name):
        return False
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return False
    return first.startswith(FILE_DROP_HEADER_PREFIX)


def prune_outbox(repo_root: str, *, now: float, ttl: Optional[float] = OUTBOX_TTL_SECONDS,
                 force: bool = False) -> List[str]:
    """Deletes outbox files omhc wrote. Shared by `cmd_mark` (hook path, only
    ones past the ttl) and `omhc clear` (force=True, all of them). Returns
    the deleted paths.

    If `.omhc/outbox/` doesn't exist at all (never did a file drop yet), does
    nothing silently — no reason to create a nonexistent directory every
    time on the hook path.
    """
    directory = os.path.join(repo_root, OUTBOX_DIR)
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    removed = []
    for name in names:
        path = os.path.join(directory, name)
        if not _is_own_outbox_file(path):
            continue
        if not force and ttl is not None:
            try:
                age = now - os.stat(path).st_mtime
            except OSError:
                continue
            if age <= ttl:
                continue
        try:
            os.unlink(path)
        except OSError:
            continue
        removed.append(path)
    return removed
