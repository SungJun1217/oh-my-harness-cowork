from __future__ import annotations

import collections
import glob
import hashlib
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
    SessionRead,
    SessionRef,
)
from ..event import ARG_LIMIT, Event
from . import _register, allow_headless, install_state_artifact, iso_epoch

ARTIFACT_NAME = "omhc.txt"

# Record types we parse. Whitelist-based, so harness machinery (273
# attachment records, etc.) never even gets parsed and therefore can't leak
# through. This is the primary defense.
_PARSED_TYPES = frozenset({"user", "assistant"})

# Tool name → neutral verb. The Event schema has no tool-name field, so this
# dict is the single point that stops the vocabulary from crossing the boundary.
_VERB_BY_TOOL = {
    "Read": "inspected",
    "Glob": "inspected",
    "Grep": "inspected",
    "NotebookRead": "inspected",
    "ToolSearch": "inspected",
    "ListAgents": "inspected",
    "LSP": "inspected",
    "Write": "modified",
    "Edit": "modified",
    "NotebookEdit": "modified",
    "Bash": "ran",
    "BashOutput": "ran",
    "KillShell": "ran",
    "Task": "delegated",
    "Agent": "delegated",
    "Workflow": "delegated",
    "TaskStop": "delegated",
    "Skill": "delegated",
    "SendMessage": "delegated",
    "WebFetch": "researched",
    "WebSearch": "researched",
    # Asking the human a question is speech, not execution.
    "AskUserQuestion": "said",
    "ExitPlanMode": "said",
    "ReportFindings": "said",
}

# Default for unknown tools. Tallied under this name instead of silently dropped.
_DEFAULT_VERB = "ran"

# Claude Code 2.1.281's own skip check: isApiErrorMessage===true ||
# isVirtual===true || message.model==="<synthetic>" (confirmed in the
# binary). assistant records with these values are text the harness
# synthesized itself, used by neither human nor agent (login notices,
# synthetic PushNotification tool_use, etc).
_SYNTHETIC_MODEL = "<synthetic>"

_PATH_KEYS = ("file_path", "path", "notebook_path")
_ARG_KEYS = ("command", "file_path", "pattern", "query", "prompt", "description", "path")

# Transcript produced by /branch, --fork-session, /fork background copying:
# starts with a new session_id and copies the parent's current message
# chain. Copied records overwrite sessionId/parentUuid/isSidechain/
# sessionKind on the original and add forkedFrom: {sessionId, messageUuid}
# (uuid/timestamp/type/message stay from the original); a
# history-suppression (cause:"fork_inherit") record may be prepended. If the
# parent had already been delivered to the other harness, even with no turn
# of its own the fork's copied human turn goes out again as "new work" under
# the new session_id (#34, measured: 425B delivered twice) — #27's offset
# guard doesn't apply here because there's no delivered row at all under this
# new session_id.
_FORKED_FROM_MARK = b'"forkedFrom"'
_FORK_INHERIT_MARK = b'"fork_inherit"'

# Cost cap for fork detection. If own tail (past the copied section) exceeds
# this many bytes without finding a new turn, or the whole scan exceeds this
# much time, give up and open with the old behavior (eligible) — a detection
# failure on the hook path must never block session start (invariant 2). The
# copied section itself is only a cheap per-line substring check, so it's not
# counted against the byte cap (review finding: counting it made forks of
# parents over 8MB always fail-open before they even saw their own tail — a
# 12.6MB fixture, a 6.9MB parent re-forked, reproduced this). Measured
# (synthetic, this machine): copy-only 6.3MB 6.7ms, 12.6MB 13.2ms, 25.1MB
# 26.3ms, 30MB (no tail, to EOF) 32.3ms; a realistic 2MB copy section + tail
# with an own turn is 2.3ms. All within the time cap (50ms).
_FORK_SCAN_BYTE_LIMIT = 8 * 1024 * 1024
_FORK_SCAN_TIME_LIMIT = 0.05

# Per-process cache of classify() results. Every brief call scans the same
# file twice — once via ref_for_path() (kept if eligible), and again via
# brief.eligible() calling adapter.classify(mark.path) if it wasn't. Since
# adapters.get() builds a new instance every call (the registry holds
# classes), the cache lives on the module, not the instance. size/mtime_ns
# go into the key so a growing fork (once it gets its own turn) invalidates
# automatically. Bounded by an LRU so a long-running process like the watch.py
# daemon doesn't grow unbounded.
_CLASSIFY_CACHE_MAX = 256
_classify_cache = collections.OrderedDict()  # (path, size, mtime_ns, headless) -> bool


