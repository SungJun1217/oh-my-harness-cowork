from __future__ import annotations

import glob
import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple

from .. import fsio, guard, hookconf, locate
from ..adapter import (
    Capability,
    HandoffBundle,
    HarnessPresence,
    InstallReceipt,
    NoInjectionChannel,
    SessionRead,
    SessionRef,
    SessionSince,
)
from ..event import ARG_LIMIT, Event
from . import _register, allow_headless, install_state_artifact, iso_epoch

ARTIFACT_NAME = "omhc.txt"

# Scan range for date directories. sessions/YYYY/MM/DD structure means a
# full walk would stat every old session too. Bounded since this runs on the
# hook path.
SCAN_DAYS = 14

# Envelope types we parse. The rest (world_state, turn_context, etc) are
# bookkeeping. event_msg only uses item_completed — the facts of shell/edit
# only live there.
_PARSED_ENVELOPES = frozenset({"response_item", "event_msg"})

# Roles let through. developer (<skills_instructions> / <multi_agent_role>
# etc) is pure machinery and isn't even parsed.
_PARSED_ROLES = frozenset({"user", "assistant"})

# Block types carrying text. user/developer use input_text, assistant uses output_text.
_TEXT_BLOCKS = frozenset({"input_text", "output_text", "text"})

# --- Measured (codex-cli 0.141.0-0.155.1, 211 rollouts on this machine, 2026-09) --------
# Three eras are mixed together.
#
# era A (0.141-0.142): shell is response_item/function_call
#   name="exec_command". arguments is a JSON string and cmd (str, measured
#   381/381) is the shell command itself. workdir (absolute path,
#   sometimes missing) comes along too. No parsed_cmd. Output is plain text
#   in function_call_output.output ("Chunk ID: ... / Wall time: ... /
#   Process exited with code N or Process running with session ID N /
#   Original token count: N / Output: ..."), not JSON. A process left
#   running in the background gets its result from a later
#   write_stdin(args.session_id==N) output. Edits are custom_tool_call
#   name="apply_patch", with the raw patch text in the top-level input
#   rather than JSON arguments (paths are read from *** Add/Update/Delete
#   File:, *** Move to: lines). The same edit comes again as
#   event_msg/item_completed FileChange (id==call_id, absolute path) — if
#   both produced events that'd be double counting, so FileChange
#   overwrites the original event.
# era B0 (0.144-0.148): the command only exists inside a JS source string in
#   custom_tool_call name="exec". JS isn't parsed (whitelist, fail-closed) —
#   files from this era are left without facts (accepted loss, since it's
#   outside SCAN_DAYS anyway).
# era B (0.149-0.155.1, current): shell is event_msg/item_completed
#   item.type="CommandExecution" (status completed<=>exit 0,
#   failed<=>exit!=0, has parsed_cmd). The custom_tool_call name="exec" in
#   the same spot is JS-wrapper bookkeeping. Multi-agent tools (spawn_agent
#   etc) come as function_call.
_JS_WRAPPER_TOOL = "exec"
# If parsed_cmd is entirely these kinds, it's a read. Codex has no dedicated
# read-only tool, so without this Codex sessions would never have an inspected event.
_INSPECT_KINDS = frozenset({"read", "list_files", "search"})
_SHELL_FLAGS = frozenset({"-c", "-lc"})

# Tool name → neutral verb. Only measured names go in here (invariant 5 —
# the tool name itself never enters the IR, only gets absorbed into a verb).
# Unknown names are only tallied as unmapped_tool, producing no event —
# better than ~300 fake "ran" events with an empty arg.
_VERB_BY_TOOL = {
    "exec_command": "ran",
    "apply_patch": "modified",
    "spawn_agent": "delegated",
}

# Bookkeeping-only tools — produce no event, only tallied under
# dropped["tool_bookkeeping"]. write_stdin is a special case, used to link
# session_id back to the original exec_command event (see read_session), but
# it never becomes an event itself.
_BOOKKEEPING_TOOLS = frozenset({
    "write_stdin", "wait", "wait_agent", "list_agents", "interrupt_agent",
    "send_message", "followup_task", "request_user_input",
    "list_available_plugins_to_install",
})

