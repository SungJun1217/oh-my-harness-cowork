from __future__ import annotations

import enum
from typing import Dict, NamedTuple, Optional, Tuple

from .event import Event


class Capability(enum.Enum):
    """Declared capabilities. Only two — no flag that no real harness actually needs.

    Read and write are independent. A harness with no session hook (e.g.
    Cursor) having a read-only adapter is a normal state, not a defect.
    """

    READ = "read"
    WRITE = "write"


class OmhcAdapterError(Exception):
    """Base for every exception in the adapter layer."""


class AdapterUnavailable(OmhcAdapterError):
    """Unknown adapter_id, or the harness isn't installed on this machine."""


class NoInjectionChannel(OmhcAdapterError):
    """No write capability, or every injection path is blocked. Caller falls back to the universal floor."""


class HarnessPresence(NamedTuple):
    present: bool
    note: str = ""


class SessionRef(NamedTuple):
    """A pointer to a single session file.

    cwd can be None — some harnesses have no working-directory concept, and
    allowing that field to be optional now is cheaper than changing the
    Protocol later.
    """

    adapter_id: str
    session_id: str
    source_path: str
    cwd: Optional[str]
    epoch: float
    size: int


class SessionRead(NamedTuple):
    """The result of reading one session into neutral Events.

    unparsed/dropped make silent degradation observable. Raising on an
    unknown record would kill the hook path; dropping silently would hide
    the loss from everyone.
    """

    ref: SessionRef
    events: Tuple[Event, ...]
    unparsed: int
    # No default here — NamedTuple defaults are shared across instances, so a
    # mutable dict default would leak one adapter's tally into another's.
    dropped: Dict[str, int]


class SessionSince(NamedTuple):
    """Result of `read_session_since`. Almost the same as `SessionRead`, but
    also carries `end_offset` — the byte offset right after the **last fully
    read line** this call actually consumed.

    The caller (cli._reactivate_grown_sessions) must use this as the next
    baseline instead of `os.stat`'s size — if stat lands mid-record (the
    harness may still be writing), taking that size as the baseline and later
    reading from there once the record finishes being written makes the
    skip-to-newline logic skip that entire record. `end_offset` only reflects
    lines actually consumed, so it doesn't have this problem. If `max_bytes`/
    early exit stopped before EOF, `end_offset` naturally stops there too —
    the next call resumes from that point."""

    events: Tuple[Event, ...]
    unparsed: int
    dropped: Dict[str, int]
    end_offset: int


class HandoffBundle(NamedTuple):
    body_md: str
    repo_root: str
    to_adapter_id: str


class InstallReceipt(NamedTuple):
    """Every injection path ends in a receipt. No silent failures."""

    channel: str
    paths_written: Tuple[str, ...] = ()
    consumed_on_read: bool = False
    cleanup_hint: str = ""


