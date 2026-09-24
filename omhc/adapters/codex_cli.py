from __future__ import annotations

import glob
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
    NoInjectionChannel,
    SessionRead,
    SessionRef,
)
from ..event import ARG_LIMIT, Event
from . import _register, allow_headless, install_state_artifact, iso_epoch

ARTIFACT_NAME = "omhc.txt"

# 날짜 디렉터리 스캔 범위. sessions/YYYY/MM/DD 구조라 전체 walk 를 하면 오래된
# 세션까지 전부 stat 한다. 훅 경로에서 돌아가므로 범위를 묶는다.
SCAN_DAYS = 14

# 파싱하는 봉투 타입. 나머지(world_state, turn_context 등)는 부기다.
# event_msg 는 item_completed 만 쓴다 — 셸·편집의 사실은 거기에만 있다.
_PARSED_ENVELOPES = frozenset({"response_item", "event_msg"})

# 통과시키는 role. developer 는 <skills_instructions> / <multi_agent_role> 등
# 순수 기계장치이므로 파싱조차 하지 않는다.
_PARSED_ROLES = frozenset({"user", "assistant"})

# 텍스트를 담는 블록 타입. user/developer 는 input_text, assistant 는 output_text.
_TEXT_BLOCKS = frozenset({"input_text", "output_text", "text"})

# --- 실측 (codex-cli 0.141.0–0.155.1, 이 머신 211개 rollout, 2026-09) --------
# 세 시대가 섞여 있다.
#
# era A (0.141–0.142): 셸은 response_item/function_call name="exec_command" 다.
#   arguments 는 JSON 문자열이고 cmd(str, 381/381 실측)가 곧 셸 명령이다.
#   workdir(절대경로, 가끔 없음)도 같이 온다. parsed_cmd 는 없다. 출력은
#   function_call_output.output 의 평문이고("Chunk ID: … / Wall time: … /
#   Process exited with code N 또는 Process running with session ID N /
#   Original token count: N / Output: …") JSON 이 아니다. 백그라운드로 돌던
#   프로세스는 나중의 write_stdin(args.session_id==N) 출력에서 결과가 온다.
#   편집은 custom_tool_call name="apply_patch" 이고 JSON arguments 가 아니라
#   최상위 input 에 원본 패치 텍스트가 들어 있다(*** Add/Update/Delete File:,
#   *** Move to: 줄에서 경로를 읽는다). 같은 편집이 event_msg/item_completed
#   FileChange(id==call_id, 절대경로)로 또 온다 — 둘 다 이벤트를 만들면 이중
#   계상이라 FileChange 로 원래 이벤트를 덮어쓴다.
# era B0 (0.144–0.148): 명령이 custom_tool_call name="exec" 의 JS 소스 문자열
#   안에만 있다. JS 는 파싱하지 않는다(화이트리스트, fail-closed) — 그 구간의
#   파일은 사실 없이 남는다(SCAN_DAYS 밖이라 손실을 감수한다).
# era B (0.149–0.155.1, 현재): 셸은 event_msg/item_completed
#   item.type="CommandExecution" 이다(status completed⇔exit 0, failed⇔exit≠0,
#   parsed_cmd 있음). 같은 자리의 custom_tool_call name="exec" 는 JS 래퍼
#   부기다. 멀티에이전트 도구(spawn_agent 등)는 function_call 로 온다.
_JS_WRAPPER_TOOL = "exec"
# parsed_cmd 가 전부 이 종류면 읽기다. Codex 에는 읽기 전용 도구가 따로 없어서
# 이걸 안 쓰면 Codex 세션에 inspected 가 영영 없다.
_INSPECT_KINDS = frozenset({"read", "list_files", "search"})
_SHELL_FLAGS = frozenset({"-c", "-lc"})

