from __future__ import annotations

import glob
import json
import os
import time
from typing import Dict, List, Optional, Tuple

from .. import guard, locate
from ..adapter import (
    Capability,
    HandoffBundle,
    HarnessPresence,
    InstallReceipt,
    SessionRead,
    SessionRef,
)
from ..event import Event
from . import _register

ARTIFACT_NAME = "omhc.txt"

# 날짜 디렉터리 스캔 범위. sessions/YYYY/MM/DD 구조라 전체 walk 를 하면 오래된
# 세션까지 전부 stat 한다. 훅 경로에서 돌아가므로 범위를 묶는다.
SCAN_DAYS = 14

# 파싱하는 봉투 타입. 나머지(event_msg, world_state, turn_context 등)는 부기다.
_PARSED_ENVELOPES = frozenset({"response_item"})

# 통과시키는 role. developer 는 <skills_instructions> / <multi_agent_role> 등
# 순수 기계장치이므로 파싱조차 하지 않는다.
_PARSED_ROLES = frozenset({"user", "assistant"})

# 텍스트를 담는 블록 타입. user/developer 는 input_text, assistant 는 output_text.
_TEXT_BLOCKS = frozenset({"input_text", "output_text", "text"})

# --- UNVERIFIED -------------------------------------------------------------
# 아래 매핑은 Codex 인증이 없어(401 Unauthorized) 실물 레코드로 검증되지 못했다.
# Rust serde 필드명 기준으로 작성했고, 모르는 모양은 예외 대신 unparsed 로
# 계상되어 `omhc status` 가 비율을 보고한다.
# `codex login` 후 `python3 tests/harvest.py --force` 로 실물을 확보해 갱신하라.
_VERB_BY_TOOL = {
    "shell": "ran",
    "local_shell": "ran",
    "exec_command": "ran",
    "apply_patch": "modified",
    "write_file": "modified",
    "read_file": "inspected",
    "view_image": "inspected",
    "update_plan": "said",
    "web_search": "researched",
}
_DEFAULT_VERB = "ran"
# ---------------------------------------------------------------------------

_PATH_HINT_KEYS = ("path", "file_path", "filename")


def _epoch_of(value) -> float:
    if not isinstance(value, str) or len(value) < 19:
        return 0.0
    try:
        import calendar

        parts = (
            int(value[0:4]), int(value[5:7]), int(value[8:10]),
            int(value[11:13]), int(value[14:16]), int(value[17:19]),
        )
        return float(calendar.timegm(parts + (0, 0, 0)))
    except (ValueError, OverflowError):
        return 0.0


def session_meta(path: str) -> Optional[dict]:
    """첫 줄만 읽는다. 나머지를 파싱하면 훅 경로에서 비용이 튄다."""
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


def _recent_date_dirs(root: str, days: int, now) -> List[str]:
    seen = []
    stamp = now()
    for offset in range(days + 1):
        parts = time.gmtime(stamp - offset * 86400)
        seen.append(os.path.join(root, time.strftime("%Y/%m/%d", parts)))
    return seen


def _text_of(blocks) -> str:
    if not isinstance(blocks, list):
        return ""
    return "".join(
        b.get("text", "")
        for b in blocks
        if isinstance(b, dict) and b.get("type") in _TEXT_BLOCKS
    )


