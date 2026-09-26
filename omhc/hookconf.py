"""Judges hook install state + installs/uninstalls. Vendor-neutral — both
harnesses share the same schema: `hooks.<Event>[].hooks[].command`.

`omhc hooks install|uninstall` and `omhc status`'s `<adapter-id> hooks` row
share this one module.
"""
from __future__ import annotations

import collections
import copy
import json
import os
import re
import shlex
import shutil
from typing import Dict, List, NamedTuple, Optional, Tuple

from . import fsio

# install.sh (strip_omhc_hooks, OMHC_CMD) finds omhc hooks with a **string**
# regex — it has to run before omhc is installed (or without it installed at
# all), so it can't call this module, and it's a delete-only operation, so
# casting a wide net over what counts as "to remove" in a hand-merged
# hooks.SessionStart group is safe. Here (structural judgment) the opposite
# mistake is riskier — calling a genuinely running hook "not installed", or
# mistaking a user hook that happens to contain the string "omhc brief" for
# an install/uninstall target — so argv is judged structurally instead.
#
# The two judgments are known to diverge on measured cases
# (tests/test_hooks_cmd.py's TestInstallShParity pins only the inputs where
# they agree), and are deliberately not merged into one:
#   - install.sh removes more broadly: both `cd ~ && omhc brief …` (the whole
#     command's argv[0] is "cd" but the string contains "omhc brief") and
#     `/usr/bin/env omhc mark …` (argv[0] is "env") get removed by install.sh,
#     but this module leaves them alone since argv[0]'s basename isn't "omhc".
#   - this module recognizes more broadly: `'omhc' 'mark' --harness x` (each
#     token quoted) becomes `omhc mark --harness x` once shlex splits it, so
#     this module counts it as installed, but install.sh's regex
#     (`omhc["']?\s+(mark|brief)`) only allows one optional quote right after
#     "omhc" and then expects the literal "mark"/"brief" after a space, so it
#     doesn't match `'mark` (which starts with a quote).
_HOME_RE = re.compile(r"\$\{HOME\}|\$HOME\b")

# v2 phase 2 (#42): the events omhc ever installs its own hooks under. Every
# function below defaults to "SessionStart" (unchanged behavior for existing
# callers) and takes an explicit `event=` to operate on "UserPromptSubmit"
# instead — so a caller that never passes `event=` sees exactly today's
# behavior even though the shipped fragment files now carry a second
# top-level key. `inspect_all`/`install_all` (below) are what combine both
# into the single `<adapter-id> hooks` row/install step.
MANAGED_EVENTS = ("SessionStart", "UserPromptSubmit")


class HookConfigError(Exception):
    """Exception merge/strip raise to fail closed. Means nothing was touched."""


class HookConfig(NamedTuple):
    """Record returned by the adapter's optional `hook_config()` method."""

    config_path: str
    fragment_name: str
    post_write_note: str = ""


class OmhcCall(NamedTuple):
    """Result of structurally judging one SessionStart hook command."""

    argv: Tuple[str, ...]
    sub: str  # "mark" | "brief"
    flags: Dict[str, str]  # {"--harness": "claude-code", ...} — compared as an order-independent set.


def fragments_dir() -> str:
    """The directory where fragments are actually installed.

    `../hooks` relative to this module's own realpath — holds for both a git
    checkout (`<repo>/hooks`) and a curl install
    (`~/.local/share/omhc/current/hooks`). `bin/omhc` resolves a symlink with
    readlink -f first and puts its parent on sys.path, so this module's
    __file__ always points at the real location too.
    """
    pkg_dir = os.path.dirname(os.path.realpath(__file__))
    return os.path.join(os.path.dirname(pkg_dir), "hooks")


def load_fragment(name: str) -> Dict[str, list]:
    """Returns only the `hooks` key from a fragment file. `_comment` and the
    rest are for humans."""
    path = os.path.join(fragments_dir(), name)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["hooks"]


class _Entry(NamedTuple):
    """One command hook under `event` — also carries the group/hook fields
    needed for judgment."""

    command: str
    matcher: object  # the group's matcher. None if absent.
    type: object  # the hook's type. None if absent.