def claude_slug(path: str) -> str:
    """cwd → ~/.claude/projects/<slug>. This transform isn't recorded
    anywhere in any file, so it has to be recomputed.

    Measured: matches all 27 directories on this machine. Beyond 200 chars,
    truncate and append a path hash to avoid collisions.
    """
    slug = re.sub(r"[^a-zA-Z0-9]", "-", path)
    if len(slug) > 200:
        digest = int(hashlib.sha1(path.encode("utf-8")).hexdigest(), 16)
        tail = ""
        while digest and len(tail) < 8:
            digest, rem = divmod(digest, 36)
            tail = "0123456789abcdefghijklmnopqrstuvwxyz"[rem] + tail
        slug = slug[:200] + "-" + tail
    return slug


# Non-interactive entrypoints. A blocklist, not an allowlist — an allowlist
# would silently lose real sessions whenever a new interactive entrypoint appears.
#
# Measured: only 1 of 31 top-level sessions in this repo has entrypoint="cli";
# the other 30 are "sdk-py" (left by security-review hooks, etc). Without
# filtering these out, omhc would mistake another tool's non-interactive
# session for human work and hand it off.
NON_INTERACTIVE_ENTRYPOINTS = frozenset({"sdk-cli", "sdk", "sdk-py"})


def head_of(path: str, limit: int = 200) -> Dict[str, object]:
    """Gathers session head info with a single pass over the beginning.

    Never read cwd from just the first record — measured, it first appears
    at index 3, and 222 of 798 sessions have no cwd at all.
    """
    info: Dict[str, object] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= limit:
                    break
                try:
                    row = json.loads(line)
                except (ValueError, RecursionError):
                    # json raises RecursionError on deeply nested lines —
                    # skip it along with malformed ones (#34 review:
                    # classify/list_sessions were crashing).
                    continue
                if not isinstance(row, dict):
                    continue
                for key in ("cwd", "entrypoint", "version", "sessionId",
                            "gitBranch", "agentId"):
                    if key not in info and row.get(key):
                        info[key] = row[key]
                if row.get("isSidechain"):
                    info["sidechain"] = True
                if "cwd" in info and "entrypoint" in info:
                    break
    except OSError:
        return info
    return info


