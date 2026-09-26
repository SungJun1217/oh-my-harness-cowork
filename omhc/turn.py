"""`omhc turn` — the UserPromptSubmit hook entry point (v2 phase 2, #42).

Deliberately thin and standalone: `bin/omhc` routes `turn` straight here
**without importing `omhc.cli`** (see the bash launcher) — cli.py pulls in
agents_md/deliver/gate/hookconf/index/managed_block/pin/watch, none of which
this path needs, and this runs on every human turn (not just SessionStart),
so the extra import cost isn't free the way it is once per session. All the
actual comparison logic lives in `omhc/stale.py`, which this module treats as
a black box that returns either a note string or "".
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from . import stale

PROG = "omhc turn"

# Same read-to-EOF as `cli._stdin_text` (the hook harness writes the payload
# and closes the pipe) — kept as its own tiny copy rather than importing cli
# (see module docstring) or fsio (stdin reading isn't a filesystem op fsio
# owns). Callers that aren't a hook must pass --stdin.
_STDIN_CAP = 1 << 20

# review #1 finding 3: NOTE_BUDGET (300) bounds the raw note text, but the
# actual bytes written to stdout are that note wrapped in the
# hookSpecificOutput/additionalContext JSON envelope — quoting/escaping and
# the envelope's own keys add overhead on top. This is the ceiling checked
# right before the write (mirrors brief.emit's pre-print recheck), generous
# enough that a correctly-capped note always clears it, but still a real
# ceiling so a bug upstream can't inject something unbounded.
WIRE_BUDGET = 600


class _SilentArgumentParser(argparse.ArgumentParser):
    """argparse's default on a bad/missing/unknown argument is to print
    usage to stderr and `sys.exit(2)` — on the UserPromptSubmit hook path,
    exit 2 would **block the user's own prompt** (invariant 2 applies to
    this hook exactly as much as to SessionStart's). `error()` here raises
    instead of exiting, so `main()` can catch it and still return 0."""

    def error(self, message):  # noqa: D102 (argparse's own signature)
        raise ValueError(message)


def _stdin_text() -> str:
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        return sys.stdin.read()
    except Exception:
        return ""


def emit(*, harness: str, stdin_text: str = "", home: Optional[str] = None,
        now: Optional[float] = None, out=None) -> int:
    """Hook entrypoint. **Never raises; on any failure, empty stdout + exit 0**
    — same invariant 2 discipline as brief.emit, since this also runs on the
    hot path of every human turn and Codex runs it synchronously (measured,
    docs/v2-concurrency.md)."""
    stream = sys.stdout if out is None else out
    try:
        note = stale.check(harness=harness, stdin_text=stdin_text, home=home, now=now)
    except Exception:
        note = ""
    if not note:
        return 0
    try:
        body = json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": note,
        }}, ensure_ascii=False) + "\n"
        # Rechecked right before writing (review #1 finding 3) — the same
        # discipline as brief.emit and mint()'s own trailing assert, but not
        # relying on the assert alone here: `python -O` strips assertions,
        # and this is the hook path (invariant 2), so the check that
        # actually gates the write must survive that.
        if len(body.encode("utf-8")) > WIRE_BUDGET:
            return 0
        stream.write(body)
    except Exception:
        # Covers BrokenPipeError (the hook's stdout consumer went away) and
        # any encode/write failure — invariant 2 still wins: never raise,
        # never let this hook's own I/O block the user's prompt.
        return 0
    return 0


def main(argv=None) -> int:
    """Never raises and never exits nonzero (review #1 finding 3) — the
    UserPromptSubmit hook's exit code gates the user's own prompt, unlike
    SessionStart's `omhc brief`/`omhc mark`, so even a bad/unknown/missing
    argument must degrade to a silent no-op, not `sys.exit(2)`."""
    # add_help=False: argparse's own -h/--help calls parser.exit() (not
    # error()) and prints usage straight to stdout — invariant 2 cares about
    # stdout staying empty just as much as about the exit code, and this
    # entry point is never meant to be run interactively.
    parser = _SilentArgumentParser(prog=PROG, add_help=False)
    parser.add_argument("--harness", default="")
    parser.add_argument("--stdin", default=None, help=argparse.SUPPRESS)
    try:
        args, _unknown = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    except Exception:
        # Missing/malformed --harness (or anything else argparse rejects)
        # degrades to "no harness" — emit()/stale.check() already treat
        # that as a silent no-op.
        return 0
    stdin_text = args.stdin if args.stdin is not None else _stdin_text()
    return emit(harness=args.harness, stdin_text=stdin_text)
