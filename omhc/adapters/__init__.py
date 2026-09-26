from __future__ import annotations

import calendar
import os
import re
import time
from typing import Dict, List, Optional

from .. import fsio, locate
from ..adapter import AdapterUnavailable, InstallReceipt

# Headless override. A switch to accept `claude -p`/`codex exec` as real
# sessions in the sandbox. No vendor string in the name since both harnesses
# share it. Subagent/sidechain never gets unlocked by this either — that's a
# different-speaker problem, not an interactive/non-interactive one.
HEADLESS_ENV = "OMHC_ALLOW_HEADLESS"


def allow_headless() -> bool:
    return os.environ.get(HEADLESS_ENV, "").strip() not in ("", "0", "false", "False")

# A literal dict of classes. Classes, not instances, so a per-repo harness
# home can later be plugged in with one line from the CLI.
#
# No entry_points, no directory scanning, no auto-registration. Build that
# the day a third adapter actually shows up — building it now would draw an
# abstraction from just two data points, and it would break on the third.
REGISTRY: Dict[str, type] = {}


_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})")


def iso_epoch(value) -> float:
    """ISO timestamp → epoch. 0.0 if unrecognized.

    **Never used as an ordering basis** — this machine's largest transcript
    has 254 timestamp regressions (up to 52ms). Order comes from ledger epoch
    and byte offset.

    Both adapters used to implement this separately (one with a regex, one
    with fixed-width slicing). They accepted different inputs, so a format
    one could parse the other returned as 0.0, and mint's header came out
    differently depending on harness.
    """
    if not isinstance(value, str):
        return 0.0
    m = _ISO.match(value)
    if not m:
        return 0.0
    try:
        return float(calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0)))
    except (ValueError, OverflowError):
        return 0.0


def install_state_artifact(bundle, *, home: Optional[str] = None) -> InstallReceipt:
    """Places the artifact where the hook will read it from. Pull, not push.

    Both adapters' install_handoff bodies were byte-for-byte identical —
    keeping generic code in an adapter would invert the layering. Kept here
    in one place; each adapter delegates in one line.
    """
    state = locate.state_dir(locate.repo_key(bundle.repo_root), home=home)
    path = locate.artifact_path(state)
    fsio.write_atomic(path, bundle.body_md)
    return InstallReceipt(
        channel="sessionstart-hook",
        paths_written=(path,),
        consumed_on_read=True,
        cleanup_hint="omhc clear",
    )


def _register(cls: type) -> type:
    REGISTRY[cls.adapter_id] = cls
    return cls


def get(adapter_id: str, *, home: Optional[str] = None, now=time.time):
    """Builds one adapter. Unknown id is never passed over silently."""
    try:
        cls = REGISTRY[adapter_id]
    except KeyError:
        raise AdapterUnavailable(
            "unknown adapter_id {!r}; known: {}".format(adapter_id, sorted(REGISTRY))
        )
    return cls(home=home, now=now)


def present(*, homes: Optional[Dict[str, str]] = None, now=time.time) -> List[str]:
    """Adapter ids installed on this machine. Deterministic order.

    One adapter's bad day must not kill the rest — if detect() raises or
    construction fails, only that adapter drops out.
    """
    homes = homes or {}
    found: List[str] = []
    for adapter_id in sorted(REGISTRY):
        try:
            inst = get(adapter_id, home=homes.get(adapter_id), now=now)
            if inst.detect().present:
                found.append(adapter_id)
        except Exception:
            continue
    return found


# Registers the v1 adapters. Imported at the bottom of the module because
# each adapter references this module back via `from . import _register` —
# the circular import only works once _register is already defined.
#
# No auto-scanning, deliberately. Build that the day there's a third adapter.
# For now these two lines are the entire registry, and the cost of adding a
# new adapter is exactly adding one line here.
from . import claude_code  # noqa: E402,F401  (registration side effect)
from . import codex_cli  # noqa: E402,F401  (registration side effect)