def _arg_and_paths(payload: dict) -> Tuple[str, Tuple[str, ...]]:
    """UNVERIFIED: function_call.arguments 는 JSON 문자열로 온다고 가정한다."""
    raw = payload.get("arguments")
    parsed = None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return raw.strip()[:120], ()
    elif isinstance(raw, dict):
        parsed = raw
    if parsed is None:
        action = payload.get("action")
        if isinstance(action, dict):
            parsed = action
    if not isinstance(parsed, dict):
        return "", ()

    command = parsed.get("command")
    if isinstance(command, list):
        arg = " ".join(str(c) for c in command)
    elif isinstance(command, str):
        arg = command
    else:
        arg = ""
        for key in ("input", "patch", "query", "prompt"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                arg = value.strip()
                break
    paths = tuple(
        str(parsed[key]) for key in _PATH_HINT_KEYS if isinstance(parsed.get(key), str)
    )
    return guard.redact_b64(arg)[:120], paths


def _output_failed(payload: dict) -> bool:
    """UNVERIFIED: function_call_output.output 의 실패 표현을 추정한다."""
    output = payload.get("output")
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            return False
    if not isinstance(output, dict):
        return False
    code = output.get("exit_code")
    if isinstance(code, int) and code != 0:
        return True
    return bool(output.get("is_error") or output.get("error"))


@_register
class CodexCliAdapter:
    adapter_id = "codex-cli"
    capabilities = frozenset({Capability.READ, Capability.WRITE})

    def __init__(self, *, home: Optional[str] = None, now=time.time) -> None:
        self._home = home
        self._now = now or time.time

    @property
    def home(self) -> str:
        return self._home or os.path.expanduser("~")

    def sessions_root(self) -> str:
        return os.path.join(self.home, ".codex", "sessions")

    # --- 5개 메서드 --------------------------------------------------------

    def detect(self) -> HarnessPresence:
        root = self.sessions_root()
        if os.path.isdir(root):
            return HarnessPresence(present=True, note=root)
        return HarnessPresence(present=False, note="not found: {}".format(root))

    def list_sessions(self, repo_root: Optional[str]) -> List[SessionRef]:
        if repo_root is None:
            return []
        root = os.path.realpath(repo_root)
        refs: List[SessionRef] = []
        for directory in _recent_date_dirs(self.sessions_root(), SCAN_DAYS, self._now):
            for path in glob.glob(os.path.join(directory, "rollout-*.jsonl")):
                meta = session_meta(path)
                if not meta:
                    continue
                cwd = meta.get("cwd")
                # Codex 는 rollout 에 레포 루트를 기록한다. equal-or-descendant 로
                # 판정해야 서브디렉터리에서 시작한 세션도 잡힌다.
                if not isinstance(cwd, str) or not locate.is_within(root, cwd):
                    continue
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                refs.append(
                    SessionRef(
                        adapter_id=self.adapter_id,
                        session_id=str(meta.get("session_id") or meta.get("id") or ""),
                        source_path=path,
                        cwd=cwd,
                        epoch=stat.st_mtime,
                        size=stat.st_size,
                    )
                )
        refs.sort(key=lambda r: (-r.epoch, r.source_path))
        return refs

    def read_session(self, ref: SessionRef) -> SessionRead:
        events: List[Event] = []
        dropped: Dict[str, int] = {}
        unparsed = 0
        pending: Dict[str, int] = {}
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

                envelope = str(row.get("type"))
                if envelope not in _PARSED_ENVELOPES:
                    bump(envelope)
                    continue
                payload = row.get("payload")
                if not isinstance(payload, dict):
                    unparsed += 1
                    continue

                epoch = _epoch_of(row.get("timestamp"))
                kind = str(payload.get("type"))

                if kind == "message":
                    role = str(payload.get("role"))
                    if role not in _PARSED_ROLES:
                        bump("role:" + role)
                        continue
                    text = guard.redact_b64(_text_of(payload.get("content")).strip())
                    author = "human" if role == "user" else "agent"
                    if not guard.safe(text, author):
                        bump("guarded_" + author)
                        continue
                    seq += 1
                    events.append(Event(
                        seq=seq, epoch=epoch, author=author, verb="said", ok=True,
                        text=text, arg="", paths=(), offset=start, length=len(raw),
                    ))
                    continue

                if kind in ("function_call", "local_shell_call", "custom_tool_call"):
                    name = str(payload.get("name") or kind.replace("_call", ""))
                    verb = _VERB_BY_TOOL.get(name)
                    if verb is None:
                        verb = _DEFAULT_VERB
                        bump("unmapped_tool")
                    arg, paths = _arg_and_paths(payload)
                    seq += 1
                    events.append(Event(
                        seq=seq, epoch=epoch, author="agent", verb=verb, ok=True,
                        text="", arg=arg, paths=paths, offset=start, length=len(raw),
                    ))
                    call_id = payload.get("call_id")
                    if isinstance(call_id, str):
                        pending[call_id] = len(events) - 1
                    continue

                if kind in ("function_call_output", "local_shell_call_output",
                            "custom_tool_call_output"):
                    if _output_failed(payload):
                        idx = pending.get(payload.get("call_id"))
                        if idx is not None:
                            old = events[idx]
                            events[idx] = Event(
                                seq=old.seq, epoch=old.epoch, author=old.author,
                                verb=old.verb, ok=False, text=old.text, arg=old.arg,
                                paths=old.paths, offset=old.offset, length=old.length,
                            )
                    bump("tool_output")
                    continue

                if kind == "reasoning":
                    bump("reasoning")
                    continue

                # response_item 인데 모양을 모른다 → 조용히 버리지 않는다.
                unparsed += 1

        return SessionRead(ref=ref, events=tuple(events), unparsed=unparsed,
                           dropped=dropped)

    def native_resume_hint(self, ref: SessionRef) -> Optional[str]:
        if not ref.session_id:
            return None
        return "codex resume {}".format(ref.session_id)

    def install_handoff(self, bundle: HandoffBundle) -> InstallReceipt:
        """Path A: 훅이 읽어갈 자리에 산출물을 둔다.

        Codex 훅 신뢰(HookStateToml)가 손으로 떨어뜨린 파일을 거부할 수 있으므로
        Path B(AGENTS.md 관리 구간)는 별도 모듈이 담당한다.
        """
        key = locate.repo_key(bundle.repo_root)
        state = locate.state_dir(key, home=self._home)
        os.makedirs(state, exist_ok=True)
        path = os.path.join(state, ARTIFACT_NAME)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(bundle.body_md)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return InstallReceipt(
            channel="sessionstart-hook",
            paths_written=(path,),
            consumed_on_read=True,
            cleanup_hint="omhc clear",
        )
