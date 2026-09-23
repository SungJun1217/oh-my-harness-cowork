from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple

from .. import fsio, guard, locate
from ..adapter import (
    Capability,
    HandoffBundle,
    HarnessPresence,
    InstallReceipt,
    SessionRead,
    SessionRef,
)
from ..event import Event
from . import _register, install_state_artifact

ARTIFACT_NAME = "omhc.txt"

# 파싱하는 레코드 타입. 화이트리스트이므로 하네스 기계장치(attachment 273개 등)는
# 파싱 자체가 되지 않으며, 따라서 누출될 수 없다. 이것이 1차 방어다.
_PARSED_TYPES = frozenset({"user", "assistant"})

# 툴 이름 → 중립 동사. Event 스키마에 툴 이름 필드가 없으므로 이 사전이 어휘가
# 경계를 넘지 못하게 하는 유일한 지점이다.
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
    # 사람에게 묻는 것은 실행이 아니라 발화다.
    "AskUserQuestion": "said",
    "ExitPlanMode": "said",
    "ReportFindings": "said",
}

# 모르는 툴의 기본값. 조용히 버리지 않고 이 이름으로 계상한다.
_DEFAULT_VERB = "ran"

_PATH_KEYS = ("file_path", "path", "notebook_path")
_ARG_KEYS = ("command", "file_path", "pattern", "query", "prompt", "description", "path")

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})")


def claude_slug(path: str) -> str:
    """cwd → ~/.claude/projects/<slug>. 이 변환은 파일 어디에도 기록되지 않으므로
    재계산해야 한다.

    실측: 이 머신의 27개 디렉터리 전부와 일치한다. 200자를 넘으면 절단 후
    경로 해시를 붙여 충돌을 막는다.
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


def _epoch(value) -> float:
    """ISO 타임스탬프 → epoch. 순서의 근거로는 쓰지 않는다(역행이 실측됐다)."""
    if not isinstance(value, str):
        return 0.0
    m = _ISO.match(value)
    if not m:
        return 0.0
    try:
        import calendar

        parts = [int(x) for x in m.groups()]
        return float(calendar.timegm(tuple(parts) + (0, 0, 0)))
    except (ValueError, OverflowError):
        return 0.0


# 비대화형 entrypoint. 차단목록이며 허용목록이 아니다 — 허용목록이면 새 대화형
# entrypoint 가 생겼을 때 진짜 세션을 조용히 잃는다.
#
# 실측: 이 레포의 최상위 세션 31개 중 1개만 entrypoint="cli" 이고 나머지 30개는
# "sdk-py" 다(보안 리뷰 훅 등이 남긴 것). 걸러내지 않으면 omhc 가 남의 도구가
# 만든 비대화형 세션을 사람의 작업으로 오인해 핸드오프한다.
NON_INTERACTIVE_ENTRYPOINTS = frozenset({"sdk-cli", "sdk", "sdk-py"})


def head_of(path: str, limit: int = 200) -> Dict[str, object]:
    """앞부분을 한 번만 훑어 세션 머리 정보를 모은다.

    첫 레코드에서 cwd 를 읽으면 안 된다 — 실측상 최초 등장은 index 3 이고
    798개 중 222개에는 cwd 가 아예 없다.
    """
    info: Dict[str, object] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= limit:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
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


def cwd_of(path: str, limit: int = 200) -> Optional[str]:
    value = head_of(path, limit).get("cwd")
    return value if isinstance(value, str) else None


def _text_of(content) -> Optional[str]:
    """사람의 말 후보 텍스트. tool_result 가 섞이면 후보가 아니다."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        kinds = {b.get("type") for b in content if isinstance(b, dict)}
        if kinds != {"text"}:
            return None
        return "".join(b.get("text", "") for b in content if b.get("type") == "text")
    return None


def _one_line_limit(text: str, limit: int = 600) -> str:
    """봉투에서 꺼낸 본문은 아주 길 수 있다. 한 줄로 접고 상한을 둔다."""
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