def _extract_entries(hooks_by_event, event: str = "SessionStart") -> List[_Entry]:
    """Flattens the command hooks under `event`'s groups, in order. Other
    events are ignored as if never seen — a user hook like "omhc done" on a
    different event must not trip this judgment."""
    out: List[_Entry] = []
    if not isinstance(hooks_by_event, dict):
        return out
    for group in hooks_by_event.get(event) or []:
        if not isinstance(group, dict):
            continue
        matcher = group.get("matcher")
        for h in group.get("hooks") or []:
            if isinstance(h, dict) and isinstance(h.get("command"), str):
                out.append(_Entry(h["command"], matcher, h.get("type")))
    return out


_SIMPLE_MATCHER_RE = re.compile(r"^[a-zA-Z0-9_|]+$")


def _matcher_runs_at_startup(matcher) -> bool:
    """Does the group's matcher also fire at session start (SessionStart's
    "startup")?

    Mirrors a measurement of Claude Code 2.1.281 (a function inside the
    binary) as-is — Codex's exact semantics haven't been confirmed, and for
    now we assume the same schema: empty or "*" matches everything. A
    "simple" matcher satisfying `^[a-zA-Z0-9_|]+$` (e.g.
    `startup|resume|clear|compact`) isn't used as a regex — it's split on "|"
    and checked for an exact "startup" entry, so a simple matcher with just
    "start" doesn't fire (the string differs from "startup"). Anything else
    behaves like `new RegExp(m).test(source)` (unanchored substring search),
    mimicked here with `re.search` (not fullmatch). A string the regex engine
    can't read doesn't fire (False).
    """
    if matcher is None or matcher == "" or matcher == "*":
        return True
    if not isinstance(matcher, str):
        return False
    if _SIMPLE_MATCHER_RE.match(matcher):
        return "startup" in matcher.split("|")
    try:
        pattern = re.compile(matcher)
    except re.error:
        return False
    return pattern.search("startup") is not None


def _runnable_at_startup(entry: "_Entry") -> bool:
    return entry.type == "command" and _matcher_runs_at_startup(entry.matcher)


def _not_runnable_reason(entry: "_Entry") -> str:
    if entry.type != "command":
        return "type {!r}".format(entry.type)
    return "matcher {!r}".format(entry.matcher)