def _text_of(content) -> Optional[str]:
    """Candidate human speech text. Not a candidate if a tool_result is mixed in."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        kinds = {b.get("type") for b in content if isinstance(b, dict)}
        if kinds != {"text"}:
            return None
        return "".join(b.get("text", "") for b in content if b.get("type") == "text")
    return None


def _one_line_limit(text: str, limit: int = 600) -> str:
    """Body pulled from an envelope can be very long. Fold to one line and cap it."""
    flat = " ".join(text.split())
    return flat[:limit]


def _arg_of(tool_input) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in _ARG_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _paths_of(tool_input) -> Tuple[str, ...]:
    if not isinstance(tool_input, dict):
        return ()
    found = []
    for key in _PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            found.append(value)
    return tuple(found)


def _looks_like_fork(path: str) -> bool:
    """Cheap pre-check. A fork's copied section always starts at the top of
    the file (with an optional history-suppression prepend line), so only
    the first few lines need checking — the full scan below only runs if
    this looks like a fork."""
    try:
        with open(path, "rb") as fh:
            for i, raw in enumerate(fh):
                if i >= 4:
                    break
                if _FORKED_FROM_MARK in raw or _FORK_INHERIT_MARK in raw:
                    return True
    except OSError:
        return False
    return False


def _forked_lacks_own_turn(path: str) -> bool:
    """Is this a fork that still has no human turn of its own?

    Copied records all carry forkedFrom and come consecutively at the start
    of the file (per analysis). Where forkedFrom stops is where the fork's
    own newly written section begins, and we look for human speech there
    using the same whitelist as read_session — if there's even one, it's
    eligible.

    If the byte/time cap is exceeded, give up and return False (eligible,
    the old behavior). Opening (over-eligible, risking a duplicate parent
    turn once) is cheaper than not opening (over-conservative, permanently
    hiding a real new turn) — worst case is the pre-existing bug "the
    parent's turn goes out once more", not a new one.

    The byte cap only counts **past the copied section (own tail)** — the
    copied section itself is just a cheap per-line substring check (review
    measurement: 6.3MB 7ms, 12.6MB 9.5ms, 25.1MB 9.8ms), and counting those
    bytes against the cap would make even a moderately large parent (over
    8MB) always give up before seeing its own tail, leaving the
    re-delivery bug unfixed (review confirmed: a 12.6MB fixture, a 6.9MB
    parent re-forked, always fail-opened). The time cap is checked every
    line, including the copied section — it's the only safety net on total
    scan time.

    Unexpected record shapes (message not a dict, a text block's text not a
    string, etc) would raise here — wrapped per line to fail-open, since
    giving up on one verdict is cheaper than crashing classify()/
    list_sessions() and losing that harness's sessions entirely (watch would
    drop all Claude refs for that repo).
    """
    if not _looks_like_fork(path):
        return False
    start = time.time()
    scanned = 0
    in_copy = False
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                if time.time() - start > _FORK_SCAN_TIME_LIMIT:
                    return False
                if _FORKED_FROM_MARK in raw:
                    in_copy = True
                    continue
                if not in_copy:
                    # Not yet into the copied section — e.g. a history-suppression prepend.
                    continue
                # From the first record past the copied section on, it's the fork's own new content.
                scanned += len(raw)
                if scanned > _FORK_SCAN_BYTE_LIMIT:
                    return False
                try:
                    row = json.loads(raw.decode("utf-8", "replace"))
                except (ValueError, RecursionError):
                    continue
                try:
                    if not isinstance(row, dict) or row.get("type") != "user":
                        continue
                    if (row.get("isSidechain") or row.get("agentId")
                            or row.get("isMeta") or row.get("isCompactSummary")):
                        continue
                    message = row.get("message") or {}
                    text = _text_of(message.get("content"))
                    if text is None:
                        continue
                    text = text.strip()
                    if guard.is_envelope(text):
                        inner = guard.unwrap_command_args(text)
                        text = inner.strip() if inner else ""
                        if not text:
                            continue
                    if not guard.safe(text, "human"):
                        continue
                    return False
                except Exception:
                    return False
    except OSError:
        return False
    return True


@_register
class ClaudeCodeAdapter:
    adapter_id = "claude-code"
    capabilities = frozenset({Capability.READ, Capability.WRITE})
    wire = "claude"

    def __init__(self, *, home: Optional[str] = None, now=time.time) -> None:
        # No I/O in __init__.
        self._home = home
        self._now = now or time.time

    # --- paths --------------------------------------------------------

    @property
    def home(self) -> str:
        return self._home or os.path.expanduser("~")

    def projects_dir(self) -> str:
        return os.path.join(self.home, ".claude", "projects")

    # --- the 5 methods --------------------------------------------------------

    def detect(self) -> HarnessPresence:
        path = self.projects_dir()
        if os.path.isdir(path):
            return HarnessPresence(present=True, note=path)
        return HarnessPresence(present=False, note="not found: {}".format(path))

    def list_sessions(self, repo_root: Optional[str]) -> List[SessionRef]:
        if repo_root is None:
            return []
        root = os.path.realpath(repo_root)
        directory = os.path.join(self.projects_dir(), claude_slug(root))
        # Depth 1 only. <session>/subagents/** is someone else's agent speech, so never read.
        refs: List[SessionRef] = []
        for path in glob.glob(os.path.join(directory, "*.jsonl")):
            try:
                stat = os.stat(path)
            except OSError:
                continue
            if not self.classify(path):
                continue
            head = head_of(path)
            cwd = head.get("cwd")
            refs.append(
                SessionRef(
                    adapter_id=self.adapter_id,
                    session_id=os.path.basename(path)[: -len(".jsonl")],
                    source_path=path,
                    cwd=cwd if isinstance(cwd, str) else root,
                    epoch=stat.st_mtime,
                    size=stat.st_size,
                )
            )
        # Newest first. The caller almost always wants the last session.
        refs.sort(key=lambda r: (-r.epoch, r.session_id))
        return refs

    def read_session(self, ref: SessionRef) -> SessionRead:
        events: List[Event] = []
        dropped: Dict[str, int] = {}
        unparsed = 0
        pending: Dict[str, int] = {}  # tool_use_id → index into events
        seq = 0
        offset = 0

        def bump(key: str) -> None:
            dropped[key] = dropped.get(key, 0) + 1

        try:
            fh = open(ref.source_path, "rb")
        except OSError as exc:
            return SessionRead(ref=ref, events=(), unparsed=0,
                               dropped={"open_failed": 1, str(exc.errno): 1})

        with fh:
            for raw in fh:
                start = offset
                offset += len(raw)
                try:
                    row = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    unparsed += 1
                    continue
                if not isinstance(row, dict):
                    unparsed += 1
                    continue

                kind = str(row.get("type"))
                if kind not in _PARSED_TYPES:
                    bump(kind)
                    continue
                # Sidechain records are someone else's agent speech. This
                # machine had 1766 such records — a type-based allowlist
                # alone would turn every one of them into human speech.
                if row.get("isSidechain") or row.get("agentId"):
                    bump("sidechain")
                    continue
                if row.get("isMeta"):
                    bump("meta")
                    continue
                if row.get("isCompactSummary"):
                    bump("compact_summary")
                    continue

                message = row.get("message") or {}
                if kind == "assistant" and (
                    row.get("isApiErrorMessage") is True
                    or row.get("isVirtual") is True
                    # isVirtual hasn't been observed in the wild yet, but is
                    # part of upstream's own check so it's included too.
                    or message.get("model") == _SYNTHETIC_MODEL
                ):
                    bump("synthetic")
                    continue

                epoch = iso_epoch(row.get("timestamp"))
                content = message.get("content")

                if kind == "user":
                    text = _text_of(content)
                    if text is None:
                        # user record containing only a tool_result — the outcome of an earlier tool_use.
                        self._apply_results(content, pending, events)
                        bump("tool_result")
                        continue
                    # Judge first, redact only **after** truncating. Running
                    # a regex over the full text pays a cost even though most
                    # of it is soon dropped or cut to 600 chars anyway
                    # (measured: 160k chars 7.3ms → first 600 chars only 1.4ms).
                    text = text.strip()
                    if guard.is_envelope(text):
                        # <command-args> inside a slash-command envelope is
                        # what the human actually typed. Dropping the whole
                        # envelope loses the session's first message (usually
                        # the goal statement), so the GOAL slot ends up filled
                        # from a mid-conversation message instead.
                        inner = guard.unwrap_command_args(text)
                        if inner:
                            text = _one_line_limit(inner)
                        else:
                            bump("envelope")
                            continue
                    if not guard.safe(text, "human"):
                        bump("guarded_human")
                        continue
                    text = guard.redact_b64(_one_line_limit(text))
                    seq += 1
                    events.append(Event(
                        seq=seq, epoch=epoch, author="human", verb="said", ok=True,
                        text=text, arg="", paths=(), offset=start, length=len(raw),
                    ))
                    continue

                # assistant
                if not isinstance(content, list):
                    bump("assistant_nonlist")
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "thinking":
                        # The model's private reasoning never crosses vendors.
                        bump("thinking")
                        continue
                    if btype == "text":
                        text = str(block.get("text") or "").strip()
                        if not text or not guard.safe(text, "agent"):
                            bump("guarded_agent")
                            continue
                        text = guard.redact_b64(_one_line_limit(text))
                        seq += 1
                        events.append(Event(
                            seq=seq, epoch=epoch, author="agent", verb="said",
                            ok=True, text=text, arg="", paths=(),
                            offset=start, length=len(raw),
                        ))
                        continue
                    if btype == "tool_use":
                        name = str(block.get("name") or "")
                        verb = _VERB_BY_TOOL.get(name)
                        if verb is None:
                            verb = _DEFAULT_VERB
                            bump("unmapped_tool")
                        tool_input = block.get("input")
                        arg = guard.redact_b64(_arg_of(tool_input))[:ARG_LIMIT]
                        seq += 1
                        events.append(Event(
                            seq=seq, epoch=epoch, author="agent", verb=verb, ok=True,
                            text="", arg=arg, paths=_paths_of(tool_input),
                            offset=start, length=len(raw),
                        ))
                        tool_id = block.get("id")
                        if isinstance(tool_id, str):
                            pending[tool_id] = len(events) - 1
                        continue
                    bump("block:" + str(btype))

        return SessionRead(ref=ref, events=tuple(events), unparsed=unparsed,
                           dropped=dropped)

    def read_session_since(self, ref: SessionRef, offset: int, *,
                           max_bytes: Optional[int] = None,
                           stop_at_human_turn: bool = False):
        """Unimplemented (optional method, same pattern as discover/health) —
        Claude-side live-continue (a new turn appended to the same Claude
        session after a handoff) isn't detected yet (#22, a remaining
        limitation noted in the README). The default None means "this
        adapter can't distinguish", and the caller then falls back to
        today's full-scan path — this just accepts the arguments and always
        ignores them (must keep the same keyword shape as the base contract
        so cli.py can call any adapter without special-casing)."""
        return None

    @staticmethod
    def _apply_results(content, pending: Dict[str, int], events: List[Event]) -> None:
        """Attaches a tool_result to the earlier tool_use Event by id.

        Event is frozen, so only failed entries get replaced. Success is the default.
        """
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            if not block.get("is_error"):
                continue
            idx = pending.get(block.get("tool_use_id"))
            if idx is None:
                continue
            events[idx] = events[idx]._replace(ok=False)

    def classify(self, source_path: str) -> bool:
        """Was this a session a human talked in? On top of the entrypoint/
        sidechain markers, a fork (#34) is only eligible if it has at least
        one human turn of its own — without one it just inherits the
        parent's delivery history, and an already-delivered turn goes out
        again under the new session_id.

        Measured: only 1 of 31 top-level sessions in this repo has
        entrypoint=cli; 30 were sdk-py (left by security-review hooks, etc).

        This method can be called twice on the same file in one brief run —
        once via ref_for_path(), and again via brief.eligible() if that
        rejected it. Caching the result by (path, size, mtime_ns) keeps the
        second call from rescanning (review finding) — if the file grows
        (a fork gets its own turn), the key changes and the cache
        invalidates automatically.
        """
        try:
            stat = os.stat(source_path)
            # allow_headless() also changes the verdict, so it goes into the
            # key too (#34 review) — env doesn't actually change within a
            # real process, but this stays safe even if it's toggled within one.
            key = (source_path, stat.st_size, stat.st_mtime_ns, allow_headless())
        except OSError:
            key = None
        if key is not None and key in _classify_cache:
            _classify_cache.move_to_end(key)
            return _classify_cache[key]

        result = self._classify_uncached(source_path)

        if key is not None:
            _classify_cache[key] = result
            _classify_cache.move_to_end(key)
            if len(_classify_cache) > _CLASSIFY_CACHE_MAX:
                _classify_cache.popitem(last=False)
        return result

    @staticmethod
    def _classify_uncached(source_path: str) -> bool:
        head = head_of(source_path)
        if (str(head.get("entrypoint") or "") in NON_INTERACTIVE_ENTRYPOINTS
                and not allow_headless()):
            return False
        if head.get("sidechain") or head.get("agentId"):
            return False
        return not _forked_lacks_own_turn(source_path)

    def ref_for_path(self, source_path: str, session_id: str,
                     cwd: Optional[str] = None) -> Optional[SessionRef]:
        try:
            stat = os.stat(source_path)
        except OSError:
            return None
        if not stat.st_size or not self.classify(source_path):
            return None
        return SessionRef(
            adapter_id=self.adapter_id,
            session_id=session_id or os.path.basename(source_path)[: -len(".jsonl")],
            source_path=source_path,
            cwd=head_of(source_path).get("cwd") or cwd,
            epoch=stat.st_mtime,
            size=stat.st_size,
        )

    def discover(self, repo_root: Optional[str],
                deadline: Optional[float] = None) -> Tuple[SessionRef, ...]:
        """Explicitly returns an empty tuple. list_sessions measured 249ms on
        this machine, reading 130 files, 34.3MB (see the _ref_for comment in
        brief.py) — Codex-side mark would blow the hook budget by running
        this. Claude sessions' own hook is always trusted, so the Claude
        adapter doesn't even need to implement a Codex→Claude backfill."""
        return ()

    def native_resume_hint(self, ref: SessionRef) -> Optional[str]:
        """Between the same vendor, this is lossless and superior. Our summary is inferior."""
        return "claude --resume {}".format(ref.session_id)

    def install_handoff(self, bundle: HandoffBundle) -> InstallReceipt:
        return install_state_artifact(bundle, home=self._home)

    def fallback_channels(self):
        """Claude Code's SessionStart hook works with no trust issues, so there's no fallback.

        Explicitly empty — the base class is never subclassed by anyone
        else, so this default isn't obtained via inheritance.
        """
        return ()

    def health(self, repo_root: Optional[str], ledger_rows):
        """Claude Code's SessionStart hook has no trust issues, so there's
        no codex-style silent skipping — no behavioral defect to diagnose,
        hence an empty tuple.
        """
        return ()

    def hook_config(self):
        return hookconf.HookConfig(
            config_path=os.path.join(self.home, ".claude", "settings.json"),
            fragment_name="claude-settings.fragment.json",
            post_write_note="",
        )

    def on_session_start_mark(self, repo_root: str, *, source: str, epoch: float) -> None:
        """When Claude Code reads CLAUDE.md-style files hasn't been measured
        yet (#36) — with no evidence, this is a no-op. Since the base class
        is never subclassed by anyone else (true even before #36), this
        default isn't obtained via inheritance."""
        return None