@_register
class ClaudeCodeAdapter:
    adapter_id = "claude-code"
    capabilities = frozenset({Capability.READ, Capability.WRITE})
    wire = "claude"

    def __init__(self, *, home: Optional[str] = None, now=time.time) -> None:
        # __init__ 에서 I/O 를 하지 않는다.
        self._home = home
        self._now = now or time.time

    # --- 경로 -------------------------------------------------------------

    @property
    def home(self) -> str:
        return self._home or os.path.expanduser("~")

    def projects_dir(self) -> str:
        return os.path.join(self.home, ".claude", "projects")

    # --- 5개 메서드 --------------------------------------------------------

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
        # 깊이 1만. <session>/subagents/** 는 남의 에이전트 발화이므로 읽지 않는다.
        refs: List[SessionRef] = []
        for path in glob.glob(os.path.join(directory, "*.jsonl")):
            try:
                stat = os.stat(path)
            except OSError:
                continue
            head = head_of(path)
            if str(head.get("entrypoint") or "") in NON_INTERACTIVE_ENTRYPOINTS:
                continue
            if head.get("sidechain") or head.get("agentId"):
                continue
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
        # 최근 것이 먼저. 호출자는 거의 항상 마지막 세션을 원한다.
        refs.sort(key=lambda r: (-r.epoch, r.session_id))
        return refs

    def read_session(self, ref: SessionRef) -> SessionRead:
        events: List[Event] = []
        dropped: Dict[str, int] = {}
        unparsed = 0
        pending: Dict[str, int] = {}  # tool_use_id → events 인덱스
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
                # 서브체인은 남의 에이전트 발화다. 이 머신에서 그런 레코드가
                # 1766개 있었고, 타입 기반 허용목록이면 전부 사람의 말이 된다.
                if row.get("isSidechain") or row.get("agentId"):
                    bump("sidechain")
                    continue
                if row.get("isMeta"):
                    bump("meta")
                    continue
                if row.get("isCompactSummary"):
                    bump("compact_summary")
                    continue

                epoch = _epoch(row.get("timestamp"))
                message = row.get("message") or {}
                content = message.get("content")

                if kind == "user":
                    text = _text_of(content)
                    if text is None:
                        # tool_result 만 담긴 user 레코드. 앞선 tool_use 의 결과다.
                        self._apply_results(content, pending, events)
                        bump("tool_result")
                        continue
                    # 먼저 판정하고 **자른 뒤에** redact 한다. 전문에 정규식을
                    # 돌리면 대부분이 곧 버려지거나 600자로 잘리는데도 비용을
                    # 낸다(실측 16만자 7.3ms → 앞 600자만 1.4ms).
                    text = text.strip()
                    if guard.is_envelope(text):
                        # 슬래시 명령 봉투 안의 <command-args> 는 사람이 실제로
                        # 타이핑한 말이다. 봉투째 버리면 세션 첫 메시지(대개 목표
                        # 진술)가 사라져 GOAL 슬롯이 중간 메시지로 채워진다.
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
                        # 모델의 사적 추론은 교차 벤더로 옮기지 않는다.
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
                        arg = guard.redact_b64(_arg_of(tool_input))[:120]
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

    @staticmethod
    def _apply_results(content, pending: Dict[str, int], events: List[Event]) -> None:
        """tool_result 를 id 로 앞선 tool_use Event 에 붙인다.

        Event 는 frozen 이므로 실패한 항목만 교체한다. 성공은 기본값이다.
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
        """사람이 대화한 세션인가. entrypoint 와 서브체인 표식으로 판정한다.

        실측: 이 레포의 최상위 세션 31개 중 1개만 entrypoint=cli 이고 30개가
        sdk-py(보안 리뷰 훅 등이 남긴 것)였다.
        """
        head = head_of(source_path)
        if str(head.get("entrypoint") or "") in NON_INTERACTIVE_ENTRYPOINTS:
            return False
        return not (head.get("sidechain") or head.get("agentId"))

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
            cwd=cwd_of(source_path) or cwd,
            epoch=stat.st_mtime,
            size=stat.st_size,
        )

    def native_resume_hint(self, ref: SessionRef) -> Optional[str]:
        """같은 벤더끼리는 이것이 무손실이며 우월하다. 우리 요약은 열등하다."""
        return "claude --resume {}".format(ref.session_id)

    def install_handoff(self, bundle: HandoffBundle) -> InstallReceipt:
        return install_state_artifact(bundle, home=self._home)

    def fallback_channels(self):
        """Claude Code 는 SessionStart 훅이 신뢰 문제 없이 동작하므로 폴백이 없다.

        비어 있음을 명시한다 — 기반 클래스를 아무도 상속하지 않으므로 기본값이
        상속으로 얻어지지 않는다.
        """
        return ()