def _parse_call(command: str) -> Optional[OmhcCall]:
    """Splits a command string into argv, and returns its structure if it's
    an omhc mark/brief call.

    argv[0]'s basename must be exactly "omhc" — an absolute path, `~`,
    `${HOME}`, or a bare `omhc` on PATH all satisfy this, but something like
    `echo 'omhc brief'` (where the command itself is different — the first
    token isn't omhc) doesn't match.
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        return None  # unbalanced quotes etc. — not even shell-splittable, so not an omhc command.
    if not argv:
        return None
    if os.path.basename(argv[0]) != "omhc":
        return None
    if len(argv) < 2 or argv[1] not in ("mark", "brief", "turn"):
        return None
    flags: Dict[str, str] = {}
    rest = argv[2:]
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok.startswith("--") and "=" in tok:
            # argparse's --harness=claude-code form. The value is everything after the first =.
            key, value = tok.split("=", 1)
            flags[key] = value
            i += 1
        elif tok.startswith("--") and i + 1 < len(rest) and not rest[i + 1].startswith("--"):
            flags[tok] = rest[i + 1]
            i += 2
        else:
            # A valueless flag (next token also starts with "--", or this is
            # the last token) / a leftover token — skip it quietly, since
            # comparison only looks at known keys. Treating the next token as
            # the value unconditionally would let `--text --harness
            # codex-cli` swallow "--harness" as --text's value and lose the
            # real --harness.
            i += 1
    return OmhcCall(argv=tuple(argv), sub=argv[1], flags=flags)


def _omhc_calls(entries: List["_Entry"], *, runnable_only: bool = False) -> List[OmhcCall]:
    out = []
    for entry in entries:
        if runnable_only and not _runnable_at_startup(entry):
            continue
        call = _parse_call(entry.command)
        if call is not None:
            out.append(call)
    return out


def _resolve_binary(argv0: str, home: str) -> Optional[str]:
    """Resolves argv[0] to an executable absolute path. None if not found.

    `$HOME`/`${HOME}`/a leading `~` are all substituted with the given `home`
    argument (not the real os.environ at run time) — this check needs to be
    able to simulate a home other than the real user's $HOME (e.g. a test's
    temp home). Only the rare `~otheruser` form is left to
    os.path.expanduser (which sees the real running process's environment).
    A bare name with no slash is looked up on PATH (shutil.which, the real
    PATH at run time) — that's not something this check should simulate, it's
    something that actually has to exist.
    """
    token = _HOME_RE.sub(lambda _m: home, argv0)  # avoid re.sub reading home as a substitution template
    if token == "~" or token.startswith("~/"):
        # os.path.expanduser looks at the running process's real $HOME
        # (os.environ) — that would diverge from the `home` argument this
        # function is trying to simulate. Substitute `~` directly. Only the
        # rare `~otheruser` form is left to expanduser.
        token = home.rstrip("/") + token[1:]
    else:
        token = os.path.expanduser(token)
    if "/" not in token:
        return shutil.which(token)
    return token


def _diff_reason(shipped: List[OmhcCall], installed: List[OmhcCall]) -> str:
    shipped_subs = [c.sub for c in shipped]
    installed_subs = [c.sub for c in installed]
    if installed_subs != shipped_subs:
        missing = [s for s in shipped_subs if s not in installed_subs]
        if missing:
            return "missing {}".format(" and ".join(missing))
        installed_counts = collections.Counter(installed_subs)
        shipped_counts = collections.Counter(shipped_subs)
        if any(installed_counts[s] > shipped_counts[s] for s in installed_counts):
            return "duplicate omhc hooks (found {})".format(", ".join(installed_subs))
        return "wrong order (found {})".format(", ".join(installed_subs) or "nothing")
    for want, got in zip(shipped, installed):
        if want.flags != got.flags:
            keys = sorted(set(want.flags) | set(got.flags))
            parts = [
                "{} expected {!r} found {!r}".format(k, want.flags.get(k), got.flags.get(k))
                for k in keys if want.flags.get(k) != got.flags.get(k)
            ]
            return "{}: {}".format(want.sub, "; ".join(parts))
    return "commands differ"  # shouldn't be reached (if everything above matched this wouldn't be called), kept defensively.


def inspect(config_path: str, fragment: Dict[str, list], home: str,
           event: str = "SessionStart") -> Tuple[bool, str]:
    """Judges install state. Never raises — the caller (cmd_status) wraps it,
    but this function is a diagnostic tool in its own right so it's written
    to fail closed on its own.

    `fragment` is the `hooks` value from `load_fragment()`, i.e. the fragment
    that harness currently ships. `event` (v2 phase 2, #42) picks which of
    its top-level keys to judge — defaults to "SessionStart" so every
    existing caller that never passes `event=` sees exactly today's
    behavior, even now that the shipped fragments also carry a
    "UserPromptSubmit" key for `omhc turn`. `inspect_all` combines both.
    """
    try:
        with open(config_path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return False, "not installed in {} — run `omhc hooks install`".format(config_path)
    except UnicodeDecodeError:
        return False, "cannot parse {}".format(config_path)

    try:
        conf = json.loads(raw)
    except ValueError:
        return False, "cannot parse {}".format(config_path)
    if not isinstance(conf, dict):
        return False, "cannot parse {}".format(config_path)

    return _judge(conf.get("hooks"), fragment, home, config_path, event=event)


def inspect_toml(config_path: str, fragment: Dict[str, list], home: str,
                 event: str = "SessionStart") -> Tuple[bool, str]:
    """The TOML counterpart of `inspect()` (per the official docs, config.toml's
    inline `[[hooks.<Event>]]` loads into the same `hooks.<Event>[].hooks[]`
    structure as hooks.json). 3.9 has no tomllib, so `parse_toml_hooks` picks
    out just this one structure and hands it to the same judgment function
    (`_judge`) — no second copy of the comparison logic. Never raises:
    every failure becomes "not installed" (doesn't distinguish "not
    installed" from "unparsable", same principle as has_runnable_call)."""
    try:
        with open(config_path, encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return False, "not installed in {} — run `omhc hooks install`".format(config_path)
    try:
        hooks_by_event = parse_toml_hooks(text)
    except Exception:
        # parse_toml_hooks is written to fail open on its own (aiming to
        # never raise), but this function is also called near the hook path
        # (status/install), so it's wrapped once more — the last layer of
        # defense.
        return False, "cannot parse {}".format(config_path)
    if not hooks_by_event:
        return False, "not installed in {} — run `omhc hooks install`".format(config_path)
    return _judge(hooks_by_event, fragment, home, config_path, event=event)


def inspect_all(config_path: str, fragment: Dict[str, list], home: str,
                events: Tuple[str, ...] = MANAGED_EVENTS) -> Tuple[bool, str]:
    """Combines `inspect()` across every event the shipped `fragment` actually
    declares (v2 phase 2, #42) — the single verdict `cmd_status`'s
    `<adapter-id> hooks` row and `cmd_hooks`'s pre/post-install check use, so
    an install that only has yesterday's SessionStart group (missing the new
    `omhc turn` UserPromptSubmit group) is flagged FAIL and the message tells
    the human to rerun `omhc hooks install`, instead of quietly staying PASS
    forever. An event absent from `fragment` (e.g. a harness whose fragment
    never grew a second group) isn't judged at all — same reasoning as
    `_judge`'s "other events are ignored"."""
    details = []
    ok = True
    for event in events:
        if not fragment.get(event):
            continue
        e_ok, detail = inspect(config_path, fragment, home, event=event)
        if not e_ok:
            ok = False
            details.append("{}: {}".format(event, detail))
    if not details:
        return True, "installed"
    return ok, ("installed" if ok else "; ".join(details))


def _judge(hooks_by_event, fragment: Dict[str, list], home: str,
           config_path: str, event: str = "SessionStart") -> Tuple[bool, str]:
    """Shared by `inspect`/`inspect_toml` — compares the installed
    hooks.SessionStart shape against the shipped fragment. Whether the
    source is JSON (above) or TOML (parse_toml_hooks), the same
    `{event: [{"matcher":…, "hooks":[{"type":…, "command":…}]}]}` shape gets
    judged identically. `_matcher_runs_at_startup`'s "startup" semantics are
    Claude Code SessionStart terminology, but an empty/"*" matcher (what
    every shipped fragment group uses) means "always" regardless of event, so
    reusing it for "UserPromptSubmit" groups is still correct — it just never
    gets exercised by the "narrower than startup" branch on that event."""
    installed_entries = _extract_entries(hooks_by_event, event=event)
    installed_omhc_any = _omhc_calls(installed_entries)
    if not installed_omhc_any:
        return False, "not installed in {} — run `omhc hooks install`".format(config_path)

    installed_omhc = _omhc_calls(installed_entries, runnable_only=True)
    if not installed_omhc:
        # There are omhc calls (confirmed above), but none of them ever run
        # at session start — either the matcher narrowed it down, or the type
        # isn't "command". This keeps a "PASS but never runs" install from
        # hitting merge()'s "already PASS, leave it alone" path and staying
        # broken forever.
        reason = None
        for entry in installed_entries:
            if _parse_call(entry.command) is not None and not _runnable_at_startup(entry):
                reason = _not_runnable_reason(entry)
                break
        # Kept as "at session start" verbatim (tests pin this substring) even
        # for the "UserPromptSubmit" event — the phrase describes
        # `_runnable_at_startup`'s judgment (an empty/"*" matcher fires on
        # every turn too), not literally the SessionStart event name.
        return False, ("omhc hooks never run at session start ({}) — "
                        "run `omhc hooks install`").format(reason or "not runnable")

    shipped_entries = _extract_entries(fragment, event=event)
    shipped_any = _omhc_calls(shipped_entries)
    # Comparing only runnable calls could still show PASS with an extra omhc
    # group that never actually runs (e.g. a second brief pinned to matcher
    # "resume") — breaking merge()'s assumption that "PASS means no
    # duplicates yet". So counts including non-runnable ones are separately
    # checked not to exceed shipped (#20 review).
    installed_counts = collections.Counter(c.sub for c in installed_omhc_any)
    shipped_counts = collections.Counter(c.sub for c in shipped_any)
    if any(installed_counts[s] > shipped_counts[s] for s in installed_counts):
        return False, "differs from shipped fragment ({}) — run `omhc hooks install`".format(
            _diff_reason(shipped_any, installed_omhc_any))

    shipped = _omhc_calls(shipped_entries, runnable_only=True)
    if [c.sub for c in installed_omhc] != [c.sub for c in shipped] or \
            any(w.flags != g.flags for w, g in zip(shipped, installed_omhc)):
        return False, "differs from shipped fragment ({}) — run `omhc hooks install`".format(
            _diff_reason(shipped, installed_omhc))

    for call in installed_omhc:
        binpath = _resolve_binary(call.argv[0], home)
        if binpath is None:
            return False, "cannot verify {} on PATH".format(call.argv[0])
        if not (os.path.isfile(binpath) and os.access(binpath, os.X_OK)):
            return False, "{} not executable".format(binpath)

    return True, "installed"


def has_runnable_call(config_path: str, sub: str, flags: Optional[Dict[str, str]] = None,
                      event: str = "SessionStart") -> bool:
    """Does `config_path`'s `event` (default "SessionStart") have an omhc
    call to `sub` (e.g. "brief") that actually runs? If `flags` is given, that
    key=value must match too (e.g. `{"--harness": "codex-cli"}`).

    Used on the hook path (install_handoff -> hook_is_installed) — never
    raises: every failure is False (indistinguishable from "not installed",
    but the hook path never dying wins over that distinction — invariant 2).
    """
    try:
        with open(config_path, encoding="utf-8") as fh:
            conf = json.loads(fh.read())
        if not isinstance(conf, dict):
            return False
        for call in _omhc_calls(_extract_entries(conf.get("hooks"), event=event),
                                runnable_only=True):
            if call.sub != sub:
                continue
            if flags and any(call.flags.get(k) != v for k, v in flags.items()):
                continue
            return True
        return False
    except Exception:
        return False


def has_runnable_call_toml(config_path: str, sub: str,
                          flags: Optional[Dict[str, str]] = None,
                          event: str = "SessionStart") -> bool:
    """The TOML counterpart of `has_runnable_call` — looks at config.toml's
    inline `[[hooks.<Event>]]` (#32). Never raises: every failure is False."""
    try:
        with open(config_path, encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
        hooks_by_event = parse_toml_hooks(text)
        if not hooks_by_event:
            return False
        for call in _omhc_calls(_extract_entries(hooks_by_event, event=event),
                                runnable_only=True):
            if call.sub != sub:
                continue
            if flags and any(call.flags.get(k) != v for k, v in flags.items()):
                continue
            return True
        return False
    except Exception:
        return False


# --- TOML header scanner + inline [hooks] ------------------------------------

# This module knows exactly one TOML schema — `hooks.<Event>[].hooks[].command`
# expanded as array-of-tables:
#   [[hooks.PreToolUse]]
#   matcher = "^Bash$"
#
#   [[hooks.PreToolUse.hooks]]
#   type = "command"
#   command = '...'
#   timeout = 30
#   statusMessage = "..."
# Whichever harness uses this TOML inline representation, it's judged by this
# one module (3.9 has no tomllib, so the whole TOML doc is never parsed) —
# no harness name involved.

# Finds only top-level headers (at line start, at array depth 0), skipping
# strings/comments/brackets inside arrays or strings. `toml_header_lines`
# yields all of them across the document (not just the first), in document
# order — both this module's inline [hooks] judgment and an adapter picking
# out just its own single top-level key (e.g. checking whether a particular
# key comes before the first header) share this one scanner.
_TOML_STRING_OR_BRACKET_RE = re.compile(
    r'"""|\'\'\'|"(?:[^"\\\n]|\\.)*"|\'[^\'\n]*\'|#[^\n]*|[\[\]]')

# One header line: `[a.b.c]` or `[[a.b.c]]`. Even if a string appears inside
# (e.g. `[projects."/a/b"]`), this only roughly extracts the dotted key —
# the hooks.<Event> pattern is always a bare key, so no precise parsing is
# needed, and other headers (e.g. projects) aren't this parser's concern
# (they simply fail to match and get silently ignored), so lower accuracy
# there is safe.
_TOML_HEADER_RE = re.compile(r'^[ \t]*(\[{1,2})([^\]]*)\]{1,2}[ \t\r]*(?:#.*)?$')

# One `key = "value"` body line (recognizes only string values — matcher/
# type/command are all strings per the measured example above, and
# timeout (int)/statusMessage aren't this module's concern). Accepts bare/
# quoted keys and basic/literal string values alike.
_TOML_KV_RE = re.compile(
    r'^[ \t]*([\w-]+|"[^"\\\n]*"|\'[^\'\n]*\')[ \t]*=[ \t]*'
    r'("(?:[^"\\\n]|\\.)*"|\'[^\'\n]*\')[ \t\r]*(?:#.*)?$')

# The keys parse_toml_hooks's two target shapes accept — a group
# (=[[hooks.<E>]]) only takes matcher, a hook (=[[hooks.<E>.hooks]]) only
# type/command. If a group's internal key like "hooks" got overwritten by a
# body key=value (e.g. `hooks = "oops"`), the next `[[hooks.<E>.hooks]]`
# would die trying to append to that list (review #2) — so this allowlist is
# used to gate it instead of `key in target`.
_TOML_GROUP_KEYS = frozenset({"matcher"})
_TOML_HOOK_KEYS = frozenset({"type", "command"})


def toml_header_lines(text: str):
    """Yields (bracket_pos, line_end) offsets, in document order, for lines
    with a top-level (line start, outside a string/array) `[...]`/`[[...]]`
    header. `bracket_pos` is where that line's `[` itself starts (not the
    line start — leading whitespace is excluded), `line_end` is up to just
    before that line's newline."""
    out = []
    depth = 0
    in_multi = None
    line_start = True
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_multi:
            if in_multi == '"""' and ch == "\\":
                i += 2
                continue
            if text.startswith(in_multi, i):
                i += 3
                in_multi = None
            else:
                i += 1
            continue
        if ch == "\n":
            line_start = True
            i += 1
            continue
        if ch in " \t\r":
            i += 1
            continue
        if line_start and depth == 0 and ch == "[":
            bracket_pos = i
            line_end = text.find("\n", i)
            if line_end == -1:
                line_end = n
            out.append((bracket_pos, line_end))
            i = line_end
            continue
        line_start = False
        m = _TOML_STRING_OR_BRACKET_RE.match(text, i)
        if not m:
            i += 1
            continue
        tok = m.group(0)
        if tok in ('"""', "'''"):
            in_multi = tok
        elif tok == "[":
            depth += 1
        elif tok == "]":
            depth = max(0, depth - 1)
        i = m.end()
    return out


def parse_toml_hooks(text: str) -> Dict[str, list]:
    """Picks only the inline `[hooks]` table out of config.toml text and
    returns it in the same shape as hooks.json:
    `{event: [{"matcher":…, "hooks":[{"type":…, "command":…}]}]}`. All other
    TOML (`[projects...]`, `[model]`, etc.) isn't this parser's concern —
    headers that don't match the hooks.<Event> / hooks.<Event>.hooks pattern
    are silently skipped.

    If `[hooks]` is absent entirely (genuinely missing, or an unrecognized
    shape), returns an empty dict — both has_runnable_call_toml/inspect_toml
    treat "empty dict = not installed", so failing open falls out naturally.
    Never raises."""
    hooks_by_event: Dict[str, list] = {}
    groups_by_event: Dict[str, dict] = {}  # event -> most recent group (last in document order)
    headers = toml_header_lines(text)

    def _read_body(body_start: int, body_end: int, target, allowed_keys) -> None:
        """Within [header_end, next_header_start), lays only the keys in
        `allowed_keys` onto `target`. Does nothing if `target` is None (under
        a header we don't care about)."""
        if target is None:
            return
        for line in text[body_start:body_end].split("\n"):
            m = _TOML_KV_RE.match(line)
            if not m:
                continue
            key = m.group(1).strip('"\'')
            if key not in allowed_keys:
                continue
            raw_value = m.group(2)
            value = raw_value[1:-1]
            if raw_value[0] == '"':
                value = value.replace('\\"', '"').replace("\\\\", "\\")
            target[key] = value

    for idx, (start, end) in enumerate(headers):
        line = text[start:end]
        m = _TOML_HEADER_RE.match(line)
        body_start = end
        body_end = headers[idx + 1][0] if idx + 1 < len(headers) else len(text)
        if not m:
            continue
        is_array = m.group(1) == "[["
        path = [p.strip() for p in m.group(2).split(".") if p.strip()]
        target = None
        allowed_keys = frozenset()
        if is_array and len(path) == 2 and path[0] == "hooks":
            event = path[1].strip('"\'')
            group = {"matcher": None, "hooks": []}
            hooks_by_event.setdefault(event, []).append(group)
            groups_by_event[event] = group
            target = group
            allowed_keys = _TOML_GROUP_KEYS
        elif is_array and len(path) == 3 and path[0] == "hooks" and path[2] == "hooks":
            event = path[1].strip('"\'')
            group = groups_by_event.get(event)
            if group is not None:
                hook = {"type": None, "command": None}
                group["hooks"].append(hook)
                target = hook
                allowed_keys = _TOML_HOOK_KEYS
            # else: a hooks table with no parent group — an orphan, dropped (target=None).
        _read_body(body_start, body_end, target, allowed_keys)

    return hooks_by_event


# --- install/uninstall (`omhc hooks install|uninstall`) ---------------------


def _load(config_path: str) -> dict:
    """Reads the config object. If the file doesn't exist, an empty dict
    (``{}``) — merge needs to be able to build fresh on top of it (Codex
    doesn't ship a hooks.json to begin with). Every other failure raises to
    fail closed — it never proceeds having read only half of it."""
    try:
        with open(config_path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return {}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HookConfigError("cannot parse {}: {}".format(config_path, exc))
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise HookConfigError("cannot parse {}: {}".format(config_path, exc))
    if not isinstance(obj, dict):
        raise HookConfigError("{}: top-level is not an object".format(config_path))
    return obj


def _strip_hooks(conf: dict, event: str = "SessionStart") -> dict:
    """Removes only omhc's own mark/brief/turn hooks from `hooks.<event>`, in
    a copy of `conf` (the full config object). Unlike install.sh's string
    regex, this judges structurally with `_parse_call` — the same mistake
    (failing to remove a genuinely running hook, or removing someone else's
    hook like `echo 'omhc brief'`) is just as dangerous here.

    `event` (v2 phase 2, #42) defaults to "SessionStart", matching every
    existing caller unchanged; `strip_all`/`omhc hooks uninstall` also pass
    "UserPromptSubmit" to remove `omhc turn`'s group.

    If the shape is unexpected (e.g. hooks isn't an object), fails closed by
    the same principle as install.sh's uninstall path — it neither leaves nor
    rewrites an unrecognized mechanical shape.
    """
    conf = copy.deepcopy(conf)
    hooks = conf.get("hooks")
    if hooks is None:
        return conf
    if not isinstance(hooks, dict):
        raise HookConfigError("hooks is not an object")
    groups = hooks.get(event)
    if groups is None:
        return conf
    if not isinstance(groups, list):
        raise HookConfigError("hooks.{} is not an array".format(event))

    kept_groups = []
    removed_any = False
    for group in groups:
        if not isinstance(group, dict):
            raise HookConfigError("a {} group is not an object".format(event))
        if not isinstance(group.get("hooks"), list):
            raise HookConfigError("a {} group's hooks is not an array".format(event))
        original_hooks = group["hooks"]
        kept_hooks = [
            h for h in original_hooks
            if not (isinstance(h, dict) and isinstance(h.get("command"), str)
                    and _parse_call(h["command"]) is not None)
        ]
        if len(kept_hooks) == len(original_hooks):
            # Nothing was removed from this group — leave it untouched even
            # if it was originally empty (matching install.sh). Dropping an
            # untouched empty group would make strip() wrongly report
            # "changed" even on a config with no omhc hooks at all (#7
            # review 4).
            kept_groups.append(group)
            continue
        removed_any = True
        if kept_hooks:
            new_group = dict(group)
            new_group["hooks"] = kept_hooks
            kept_groups.append(new_group)
        # else: this group had only omhc hooks and is now empty — drop the whole group.

    if not removed_any:
        return conf  # leave this event exactly as it was (even if it was an empty array).

    if kept_groups:
        hooks[event] = kept_groups
    else:
        del hooks[event]
    if not hooks:
        # If hooks only held this event, it's now an empty object — leaving
        # it would leave `{"hooks": {}}` as a stray artifact in a config that
        # never installed anything.
        del conf["hooks"]
    return conf


def _write_if_changed(config_path: str, original: dict, updated: dict) -> bool:
    """If `updated` equals `original` (idempotent), writes nothing — the
    Codex health row depends on hooks.json's mtime, and that guarantee must
    not break here. If they differ, backs up the existing file to
    `.omhc-bak` (if one exists) and swaps it in atomically."""
    if updated == original:
        return False
    real_target = os.path.realpath(config_path)
    if os.path.exists(real_target):
        if not os.access(real_target, os.W_OK):
            # Measured: a 0444 config file can still be swapped in via plain
            # os.replace (rename only needs directory write permission, it
            # doesn't check the file's own mode) — but a file a human
            # deliberately locked shouldn't be silently overwritten, so this
            # explicitly refuses (#7 review 5).
            raise HookConfigError(
                "{} is read-only; refusing to overwrite it silently — "
                "chmod it writable first if you want omhc to manage it".format(real_target))
        backup = config_path + ".omhc-bak"
        # If a backup left by a previous run is still 0444 (because the
        # original was read-only at that time), copy2's open(dst, "wb") dies
        # with EACCES — the backup only needs to point at the latest
        # original each time, so it's deleted first (#7 review 5).
        fsio.unlink_quiet(backup)
        shutil.copy2(config_path, backup)  # keep the permission bits matching the original too
    try:
        text = json.dumps(updated, indent=2, ensure_ascii=False) + "\n"
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(updated, indent=2, ensure_ascii=True) + "\n"
    fsio.replace_preserving(config_path, text)
    return True


def strip(config_path: str, event: str = "SessionStart") -> bool:
    """Removes only omhc's own hooks from `config_path`'s `hooks.<event>`.
    True if something changed, False if there was no omhc hook to begin
    with."""
    original = _load(config_path)
    updated = _strip_hooks(original, event=event)
    return _write_if_changed(config_path, original, updated)


def strip_all(config_path: str, events: Tuple[str, ...] = MANAGED_EVENTS) -> bool:
    """`strip()` over every managed event in one file write (v2 phase 2,
    #42) — `omhc hooks uninstall` needs both the SessionStart (mark/brief)
    and UserPromptSubmit (turn) groups gone, and doing that as two separate
    `strip()` calls would also produce two separate backups/mtimes for what
    a human experiences as one uninstall."""
    original = _load(config_path)
    updated = original
    for event in events:
        updated = _strip_hooks(updated, event=event)
    return _write_if_changed(config_path, original, updated)


def merge(config_path: str, fragment: Dict[str, list], home: str,
         event: str = "SessionStart") -> bool:
    """Merges `fragment` (=the `hooks` value from `load_fragment()`) into
    `config_path`. First `strip`s to prevent duplicates, then appends
    fragment's `event` groups (default "SessionStart") — so when a
    `hooks/*.json` command changes (e.g. `--wire sdk` -> `claude`), the new
    command lands next to the old one instead of making brief run twice.

    Leaves an install alone if `inspect()` already PASSes it — a person who
    hand-merged their own groups around the omhc group, added fields like
    `timeout`/`matcher` to the omhc hooks, or reordered things may still have
    a valid install. Rewriting here would rearrange the structure, drop
    those fields, and — worst of all — change hooks.json's mtime: omhc's
    Codex hook judgment uses that mtime as a baseline and would fall back to
    "can't judge yet", and it might make Codex re-trust a hooks.json whose
    content changed (unverified). A mere reorder of an already-working
    install shouldn't cause that (#7 review 3). `inspect()` compares every
    SessionStart group's omhc calls against the shipped fragment down to
    order, count, and flags, so a PASS already means there are no duplicate
    omhc calls."""
    ok, _detail = inspect(config_path, fragment, home, event=event)
    if ok:
        return False
    original = _load(config_path)
    updated = _strip_hooks(original, event=event)
    frag_groups = fragment.get(event) or []
    if frag_groups:
        hooks = updated.setdefault("hooks", {})
        hooks[event] = list(hooks.get(event) or []) + copy.deepcopy(frag_groups)
    return _write_if_changed(config_path, original, updated)


def install_all(config_path: str, fragment: Dict[str, list], home: str,
                events: Tuple[str, ...] = MANAGED_EVENTS) -> bool:
    """`merge()` over every managed event that `fragment` actually declares —
    mirrors `strip_all` (v2 phase 2, #42). Written as **one** load/strip/merge/
    write pass (not one `merge()` call per event) so a fresh install produces
    one file write and one backup, not one per event — Codex's hook-trust
    judgment is keyed off hooks.json's content/mtime, and turning one install
    into several successive rewrites would needlessly invalidate trust an
    extra time and leave a stale intermediate-state backup behind.

    review #1 finding 4: only strips + re-appends the events whose own
    `inspect(event=e)` doesn't already PASS — an event that's already
    installed correctly (even if hand-merged with extra fields like
    `timeout`, reordered, or sharing a group with a user hook — the same
    things `merge()`'s own docstring protects for a single event) is left
    completely untouched. Rewriting an already-passing event too (the
    original bug here) would strip and rebuild its group from the shipped
    fragment alone, silently dropping any such hand-added field and, worse,
    reassigning the group — which can shift a Codex hook-trust entry keyed by
    group/hook index even for a group that was never actually wrong.
    """
    to_fix = [e for e in events
             if fragment.get(e) and not inspect(config_path, fragment, home, event=e)[0]]
    if not to_fix:
        return False
    original = _load(config_path)
    updated = original
    for event in to_fix:
        updated = _strip_hooks(updated, event=event)
    for event in to_fix:
        frag_groups = fragment.get(event) or []
        if frag_groups:
            hooks = updated.setdefault("hooks", {})
            hooks[event] = list(hooks.get(event) or []) + copy.deepcopy(frag_groups)
    return _write_if_changed(config_path, original, updated)