# The line that extracts the path from apply_patch's top-level input. Move to: gives the destination path.
_PATCH_PATH_RE = re.compile(
    r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", re.MULTILINE)

# Extracts `project_root_markers = [...]` from `~/.codex/config.toml` (#31).
# The key name may be quoted (both TOML bare/quoted keys are valid). Uses a
# non-greedy DOTALL match up to the first `]` so it catches multi-line arrays too.
_ROOT_MARKERS_KEY_RE = re.compile(
    r'(?m)^[ \t]*[\'"]?project_root_markers[\'"]?[ \t]*=[ \t]*(\[.*?\])', re.DOTALL)
# Review #2: a table header like `[projects."..."]` changes the scope of the
# keys under it (`[table]\nproject_root_markers = ...` is not a top-level
# key) — only treat text before the first header as top-level.

# `project_doc_max_bytes = N` from `~/.codex/config.toml` (#33). The total
# budget Codex uses to read the whole AGENTS.md chain (repo root down to cwd)
# from the top (deep-research, 2026-09-25 — no value documented, so the
# embedded default 32768 is used as a measured approximation). Only
# recognizes integer values — neither measurement nor docs have ever shown
# anything else.
DEFAULT_PROJECT_DOC_MAX_BYTES = 32768
_PROJECT_DOC_MAX_BYTES_KEY_RE = re.compile(
    r'(?m)^[ \t]*[\'"]?project_doc_max_bytes[\'"]?[ \t]*=[ \t]*(-?\d+)')


def _first_table_header(text: str) -> int:
    """Position where the first `[section]`/`[[section]]` header (the `[`
    itself) starts, -1 if none.

    Reuses the scanner that skips strings/comments while tracking bracket
    depth inside array values (`hookconf.toml_header_lines`, pulled out in
    #32 to share with inline `[hooks]` detection) — only the first result is
    needed here."""
    headers = hookconf.toml_header_lines(text)
    return headers[0][0] if headers else -1


# Recognizes both TOML basic ("...", with escapes) and literal ('...', no escapes) string literals.
_TOML_STRING_RE = re.compile(r'"(?P<d>(?:[^"\\]|\\.)*)"|\'(?P<s>[^\']*)\'')

# Fixed header lines of exec_command/write_stdin output. Plain text, not JSON.
_EXIT_CODE_RE = re.compile(
    r"^(?:Process exited with code|Exit code:) (\d+)$", re.MULTILINE)
_RUNNING_SID_RE = re.compile(
    r"^Process running with session ID (\S+)$", re.MULTILINE)
_ABORTED_RE = re.compile(r"^aborted by user after ", re.MULTILINE)

# kind prefixes for content a human typed. Measured values:
#   ['user.text']                        <- a real human prompt
#   ['environments.environment_context'] <- environment prompt (role=user but machinery)
#   ['host_skills.instructions', 'multi_agent.role_instructions', ...] <- developer
#
# Prefix-allow (not deny-by-default), so a new user.* kind doesn't lose human
# speech. Used together with envelope detection as a double check — metadata
# is accurate but harness-specific, while envelope detection is less
# accurate but works across every harness.
_HUMAN_KIND_PREFIX = "user."


def human_kinds(payload: dict):
    """content_item_kinds. Nested inside metadata, not at the payload's top level."""
    meta = payload.get("internal_chat_message_metadata_passthrough")
    if not isinstance(meta, dict):
        return None
    kinds = meta.get("content_item_kinds")
    if not isinstance(kinds, list) or not kinds:
        return None
    return [str(k) for k in kinds]


def session_meta(path: str) -> Optional[dict]:
    """Reads only the first line. Parsing the rest would spike cost on the hook path."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return None
    try:
        row = json.loads(first)
    except ValueError:
        return None
    if not isinstance(row, dict) or row.get("type") != "session_meta":
        return None
    payload = row.get("payload")
    return payload if isinstance(payload, dict) else None


# Originator of programmatic app-server clients. All come with
# source="vscode", so source alone can't distinguish them. Measured (this
# machine, 2026-09-24): 36 applecider, 3 splitlane* — every one of them has a
# role=user turn that's a machine template shaped like "User goal: ...
# Current browser URL: ... Active project file: ..." (not a sentence a human
# typed). thread_source="user" isn't used as an allowlist: measured, 38 of 39
# real interactive sessions have this field, but 1 (Codex Desktop
# 0.146.0-alpha.3.1) doesn't. It's an undocumented field that could
# disappear any time, and an allowlist would then silently lose real
# sessions. Instead, new programmatic originators must be added here by hand.
_PROGRAMMATIC_ORIGINATORS = frozenset({"applecider", "codex_exec"})
_PROGRAMMATIC_ORIGINATOR_PREFIXES = ("splitlane",)

# managed_block's captured is rounded to whole seconds (#36) — if mark and
# brief, running in parallel within the same SessionStart, each measure
# their epoch in the same second or an adjacent one, without this margin
# mark could mistake the block brief just wrote for this session as an
# "already-consumed stale block" and delete it (see on_session_start_mark).
_CONSUMED_BLOCK_MARGIN_SECONDS = 2.0


def _is_headless_originator(originator) -> bool:
    if not isinstance(originator, str):
        return False
    if originator in _PROGRAMMATIC_ORIGINATORS:
        return True
    return originator.startswith(_PROGRAMMATIC_ORIGINATOR_PREFIXES)


def _is_subagent(meta: dict) -> bool:
    """Is this a subagent thread? A blocklist, not an allowlist — an unknown
    source still remains "not a subagent".

    Measured (this machine, 2026-09-24): 116 of 194 host rollouts are
    subagent threads. Since the parent agent's role=user prompt comes with
    content_item_kinds=['user.text'], it passes straight through the human
    whitelist and gets disguised as GOAL/NEXT (violates invariant 3). Real
    shape (codex-cli 0.155.1): source={"subagent": {"thread_spawn": {...}}},
    thread_source="subagent", parent_thread_id=<uuid> — any one of the three
    alone is enough to be a subagent, and this is never unlocked by
    OMHC_ALLOW_HEADLESS (it's a different-speaker problem).
    """
    source = meta.get("source")
    if isinstance(source, dict) and "subagent" in source:
        return True
    if meta.get("thread_source") == "subagent":
        return True
    if meta.get("parent_thread_id"):
        return True
    return False


def _is_headless_meta(meta: dict) -> bool:
    """Is this `codex exec`-style programmatic execution? `codex exec` comes
    with originator="codex_exec", source="exec" (measured on all 5 sandbox
    rollouts). applecider/splitlane* are programmatic clients riding inside a
    vscode extension (see the _PROGRAMMATIC_ORIGINATORS comment above)."""
    return meta.get("source") == "exec" or _is_headless_originator(meta.get("originator"))


def _is_interactive(meta: dict) -> bool:
    """Filters out subagent/headless runs. Subagent is always excluded;
    headless only passes when OMHC_ALLOW_HEADLESS is on."""
    if _is_subagent(meta):
        return False
    if _is_headless_meta(meta):
        return allow_headless()
    return True


def _recent_date_dirs(root: str, days: int, now) -> List[str]:
    """Date directories for the last N days. **Includes both UTC and local dates.**

    On this machine (TZ=UTC) there's no way to tell which timezone Codex
    names its directories by. UTC-only would make today's directory missing
    from the scan list every day between local 00:00-09:00 in an environment
    like KST (UTC+9), silently failing the handoff. Including both costs a
    few extra globs, and duplicates are removed with a dict.
    """
    seen = {}
    stamp = now()
    for offset in range(days + 1):
        moment = stamp - offset * 86400
        for parts in (time.gmtime(moment), time.localtime(moment)):
            path = os.path.join(root, time.strftime("%Y/%m/%d", parts))
            seen[path] = True
    return list(seen)


def _text_of(blocks) -> str:
    if not isinstance(blocks, list):
        return ""
    return "".join(
        b.get("text", "")
        for b in blocks
        if isinstance(b, dict) and b.get("type") in _TEXT_BLOCKS
    )


def _patch_paths(text: str, workdir: str) -> Tuple[str, ...]:
    """Reads only the *** ...: lines from apply_patch's raw patch text. A
    relative path is made absolute relative to workdir if given, otherwise left as-is."""
    paths = []
    for m in _PATCH_PATH_RE.finditer(text):
        p = m.group(1).strip()
        if workdir and not os.path.isabs(p):
            p = os.path.join(workdir, p)
        paths.append(p)
    return tuple(paths)


def _arg_and_paths(payload: dict) -> Tuple[str, Tuple[str, ...]]:
    """Reads exec_command's arguments (JSON string).cmd, apply_patch's
    top-level input (raw patch text), spawn_agent's task_name/agent_type.
    Never reads message — that's a prompt body the agent wrote."""
    name = payload.get("name")

    if name == "apply_patch":
        text = payload.get("input")
        if not isinstance(text, str):
            return "", ()
        workdir = payload.get("workdir")
        paths = _patch_paths(text, workdir if isinstance(workdir, str) else "")
        arg = paths[0] if paths else ""
        return guard.redact_b64(arg)[:ARG_LIMIT], paths

    raw = payload.get("arguments")
    parsed = None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
    elif isinstance(raw, dict):
        parsed = raw
    if not isinstance(parsed, dict):
        return "", ()

    if name == "spawn_agent":
        arg = parsed.get("task_name") or parsed.get("agent_type") or ""
        return guard.redact_b64(str(arg))[:ARG_LIMIT], ()

    # exec_command: cmd is str, measured 381/381.
    cmd = parsed.get("cmd")
    arg = cmd if isinstance(cmd, str) else ""
    return guard.redact_b64(arg)[:ARG_LIMIT], ()


def _uri_path(value) -> str:
    if not isinstance(value, str):
        return ""
    if value.startswith("file://"):
        value = value[len("file://"):]
        if "%" in value:
            from urllib.parse import unquote

            value = unquote(value)
    return value


def _item_fact(item: dict):
    """One item from item_completed → (verb, ok, arg, paths). None if unknown type."""
    kind = item.get("type")
    ok = item.get("status") == "completed"
    if kind == "CommandExecution":
        command = item.get("command")
        if isinstance(command, list):
            parts = [str(c) for c in command]
            # ["/bin/bash", "-lc", "ls omhc"] — strip the shell wrapper.
            arg = parts[2] if len(parts) == 3 and parts[1] in _SHELL_FLAGS else " ".join(parts)
        else:
            arg = str(command or "")
        parsed = [p for p in item.get("parsed_cmd") or () if isinstance(p, dict)]
        kinds = {p.get("type") for p in parsed}
        verb = "inspected" if kinds and kinds <= _INSPECT_KINDS else "ran"
        cwd = _uri_path(item.get("cwd"))
        paths = tuple(
            os.path.join(cwd, p["path"]) if cwd else p["path"]
            for p in parsed if isinstance(p.get("path"), str) and p["path"]
        )
        code = item.get("exit_code")
        ok = ok and (code is None or code == 0)
        if not ok and code == 1 and kinds and kinds <= _INSPECT_KINDS:
            # Measured (era B, 331 of 2,708 CommandExecution with non-zero
            # exit): grep/rg-style tools express "no match" as exit 1. If
            # parsed_cmd is entirely read kinds and output is empty, treat it
            # as that idiom — this rule catches 3 cases and never masks a
            # real failure.
            # Why this isn't broadened further (#11):
            # - parsed_cmd is all-or-nothing. If a compound command has even
            #   one unrecognized part (pwd, echo, 2>/dev/null redirect, ...),
            #   the whole command becomes a single unknown slot (never mixed
            #   with other items when unknown). So reaching `pwd; rg ...`
            #   would require splitting the raw shell string, which is
            #   guessing at command names the way era A did.
            # - For unknown, empty output isn't a signal of harmlessness:
            #   a build/type-check failure that redirected output to a file
            #   also looks empty.
            # - Even among read-only items, keep it a failure if there's
            #   output: sed/cat/ls on a missing path leave "No such file" on
            #   stdout (stderr is always empty — merged into stdout via
            #   pty). && short-circuits, so the last item being "search"
            #   doesn't mean that search produced the exit code either.
            # Risk: verification commands like `grep -q`/`rg -q` measure as
            # unknown, so they never hit this branch.
            output = "{}{}".format(item.get("stdout") or "", item.get("stderr") or "")
            if not output.strip():
                ok = True
        return verb, ok, guard.redact_b64(arg)[:ARG_LIMIT], paths
    if kind == "FileChange":
        changes = item.get("changes")
        paths = tuple(str(k) for k in changes) if isinstance(changes, dict) else ()
        return "modified", ok, (paths[0] if paths else "")[:ARG_LIMIT], paths
    return None


class _ExecOutcome:
    """Result of _parse_exec_outcome. ok=None means "can't tell from this
    output" — the event stays ok=True as before (same for an abort with no
    output at all)."""

    __slots__ = ("ok", "session_id")

    def __init__(self, ok: Optional[bool] = None, session_id: Optional[str] = None):
        self.ok = ok
        self.session_id = session_id


def _parse_exec_outcome(output) -> _ExecOutcome:
    """Reads the plain-text output of exec_command/write_stdin. Not JSON.

    "Process exited with code N" / "Exit code: N" → that code. "Process
    running with session ID N" → not finished yet, records only session_id
    to link with the write_stdin call. "aborted by user after ..." →
    failure. If none of the three match (unrecognized format, or an abort
    without this phrase), ok=None keeps True as before.
    """
    if not isinstance(output, str):
        return _ExecOutcome()
    # Only look at the header. The body after "Output:" is whatever the
    # program printed, and if a background build prints a line like "Exit
    # code: 0", it would beat the "running" header and lose write_stdin's
    # own failure. apply_patch's "Exit code: N\n...\nOutput:" splits at the
    # same spot.
    output = output.partition("\nOutput:")[0]
    m = _EXIT_CODE_RE.search(output)
    if m:
        return _ExecOutcome(ok=(int(m.group(1)) == 0))
    m = _RUNNING_SID_RE.search(output)
    if m:
        return _ExecOutcome(session_id=m.group(1))
    if _ABORTED_RE.search(output):
        return _ExecOutcome(ok=False)
    return _ExecOutcome()


@_register
class CodexCliAdapter:
    adapter_id = "codex-cli"
    capabilities = frozenset({Capability.READ, Capability.WRITE})
    # Measured (codex-cli 0.155.1, --dangerously-bypass-hook-trust): a
    # top-level {"additionalContext": ...} is rejected as "hook: SessionStart
    # Failed" and nothing gets injected. The nested hookSpecificOutput form
    # actually shows up in the rollout as
    # content_item_kinds=["hooks.additional_context"].
    wire = "claude"

    def __init__(self, *, home: Optional[str] = None, now=time.time) -> None:
        self._home = home
        self._now = now or time.time

    @property
    def home(self) -> str:
        return self._home or os.path.expanduser("~")

    def sessions_root(self) -> str:
        return os.path.join(self.home, ".codex", "sessions")

    # --- the 5 methods --------------------------------------------------------

    def detect(self) -> HarnessPresence:
        root = self.sessions_root()
        if os.path.isdir(root):
            return HarnessPresence(present=True, note=root)
        return HarnessPresence(present=False, note="not found: {}".format(root))

    def _scan(self, repo_root: Optional[str], include_headless: bool, *,
              deadline: Optional[float] = None, newest_first: bool = False):
        """The date-directory walk shared by list_sessions/discover/health.
        Subagent is always excluded; headless is toggled directly via
        `include_headless` — list_sessions/discover pass through
        `allow_headless()` (env) as-is, while health()'s "were there only
        headless sessions" check passes True regardless of env. Yields
        (path, meta) — assembling the SessionRef is the caller's job
        (list_sessions uses file mtime, discover uses session_meta.timestamp
        — different epoch definitions, so merging them here would blur
        invariant 6).
        """
        if repo_root is None:
            return
        root = os.path.realpath(repo_root)
        for directory in _recent_date_dirs(self.sessions_root(), SCAN_DAYS, self._now):
            if deadline is not None and time.time() > deadline:
                break
            # glob isn't sorted — it's filesystem order. Filenames start
            # with rollout-YYYY-MM-DDTHH-MM-SS-, so reverse sort is newest
            # first, and even if cut off by deadline, the most recent
            # sessions get read first (discover only).
            paths = glob.glob(os.path.join(directory, "rollout-*.jsonl"))
            if newest_first:
                paths = sorted(paths, reverse=True)
            for path in paths:
                if deadline is not None and time.time() > deadline:
                    break
                meta = session_meta(path)
                if not meta or _is_subagent(meta):
                    continue
                if _is_headless_meta(meta) and not include_headless:
                    continue
                cwd = meta.get("cwd")
                # Codex records the repo root in the rollout. Must judge
                # equal-or-descendant so a session started from a
                # subdirectory is caught too.
                if not isinstance(cwd, str) or not locate.is_within(root, cwd):
                    continue
                yield path, meta

    def list_sessions(self, repo_root: Optional[str]) -> List[SessionRef]:
        refs: List[SessionRef] = []
        for path, meta in self._scan(repo_root, allow_headless()):
            try:
                stat = os.stat(path)
            except OSError:
                continue
            refs.append(
                SessionRef(
                    adapter_id=self.adapter_id,
                    session_id=str(meta.get("session_id") or meta.get("id") or ""),
                    source_path=path,
                    cwd=meta.get("cwd"),
                    epoch=stat.st_mtime,
                    size=stat.st_size,
                )
            )
        refs.sort(key=lambda r: (-r.epoch, r.source_path))
        return refs

    def discover(self, repo_root: Optional[str],
                deadline: Optional[float] = None) -> List[SessionRef]:
        """Only used by `omhc mark`'s ledger backfill. Targets the same
        sessions as list_sessions (same directories, same interactive
        check), but **the epoch differs** — here it uses
        session_meta.timestamp (when the session actually started), not file
        mtime. Because cmd_mark compares this value against the ledger's
        latest start epoch to keep backfill order correct (invariant 6),
        mixing in mtime could make an old session, whose mtime was only
        bumped by a resume, look like the newest.

        If the timestamp can't be read (unrecognized shape), silently skip
        it — better to miss a row than put one into the ledger whose order
        can't be guaranteed.

        If `deadline` (the caller's time budget) is exceeded, stop mid-scan
        and return only what's been gathered so far — with 14 days of date
        directories, a large number of files could let this one call eat
        cmd_mark's entire hook budget.
        """
        refs: List[SessionRef] = []
        for path, meta in self._scan(repo_root, allow_headless(), deadline=deadline,
                                     newest_first=True):
            started = iso_epoch(meta.get("timestamp"))
            if not started:
                continue
            try:
                raw_size = os.path.getsize(path)
            except OSError:
                continue
            # Snap to a line boundary (review) — this value directly becomes
            # `_reactivate_grown_sessions`'s first baseline, so if stat lands
            # mid-record, that record becomes permanently unreadable once it
            # finishes being written. This is the first observation, so
            # there's no prior baseline to fall back to — if no newline is
            # found within 64KB (a single record over 64KB is being
            # written), just use the raw size. Underestimating to 0 would
            # make the next check read from the start of the file, mistake
            # the **original** human turn for a new one, and hand back old
            # content again (reproduced in review). Overestimating can only
            # miss that one long record, and that only costs something if it
            # was a human turn — a known limitation.
            size = fsio.line_aligned_size(path, raw_size)
            refs.append(
                SessionRef(
                    adapter_id=self.adapter_id,
                    session_id=str(meta.get("session_id") or meta.get("id") or ""),
                    source_path=path,
                    cwd=meta.get("cwd"),
                    epoch=started,
                    size=size,
                )
            )
        return refs

    def read_session(self, ref: SessionRef) -> SessionRead:
        events, unparsed, dropped, _end_offset = self._read(ref.source_path, 0)
        return SessionRead(ref=ref, events=tuple(events), unparsed=unparsed,
                           dropped=dropped)

    def read_session_since(self, ref: SessionRef, offset: int, *,
                           max_bytes: Optional[int] = None,
                           stop_at_human_turn: bool = False) -> Optional[SessionSince]:
        """Only reads past byte `offset` — measured (this machine, 13.8MB
        rollout): full read_session takes 593ms, so this path is the only
        thing that fits the hook budget (150ms). Since `_read` snaps to a
        line boundary, `offset` doesn't need to be a record boundary (#22,
        the file `codex exec resume` appends to).

        `max_bytes` (review: cost scales with the tail's growing size and
        can blow the hook budget — measured 396.6ms for a 17.7MB tail) and
        `stop_at_human_turn` (a caller that only needs to know whether a
        human turn exists stops at the first match instead of reading the
        rest) are optional arguments used only by
        `cli._reactivate_grown_sessions` — the defaults match today's
        (unlimited) behavior, so the conformance contract "same events as
        read_session past offset" still holds."""
        try:
            start = max(0, int(offset))
        except (TypeError, ValueError):
            start = 0
        events, unparsed, dropped, end_offset = self._read(
            ref.source_path, start, max_bytes=max_bytes,
            stop_at_human_turn=stop_at_human_turn)
        # _read already snaps to a line boundary and only produces records
        # after start, but filter once more anyway — proves the contract in
        # code too (must equal read_session restricted to offset>=start).
        events = tuple(e for e in events if e.offset >= start)
        return SessionSince(events=events, unparsed=unparsed, dropped=dropped,
                            end_offset=end_offset)

    def _read(self, path: str, start: int, *, max_bytes: Optional[int] = None,
             stop_at_human_turn: bool = False):
        """The parser shared by read_session/read_session_since (never
        duplicate the whitelist/guard logic, invariant 4). Reads from
        `start` onward — 0 means from the top. If `start` lands mid-record
        (the previous byte isn't a newline), skips the rest of that line and
        starts at the next newline. Never raises — degrades to empty events
        if the file is missing or corrupt.

        The fourth return slot is `end_offset` — the byte offset right after
        the **last fully read line** (review: taking os.stat's size directly
        as the baseline risks it landing mid-record, and once that record
        finishes being written, reading from there makes the skip-to-newline
        logic skip that entire record — the caller must use this value as
        the next baseline instead). A **final** line with no trailing
        newline (`for raw in fh` can hand over an incomplete record right at
        EOF — if it happened to be stat'd/read at that exact moment) is
        still parsed (read_session's events/unparsed output stays the same),
        but is not included in `end_offset` — once that record finishes
        being written, the next read must still be able to see the whole
        line again.
        If `max_bytes` is exceeded, lines past that cap aren't read at all
        and reading stops (naturally aligned at the last complete line
        before the cap). If `stop_at_human_turn`, stops as soon as a human
        said event is produced.
        """
        events: List[Event] = []
        dropped: Dict[str, int] = {}
        unparsed = 0
        pending: Dict[str, int] = {}  # call_id -> index into events
        # Can only be found via write_stdin's args.session_id — write_stdin's
        # own call_id is separate. Hang the original exec_command event that
        # hit "Process running with session ID N" here, and when write_stdin
        # comes later, also add its call_id to pending pointing at the same
        # index (see below).
        pending_by_session: Dict[str, int] = {}
        seq = 0
        offset = start

        def bump(key: str) -> None:
            dropped[key] = dropped.get(key, 0) + 1

        try:
            fh = open(path, "rb")
        except OSError as exc:
            return events, 0, {"open_failed": 1, str(exc.errno): 1}, start

        with fh:
            if start > 0:
                try:
                    fh.seek(start - 1)
                    prev = fh.read(1)
                    if prev != b"\n":
                        # start lands mid-record — discard the rest of that line.
                        skipped = fh.readline()
                        offset = start + len(skipped)
                except OSError:
                    return events, 0, {"seek_failed": 1}, start
            since_start = offset
            end_offset = offset
            for raw in fh:
                if max_bytes is not None and (offset - since_start) >= max_bytes:
                    # Lines past the cap aren't read at all — offset is
                    # already parked at the end of the last complete line
                    # before it, so end_offset is naturally on a line boundary.
                    bump("max_bytes_cap")
                    break
                start = offset
                offset += len(raw)
                if raw.endswith(b"\n"):
                    # Only a line ending in a newline was "safely fully read" — see docstring above.
                    end_offset = offset
                try:
                    row = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    unparsed += 1
                    continue
                if not isinstance(row, dict):
                    unparsed += 1
                    continue

                envelope = str(row.get("type"))
                if envelope not in _PARSED_ENVELOPES:
                    bump(envelope)
                    continue
                payload = row.get("payload")
                if not isinstance(payload, dict):
                    unparsed += 1
                    continue

                epoch = iso_epoch(row.get("timestamp"))
                kind = str(payload.get("type"))

                if envelope == "event_msg":
                    item = payload.get("item")
                    if kind != "item_completed" or not isinstance(item, dict):
                        bump(envelope)
                        continue
                    fact = _item_fact(item)
                    if fact is None:
                        # UserMessage/AgentMessage are copies of the response_item.
                        bump("item:" + str(item.get("type")))
                        continue
                    verb, ok, arg, paths = fact
                    item_id = item.get("id")
                    idx = pending.get(item_id) if isinstance(item_id, str) else None
                    if idx is not None:
                        # era A: apply_patch's call_id equals this FileChange's
                        # id — only here do we have the absolute-path fact, so
                        # update the original event (counting both would be
                        # double counting). Keep seq/offset/length from the
                        # first record (invariant 6).
                        events[idx] = events[idx]._replace(verb=verb, ok=ok, arg=arg,
                                                            paths=paths)
                        continue
                    seq += 1
                    events.append(Event(
                        seq=seq, epoch=epoch, author="agent", verb=verb, ok=ok,
                        text="", arg=arg, paths=paths, offset=start, length=len(raw),
                    ))
                    continue

                if kind == "message":
                    role = str(payload.get("role"))
                    if role not in _PARSED_ROLES:
                        bump("role:" + role)
                        continue
                    text = guard.redact_b64(_text_of(payload.get("content")).strip())
                    author = "human" if role == "user" else "agent"
                    if author == "human":
                        kinds = human_kinds(payload)
                        if kinds is not None and not any(
                            k.startswith(_HUMAN_KIND_PREFIX) for k in kinds
                        ):
                            # Machinery disguised as role=user. Measured:
                            # ['environments.environment_context']
                            bump("kind:" + kinds[0])
                            continue
                    if not guard.safe(text, author):
                        bump("guarded_" + author)
                        continue
                    if stop_at_human_turn and author == "human" and not raw.endswith(b"\n"):
                        # 3rd review #2: it's a human turn but no newline has
                        # been appended yet (may have been stat'd mid-write)
                        # — don't count this record as a trigger (don't even
                        # produce an event for it). Counting it would let the
                        # next round, once the newline is finally appended,
                        # keep the baseline in front of this record, find the
                        # same turn again, and deliver it twice — calls other
                        # than `stop_at_human_turn` (including read_session)
                        # don't take this branch, so their output is unchanged.
                        continue
                    seq += 1
                    events.append(Event(
                        seq=seq, epoch=epoch, author=author, verb="said", ok=True,
                        text=text, arg="", paths=(), offset=start, length=len(raw),
                    ))
                    if stop_at_human_turn and author == "human":
                        # The caller only asked "is there a new human turn" —
                        # stop as soon as it's found — end_offset already
                        # moved past this line if it ended in a newline
                        # (updated above).
                        return events, unparsed, dropped, end_offset
                    continue

                if kind == "custom_tool_call" and payload.get("name") == _JS_WRAPPER_TOOL:
                    bump("js_exec")
                    continue

                if kind in ("function_call", "local_shell_call", "custom_tool_call"):
                    name = str(payload.get("name") or kind.replace("_call", ""))
                    call_id = payload.get("call_id")

                    if name == "write_stdin":
                        # Sends input to an exec_command that was running in
                        # the background — this itself is bookkeeping, but
                        # this call_id's output (below) is the original
                        # exec_command event's final result. Find that event
                        # by session_id and hang this call_id on the same index.
                        sid = None
                        args_raw = payload.get("arguments")
                        args_parsed = None
                        if isinstance(args_raw, str):
                            try:
                                args_parsed = json.loads(args_raw)
                            except ValueError:
                                args_parsed = None
                        elif isinstance(args_raw, dict):
                            args_parsed = args_raw
                        if isinstance(args_parsed, dict):
                            sid = args_parsed.get("session_id")
                        target = pending_by_session.get(str(sid)) if sid is not None else None
                        if isinstance(call_id, str) and target is not None:
                            pending[call_id] = target
                        bump("tool_bookkeeping")
                        continue

                    if name in _BOOKKEEPING_TOOLS:
                        bump("tool_bookkeeping")
                        continue

                    verb = _VERB_BY_TOOL.get(name)
                    if verb is None:
                        bump("unmapped_tool")
                        continue
                    arg, paths = _arg_and_paths(payload)
                    seq += 1
                    events.append(Event(
                        seq=seq, epoch=epoch, author="agent", verb=verb, ok=True,
                        text="", arg=arg, paths=paths, offset=start, length=len(raw),
                    ))
                    if isinstance(call_id, str):
                        pending[call_id] = len(events) - 1
                    continue

                if kind in ("function_call_output", "local_shell_call_output",
                            "custom_tool_call_output"):
                    outcome = _parse_exec_outcome(payload.get("output"))
                    call_id = payload.get("call_id")
                    if outcome.session_id is not None:
                        idx = pending.get(call_id)
                        if idx is not None:
                            pending_by_session[outcome.session_id] = idx
                    elif outcome.ok is not None:
                        idx = pending.get(call_id)
                        if idx is not None:
                            events[idx] = events[idx]._replace(ok=outcome.ok)
                    bump("tool_output")
                    continue

                if kind == "reasoning":
                    bump("reasoning")
                    continue

                if kind in ("web_search_call", "tool_search_call", "tool_search_output",
                            "agent_message"):
                    # agent_message is an inter-agent message (170 measured
                    # instances) — neither human nor said (invariant 3).
                    # web_search_call/tool_search_* are unmeasured
                    # bookkeeping candidates, so tallied as dropped instead
                    # of unparsed.
                    bump(kind)
                    continue

                # response_item with an unrecognized shape -> never dropped silently.
                unparsed += 1

        return events, unparsed, dropped, end_offset

    def classify(self, source_path: str) -> bool:
        """Was this a Codex rollout started by a human?

        Only returns False when it's **certain** to be a subagent/headless
        exec (`_is_interactive`). If session_meta can't be read (empty file,
        unrecognized first line), it can't be judged, so True — brief only
        skips rows classified False, so returning False here would make
        every new rollout since a format change get skipped, sending out the
        stale session before it instead (#21). The eligibility check for
        opening is handled separately by ref_for_path.
        """
        meta = session_meta(source_path)
        return meta is None or _is_interactive(meta)

    def ref_for_path(self, source_path: str, session_id: str,
                     cwd: Optional[str] = None) -> Optional[SessionRef]:
        meta = session_meta(source_path)
        if meta is None or not _is_interactive(meta):
            return None
        try:
            stat = os.stat(source_path)
        except OSError:
            return None
        if not stat.st_size:
            return None
        meta_cwd = meta.get("cwd")
        return SessionRef(
            adapter_id=self.adapter_id,
            session_id=session_id or str(meta.get("session_id") or ""),
            source_path=source_path,
            cwd=meta_cwd if isinstance(meta_cwd, str) else cwd,
            epoch=stat.st_mtime,
            size=stat.st_size,
        )

    def native_resume_hint(self, ref: SessionRef) -> Optional[str]:
        if not ref.session_id:
            return None
        return "codex resume {}".format(ref.session_id)

    def hooks_path(self) -> str:
        return os.path.join(self.home, ".codex", "hooks.json")

    def toml_config_path(self) -> str:
        return os.path.join(self.home, ".codex", "config.toml")

    def hook_config(self):
        return hookconf.HookConfig(
            config_path=self.hooks_path(),
            fragment_name="codex-hooks.json",
            post_write_note=(
                "Measured (codex-cli 0.155.1): a hand-placed hooks.json is untrusted "
                "by default — it only actually runs after you approve it once through "
                "Codex's own hook-trust procedure."
            ),
        )

    def _project_trust_level(self, repo_root: str) -> Optional[str]:
        """The `trust_level` value under `[projects."<repo_root>"]` in
        `~/.codex/config.toml` (#32). Doesn't parse the whole TOML file —
        finds every top-level header with the shared scanner
        (`hookconf.toml_header_lines`, which recognizes strings/comments),
        picks the one header exactly matching this repo path, and reads only
        the `trust_level` key from its body (up to the next header) with a
        regex — review: this used to re-find the end of the body with
        `^[ \\t]*\\[`, which repeats exactly the problem #31 already fixed
        ("a `[` inside a multi-line array value can also sit at the start of
        a line"). If not found (file missing, no header for this repo at
        all, unreadable), returns None — "trust unknown", not "untrusted"
        (the caller treats this as grounds to ignore the project layer entirely).
        """
        real = os.path.realpath(repo_root)
        try:
            with open(self.toml_config_path(), encoding="utf-8-sig", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return None
        try:
            headers = hookconf.toml_header_lines(text)
        except Exception:
            return None
        needle = 'projects."{}"'.format(real)
        for idx, (start, end) in enumerate(headers):
            line = text[start:end].strip()
            if not (line.startswith("[") and line.endswith("]")):
                continue
            if line.strip("[]") != needle:
                continue
            body_end = headers[idx + 1][0] if idx + 1 < len(headers) else len(text)
            body = text[end:body_end]
            tm = re.search(r'(?m)^[ \t]*trust_level[ \t]*=[ \t]*"([^"]*)"', body)
            return tm.group(1) if tm else None
        return None

    def _hook_layers(self, repo_root: Optional[str]):
        """(path, does it have an omhc call that runs at session start) for
        each of the four places omhc brief can actually be called from —
        per official docs (config-advanced, "Hooks" section, #32):
        `~/.codex/hooks.json`, the inline `[hooks]` in `~/.codex/config.toml`,
        `<repo>/.codex/hooks.json`, `<repo>/.codex/config.toml` (project side
        only when that `.codex/` layer is trusted). `hook_is_installed` and
        `_install_source` (#32 review 2 — the mtime check used to only look
        at hooks.json) share this one list."""
        flags = {"--harness": self.adapter_id}
        layers = [
            (self.hooks_path(), hookconf.has_runnable_call(
                self.hooks_path(), "brief", flags)),
            (self.toml_config_path(), self.inline_hook_present()),
        ]
        if repo_root and self._project_trust_level(repo_root) == "trusted":
            project_dir = os.path.join(repo_root, ".codex")
            project_json = os.path.join(project_dir, "hooks.json")
            project_toml = os.path.join(project_dir, "config.toml")
            layers.append((project_json,
                           hookconf.has_runnable_call(project_json, "brief", flags)))
            layers.append((project_toml,
                           hookconf.has_runnable_call_toml(project_toml, "brief", flags)))
        return layers

    def hook_is_installed(self, repo_root: Optional[str] = None) -> bool:
        """Is a SessionStart hook that calls omhc installed?

        A pull channel (state artifact) only counts as delivered if a hook
        actually reads it. Without a hook, writing the artifact is seen by
        no one, so it isn't delivery — reporting that as success would mean
        Path B never fires. Only looks at the project layer when
        `repo_root` is given and that repo is confirmed trusted — if trust
        can't be confirmed, the project layer isn't looked at (ignoring is
        safer than over-trusting).
        """
        try:
            return any(present for _path, present in self._hook_layers(repo_root))
        except Exception:
            return False

    def _install_source(self, repo_root: Optional[str]):
        """The (epoch, path) with the most recent mtime among the files
        where the hook is actually installed (those `_hook_layers` marked
        present=True) — this is the reference point `_hook_health` uses to
        measure "has a session run since this time" (#32 review 1).

        This used to always take `hooks_path()`'s mtime as the reference
        point — for an inline-only install (no hooks.json file at all), that
        stat died with ENOENT, leaving `_hook_health` permanently stuck at
        `----(unknown)`, and the very row meant to catch a silently skipped
        untrusted inline hook couldn't do its job.

        If both are installed, uses whichever changed more recently — either
        one being recently touched is a signal. Codex itself rewrites
        config.toml often (hooks.state trust hash, `[projects...]`
        trust_level, etc) — that pushes this mtime well past the actual hook
        install time, narrowing the judgment window (post install_epoch) and
        increasing how often it stays `----` (unjudged). That's a
        conservative failure direction and safe (it misses a FAIL rather
        than mislabeling one) — so it deliberately doesn't try to more
        precisely determine "was this mtime change a real hook reinstall".
        """
        candidates = []
        for path, present in self._hook_layers(repo_root):
            if not present:
                continue
            try:
                candidates.append((os.path.getmtime(path), path))
            except OSError:
                continue
        if not candidates:
            return None
        return max(candidates, key=lambda c: c[0])

    def inline_hook_present(self) -> bool:
        """Does config.toml's inline `[hooks]` have an omhc brief call that
        actually runs at session start (even if not identical to the
        shipped fragment)? "Does it exist" and "is it identical to the
        shipped fragment" are different questions (review #1: conflating the
        two made `hooks_status()` wrongly say "not found" even when the
        inline hook was broken — e.g. missing mark — and let `omhc hooks
        install` layer another hooks.json on top, ending up with Codex
        loading both layers and warning). `omhc hooks install` uses only this
        function to decide whether to write a duplicate into hooks.json —
        the "exists" check is here, and the "is it correct" check is
        `hooks_status()`."""
        return hookconf.has_runnable_call_toml(
            self.toml_config_path(), "brief", {"--harness": self.adapter_id})

    def hooks_status(self):
        """Only used for `omhc status`'s `codex-cli hooks` row — the
        general `hookconf.inspect` only looks at hooks.json. Codex also reads
        the inline `config.toml [hooks]` alongside it (#32), so this looks
        at both.

        Which layer counts as "installed" is decided by existence
        (`has_runnable_call*`, whether an omhc brief call actually runs at
        session start) — a previous defect (review #1) conflated this with
        `inspect`/`inspect_toml`'s "is it exactly identical to the shipped
        fragment" check: it wrongly said "not found" for a situation that
        should have said "differs" (inline exists but mark is missing, or
        flags differ). Only for layers that exist does it add `inspect`/
        `inspect_toml`'s "is it correct" check.

        If both layers exist, the docs (config-advanced, Hooks section)
        state explicitly that "Codex loads both and warns" — if both are
        correct, that warning is surfaced as unjudged (`----`) instead of
        PASS, and if either is broken, FAIL points out which one. This row
        doesn't look at the project layer (`<repo>/.codex/...`) — this is a
        generic status row with no specific repo context; the project hook
        is checked separately by `hook_is_installed(repo_root)` at
        `install_handoff` time.
        """
        fragment = hookconf.load_fragment(self.hook_config().fragment_name)
        flags = {"--harness": self.adapter_id}
        json_path = self.hooks_path()
        toml_path = self.toml_config_path()

        json_present = hookconf.has_runnable_call(json_path, "brief", flags)
        toml_present = self.inline_hook_present()
        json_ok, json_detail = hookconf.inspect(json_path, fragment, self.home)
        toml_ok, toml_detail = hookconf.inspect_toml(toml_path, fragment, self.home)
        if toml_present and not toml_ok:
            # hookconf's generic detail ends with "... — run `omhc hooks
            # install`" — right advice if the target is hooks.json, but once
            # inline already exists (post review #1), `omhc hooks install`
            # just fails instead of writing hooks.json on top of it —
            # following that advice goes in a circle (review #2). Replace it
            # with advice that's actually correct.
            toml_detail = self._inline_advice(toml_path, toml_detail)

        if json_present and toml_present:
            if json_ok and toml_ok:
                return None, (
                    "installed in both {} and inline [hooks] in {} — Codex loads both "
                    "and warns (see config-advanced docs, Hooks section); keep one"
                    .format(json_path, toml_path))
            broken = []
            if not json_ok:
                broken.append("{}: {}".format(json_path, json_detail))
            if not toml_ok:
                broken.append(toml_detail)
            return False, "installed in both layers but {}".format("; ".join(broken))
        if json_present:
            if json_ok:
                return True, "installed ({})".format(json_path)
            return False, "{}: {}".format(json_path, json_detail)
        if toml_present:
            if toml_ok:
                return True, "installed via inline [hooks] in {}".format(toml_path)
            return False, toml_detail
        return False, "{} / also not found via inline [hooks] in {} ({})".format(
            json_detail, toml_path, toml_detail)

    def _inline_advice(self, toml_path: str, detail: str) -> str:
        """Replaces `hookconf.inspect_toml`'s generic "... — run `omhc hooks
        install`" advice with advice that's actually correct once inline
        already exists (#32 review 2 — that command just fails instead of
        writing on top of hooks.json when inline exists, so following the
        advice circles back to the same place)."""
        circular_suffix = " — run `omhc hooks install`"
        if detail.endswith(circular_suffix):
            detail = detail[:-len(circular_suffix)]
        return (
            "inline [hooks] in {}: {} — fix or remove the omhc entries in the "
            "inline [hooks] of {} (or remove them and run `omhc hooks install`)"
        ).format(toml_path, detail, toml_path)

    def install_handoff(self, bundle: HandoffBundle) -> InstallReceipt:
        if not self.hook_is_installed(bundle.repo_root):
            raise NoInjectionChannel(
                "no omhc SessionStart hook at {}; the artifact would be written but "
                "never read".format(self.hooks_path())
            )
        receipt = install_state_artifact(bundle, home=self._home)
        self._collapse_stale_agents_md_block(bundle.repo_root)
        return receipt

    def _collapse_stale_agents_md_block(self, repo_root: str) -> None:
        """If Path A (a fresh hook handoff) succeeded but an old Path B
        section (laid down during a #33 budget overrun, or a period the hook
        wasn't running) still sits beside it, the next Codex session reads
        both the fresh hook handoff and the stale AGENTS.md instructions at
        once (#36) — collapse() itself only deletes after 24 hours, so
        force=True here deletes it immediately. Uses the exact same guard as
        the `#33` rejection path: never touches it if AGENTS.md is shared
        with Claude Code. Since this method is called from the hook path
        (brief -> deliver -> install_handoff), it must never raise no matter
        what it does (invariant 2)."""
        from .. import agents_md

        try:
            if not agents_md.shared_with_claude(repo_root):
                agents_md.collapse(repo_root, force=True)
        except Exception:
            pass

    def on_session_start_mark(self, repo_root: str, *, source: str, epoch: float) -> None:
        """#36: Codex reads AGENTS.md **before** its own SessionStart hook
        runs (measured, codex-cli 0.156.1 sandbox, rollout evidence) — the
        first turn has already read it before the hook can edit or delete
        the file, so deleting the block here can't change the fact that
        "this session already read it". Still, deleting it now means the
        **next** Codex session can't read this block — the block's lifetime
        shrinks to "until this session consumes it" (it used to be only a
        24-hour staleness window). On the next turn, Codex itself notes that
        "the previous AGENTS.md instructions no longer apply" (measured) —
        consistent with the handoff's "not an instruction" nature.

        Only checks `source` == "startup" — the ordering "reading happens
        before the hook" was **only measured at startup** (review). Whether
        Codex computes the AGENTS.md diff at resume (the next turn of the
        same session) hasn't been measured yet (possibly at the moment of
        the human's first turn input) — if that happens after the hook,
        collapsing at resume too could delete a block this very session's
        own brief just wrote, that this turn hasn't even seen yet. So resume
        is left untouched. "compact" isn't a new turn (same distinction as
        #30), so the caller (cmd_mark) never calls this for it, but it's
        checked again here too as a defense.

        Race (review #1): within the same SessionStart, Codex runs its hooks
        in parallel (measured) — mark (this method) and brief (installing
        Path B) can run concurrently. If brief already wrote a new block for
        **this session** because it chose Path B (e.g. hooks.json
        untrusted), and mark mistakes it for an "already-read stale block"
        and deletes it, the handoff becomes unreadable even for this
        session. Since the block's captured_at is rounded to whole seconds
        (managed_block), if it's the same second as `epoch` (the ledger
        epoch mark recorded for this session) or later, or within
        `_CONSUMED_BLOCK_MARGIN_SECONDS`, it could belong to this session
        (or a later one), so it's left alone. Even so, there's still a
        window between judging it here and actually deleting it
        (check-then-act) — `agents_md.collapse_if_captured` only deletes
        after re-checking that value (see managed_block.strip_if_captured):
        if another process wrote a new section in between, the value would
        differ and it's left untouched. Uses the same guard as the `#33`
        rejection path too: never touches it if AGENTS.md is shared with
        Claude Code (checked first). Called from the hook path, so it must
        never raise (invariant 2)."""
        if source != "startup":
            return
        from .. import agents_md, managed_block

        try:
            if agents_md.shared_with_claude(repo_root):
                return
            path = agents_md.path_for(repo_root)
            captured = managed_block.installed_captured_at(path)
            if captured is None:
                return
            if captured > epoch - _CONSUMED_BLOCK_MARGIN_SECONDS:
                return
            agents_md.collapse_if_captured(repo_root, captured)
        except Exception:
            pass

    def fallback_channels(self):
        """Path B: only opens when install_handoff fails —

        i.e. this brief call actually ran (the codex-cli hook ran trusted,
        or it was manual `omhc brief --harness codex-cli`), but
        install_handoff failed. Typically raises NoInjectionChannel when
        hooks.json has no omhc hook string, but deliver()'s channel loop
        routes other install_handoff exceptions here too the same way (e.g.
        failure writing ~/.omhc). If the hook itself is untrusted and brief
        never runs at all, deliver() itself is never called, so Path B never
        opens either — in that case the Codex-bound direction receives
        nothing at all."""
        return (self._install_agents_md,)

    def _install_agents_md(self, bundle: HandoffBundle) -> InstallReceipt:
        """Measures the #33 budget before calling `agents_md.install`.

        The section is now always at the top of the file
        (managed_block.splice), so where it ends is determined solely by
        `len(prefix+block)`, regardless of the existing AGENTS.md size.
        Doesn't add in ancestor directories' AGENTS.md bytes — Codex's
        project root is approximated as this repo's `repo_root` (a personal
        tool, rarely used starting from a subdirectory), and measuring the
        case of a large ancestor AGENTS.md too would be much more expensive
        than just looking at this one file (every ancestor's file would need
        opening) — if the budget is exceeded, install is never claimed at
        all, and deliver() falls through to the universal floor (outbox)
        (invariant 2: doesn't die here; deliver()'s exception catch already
        exists, but the reason is also logged to guard.log).

        `project_doc_max_bytes = 0` means (per codex-rs project_doc.rs, an
        unconfirmed measured basis) Codex doesn't read AGENTS.md at all, so
        it's always rejected — anything else below 0 / unparseable is
        already folded to the default by `_project_doc_max_bytes`.

        Review defect: rejecting for budget overrun while leaving an
        already-installed (from-when-it-was-smaller) stale section in place
        means Codex keeps reading that stale section instead of the new
        handoff that fell to outbox — so the stale section is also deleted
        on rejection. Left untouched if AGENTS.md is shared with Claude (same
        guard as agents_md.install)."""
        from .. import agents_md, brief, managed_block

        limit = self._project_doc_max_bytes(self.toml_config_path())
        if limit <= 0:
            reason = (
                "project_doc_max_bytes={} in {} — Codex does not load AGENTS.md at all"
            ).format(limit, self.toml_config_path())
        else:
            path = agents_md.path_for(bundle.repo_root)
            end = managed_block.prospective_block_end_bytes(
                path, bundle.body_md, captured_at=time.time())
            reason = None
            if end > limit:
                reason = (
                    "AGENTS.md omhc block would end at byte {} in {}, past Codex's "
                    "project_doc_max_bytes={} ({}) — Codex would not see it"
                ).format(end, path, limit, self.toml_config_path())

        if reason is None:
            return agents_md.install(bundle)

        brief.log_failure(self.home, reason)
        if not agents_md.shared_with_claude(bundle.repo_root):
            agents_md.collapse(bundle.repo_root, force=True)
        raise NoInjectionChannel(reason)

    def _config_trusts(self, hook_file_path: str) -> bool:
        """Does `~/.codex/config.toml` have a `hooks.state` entry saying the
        hook at `hook_file_path` (usually hooks.json — see below for
        inline) is trusted? This is a different mechanism from
        `[projects."<repo>"] trust_level` — that gates whether the whole
        project-side `.codex/` layer (hooks.json or inline) is read at all,
        while this is a hash saying Codex has already approved one
        **specific piece of content** in the user-level hooks.json (measured,
        this machine: `hooks.state."<absolute hooks.json path>:session_start:<idx>:<idx>"`).
        3.9 has no tomllib, so a plain substring search is enough — the hash
        semantics (what exactly Codex hashes in the hook content) haven't
        been confirmed, so this signal alone never decides a failure verdict
        (it's just a static hint).

        Whether inline hooks (`[[hooks.SessionStart]]` written directly in
        `~/.codex/config.toml`) use the same
        `hooks.state."<config.toml path>:session_start:..."` key format also
        hasn't been confirmed — the caller (`_hook_health`) states that
        uncertainty explicitly in its detail."""
        config_path = self.toml_config_path()
        needle = 'hooks.state."{}:session_start:'.format(hook_file_path)
        try:
            with open(config_path, encoding="utf-8", errors="replace") as fh:
                return needle in fh.read()
        except OSError:
            return False

    def _config_trusts_hook(self) -> bool:
        """Backward-compat alias — only looks at hooks.json's own trust hash (the measured path)."""
        return self._config_trusts(self.hooks_path())

    def _repo_matches(self, repo_root: Optional[str], cwd) -> bool:
        """Does this candidate (cwd) actually belong to `repo_root`?

        If `repo_root` itself is a real repo with a `.git`, then a
        worktree/submodule inside it (a nested directory with its own
        `.git`, e.g. `.claude/worktrees/*`) is still considered to belong to
        this repo — trusting list_sessions's is_within (path-containment)
        check as-is (TestHealthMatchesAcrossNestedGitRoots). It's a
        different story if `repo_root` itself has no `.git` (resolve_repo_root's
        fallback, i.e. status was called from a "non-git parent directory")
        and there's a completely **unrelated** repo inside it with its own
        `.git` — that session is not evidence that this repo's hook ran.
        Only in that case does it strictly compare repo keys.
        """
        if repo_root is None:
            return True
        if os.path.exists(os.path.join(repo_root, ".git")):
            return True
        return locate.owning_repo_key(cwd) == locate.owning_repo_key(repo_root)

    def _codex_root_markers(self, config_path: str):
        """Reads the `project_root_markers` value from `~/.codex/config.toml`.

        3.9 has no tomllib — instead of parsing the whole TOML file, pulls
        just this one key with a regex (single/multi-line array, bare/quoted
        key, both basic/literal strings). Returns (state, markers-or-error-string):
          "missing"  — file doesn't exist at all (default [".git"] applies)
          "unreadable" — exists but can't be read (permissions, is a directory, etc) — reason in the second slot
          "absent"   — key isn't present at the top level (before the first `[section]`) (same default applies)
          "table"    — key only appears under a `[section]` (TOML tables
                       scope keys — not a top-level key)
          "unparseable" — array contains something that isn't a string (integer, nested expression, ...)
          "ok"       — value tuple in the second slot
        Opening with `utf-8-sig` prevents a BOM'd file from making the regex
        miss the line start (`^`) and look like the key is absent.
        """
        try:
            with open(config_path, encoding="utf-8-sig", errors="replace") as fh:
                text = fh.read()
        except FileNotFoundError:
            return "missing", None
        except OSError as exc:
            return "unreadable", str(exc)
        header = _first_table_header(text)
        top_level = text[:header] if header >= 0 else text
        m = _ROOT_MARKERS_KEY_RE.search(top_level)
        if not m:
            if header >= 0 and _ROOT_MARKERS_KEY_RE.search(text[header:]):
                return "table", None
            return "absent", None
        array_text = m.group(1)
        # A second `[` inside the value means a nested array — not the
        # string array Codex expects, so this can't be confidently read (review).
        if _TOML_STRING_RE.sub("", array_text).count("[") > 1:
            return "unparseable", None
        # findall fills unmatched groups with an empty string (not None) —
        # to distinguish the two string kinds, need finditer and check group
        # participation (None) directly.
        values = [sm.group("d") if sm.group("d") is not None else sm.group("s")
                  for sm in _TOML_STRING_RE.finditer(array_text)]
        # If anything remains after stripping out every string literal
        # (integer, nested array, comment, ...), this array can't be
        # confidently read.
        leftover = _TOML_STRING_RE.sub("", array_text)
        leftover = re.sub(r"[\[\],\s]", "", leftover)
        if leftover:
            return "unparseable", None
        return "ok", tuple(v.replace('\\"', '"') for v in values)

    def _project_doc_max_bytes(self, config_path: str) -> int:
        """`project_doc_max_bytes` value from `~/.codex/config.toml`, or the
        embedded default if missing/unreadable (#33). Same principle as
        `_codex_root_markers` — only looks at the top-level key (before the
        first `[section]`), never raises (fail-open).

        Returns `0` as-is (doesn't fold it to the default) — per codex-rs's
        project_doc.rs (review, unconfirmed by binary strings), `0` means
        "doesn't read AGENTS.md at all", and the caller
        (`_install_agents_md`/`_agents_md_budget_health`) must judge on that
        value itself. Only negative values / unparseable ones fold to the default."""
        try:
            with open(config_path, encoding="utf-8-sig", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return DEFAULT_PROJECT_DOC_MAX_BYTES
        header = _first_table_header(text)
        top_level = text[:header] if header >= 0 else text
        m = _PROJECT_DOC_MAX_BYTES_KEY_RE.search(top_level)
        if not m:
            return DEFAULT_PROJECT_DOC_MAX_BYTES
        try:
            value = int(m.group(1))
        except ValueError:
            return DEFAULT_PROJECT_DOC_MAX_BYTES
        return value if value >= 0 else DEFAULT_PROJECT_DOC_MAX_BYTES

    def _ancestor_has_git(self, repo_root: str, git_marker: str) -> bool:
        """Review #4: if an ancestor above `repo_root` has a `.git`, then
        Codex's default (`[".git"]`) also picks that ancestor as the root
        and reads AGENTS.md down to cwd (the repo's own AGENTS.md is on
        that path too) — in that case there's no need to add `.omhc-root`."""
        real = os.path.realpath(repo_root).rstrip("/") or "/"
        current = os.path.dirname(real)
        while True:
            if os.path.exists(os.path.join(current, git_marker)):
                return True
            parent = os.path.dirname(current)
            if parent == current:
                return False
            current = parent

    def _root_marker_health(self, repo_root: Optional[str]):
        """#31: in a project determined only by `.omhc-root` (no `.git`, and
        no `.git` ancestor above it either), Codex started from a subfolder
        won't read the ancestor AGENTS.md with the default
        `project_root_markers = [".git"]` (measured, codex-cli 0.155.1) —
        Path B (AGENTS.md managed block) is silently neutered. Needs
        `.omhc-root` added to `~/.codex/config.toml` to work.

        Not gated (review defect): Path B is a fallback that only opens when
        `install_handoff` fails, so when the omhc hook is installed and
        trusted (the usual case) this setting has no effect at all, yet
        gating with FAIL would make a normal install always FAIL. Instead
        this is always `----` (ok=None) unless PASS, and only notes "this is
        currently the only channel" when the hook isn't installed.
        """
        if repo_root is None:
            return None
        git_marker, omhc_marker = locate.ROOT_MARKERS
        if os.path.exists(os.path.join(repo_root, git_marker)):
            return None
        if not os.path.exists(os.path.join(repo_root, omhc_marker)):
            return None
        if self._ancestor_has_git(repo_root, git_marker):
            return None
        config_path = os.path.join(self.home, ".codex", "config.toml")
        hint = ('add to {}: project_root_markers = ["{}", "{}"] (keep "{}")'
                .format(config_path, git_marker, omhc_marker, git_marker))
        if self.hook_is_installed(repo_root):
            channel_note = ("only matters if the omhc Codex hook stops being "
                            "installed/trusted — Path A currently delivers")
        else:
            channel_note = ("the omhc Codex hook isn't installed, so this AGENTS.md "
                            "fallback (Path B) is currently the only channel to Codex")
        state, extra = self._codex_root_markers(config_path)
        if state == "unreadable":
            return ("codex root markers", None,
                    "cannot read {} ({}) — {}".format(config_path, extra, channel_note))
        if state == "unparseable":
            return ("codex root markers", None,
                    "cannot judge project_root_markers in {} (unrecognized format) — {}"
                    .format(config_path, channel_note))
        if state == "table":
            return ("codex root markers", None,
                    "project_root_markers in {} is inside a [section] (TOML tables "
                    "scope keys) — move it to the top level, above the first "
                    "[section] — {} — {}"
                    .format(config_path, hint, channel_note))
        if state == "missing":
            return ("codex root markers", None,
                    "{} not found (default project_root_markers = [\"{}\"]) — {} — {}"
                    .format(config_path, git_marker, hint, channel_note))
        if state == "absent" or omhc_marker not in extra:
            return ("codex root markers", None,
                    "project_root_markers in {} lacks \"{}\" — {} — {}"
                    .format(config_path, omhc_marker, hint, channel_note))
        return ("codex root markers", True,
                "project_root_markers includes \"{}\"".format(omhc_marker))

    def _agents_md_budget_health(self, repo_root: Optional[str]):
        """#33: if an already-installed section ends past Codex's
        `project_doc_max_bytes` budget, Codex can't read it — this is
        prevented at the point `_install_agents_md` writes (from the next
        splice onward), but a section that's been installed past budget and
        left alone for a while also needs to be surfaced via status.

        Follows the same two-stage principle as `codex root markers`: for a
        repo where this diagnostic is inherently meaningless (AGENTS.md is
        shared with Claude, so Path B is never used at all — same guard as
        `agents_md.install`/`_install_agents_md`), no row is emitted at all
        (``None``). If the diagnostic is valid but there's no basis yet to
        judge (no installed section — either not installed yet, or
        collapsed), the row is emitted as `----` but not gated — "every
        check gets PASS/FAIL/---- (never SKIP)" (README) applies to
        diagnostics that can actually be judged, not to a repo where the
        diagnostic itself is meaningless."""
        if repo_root is None:
            return None
        from .. import agents_md, managed_block

        if agents_md.shared_with_claude(repo_root):
            return None

        path = agents_md.path_for(repo_root)
        end = managed_block.installed_block_end_bytes(path)
        if end is None:
            return ("codex agents.md budget", None, "no omhc block in AGENTS.md")
        limit = self._project_doc_max_bytes(self.toml_config_path())
        if limit <= 0:
            return ("codex agents.md budget", False,
                     "project_doc_max_bytes={} in {} — Codex does not load AGENTS.md at all"
                     .format(limit, self.toml_config_path()))
        if end > limit:
            return ("codex agents.md budget", False,
                     "AGENTS.md omhc block ends at byte {} > project_doc_max_bytes={} "
                     "in {} — Codex would not see it".format(
                         end, limit, self.toml_config_path()))
        return ("codex agents.md budget", True,
                "AGENTS.md omhc block ends at byte {} (limit {})".format(end, limit))

    def health(self, repo_root: Optional[str], ledger_rows):
        rows = []
        try:
            marker_row = self._root_marker_health(repo_root)
        except Exception as exc:  # this diagnostic must not kill status itself either
            marker_row = ("codex root markers", None, "unknown ({})".format(exc))
        if marker_row is not None:
            rows.append(marker_row)
        try:
            budget_row = self._agents_md_budget_health(repo_root)
        except Exception as exc:
            budget_row = ("codex agents.md budget", None, "unknown ({})".format(exc))
        if budget_row is not None:
            rows.append(budget_row)
        rows.extend(self._hook_health(repo_root, ledger_rows))
        return tuple(rows)

    def _hook_health(self, repo_root: Optional[str], ledger_rows):
        """Diagnoses through behavior whether the hook is installed but has
        never actually run.

        Static signals alone (hooks.json existing) can't tell trust status —
        codex-cli 0.155.1 skips an untrusted hook with no message and no
        ledger row. So it cross-checks the ledger for whether a Codex
        session actually left an omhc mark since the install. Since status
        itself must not die no matter what goes wrong (it's a diagnostic
        tool), this is wrapped wholesale and failure degrades to ok=None
        (unjudged).

        ok=None (`----`, the AGENTS.md status convention) is a third state
        meaning "nothing can be judged yet" — if there's been no interactive
        Codex session since install, or only headless (`codex exec`) ones,
        it's neither PASS nor FAIL.
        """
        try:
            if not self.hook_is_installed(repo_root):
                # Showing this row every time to a user who's never
                # installed the hook is noise — "not installed" is already
                # said by the `<adapter-id> hooks` row (hookconf-based, cmd_status).
                return ()
            source = self._install_source(repo_root)
            if source is None:
                # hook_is_installed(repo_root) said True, but the file(s)
                # couldn't be stat'd again — a race, e.g. just deleted. Can't judge.
                return (("codex hook", None,
                         "unknown (installed hook file could not be stat'd)"),)
            install_epoch, install_path = source
            install_date = time.strftime("%Y-%m-%d %H:%M %z", time.localtime(install_epoch))

            # Session ids are globally unique (assigned by Codex) — not
            # filtered by repo boundary. If ledger_rows were pre-filtered by
            # repo, a session started from a nested directory with its own
            # .git (worktree/submodule) would be recorded under a leaking
            # repo key and look permanently "never ran" (review defect) — so
            # the caller (cli.py) passes the ledger here unfiltered by repo.
            ran_sessions = set()
            for row in ledger_rows:
                if row.get("harness") != self.adapter_id:
                    continue
                if row.get("via") == "scan":
                    # A future backfill row. Not evidence the hook actually
                    # ran, so not counted — counting it would hide the problem.
                    continue
                if row.get("event") != "start":
                    # "The hook ran" is only proven by the existence of a
                    # session-start row — other events, like a scheduled
                    # pull row, aren't evidence the hook ran.
                    continue
                sid = row.get("session")
                if sid:
                    ran_sessions.add(sid)

            def _post_install(entries):
                """entries: tuples of (session_id, cwd, meta). Filters by
                repo membership and start time after install to build
                (started, session_id, meta)."""
                out = []
                for session_id, cwd, meta in entries:
                    if not self._repo_matches(repo_root, cwd):
                        continue
                    # Compares timestamps of two different files (hooks.json
                    # mtime, the rollout's session_meta.timestamp) — one side
                    # is mtime, which isn't the "ordering basis" invariant 6
                    # forbids, since this is a one-off diagnostic (this
                    # comparison never orders events). Different from the
                    # comparison cli._backfill_foreign_sessions does — that
                    # compares two session-start epochs (both
                    # session_meta.timestamp-style, not mtime) to decide
                    # ledger append order, the exception invariant 6 permits.
                    started = iso_epoch(meta.get("timestamp"))
                    if not started or started <= install_epoch:
                        continue
                    out.append((started, session_id, meta))
                return out

            def _entries_from_refs(refs):
                for ref in refs:
                    meta = session_meta(ref.source_path)
                    if meta:
                        yield ref.session_id, ref.cwd, meta

            candidates = _post_install(_entries_from_refs(self.list_sessions(repo_root)))

            if not candidates:
                # If OMHC_ALLOW_HEADLESS=1, list_sessions() itself already
                # includes headless as candidates, so by the time we're here
                # there's genuinely nothing at all — this headless-only
                # rescan only checks existence, regardless of env.
                headless_entries = (
                    (str(meta.get("session_id") or meta.get("id") or ""),
                     meta.get("cwd"), meta)
                    for _path, meta in self._scan(repo_root, True)
                )
                if _post_install(headless_entries):
                    return (("codex hook", None,
                              "not judged — only headless (codex exec) sessions since "
                              "{} changed ({}); they don't count, open an "
                              "interactive codex here once".format(install_path, install_date)),)
                return (("codex hook", None,
                          "not judged yet — no interactive Codex session in this repo "
                          "since {} changed ({})".format(install_path, install_date)),)

            # Trust changes config.toml, not hooks.json — a pre-trust
            # session stays permanently "never ran" even past install_epoch.
            # So this judges by the behavior of the **newest** session, not
            # the total count: if the newest ran, trust must have been
            # established by then, hence PASS; otherwise counts consecutive
            # non-ran sessions going backward from the newest.
            candidates.sort(key=lambda c: c[0], reverse=True)
            missing_streak = 0
            newest_missing_meta = None
            for started, session_id, meta in candidates:
                if session_id in ran_sessions:
                    break
                missing_streak += 1
                if newest_missing_meta is None:
                    newest_missing_meta = meta

            if missing_streak == 0:
                return (("codex hook", True, "ran for the latest session since install"),)

            # UNVERIFIED: a Codex Desktop/IDE session (originator e.g.
            # codex_work_desktop) may never run this hook at all in the
            # first place — unconfirmed. Records the newest never-run
            # session's originator in the detail so a misdiagnosis stays visible.
            originator = newest_missing_meta.get("originator")
            if not isinstance(originator, str) or not originator:
                originator = "unknown"
            detail = ("{} consecutive Codex session(s) since install never ran the "
                       "omhc hook (newest: {}) — trust it in Codex (untrusted hooks "
                       "are skipped silently)".format(missing_streak, originator))
            user_hooks_json = self.hooks_path()
            user_toml = self.toml_config_path()
            if install_path == user_hooks_json:
                if not self._config_trusts(install_path):
                    detail += (
                        "; no hooks.state trust hash for {} in ~/.codex/config.toml "
                        "(that's the per-hook-content trust Codex records after you "
                        "approve it; separate from `[projects...] trust_level`, which "
                        "only gates project-level `.codex/` layers)".format(install_path))
            elif install_path == user_toml:
                if not self._config_trusts(install_path):
                    detail += (
                        "; also found no hooks.state trust hash for {} — but that "
                        "check is only verified for hooks.json installs, not inline "
                        "config.toml ones, so treat this as a hint, not a diagnosis"
                        .format(install_path))
            # else: project-level install — its trust was already confirmed
            # via `[projects...] trust_level` (getting here requires that to
            # be trusted, see _hook_layers) — the hooks.state hint is
            # irrelevant to this layer, so it's not appended.
            # Thanks to the hook backfill (cmd_mark), the Codex→Claude
            # direction survives regardless of this FAIL — make explicit
            # that only Claude→Codex is broken.
            detail += (" — Claude→Codex is not delivered; Codex→Claude still works "
                       "via Claude's mark backfill")
            return (("codex hook", False, detail),)
        except Exception as exc:  # a diagnostic must not kill status itself (invariant 7)
            return (("codex hook", None, "unknown ({})".format(exc)),)
