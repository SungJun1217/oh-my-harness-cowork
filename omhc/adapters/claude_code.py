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

# Claude Code 2.1.281 자신의 스킵 판정: isApiErrorMessage===true ||
# isVirtual===true || message.model==="<synthetic>" (바이너리에서 확인).
# 이 값들을 가진 assistant 레코드는 사람도 에이전트도 쓰지 않은, 하네스가
# 스스로 합성한 텍스트다(로그인 안내, 합성 PushNotification tool_use 등).
_SYNTHETIC_MODEL = "<synthetic>"

_PATH_KEYS = ("file_path", "path", "notebook_path")
_ARG_KEYS = ("command", "file_path", "pattern", "query", "prompt", "description", "path")

# /branch, --fork-session, /fork 백그라운드 복사가 만드는 트랜스크립트: 새
# session_id 로 시작해 부모의 현재 메시지 사슬을 복사한다. 복사된 레코드는
# 원본에 sessionId/parentUuid/isSidechain/sessionKind 를 덮어쓰고 forkedFrom:
# {sessionId, messageUuid} 을 얹은 것이고(uuid/timestamp/type/message 는 원본
# 그대로), history-suppression(cause:"fork_inherit") 레코드가 맨 앞에 붙을 수
# 있다. 부모가 이미 상대 하네스에 전달됐었다면 포크 자신의 사람 턴이 없어도
# 그 사람 턴이 새 session_id 아래 "새 일"처럼 다시 나간다(#34, 실측: 425B 가
# 두 번 전달됨) — #27 의 offset 가드는 delivered 행이 이 새 session_id 에는
# 아예 없어서 적용되지 않는다.
_FORKED_FROM_MARK = b'"forkedFrom"'
_FORK_INHERIT_MARK = b'"fork_inherit"'

# fork 판정 비용 상한. own tail(복사 구간을 벗어난 뒤)이 이 바이트를 넘도록
# 새 턴을 못 찾으면, 또는 전체 스캔이 이 시간을 넘으면 판정을 포기하고 예전
# 동작(적격)으로 연다 — 훅 경로에서 판정 실패가 세션 시작을 막으면 안 된다
# (invariant 2). 복사 구간 자체는 줄당 값싼 substring 검사뿐이라 바이트 상한에
# 넣지 않는다(리뷰 지적: 넣으면 8MB 넘는 부모의 포크가 own tail 을 보기도
# 전에 항상 fail-open 됐다 — 6.9MB 부모를 포크로 다시 쓴 12.6MB 픽스처가
# 재현) — 실측(synthetic, 이 머신): 복사만 있는 6.3MB 6.7ms, 12.6MB 13.2ms,
# 25.1MB 26.3ms, 30MB(tail 없이 EOF 까지) 32.3ms, own turn 이 있는 현실적인
# 2MB 복사 구간 + tail 은 2.3ms. 전부 시간 상한(50ms) 안이다.
_FORK_SCAN_BYTE_LIMIT = 8 * 1024 * 1024
_FORK_SCAN_TIME_LIMIT = 0.05

# classify() 결과의 프로세스당 캐시. brief 한 번마다 같은 파일이 두 번
# 스캔된다 — ref_for_path() 가 한 번(적격이면 그대로 쓰고), 적격이 아닐 때는
# brief.eligible() 이 adapter.classify(mark.path) 로 다시 부른다. adapters.get()
# 이 호출마다 새 인스턴스를 만들므로(레지스트리가 클래스를 들고 있다) 캐시는
# 인스턴스가 아니라 모듈에 둔다. 키에 size·mtime_ns 를 넣어 포크가 자라
# (own turn 이 생기면) 자동으로 무효화되게 한다. watch.py 데몬처럼 오래 도는
# 프로세스에서 무한히 늘지 않도록 LRU 로 크기를 제한한다.
_CLASSIFY_CACHE_MAX = 256
_classify_cache = collections.OrderedDict()  # (path, size, mtime_ns, headless) -> bool


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
                except (ValueError, RecursionError):
                    # 깊게 중첩된 줄은 json 이 RecursionError 를 낸다 — 깨진 줄과
                    # 같이 건너뛴다(#34 리뷰: classify/list_sessions 가 죽었다).
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