class HarnessAdapter:
    """An adapter for one harness (more precisely, one surface). Five methods, total.

    Side rules:
    - No I/O in `__init__`. Takes `home=` and `now=` as keywords.
    - `detect()` must be cheap and **must never raise**.
    - Unknown record types default to DROP and are reported in `SessionRead.dropped`.
    - `author == "human"` only ever comes from the top-level session file.
    - Globs are depth 1 only. Nested `<session>/subagents/**` is never read.

    Why not typing.Protocol: runtime checking is limited on 3.9, and the
    conformance suite already verifies the contract against real instances,
    so a nominal base class is more honest.
    """

    adapter_id: str = ""
    capabilities: frozenset = frozenset()

    # Injection JSON shape. The most harness-specific fact there is, so the adapter owns it.
    #   "claude" : {"hookSpecificOutput": {"hookEventName": …, "additionalContext": …}}
    #   "cursor" : {"additional_context": …}
    #   "sdk"    : {"additionalContext": …}   (SDK standard / Copilot CLI)
    # A harness→wire table in the core would force new adapters to touch the
    # core, and if they don't, they'd silently emit a field their own harness
    # ignores (with no receipt to show for it).
    wire: str = "sdk"

    def __init__(self, *, home: Optional[str] = None, now=None) -> None:
        raise NotImplementedError

    def detect(self) -> HarnessPresence:
        raise NotImplementedError

    def list_sessions(self, repo_root: Optional[str]):
        raise NotImplementedError

    def read_session(self, ref: SessionRef) -> SessionRead:
        raise NotImplementedError

    def read_session_since(self, ref: SessionRef, offset: int, *,
                           max_bytes: Optional[int] = None,
                           stop_at_human_turn: bool = False) -> Optional[SessionSince]:
        """Reads starting at byte `offset`. Optional method — same pattern as
        `discover`/`health`. The default `None` means "this adapter can't
        distinguish", and the caller then falls back to today's full-scan
        path — **this decision is per adapter**: once the caller calls it
        once with any argument and gets `None`, that adapter is skipped
        entirely afterward (it doesn't vary call to call).

        Harnesses like `codex exec resume`, where a new turn is appended to
        the same file (same inode) without rewriting `session_meta`,
        implement this — full `read_session` measured 593ms on a 13.8MB
        rollout, which blows the hook-path budget (150ms) if run every time.
        `offset` is the previously observed file size in bytes — it need not
        be a record boundary: mid-line, the implementation skips to the next
        newline. Returned events only include `event.offset >= offset` (after
        snapping to a line boundary). Never raises — degrades to an empty
        result even on garbage.

        `max_bytes` (optional, unlimited by default) reads only that much
        and stops — measured (17.7MB tail): cost scales proportionally with
        size (396.6ms), which can blow the hook budget. `stop_at_human_turn`
        (optional, False by default) stops as soon as it finds a single
        human `said` event — for callers that only need existence (resume
        detection) so they don't read the rest. The returned `end_offset` is
        always the offset right after **the last fully read complete line**
        — with defaults (unlimited, no early exit) that equals EOF.
        """
        return None

    def native_resume_hint(self, ref: SessionRef) -> Optional[str]:
        raise NotImplementedError

    def install_handoff(self, bundle: HandoffBundle) -> InstallReceipt:
        raise NotImplementedError

    def classify(self, source_path: str) -> bool:
        """Is this transcript a **session a human actually talked in**?

        Only return False when it's certain to be headless/subagent. If it
        can't be determined, return True — brief skips ledger rows classified
        False and falls back to the row before it, so classifying an unknown
        file as False lets a stale session go out instead (#21).

        Owned by the adapter because it's harness-specific knowledge. The
        core must never judge one harness's session with another adapter's
        parser — looking for Claude's entrypoint/isSidechain in a Codex
        rollout finds nothing, and the filter silently becomes a no-op.
        """
        raise NotImplementedError

    def discover(self, repo_root: Optional[str], deadline: Optional[float] = None):
        """Sessions newly appeared in this repo. Optional method, used only
        by `mark`'s ledger backfill.

        Follows the same optional-method pattern as `fallback_channels`/
        `health` — default is an empty tuple, and not every adapter is
        obligated to implement an expensive full scan like list_sessions
        (Claude measured this scan at 249ms, which must never run on the hook
        path, so it explicitly returns an empty tuple).

        An adapter that implements this must fill the returned
        `SessionRef.epoch` with the **session's start time** — different from
        list_sessions's epoch (file mtime, used to sort "newest first").
        cmd_mark compares this value against the ledger's latest start epoch
        to decide append order (this isn't the "mtime as ordering basis" that
        invariant 6 forbids — it's simply reusing the session-start epoch,
        the same key `due()` uses — a permitted exception), so mixing in
        mtime here could make an old session look like the newest.

        `deadline` is an optional absolute time on the same clock as
        `time.time()`. Adapters with expensive scans (Codex's date-directory
        walk) should check it periodically inside the loop and, once passed,
        return only what's been gathered so far and stop — otherwise
        cmd_mark's time budget becomes meaningless since it's only measured
        after the discover() call returns. The default `None` means "don't
        cut it short".
        """
        return ()

    def fallback_channels(self):
        """Channels to try in order when install_handoff fails.

        Each entry takes a HandoffBundle and returns an InstallReceipt or
        raises NoInjectionChannel. If the router held vendor strings, the
        "adding an adapter is one file" contract would literally break — a
        third WRITE adapter would have no way to declare its own file-based
        fallback (Cursor's rules, kimi-code's memory).
        """
        return ()

    def health(self, repo_root: Optional[str], ledger_rows) -> Tuple[Tuple[str, Optional[bool], str], ...]:
        """Optional diagnostics. Default is an empty tuple — no adapter is
        obligated to implement this.

        Measured (codex-cli 0.155.1): an untrusted `~/.codex/hooks.json` hook
        is silently skipped, with no message and no ledger row. `omhc status`
        showed all-PASS even though brief had never run once — the user had
        no way to know. This needs harness-specific behavioral evidence, so
        the adapter owns it, not the core.

        Follows the same optional-method pattern as `fallback_channels` —
        adapters that don't implement it are fine with this default, and only
        need to honor the `(label, ok, detail)` shape `cmd_status` asserts on.
        `ok` is `True`/`False`/`None` — only return `True`/`False` (gating)
        when an actual verdict is possible; return `None` (`----`, not
        gating) for an informational diagnostic with no basis yet to judge.
        Never raises — status is a diagnostic tool, and if the diagnostic
        dies there's no way left to report degradation.

        `ledger_rows` is the ledger **not filtered by repo** — session ids
        are globally unique, so if the caller pre-filtered by this repo key,
        a session started from a nested worktree/submodule with its own
        `.git` would be recorded under a different repo key and look
        permanently "never ran".
        """
        return ()

    def on_session_start_mark(self, repo_root: str, *, source: str, epoch: float) -> None:
        """Optional method called every time a session is marked under this
        harness, from `mark`'s SessionStart hook path — same pattern as
        `discover`/`health`. Default is a no-op.

        "This harness reads AGENTS.md/CLAUDE.md-style files at session start
        before its own SessionStart hook runs" is harness-specific measured
        fact (#36, codex-cli 0.156.1 sandbox measurement: the first turn has
        already read the file before the hook runs, so the hook editing it
        does nothing), so the adapter knows it, not the core. The core
        (cmd_mark) only decides under what condition to call this (`source`
        != "compact"); the judgment of "has it already been read, so is it
        safe to delete" (margin against capture time, the Claude shared
        guard) belongs to the implementation.

        `source` is the raw value from the SessionStart hook payload
        ("startup"/"resume"/"compact"/empty string etc — vocabulary shared by
        both harnesses, see cli.py). `epoch` is the epoch of the ledger row
        this mark call recorded (the earliest time mark has that this
        session just started/resumed under this harness).

        Called from the hook path (invariant 2), so it must never raise —
        the caller wraps it too, but the implementation must guard itself as
        well (why the default behavior is a no-op)."""
        return None

    def hook_config(self):
        """Where this harness's SessionStart hook config lives. Optional
        method — same pattern as `health`/`fallback_channels`/`discover`.
        Default is None (a harness with no hook concept, or not yet
        supported).

        If implemented, returns `omhc.hookconf.HookConfig` (or an equivalent
        3-field record): `config_path` (absolute path of the harness config
        this fragment merges into), `fragment_name` (the fragment file name
        under `hooks/` for that harness), `post_write_note` (guidance the user
        should see right after install — e.g. Codex's hook-trust re-approval
        warning; empty string if none). `omhc status`'s `<adapter-id> hooks`
        row and `omhc hooks install` treat both harnesses identically off
        this one record — no vendor name enters the core.
        """
        return None

    def ref_for_path(self, source_path: str, session_id: str,
                     cwd: Optional[str] = None) -> Optional[SessionRef]:
        """Makes a SessionRef from one known path. None if ineligible.

        Since the ledger already recorded the path, this can open that exact
        file directly instead of scanning the full session list — measured:
        list_sessions read 130 files, 34MB, to yield 1 hit, which was 1.7x
        the hook budget (150ms).
        """
        raise NotImplementedError