# 도구 이름 → 중립 동사. 실측된 이름만 올린다(invariant 5 — 도구명 자체는 IR에
# 남기지 않고 동사로만 흡수한다). 모르는 이름은 unmapped_tool 로만 세고 이벤트를
# 만들지 않는다 — 빈 arg 의 가짜 ran 이 300개쯤 생기는 것보다 낫다.
_VERB_BY_TOOL = {
    "exec_command": "ran",
    "apply_patch": "modified",
    "spawn_agent": "delegated",
}

# 부기 전용 도구 — 이벤트를 만들지 않고 dropped["tool_bookkeeping"] 으로만
# 센다. write_stdin 은 예외적으로 session_id 를 원래 exec_command 이벤트에
# 되돌려 붙이는 데 쓰이지만(read_session 참고), 그 자신은 이벤트가 되지 않는다.
_BOOKKEEPING_TOOLS = frozenset({
    "write_stdin", "wait", "wait_agent", "list_agents", "interrupt_agent",
    "send_message", "followup_task", "request_user_input",
    "list_available_plugins_to_install",
})

# apply_patch 의 top-level input 에서 경로를 뽑는 줄. Move to: 는 목적지 경로다.
_PATCH_PATH_RE = re.compile(
    r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", re.MULTILINE)

# exec_command/write_stdin 출력의 고정 헤더 줄. JSON 이 아니라 평문이다.
_EXIT_CODE_RE = re.compile(
    r"^(?:Process exited with code|Exit code:) (\d+)$", re.MULTILINE)
_RUNNING_SID_RE = re.compile(
    r"^Process running with session ID (\S+)$", re.MULTILINE)
_ABORTED_RE = re.compile(r"^aborted by user after ", re.MULTILINE)

# 사람이 타이핑한 내용의 kind 접두. 실측된 값:
#   ['user.text']                        ← 진짜 사람의 프롬프트
#   ['environments.environment_context'] ← 환경 프롬프트 (role=user 인데 기계장치)
#   ['host_skills.instructions', 'multi_agent.role_instructions', …] ← developer
#
# 접두 허용(deny-by-default 아님)이라 새로운 user.* kind 가 생겨도 사람의 말을
# 잃지 않는다. 봉투 판정과 함께 2중으로 쓴다 — 메타데이터는 정확하지만
# 하네스별이고, 봉투는 덜 정확하지만 모든 하네스에서 동작한다.
_HUMAN_KIND_PREFIX = "user."


def human_kinds(payload: dict):
    """content_item_kinds. payload 최상위가 아니라 메타데이터 안에 중첩돼 있다."""
    meta = payload.get("internal_chat_message_metadata_passthrough")
    if not isinstance(meta, dict):
        return None
    kinds = meta.get("content_item_kinds")
    if not isinstance(kinds, list) or not kinds:
        return None
    return [str(k) for k in kinds]


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


# 프로그래매틱 앱서버 클라이언트의 originator. 전부 source="vscode" 로 오므로
# source 값으로는 구분이 안 된다. 실측(이 머신, 2026-09-24): applecider 36개,
# splitlane* 3개 — 전부 role=user 턴이 "User goal: … Current browser URL: …
# Active project file: …" 형태의 기계 템플릿이다(사람이 타이핑한 문장이 아니다).
# thread_source="user" 를 허용목록으로 쓰지 않는다: 실측상 진짜 대화형 39개 중
# 38개가 이 필드를 갖지만 1개(Codex Desktop 0.146.0-alpha.3.1)는 없다. 문서화되지
# 않은 필드라 언제든 사라질 수 있고, 허용목록이면 그때 진짜 세션을 조용히 잃는다.
# 대신 새 프로그래매틱 originator 는 여기에 손으로 더해야 한다.
_PROGRAMMATIC_ORIGINATORS = frozenset({"applecider", "codex_exec"})
_PROGRAMMATIC_ORIGINATOR_PREFIXES = ("splitlane",)


def _is_headless_originator(originator) -> bool:
    if not isinstance(originator, str):
        return False
    if originator in _PROGRAMMATIC_ORIGINATORS:
        return True
    return originator.startswith(_PROGRAMMATIC_ORIGINATOR_PREFIXES)


def _is_interactive(meta: dict) -> bool:
    """서브에이전트·헤드리스 실행을 걸러낸다. 차단목록이며 허용목록이 아니다 —
    모르는 source/originator 는 여전히 대화형으로 남는다.

    실측(이 머신, 2026-09-24): host rollout 194개 중 116개가 서브에이전트
    스레드다. 부모 에이전트의 role=user 프롬프트가 content_item_kinds=
    ['user.text'] 로 오기 때문에 사람 화이트리스트를 그대로 통과해 GOAL/NEXT 로
    둔갑한다(invariant 3 위반). 실물 모양(codex-cli 0.155.1):
    source={"subagent": {"thread_spawn": {...}}}, thread_source="subagent",
    parent_thread_id=<uuid> — 셋 중 하나만 있어도 서브에이전트이고, 이건
    OMHC_ALLOW_HEADLESS 로도 절대 풀리지 않는다(발화자가 다른 문제라서).
    `codex exec` 는 originator="codex_exec", source="exec" (샌드박스 rollout
    5개 전부 실측). applecider/splitlane* 은 vscode 확장 안에 얹힌 프로그래매틱
    클라이언트다(위 주석) — 셋 다 헤드리스이고 오버라이드가 켜졌을 때만
    대화형이다.
    """
    source = meta.get("source")
    if isinstance(source, dict) and "subagent" in source:
        return False
    if meta.get("thread_source") == "subagent":
        return False
    if meta.get("parent_thread_id"):
        return False
    if source == "exec" or _is_headless_originator(meta.get("originator")):
        return allow_headless()
    return True


def _recent_date_dirs(root: str, days: int, now) -> List[str]:
    """최근 N일의 날짜 디렉터리. **UTC 와 로컬 날짜를 모두 넣는다.**

    Codex 가 디렉터리를 어느 시간대로 이름 붙이는지 이 머신(TZ=UTC)에서는
    구분할 수 없다. UTC 만 쓰면 KST(UTC+9) 같은 환경에서 매일 로컬 00:00~09:00
    동안 오늘 디렉터리가 스캔 목록에 없어 핸드오프가 조용히 실패한다. 양쪽을
    넣는 비용은 glob 몇 번이고, 중복은 dict 로 제거한다.
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
    """apply_patch 의 원본 패치 텍스트에서 *** …: 줄만 읽는다. 상대경로는
    workdir 이 있으면 그걸 기준으로 절대화하고, 없으면 받은 그대로 둔다."""
    paths = []
    for m in _PATCH_PATH_RE.finditer(text):
        p = m.group(1).strip()
        if workdir and not os.path.isabs(p):
            p = os.path.join(workdir, p)
        paths.append(p)
    return tuple(paths)


def _arg_and_paths(payload: dict) -> Tuple[str, Tuple[str, ...]]:
    """exec_command 는 arguments(JSON 문자열).cmd, apply_patch 는 최상위 input
    (원본 패치 텍스트), spawn_agent 는 task_name/agent_type 을 읽는다.
    message 는 절대 읽지 않는다 — 에이전트가 쓴 프롬프트 본문이다."""
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

    # exec_command: cmd 는 str, 실측 381/381.
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
    """item_completed 의 item 하나 → (verb, ok, arg, paths). 모르는 타입이면 None."""
    kind = item.get("type")
    ok = item.get("status") == "completed"
    if kind == "CommandExecution":
        command = item.get("command")
        if isinstance(command, list):
            parts = [str(c) for c in command]
            # ["/bin/bash", "-lc", "ls omhc"] — 셸 래퍼를 벗긴다.
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
            # 실측(era B, 2건): grep/rg 류는 매치 없음을 exit 1 로 표현하는
            # 관용구가 있다 — parsed_cmd 가 전부 읽기이고 출력도 비었으면 그
            # 관용구로 본다. 위험: 검증용 `grep -q` 는 보통 parsed_cmd 가
            # unknown 이라 여기 안 걸리고 실패로 남는다(의도적으로 손대지 않음
            # — era A 처럼 명령어 이름으로 짐작하지 않는다).
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
    """_parse_exec_outcome 의 결과. ok=None 은 '이 출력에서 알 수 없다'다 —
    오늘처럼 이벤트는 ok=True 로 남는다(출력이 아예 없는 abort 도 마찬가지)."""

    __slots__ = ("ok", "session_id")

    def __init__(self, ok: Optional[bool] = None, session_id: Optional[str] = None):
        self.ok = ok
        self.session_id = session_id


def _parse_exec_outcome(output) -> _ExecOutcome:
    """exec_command/write_stdin 의 평문 출력을 읽는다. JSON 이 아니다.

    "Process exited with code N" / "Exit code: N" → 그 코드. "Process running
    with session ID N" → 아직 안 끝났다, session_id 만 기록해 write_stdin 쪽
    호출과 잇는다. "aborted by user after …" → 실패. 셋 다 없으면(포맷을 모름,
    또는 abort 인데 이 문구가 아닌 경우) ok=None 으로 오늘처럼 True 를 유지한다.
    """
    if not isinstance(output, str):
        return _ExecOutcome()
    # 헤더만 본다. "Output:" 뒤 본문은 프로그램이 찍은 것이라, 백그라운드 빌드가
    # "Exit code: 0" 같은 줄을 찍으면 "running" 헤더를 이기고 write_stdin 쪽 실패를
    # 잃는다. apply_patch 의 "Exit code: N\n…\nOutput:" 도 같은 자리에서 갈린다.
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
    # 실측(codex-cli 0.155.1, --dangerously-bypass-hook-trust): 최상위
    # {"additionalContext": …} 는 "hook: SessionStart Failed" 로 거부되고
    # 아무것도 주입되지 않는다. hookSpecificOutput 중첩 형식은 rollout 에
    # content_item_kinds=["hooks.additional_context"] 로 실제로 나타난다.
    wire = "claude"

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
                if not meta or not _is_interactive(meta):
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

    def discover(self, repo_root: Optional[str],
                deadline: Optional[float] = None) -> List[SessionRef]:
        """`omhc mark` 의 원장 백필 전용. list_sessions 와 대상은 같지만(같은
        디렉터리, 같은 대화형 판정), **epoch 이 다르다** — 여기서는 파일
        mtime 이 아니라 session_meta.timestamp(세션이 실제로 시작한 시각)를
        쓴다. cmd_mark 가 이 값을 원장의 최신 시작 epoch 와 비교해 백필 순서를
        지키므로(invariant 6), mtime 을 섞으면 재개(resume)로 mtime 만 갱신된
        옛 세션이 최신으로 오인될 수 있다.

        타임스탬프를 못 읽으면(모르는 모양) 조용히 건너뛴다 — 순서를 보장할
        수 없는 행을 원장에 넣는 것보다 놓치는 편이 낫다.

        `deadline` 을 넘기면(호출자의 시간 예산) 스캔 도중이라도 지금까지
        모은 것만 돌려주고 멈춘다 — 날짜 디렉터리가 14일치라 파일이 많을 때
        cmd_mark 의 훅 예산을 이 호출 하나가 다 쓸 수 있어서다.
        """
        if repo_root is None:
            return []
        root = os.path.realpath(repo_root)
        refs: List[SessionRef] = []
        for directory in _recent_date_dirs(self.sessions_root(), SCAN_DAYS, self._now):
            if deadline is not None and time.time() > deadline:
                break
            # glob 은 파일시스템 순서라 정렬되지 않는다. 파일명이
            # rollout-YYYY-MM-DDTHH-MM-SS- 로 시작하므로 역순 정렬이 곧 최신순이고,
            # deadline 에 잘려도 가장 최근 세션부터 읽힌다.
            for path in sorted(glob.glob(os.path.join(directory, "rollout-*.jsonl")),
                               reverse=True):
                if deadline is not None and time.time() > deadline:
                    break
                meta = session_meta(path)
                if not meta or not _is_interactive(meta):
                    continue
                cwd = meta.get("cwd")
                if not isinstance(cwd, str) or not locate.is_within(root, cwd):
                    continue
                started = iso_epoch(meta.get("timestamp"))
                if not started:
                    continue
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                refs.append(
                    SessionRef(
                        adapter_id=self.adapter_id,
                        session_id=str(meta.get("session_id") or meta.get("id") or ""),
                        source_path=path,
                        cwd=cwd,
                        epoch=started,
                        size=size,
                    )
                )
        return refs

    def read_session(self, ref: SessionRef) -> SessionRead:
        events: List[Event] = []
        dropped: Dict[str, int] = {}
        unparsed = 0
        pending: Dict[str, int] = {}  # call_id -> events 인덱스
        # write_stdin 의 args.session_id 로만 찾을 수 있다 — write_stdin 자신의
        # call_id 는 별개다. "Process running with session ID N" 을 만난 원래
        # exec_command 이벤트를 여기 걸어 두고, 나중에 write_stdin 이 오면
        # 그 call_id 를 pending 에도 같은 인덱스로 얹는다(아래 참고).
        pending_by_session: Dict[str, int] = {}
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

                epoch = iso_epoch(row.get("timestamp"))
                kind = str(payload.get("type"))

                if envelope == "event_msg":
                    item = payload.get("item")
                    if kind != "item_completed" or not isinstance(item, dict):
                        bump(envelope)
                        continue
                    fact = _item_fact(item)
                    if fact is None:
                        # UserMessage/AgentMessage 는 response_item 의 사본이다.
                        bump("item:" + str(item.get("type")))
                        continue
                    verb, ok, arg, paths = fact
                    item_id = item.get("id")
                    idx = pending.get(item_id) if isinstance(item_id, str) else None
                    if idx is not None:
                        # era A: apply_patch 의 call_id 와 이 FileChange 의 id 가
                        # 같다 — 절대경로 사실은 여기에만 있으므로 원래 이벤트를
                        # 갱신한다(둘 다 세면 이중 계상). seq/offset/length 는
                        # 첫 레코드 것을 유지한다(invariant 6).
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
                            # role=user 로 위장한 기계장치. 실측:
                            # ['environments.environment_context']
                            bump("kind:" + kinds[0])
                            continue
                    if not guard.safe(text, author):
                        bump("guarded_" + author)
                        continue
                    seq += 1
                    events.append(Event(
                        seq=seq, epoch=epoch, author=author, verb="said", ok=True,
                        text=text, arg="", paths=(), offset=start, length=len(raw),
                    ))
                    continue

                if kind == "custom_tool_call" and payload.get("name") == _JS_WRAPPER_TOOL:
                    bump("js_exec")
                    continue

                if kind in ("function_call", "local_shell_call", "custom_tool_call"):
                    name = str(payload.get("name") or kind.replace("_call", ""))
                    call_id = payload.get("call_id")

                    if name == "write_stdin":
                        # 백그라운드로 돌던 exec_command 로 입력을 보낸다 — 그
                        # 자신은 부기이지만, 이 call_id 의 출력(아래)이 원래
                        # exec_command 이벤트의 최종 결과다. session_id 로 그
                        # 이벤트를 찾아 같은 인덱스를 이 call_id 에도 건다.
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
                    # agent_message 는 에이전트 간 메시지다(실측 170건) — human
                    # 도 said 도 아니다(invariant 3). web_search_call/
                    # tool_search_* 는 실측되지 않은 부기 후보라 unparsed 대신
                    # dropped 로 계상한다.
                    bump(kind)
                    continue

                # response_item 인데 모양을 모른다 → 조용히 버리지 않는다.
                unparsed += 1

        return SessionRead(ref=ref, events=tuple(events), unparsed=unparsed,
                           dropped=dropped)

    def classify(self, source_path: str) -> bool:
        """Codex rollout 에 사람이 시작한 세션인가.

        session_meta 를 읽을 수 있어야 하고, 서브에이전트·헤드리스 exec 가
        아니어야 한다(`_is_interactive`).
        """
        meta = session_meta(source_path)
        return meta is not None and _is_interactive(meta)

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

    def hook_is_installed(self) -> bool:
        """omhc 를 부르는 SessionStart 훅이 설치돼 있는가.

        pull 채널(state 산출물)은 훅이 그것을 읽어갈 때만 전달이 성립한다.
        훅이 없으면 산출물을 써도 아무도 보지 않으므로 전달이 아니다 — 그것을
        성공으로 보고하면 Path B 가 영원히 발동하지 않는다.
        """
        try:
            with open(self.hooks_path(), encoding="utf-8", errors="replace") as fh:
                return "omhc" in fh.read()
        except OSError:
            return False

    def install_handoff(self, bundle: HandoffBundle) -> InstallReceipt:
        if not self.hook_is_installed():
            raise NoInjectionChannel(
                "no omhc SessionStart hook at {}; the artifact would be written but "
                "never read".format(self.hooks_path())
            )
        return install_state_artifact(bundle, home=self._home)

    def fallback_channels(self):
        """Path B: install_handoff 가 실패할 때만 열린다 —

        즉 이 brief 호출이 실제로 실행됐는데(codex-cli 훅이 신뢰돼 돌았거나,
        수동 `omhc brief --harness codex-cli` 였거나) install_handoff 가 실패한
        경우다. 대표적으로 hooks.json 에 omhc 훅 문자열이 없을 때
        NoInjectionChannel 을 던지지만, deliver() 의 채널 루프는 install_handoff
        의 다른 예외(예: ~/.omhc 쓰기 실패)도 같은 방식으로 여기로 넘긴다.
        훅 자체가 신뢰되지 않아 brief 가 한 번도 안 돌면 deliver() 호출 자체가
        없으므로 Path B 도 열리지 않는다 — 그 경우 Codex 로 들어가는 방향은
        아무것도 받지 못한다."""
        from .. import agents_md

        return (agents_md.install,)

    def _config_trusts_hook(self) -> bool:
        """`~/.codex/config.toml` 에 이 hooks.json 을 신뢰한다는 항목이 있는가.

        3.9 에는 tomllib 이 없으므로 평문 부분 문자열 검색으로 충분하다 — 해시
        의미론(codex 가 훅 내용의 무엇을 해시하는지)은 확인된 바 없으므로, 이
        신호 하나만으로 실패 판정을 내리지 않는다(정적 힌트일 뿐이다).
        """
        config_path = os.path.join(self.home, ".codex", "config.toml")
        needle = 'hooks.state."{}:session_start:'.format(self.hooks_path())
        try:
            with open(config_path, encoding="utf-8", errors="replace") as fh:
                return needle in fh.read()
        except OSError:
            return False

    def health(self, repo_root: Optional[str], ledger_rows):
        """훅이 설치돼 있는데 실제로 돈 적이 없는지 행태로 진단한다.

        정적 신호(hooks.json 존재)만으로는 신뢰 여부를 알 수 없다 — codex-cli
        0.155.1 은 신뢰되지 않은 훅을 메시지도 원장 행도 없이 건너뛴다. 그래서
        설치 이후 Codex 세션이 실제로 omhc mark 를 남겼는지를 원장과 대조한다.
        무엇이 잘못돼도 status 자체가 죽으면 안 되므로(진단 도구), 통째로
        감싸 실패는 PASS/unknown 으로 열화시킨다.
        """
        try:
            if not self.hook_is_installed():
                # 훅을 설치한 적 없는 사용자에게 매번 행을 보여주는 건 소음이다
                # — "설치 안 됨" 은 기존 "adapters" 행이 이미 말해준다.
                return ()
            hooks_path = self.hooks_path()
            try:
                install_epoch = os.path.getmtime(hooks_path)
            except OSError as exc:
                return (("codex hook", True, "unknown ({})".format(exc)),)

            # 세션 id 는 전역 유일이다(Codex 가 부여) — 레포 경계로 거르지 않는다.
            # ledger_rows 를 레포로 먼저 거르면 워크트리·서브모듈처럼 자기 .git 을
            # 가진 중첩 디렉터리에서 시작한 세션이 새는 repo 키로 기록돼 영원히
            # 안 돈 것으로 보인다(리뷰 결함) — 그래서 호출자(cli.py)는 레포로
            # 거르지 않은 원장을 여기로 넘긴다.
            ran_sessions = set()
            for row in ledger_rows:
                if row.get("harness") != self.adapter_id:
                    continue
                if row.get("via") == "scan":
                    # 향후 백필 행. 훅이 실제로 돌았다는 증거가 아니므로 세지
                    # 않는다 — 세면 문제를 가려버린다.
                    continue
                sid = row.get("session")
                if sid:
                    ran_sessions.add(sid)

            candidates = []  # (started_epoch, ref, meta)
            for ref in self.list_sessions(repo_root):
                meta = session_meta(ref.source_path)
                if not meta:
                    continue
                # 파일 두 개(hooks.json mtime, rollout 의 session_meta.timestamp)의
                # 시각을 비교한다 — 한쪽이 mtime 이라 invariant 6 이 금지하는
                # "순서의 근거"가 아니라 일회성 진단이라 허용한다(이 비교 결과로
                # 이벤트를 정렬하지 않는다). cli._backfill_foreign_sessions 가
                # 하는 비교와는 다르다 — 거기는 두 세션 시작 epoch(둘 다
                # session_meta.timestamp 계열, mtime 아님)를 비교해 원장 append
                # 순서를 정하는, invariant 6 이 허용하는 예외다.
                started = iso_epoch(meta.get("timestamp"))
                if not started or started <= install_epoch:
                    continue
                candidates.append((started, ref, meta))

            if not candidates:
                return (("codex hook", True, "no Codex sessions since install"),)

            # 신뢰는 config.toml 을 바꾸지, hooks.json 을 바꾸지 않는다 — 신뢰
            # 이전 세션은 install_epoch 이후라도 영원히 "안 돈 것"으로 남는다.
            # 그래서 전체 개수가 아니라 **가장 최신** 세션의 행태로 판정한다:
            # 최신이 돌았으면 그 시점부터는 신뢰가 성립한 것이므로 PASS, 아니면
            # 최신에서부터 거슬러 연속으로 안 돈 세션 수를 센다.
            candidates.sort(key=lambda c: c[0], reverse=True)
            missing_streak = 0
            newest_missing_meta = None
            for started, ref, meta in candidates:
                if ref.session_id in ran_sessions:
                    break
                missing_streak += 1
                if newest_missing_meta is None:
                    newest_missing_meta = meta

            if missing_streak == 0:
                return (("codex hook", True, "ran for the latest session since install"),)

            # UNVERIFIED: Codex Desktop/IDE 세션(originator 예: codex_work_desktop)이
            # 이 훅을 애초에 전혀 돌리지 않을 수 있다 — 확인된 바 없다. 오진단이
            # 눈에 보이도록 가장 최신 미실행 세션의 originator 를 detail 에 남긴다.
            originator = newest_missing_meta.get("originator")
            if not isinstance(originator, str) or not originator:
                originator = "unknown"
            detail = ("{} consecutive Codex session(s) since install never ran the "
                       "omhc hook (newest: {}) — trust it in Codex (untrusted hooks "
                       "are skipped silently)".format(missing_streak, originator))
            if not self._config_trusts_hook():
                detail += "; no trust entry in ~/.codex/config.toml"
            return (("codex hook", False, detail),)
        except Exception as exc:  # 진단이 status 자체를 죽이면 안 된다 (invariant 7)
            return (("codex hook", True, "unknown ({})".format(exc)),)