def _looks_like_fork(path: str) -> bool:
    """싸구려 사전판정. 포크의 복사 구간은 파일 맨 앞에서 시작하므로(선택적
    history-suppression 프리펜드 한 줄 포함) 앞 몇 줄만 보면 된다 — 전체
    스캔은 포크로 보일 때만 아래에서 한다."""
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
    """포크인데 포크 자신의 사람 턴이 아직 하나도 없는가.

    복사된 레코드는 전부 forkedFrom 을 달고 파일 앞쪽에 연속으로 온다(분석
    근거). forkedFrom 이 끊기는 지점부터가 포크 자신이 새로 쓴 구간이고, 거기
    안에서 read_session 과 같은 화이트리스트로 사람의 말을 찾는다 — 하나라도
    있으면 적격이다.

    바이트/시간 상한을 넘기면 판정을 포기하고 False(예전 동작인 적격)로
    돌아간다. 안 여는 쪽(과대판정으로 진짜 새 턴을 계속 숨김)보다 여는 쪽이
    싸다 — 최악이 "부모 턴이 한 번 더 나간다"는 이미 있던 결함이지 새 결함이
    아니다.

    바이트 상한은 **복사 구간을 벗어난 뒤(own tail)** 만 센다 — 복사 구간은
    줄당 값싼 substring 검사뿐이고(리뷰 실측: 6.3MB 7ms, 12.6MB 9.5ms,
    25.1MB 9.8ms) 그 바이트까지 상한에 넣으면 부모가 조금만 커도(8MB 넘으면)
    own tail 을 보기도 전에 늘 포기해 재전달 버그를 못 고친 채로 둔다(리뷰
    확인: 6.9MB 부모를 포크로 다시 쓴 12.6MB 픽스처가 항상 fail-open 됐다).
    시간 상한은 복사 구간을 포함해 매 줄마다 확인한다 — 전체 스캔 시간의
    유일한 안전망이다.

    예상 밖의 레코드 모양(message 가 dict 가 아니거나 text 블록의 text 가
    문자열이 아닌 등)은 여기서 예외를 낸다 — classify()/list_sessions() 를
    깨뜨려 그 하네스의 세션 전체를 잃는 것(watch 가 그 레포의 Claude ref 를
    전부 드롭)보다는 판정 하나를 포기하고 여는 쪽이 싸므로 한 줄 단위로
    감싸 fail-open 한다.
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
                    # history-suppression 프리펜드 등 복사 구간 진입 전이다.
                    continue
                # 복사 구간을 벗어난 첫 레코드부터 포크 자신의 새 내용이다.
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

                message = row.get("message") or {}
                if kind == "assistant" and (
                    row.get("isApiErrorMessage") is True
                    or row.get("isVirtual") is True
                    # isVirtual 은 실물에서 아직 목격되지 않았지만 업스트림
                    # 판정식의 일부라 함께 넣는다.
                    or message.get("model") == _SYNTHETIC_MODEL
                ):
                    bump("synthetic")
                    continue

                epoch = iso_epoch(row.get("timestamp"))
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
        """미구현(선택 메서드, discover/health 와 같은 패턴) — Claude 쪽
        live-continue(핸드오프 뒤 같은 Claude 세션에 새 턴만 이어지는 경우)는
        아직 감지하지 않는다(#22, README 의 남은 한계). 기본값 None 은
        "이 어댑터는 구분할 수 없다"이고, 호출자는 그러면 오늘의 전체 스캔
        경로를 그대로 쓴다 — 인자를 받기만 하고 항상 무시한다(base 계약과
        같은 키워드 모양을 유지해야 cli.py 가 어댑터를 구분 안 하고 부를 수
        있다)."""
        return None

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
        """사람이 대화한 세션인가. entrypoint·서브체인 표식에 더해, 포크(#34)면
        포크 자신의 사람 턴이 하나라도 있어야 적격이다 — 없으면 부모 세션의
        전달 이력을 그대로 물려받아 이미 전달된 턴이 새 session_id 아래 또
        나간다.

        실측: 이 레포의 최상위 세션 31개 중 1개만 entrypoint=cli 이고 30개가
        sdk-py(보안 리뷰 훅 등이 남긴 것)였다.

        brief 한 번에 이 메서드가 같은 파일에 두 번 불릴 수 있다 —
        ref_for_path() 가 한 번, 그게 거절하면 brief.eligible() 이 다시 한 번.
        결과를 (path, size, mtime_ns) 로 캐싱해 두 번째 호출이 다시 스캔하지
        않게 한다(리뷰 지적) — 파일이 자라면(포크가 own turn 을 얻으면) 키가
        바뀌므로 자동으로 무효화된다.
        """
        try:
            stat = os.stat(source_path)
            # allow_headless() 도 판정을 바꾸므로 키에 넣는다(#34 리뷰) — 실제
            # 프로세스에선 환경이 안 바뀌지만, 한 프로세스에서 켰다 끄는 쪽에도 안전하다.
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
        """빈 튜플을 명시한다. list_sessions 는 이 머신에서 130개 파일 34.3MB 를
        읽어 249ms 였다(brief.py 의 _ref_for 주석) — Codex 쪽 mark 가 이걸
        돌리면 훅 예산을 넘긴다. Claude 세션은 자기 훅이 항상 신뢰되므로
        Codex→Claude 백필을 Claude 어댑터가 구현할 필요도 없다."""
        return ()

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

    def health(self, repo_root: Optional[str], ledger_rows):
        """Claude Code 의 SessionStart 훅은 신뢰 문제가 없어 codex 류의 조용한
        생략이 없다 — 진단할 행태 결함이 없으므로 빈 튜플이다.
        """
        return ()

    def hook_config(self):
        return hookconf.HookConfig(
            config_path=os.path.join(self.home, ".claude", "settings.json"),
            fragment_name="claude-settings.fragment.json",
            post_write_note="",
        )
