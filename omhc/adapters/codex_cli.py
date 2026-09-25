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

# `~/.codex/config.toml` 의 `project_root_markers = [...]` 를 뽑는다(#31). 키
# 이름은 따옴표로 감싸일 수도 있다(TOML bare/quoted key 둘 다 유효). 여러 줄
# 배열까지 잡도록 비탐욕 DOTALL 로 첫 `]` 까지 본다.
_ROOT_MARKERS_KEY_RE = re.compile(
    r'(?m)^[ \t]*[\'"]?project_root_markers[\'"]?[ \t]*=[ \t]*(\[.*?\])', re.DOTALL)
# 리뷰 #2: `[projects."..."]` 같은 테이블 헤더는 그 아래 키의 스코프를 바꾼다
# (`[table]\nproject_root_markers = …` 는 최상위 키가 아니다) — 첫 헤더 앞까지만
# 최상위로 본다.

# `~/.codex/config.toml` 의 `project_doc_max_bytes = N`(#33). Codex 가 AGENTS.md
# 체인 전체(레포 루트부터 cwd 까지)를 머리부터 읽는 총 예산이다(deep-research,
# 2026-09-25 — 문서에 값이 없어 임베디드 기본값 32768 을 실측 근사로 쓴다).
# 정수 값만 인식한다 — 실측/문서 모두 정수 외의 꼴을 보인 적이 없다.
DEFAULT_PROJECT_DOC_MAX_BYTES = 32768
_PROJECT_DOC_MAX_BYTES_KEY_RE = re.compile(
    r'(?m)^[ \t]*[\'"]?project_doc_max_bytes[\'"]?[ \t]*=[ \t]*(-?\d+)')


def _first_table_header(text: str) -> int:
    """첫 `[section]`/`[[section]]` 머리(`[` 자체)가 시작하는 위치, 없으면 -1.

    문자열·주석을 건너뛰며 값 배열의 괄호 깊이를 추적하는 스캐너
    (`hookconf.toml_header_lines`, #32 에서 인라인 `[hooks]` 판정과 이걸
    공유하도록 뽑아냈다)를 그대로 쓴다 — 첫 것만 필요하면 결과의 첫 원소."""
    headers = hookconf.toml_header_lines(text)
    return headers[0][0] if headers else -1


# TOML basic("...", 이스케이프 있음)과 literal('...', 이스케이프 없음) 문자열
# 리터럴을 모두 인식한다.
_TOML_STRING_RE = re.compile(r'"(?P<d>(?:[^"\\]|\\.)*)"|\'(?P<s>[^\']*)\'')

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


def _is_subagent(meta: dict) -> bool:
    """서브에이전트 스레드인가. 차단목록이며 허용목록이 아니다 — 모르는
    source 는 여전히 서브에이전트가 아닌 것으로 남는다.

    실측(이 머신, 2026-09-24): host rollout 194개 중 116개가 서브에이전트
    스레드다. 부모 에이전트의 role=user 프롬프트가 content_item_kinds=
    ['user.text'] 로 오기 때문에 사람 화이트리스트를 그대로 통과해 GOAL/NEXT 로
    둔갑한다(invariant 3 위반). 실물 모양(codex-cli 0.155.1):
    source={"subagent": {"thread_spawn": {...}}}, thread_source="subagent",
    parent_thread_id=<uuid> — 셋 중 하나만 있어도 서브에이전트이고, 이건
    OMHC_ALLOW_HEADLESS 로도 절대 풀리지 않는다(발화자가 다른 문제라서).
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
    """`codex exec` 류 프로그래매틱 실행인가. `codex exec` 는
    originator="codex_exec", source="exec" (샌드박스 rollout 5개 전부 실측).
    applecider/splitlane* 은 vscode 확장 안에 얹힌 프로그래매틱 클라이언트다
    (위 _PROGRAMMATIC_ORIGINATORS 주석)."""
    return meta.get("source") == "exec" or _is_headless_originator(meta.get("originator"))


def _is_interactive(meta: dict) -> bool:
    """서브에이전트·헤드리스 실행을 걸러낸다. 서브에이전트는 언제나 제외,
    헤드리스는 OMHC_ALLOW_HEADLESS 가 켜졌을 때만 통과시킨다."""
    if _is_subagent(meta):
        return False
    if _is_headless_meta(meta):
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
            # 실측(era B, CommandExecution 2,708개 중 비0 종료 331개): grep/rg 류는
            # 매치 없음을 exit 1 로 표현한다. parsed_cmd 가 전부 읽기이고 출력도
            # 비었으면 그 관용구로 본다 — 이 규칙이 잡는 것은 3건이고 실제 실패는
            # 하나도 가리지 않는다.
            # 더 넓히지 않는 이유(#11):
            # - parsed_cmd 는 전부 아니면 전무다. 복합 명령에 모르는 부분(pwd,
            #   echo, 2>/dev/null 리다이렉트 …)이 하나라도 있으면 명령 전체가
            #   unknown 한 칸이 된다(unknown 이 다른 항목과 섞인 경우 0건). 그러니
            #   `pwd; rg …` 에 닿으려면 원문 셸 문자열을 쪼개야 하고, 그건 era A
            #   처럼 명령어 이름으로 짐작하는 일이다.
            # - unknown 에서는 빈 출력이 무해의 신호가 아니다: 출력을 파일로 돌린
            #   빌드·타입 검사 실패도 비어 보인다.
            # - 읽기 항목끼리여도 출력이 있으면 실패로 둔다: 없는 경로의 sed/cat/ls
            #   는 "No such file" 을 stdout 에 남긴다(stderr 는 늘 비어 있다 — pty
            #   로 stdout 에 합쳐진다). && 는 짧게 끊기므로 마지막 항목이 search
            #   라고 그 search 가 종료 코드를 낸 것도 아니다.
            # 위험: 검증용 `grep -q`/`rg -q` 는 실측상 unknown 이라 여기 안 걸린다.
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

    def _scan(self, repo_root: Optional[str], include_headless: bool, *,
              deadline: Optional[float] = None, newest_first: bool = False):
        """list_sessions/discover/health 가 공유하는 날짜 디렉터리 walk.
        서브에이전트는 언제나 뺀다; 헤드리스는 `include_headless` 로 직접
        켠다 — list_sessions/discover 는 `allow_headless()`(env)를 그대로
        넘기고, health() 의 "헤드리스만 있었나" 판별은 env 와 무관하게 True 를
        넘긴다. (path, meta) 를 yield 한다 — SessionRef 조립은 호출자 몫이다
        (list_sessions 는 파일 mtime, discover 는 session_meta.timestamp 를
        쓴다 — 서로 다른 epoch 정의라 여기서 합치면 invariant 6 을 흐린다).
        """
        if repo_root is None:
            return
        root = os.path.realpath(repo_root)
        for directory in _recent_date_dirs(self.sessions_root(), SCAN_DAYS, self._now):
            if deadline is not None and time.time() > deadline:
                break
            # glob 은 파일시스템 순서라 정렬되지 않는다. 파일명이
            # rollout-YYYY-MM-DDTHH-MM-SS- 로 시작하므로 역순 정렬이 곧 최신순이고,
            # deadline 에 잘려도 가장 최근 세션부터 읽힌다(discover 전용).
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
                # Codex 는 rollout 에 레포 루트를 기록한다. equal-or-descendant 로
                # 판정해야 서브디렉터리에서 시작한 세션도 잡힌다.
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
            # 줄 경계로 스냅한다(리뷰) — 이 값이 그대로
            # `_reactivate_grown_sessions` 의 첫 baseline 이 되므로, stat 이
            # 레코드 중간을 잡으면 그 레코드가 마저 쓰인 뒤 영영 못 읽는다.
            # 첫 관측이라 이전 baseline 이 없으므로 fallback 을 주지 않는다 —
            # 64KB 안에 개행을 못 찾으면(64KB 넘는 단일 레코드를 쓰는 중)
            # size 를 그대로 쓴다. 0 으로 과소평가하면 다음 판정이 파일
            # 처음부터 읽어 **원래의** 사람 턴을 새 턴으로 착각하고 옛 내용을
            # 다시 넘긴다(리뷰에서 재현). 과대평가는 그 긴 레코드 하나를
            # 놓칠 수 있을 뿐이고, 그게 사람 턴일 때만 손해다 — 알려진 한계.
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
        """`offset` 바이트 뒤만 읽는다 — 실측(이 머신, 13.8MB rollout):
        전체 read_session 593ms, 이 경로가 훅 예산(150ms)에서 유일하게 쓸 수
        있다. `_read` 가 줄 경계로 스냅하므로 `offset` 은 레코드 경계일 필요가
        없다(#22, `codex exec resume` 이 이어붙인 파일).

        `max_bytes`(리뷰: 늘어난 꼬리 크기에 비례해 비용이 늘어 훅 예산을
        넘길 수 있다 — 17.7MB 꼬리 실측 396.6ms)와 `stop_at_human_turn`(사람의
        새 턴이 있는지만 알면 되는 호출자는 첫 매치에서 멈춰 나머지를 안
        읽는다)은 `cli._reactivate_grown_sessions` 전용 선택 인자다 — 기본값은
        오늘의(무제한) 동작과 같아 conformance 의 "offset 이후 read_session 과
        같은 이벤트" 계약을 그대로 지킨다."""
        try:
            start = max(0, int(offset))
        except (TypeError, ValueError):
            start = 0
        events, unparsed, dropped, end_offset = self._read(
            ref.source_path, start, max_bytes=max_bytes,
            stop_at_human_turn=stop_at_human_turn)
        # _read 가 이미 줄 경계로 스냅해 start 이후의 레코드만 만들지만, 필터를
        # 한 번 더 걸어 둔다 — 계약(read_session 을 offset>=start 로 제한한 것과
        # 같아야 한다)을 코드로도 증명한다.
        events = tuple(e for e in events if e.offset >= start)
        return SessionSince(events=events, unparsed=unparsed, dropped=dropped,
                            end_offset=end_offset)

    def _read(self, path: str, start: int, *, max_bytes: Optional[int] = None,
             stop_at_human_turn: bool = False):
        """read_session/read_session_since 가 공유하는 파서(화이트리스트·guard
        로직을 두 벌로 두지 않는다, invariant 4). `start` 뒤부터 읽는다 —
        0 이면 처음부터. `start` 가 레코드 중간이면(직전 바이트가 개행이
        아니면) 그 줄의 나머지를 건너뛰고 다음 개행부터 시작한다. 절대
        던지지 않는다 — 파일이 없거나 깨졌으면 빈 이벤트로 열화한다.

        반환값 네 번째 자리는 `end_offset` — **마지막으로 완전히 읽은 줄
        바로 뒤**의 바이트 오프셋이다(리뷰: os.stat 의 크기를 그대로 baseline
        으로 쓰면 그 크기가 레코드 중간일 수 있어, 나중에 그 레코드가 마저
        쓰인 뒤 거기서부터 읽으면 skip-to-newline 로직이 그 레코드 전체를
        건너뛴다 — 호출자는 이 값을 다음 baseline 으로 써야 한다). 개행으로
        끝나지 않는 **마지막** 줄(`for raw in fh` 가 EOF 에서 미완성 레코드를
        그대로 넘길 수 있다 — 마침 그 순간 stat 해 읽은 경우)은 여전히
        파싱은 하지만(read_session 의 events/unparsed 출력은 그대로 유지한다)
        `end_offset` 에는 포함하지 않는다 — 그 레코드가 마저 쓰인 뒤에도
        다음 읽기가 그 줄 전체를 다시 볼 수 있어야 한다.
        `max_bytes` 를 넘기면 그 캡을 넘는 줄은 아예 읽지 않고 멈춘다(캡
        직전의 완전한 줄에서 자연히 정렬된다). `stop_at_human_turn` 이면
        사람의 said 이벤트를 만든 즉시 멈춘다.
        """
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
                        # start 가 레코드 중간이다 — 그 줄의 나머지를 버린다.
                        skipped = fh.readline()
                        offset = start + len(skipped)
                except OSError:
                    return events, 0, {"seek_failed": 1}, start
            since_start = offset
            end_offset = offset
            for raw in fh:
                if max_bytes is not None and (offset - since_start) >= max_bytes:
                    # 캡을 넘는 줄은 아예 안 읽는다 — offset 은 그 직전 완전한
                    # 줄 끝에 멈춰 있으므로 end_offset 이 저절로 줄 경계다.
                    bump("max_bytes_cap")
                    break
                start = offset
                offset += len(raw)
                if raw.endswith(b"\n"):
                    # 개행으로 끝난 줄만 "안전하게 다 읽었다" — 위 docstring.
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
                    if stop_at_human_turn and author == "human" and not raw.endswith(b"\n"):
                        # 리뷰(3차) #2: 사람 턴인데 아직 개행이 안 붙었다(쓰는
                        # 도중 stat 했을 수 있다) — 이 레코드를 트리거로 세지
                        # 않는다(이벤트조차 만들지 않는다). 세면 개행이 마저
                        # 붙은 뒤 다음 라운드가 baseline 을 이 레코드 앞에 둔
                        # 채로 같은 턴을 또 찾아 재전달을 두 번 하게 된다 —
                        # `stop_at_human_turn` 이 아닌 호출(read_session 포함)
                        # 은 이 분기를 타지 않으므로 출력이 그대로다.
                        continue
                    seq += 1
                    events.append(Event(
                        seq=seq, epoch=epoch, author=author, verb="said", ok=True,
                        text=text, arg="", paths=(), offset=start, length=len(raw),
                    ))
                    if stop_at_human_turn and author == "human":
                        # 호출자는 "사람의 새 턴이 있는가"만 물었다 — 찾은
                        # 즉시 멈춘다 — end_offset 은 이미 이 줄이 개행으로
                        # 끝났으면 그 뒤로 넘어가 있다(위에서 갱신).
                        return events, unparsed, dropped, end_offset
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

        return events, unparsed, dropped, end_offset

    def classify(self, source_path: str) -> bool:
        """Codex rollout 에 사람이 시작한 세션인가.

        False 는 서브에이전트·헤드리스 exec 라고 **확실할 때만** 낸다
        (`_is_interactive`). session_meta 를 못 읽으면(빈 파일, 모르는 첫 줄)
        판단할 수 없으므로 True 다 — brief 는 False 인 행만 건너뛰므로, 여기서
        False 를 내면 포맷이 바뀐 날부터 새 rollout 이 전부 건너뛰어지고 그 전의
        낡은 세션이 나간다(#21). 여는 판정은 ref_for_path 가 따로 한다.
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
                "codex-cli 0.155.1 실측: 손으로 놓인 hooks.json 은 기본적으로 신뢰되지 "
                "않는다 — Codex 자체의 훅 신뢰 절차로 한 번 승인해야 실제로 돈다."
            ),
        )

    def _project_trust_level(self, repo_root: str) -> Optional[str]:
        """`~/.codex/config.toml` 의 `[projects."<repo_root>"]` 아래
        `trust_level` 값 (#32). 전체 TOML 을 파싱하지 않는다 — 공유 스캐너
        (`hookconf.toml_header_lines`, 문자열·주석 인식)로 최상위 헤더를 모두
        찾은 뒤, 이 레포 경로와 정확히 같은 헤더 하나를 골라 그 본문(다음
        헤더 전까지)에서 `trust_level` 키만 정규식으로 읽는다 — 리뷰: 예전
        엔 본문 끝을 `^[ \\t]*\\[` 로 다시 찾았는데, 이건 #31 이 이미 걸러낸
        "여러 줄 배열 값 안의 `[` 도 줄 맨 앞에 올 수 있다" 문제를 그대로
        반복한다. 못 찾으면(파일 없음, 이 레포의 헤더가 아예 없음, 못 읽음)
        None — "신뢰 여부를 모른다" 이지 "신뢰 안 됨" 이 아니다(호출자가
        project 레이어를 아예 무시하는 근거가 된다).
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
        """omhc brief 를 실제로 부를 수 있는 네 위치 각각의 (path, 거기에
        세션 시작 시점에 도는 omhc 호출이 있는가) — 공식 문서(config-advanced,
        "Hooks" 절, #32): `~/.codex/hooks.json`, `~/.codex/config.toml` 의
        인라인 `[hooks]`, `<repo>/.codex/hooks.json`, `<repo>/.codex/config.toml`
        (project 쪽은 그 `.codex/` 레이어가 trusted 일 때만). `hook_is_installed`
        와 `_install_source`(#32 리뷰 2 — mtime 판정이 hooks.json 하나만
        보던 결함) 가 이 목록 하나를 공유한다."""
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
        """omhc 를 부르는 SessionStart 훅이 설치돼 있는가.

        pull 채널(state 산출물)은 훅이 그것을 읽어갈 때만 전달이 성립한다.
        훅이 없으면 산출물을 써도 아무도 보지 않으므로 전달이 아니다 — 그것을
        성공으로 보고하면 Path B 가 영원히 발동하지 않는다. `repo_root` 가
        주어지고 그 레포가 trusted 로 확인될 때만 project 레이어까지 본다 —
        신뢰를 확인 못 하면 project 레이어는 보지 않는다(과다 신뢰보다
        무시가 안전하다).
        """
        try:
            return any(present for _path, present in self._hook_layers(repo_root))
        except Exception:
            return False

    def _install_source(self, repo_root: Optional[str]):
        """훅이 실제로 설치된 파일들(`_hook_layers` 가 present=True 로 표시한
        것들) 중 mtime 이 가장 최근인 (epoch, path) — `_hook_health` 가
        "이 시각 이후 세션이 돌았는가" 를 재는 기준점이다(#32 리뷰 1).

        예전엔 이 기준점을 언제나 `hooks_path()` 의 mtime으로 삼았다 —
        인라인 전용 설치(hooks.json 자체가 없는 경우)에서는 그 stat 이
        ENOENT 로 죽어 `_hook_health` 가 매번 `----(unknown)` 으로만
        남고, 정작 신뢰 안 된 인라인 훅이 조용히 스킵되는 걸 잡아야 할
        행이 그 역할을 못 했다.

        둘 다 설치돼 있으면 둘 중 더 최근에 바뀐 쪽을 쓴다 — 어느 쪽이든
        최근에 손을 댔다는 신호이기 때문이다. Codex 자신도 config.toml 을
        자주 고쳐 쓴다(hooks.state 신뢰 해시, `[projects...]` trust_level
        등) — 그러면 이 mtime 이 실제 훅 설치 시점보다 훨씬 뒤로 밀려
        판정 창(install_epoch 이후)이 좁아지고, `----`(미판정)로 남는
        경우가 늘어난다. 그건 보수적인 실패 방향이라 안전하다(FAIL 을
        놓치는 대신 그냥 못 판정한 것으로 남는다) — 그래서 굳이 "이 mtime
        변화가 진짜 훅 재설치였는지" 를 더 정교하게 가려내지 않는다.
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
        """config.toml 의 인라인 `[hooks]` 에 (배포 조각과 정확히 같지 않아도)
        세션 시작 시점에 실제로 도는 omhc brief 호출이 있는가 — "존재하는가"
        와 "배포 조각과 똑같은가" 는 다른 질문이다(리뷰 #1: 이 둘을 섞어
        쓰면 인라인 훅이 깨져 있어도(예: mark 가 빠짐) `hooks_status()` 가
        "not found" 라고 잘못 말하고, `omhc hooks install` 이 그 위에 다시
        hooks.json 을 겹쳐 써 Codex 가 두 층 다 로드하며 경고하는 상태를
        만든다). `omhc hooks install` 이 hooks.json 에 중복을 쓸지 판단하는
        데 이 함수 하나만 쓴다 — "존재" 판정은 여기, "제대로 됐는가" 판정은
        `hooks_status()`."""
        return hookconf.has_runnable_call_toml(
            self.toml_config_path(), "brief", {"--harness": self.adapter_id})

    def hooks_status(self):
        """`omhc status` 의 `codex-cli hooks` 행 전용 — 일반
        `hookconf.inspect` 는 hooks.json 하나만 본다. Codex 는 그 옆에 인라인
        `config.toml [hooks]` 도 읽으므로(#32) 여기서 둘 다 본다.

        "설치돼 있다" 고 부를 층은 존재 여부(`has_runnable_call*`, 세션 시작
        시점에 실제로 도는 omhc brief 호출이 있는가)로 가른다 — 기존 결함
        (리뷰 #1)은 이걸 `inspect`/`inspect_toml` 의 "배포 조각과 완전히
        같은가" 판정과 섞어 썼다: 인라인이 있는데 mark 가 빠졌거나 플래그가
        달라 "differs" 여야 할 상황을 "not found" 라고 잘못 말했다. 존재하는
        층에 대해서만 `inspect`/`inspect_toml` 로 "제대로 됐는가" 를 덧붙인다.

        두 층 다 존재하면 문서(config-advanced, Hooks 절)가 "Codex 는 둘 다
        로드하고 경고한다"고 명시한다 — 둘 다 정확하면 PASS 대신 미판정
        (`----`)으로 그 경고를 드러내고, 하나라도 깨져 있으면 FAIL 로 어느
        쪽인지 짚는다. project 레이어(`<repo>/.codex/…`)는 이 행이 안 본다 —
        이 행은 특정 레포 문맥이 없는 범용 상태 행이고, project 훅은
        `hook_is_installed(repo_root)` 가 `install_handoff` 시점에 따로 본다.
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
            # hookconf 의 일반 detail 은 "... — run `omhc hooks install`" 로
            # 끝난다 — hooks.json 대상이면 맞는 조언이지만, 인라인이 이미
            # 존재하면(리뷰 #1 이후) `omhc hooks install` 은 그 위에
            # hooks.json 을 덧쓰지 않고 그냥 실패한다 — 이 조언을 따라가면
            # 제자리다(리뷰 #2). 실제로 맞는 조언으로 바꾼다.
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
        """`hookconf.inspect_toml` 의 일반적인 "... — run `omhc hooks install`"
        조언을, 인라인이 이미 존재하는 상태에서 실제로 맞는 조언으로 바꾼다
        (#32 리뷰 2 — 그 명령은 인라인이 있으면 hooks.json 을 덧쓰지 않고
        그냥 실패하므로, 그 조언을 따르면 같은 자리를 맴돈다)."""
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
        """Path A(신선한 훅 핸드오프) 가 성공했는데 그 옆에 예전 Path B 구간
        (#33 예산 초과나 훅이 한동안 안 돌던 시기에 깔린 것)이 남아 있으면,
        다음 Codex 세션이 신선한 훅 핸드오프와 낡은 AGENTS.md 지시를 동시에
        읽는다(#36) — collapse() 자체는 24시간 지나야 지우므로 여기서
        force=True 로 즉시 지운다. `#33` 거절 경로와 똑같은 가드를 쓴다:
        AGENTS.md 가 Claude Code 와 공유되면 절대 건드리지 않는다. 이 메서드는
        훅 경로(brief → deliver → install_handoff)에서 불리므로 무엇을 하든
        절대 던지지 않는다(invariant 2)."""
        from .. import agents_md

        try:
            if not agents_md.shared_with_claude(repo_root):
                agents_md.collapse(repo_root, force=True)
        except Exception:
            pass

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
        return (self._install_agents_md,)

    def _install_agents_md(self, bundle: HandoffBundle) -> InstallReceipt:
        """`agents_md.install` 을 부르기 전에 #33 예산을 미리 잰다.

        구간은 이제 항상 파일 맨 앞이라(managed_block.splice) 끝나는 지점은
        기존 AGENTS.md 크기와 무관하게 `len(prefix+block)` 하나로 정해진다.
        조상 디렉터리의 AGENTS.md 바이트까지 더하는 건 하지 않는다 — Codex
        프로젝트 루트는 이 레포의 `repo_root` 로 근사하고(개인용 도구, 서브
        디렉터리에서 시작하는 사용이 드물다), 조상에 큰 AGENTS.md 가 있는
        경우까지 재는 건 이 파일 하나만 보는 것보다 훨씬 비싸다(각 조상마다
        파일을 열어야 한다) — 넘으면 install 을 아예 claim 하지 않고
        deliver() 가 보편 바닥(outbox)으로 떨어지게 한다(invariant 2: 여기서
        죽지 않는다, deliver() 의 예외 캐치가 이미 있지만 사유를 guard.log 에도
        남긴다).

        `project_doc_max_bytes = 0` 은 (codex-rs project_doc.rs 실측 근거로,
        미확인) Codex 가 AGENTS.md 자체를 아예 안 읽는다는 뜻이라 항상 거절한다
        — 그 외 0 미만/파싱 불가는 `_project_doc_max_bytes` 가 이미 기본값으로
        접는다.

        리뷰 결함: 예산 초과로 거절만 하고 이미 설치된(더 작았던 시절의) 낡은
        구간을 그대로 두면, Codex 는 outbox 로 떨어진 새 핸드오프 대신 그
        낡은 구간을 계속 읽는다 — 거절할 때 낡은 구간도 지운다. AGENTS.md 가
        Claude 와 공유되면(agents_md.install 과 같은 가드) 건드리지 않는다."""
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
        """`~/.codex/config.toml` 에 `hook_file_path`(보통 hooks.json — 실측상
        인라인은 아래 참고)의 훅을 신뢰한다는 `hooks.state` 항목이 있는가.
        이건 `[projects."<repo>"] trust_level` 과는 다른 메커니즘이다 —
        저건 project 쪽 `.codex/` 레이어(hooks.json 이든 인라인이든) 전체를
        읽을지 말지를 가르고, 이건 사용자 레벨 hooks.json **내용 하나**를
        Codex 가 이미 승인했다는 해시다(실측, 이 머신: `hooks.state."<hooks.json
        절대경로>:session_start:<idx>:<idx>"`). 3.9 엔 tomllib 이 없으므로
        평문 부분 문자열 검색으로 충분하다 — 해시 의미론(codex 가 훅 내용의
        무엇을 해시하는지)은 확인된 바 없으므로, 이 신호 하나만으로 실패
        판정을 내리지 않는다(정적 힌트일 뿐이다).

        인라인(`~/.codex/config.toml` 자신에 적힌 `[[hooks.SessionStart]]`)도
        같은 `hooks.state."<config.toml 경로>:session_start:..."` 키 형식을
        쓰는지는 확인된 바 없다 — 호출부(`_hook_health`)가 그 불확실성을
        detail 에 명시한다."""
        config_path = self.toml_config_path()
        needle = 'hooks.state."{}:session_start:'.format(hook_file_path)
        try:
            with open(config_path, encoding="utf-8", errors="replace") as fh:
                return needle in fh.read()
        except OSError:
            return False

    def _config_trusts_hook(self) -> bool:
        """하위호환 별칭 — hooks.json 자신의 신뢰 해시만 본다(실측된 경로)."""
        return self._config_trusts(self.hooks_path())

    def _repo_matches(self, repo_root: Optional[str], cwd) -> bool:
        """이 후보(cwd)가 `repo_root` 에 실제로 속하는가.

        `repo_root` 자신이 `.git` 을 가진 진짜 레포면 그 안의 워크트리·서브모듈
        (자기 `.git` 을 가진 중첩 디렉터리, 예: `.claude/worktrees/*`)도 여전히
        이 레포에 속한 것으로 본다 — list_sessions 의 is_within(경로 포함)
        판정을 그대로 신뢰한다(TestHealthMatchesAcrossNestedGitRoots).
        `repo_root` 자신은 `.git` 이 없는데(resolve_repo_root 의 fallback,
        "non-git 부모 디렉터리"에서 status 를 부른 경우) 안에 자기 `.git` 을
        가진 **남남** 레포가 있으면 이야기가 다르다 — 그 세션은 여기 레포의
        훅이 돈 증거가 아니다. 그 경우에만 repo 키를 엄격히 대조한다.
        """
        if repo_root is None:
            return True
        if os.path.exists(os.path.join(repo_root, ".git")):
            return True
        return locate.owning_repo_key(cwd) == locate.owning_repo_key(repo_root)

    def _codex_root_markers(self, config_path: str):
        """`~/.codex/config.toml` 의 `project_root_markers` 값을 읽는다.

        3.9 엔 tomllib 이 없다 — TOML 전체를 파싱하는 대신 이 키 하나만 정규식으로
        뽑는다(한 줄/여러 줄 배열, bare/quoted 키, basic/literal 문자열 모두). 반환은
        (state, markers-또는-에러문자열):
          "missing"  — 파일이 아예 없다(기본값 [".git"] 이 적용된다)
          "unreadable" — 있는데 못 읽는다(권한, 디렉터리 등) — 두 번째 자리에 사유
          "absent"   — 최상위(첫 `[section]` 이전)에 이 키가 없다(마찬가지로 기본값)
          "table"    — 이 키가 `[section]` 아래에서만 보인다(TOML 은 테이블이
                       스코프를 바꾼다 — 최상위 키가 아니다)
          "unparseable" — 배열 안에 문자열이 아닌 것(정수, 중첩 표현 …)이 있다
          "ok"       — 두 번째 자리에 값 tuple
        `utf-8-sig` 로 여는 것은 BOM 이 있는 파일에서 정규식이 줄 시작(`^`)을 못
        맞춰 키가 없는 것처럼 보이는 걸 막는다.
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
        # 값 안에 두 번째 `[` 가 있으면 중첩 배열이다 — Codex 가 기대하는 문자열
        # 배열이 아니므로 확신 있게 못 읽은 것으로 본다(리뷰).
        if _TOML_STRING_RE.sub("", array_text).count("[") > 1:
            return "unparseable", None
        # findall 은 매칭 안 된 그룹을 빈 문자열로 채운다(None 아님) — 두 문자열
        # 종류를 구분하려면 finditer 로 그룹 참여 여부(None)를 직접 봐야 한다.
        values = [sm.group("d") if sm.group("d") is not None else sm.group("s")
                  for sm in _TOML_STRING_RE.finditer(array_text)]
        # 문자열 리터럴을 전부 지우고 남는 게 있으면(정수, 중첩 배열, 주석 …)
        # 이 배열을 확신 있게 못 읽은 것이다.
        leftover = _TOML_STRING_RE.sub("", array_text)
        leftover = re.sub(r"[\[\],\s]", "", leftover)
        if leftover:
            return "unparseable", None
        return "ok", tuple(v.replace('\\"', '"') for v in values)

    def _project_doc_max_bytes(self, config_path: str) -> int:
        """`~/.codex/config.toml` 의 `project_doc_max_bytes` 값, 없거나 못 읽으면
        임베디드 기본값(#33). `_codex_root_markers` 와 같은 원칙 — 최상위(첫
        `[section]` 이전) 키만 본다, 절대 던지지 않는다(fail-open).

        `0` 은 그대로 돌려준다(기본값으로 접지 않는다) — codex-rs 의
        project_doc.rs 실측 근거(리뷰, 바이너리 문자열로는 미확인)로 `0` 은
        "AGENTS.md 를 아예 안 읽는다"는 뜻이고, 호출자(`_install_agents_md`/
        `_agents_md_budget_health`)가 그 값 자체로 따로 판정해야 한다. 음수·
        파싱 불가만 기본값으로 접는다."""
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
        """리뷰 #4: `repo_root` 위 조상 중 `.git` 을 가진 것이 있으면, Codex
        기본값(`[".git"]`)으로도 그 조상을 루트로 잡아 AGENTS.md 를 cwd 까지
        내려오며 읽는다(레포 자신의 AGENTS.md 도 그 경로 위에 있다) — 이땐
        `.omhc-root` 를 더할 필요가 없다."""
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
        """#31: `.omhc-root` 로만 정해진(= `.git` 없는, 위에도 `.git` 조상이 없는)
        프로젝트에서, 서브폴더에서 시작한 Codex 가 기본
        `project_root_markers = [".git"]` 로는 조상 AGENTS.md 를 읽지 않는다
        (실측, codex-cli 0.155.1) — Path B(AGENTS.md managed block)가 조용히
        무력해진다. `~/.codex/config.toml` 에 `.omhc-root` 를 더해야 통한다.

        게이팅하지 않는다(리뷰 결함): Path B 는 `install_handoff` 가 실패할
        때만 열리는 폴백이라, omhc 훅이 설치·신뢰돼 있으면(보통의 경우) 이
        설정은 아무 효과가 없는데도 FAIL 로 게이팅하면 정상 설치를 매번
        FAIL 로 만든다. 대신 PASS 아니면 언제나 `----`(ok=None)이고, 훅이
        설치돼 있지 않을 때만 "지금 유일한 채널"이라고 명시한다.
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
        """#33: 이미 설치된 구간이 Codex 의 `project_doc_max_bytes` 예산을 넘겨
        끝나면 Codex 는 그걸 못 읽는다 — `_install_agents_md` 가 쓰는 시점에
        막지만(다음 splice 부터), 이미 예산을 넘겨 설치된 채로 오래 방치된
        구간은 status 로도 알려야 한다.

        `codex root markers` 와 같은 두 단계 원칙을 따른다: 이 진단 자체가
        무의미한 레포(AGENTS.md 가 Claude 와 공유돼 Path B 를 절대 안 쓰는
        레포 — `agents_md.install`/`_install_agents_md` 와 같은 가드)는 행을
        아예 내지 않는다(``None``). 진단은 유효한데 아직 판단할 근거가 없으면
        (설치된 구간이 없다 — 설치 전이거나 collapse 됨) `----` 로 행은 내되
        게이팅은 하지 않는다 — "every check gets PASS/FAIL/---- (never
        SKIP)"(README) 는 판정 가능한 진단에 적용되지, 진단 자체가 무의미한
        레포에는 적용되지 않는다."""
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
        except Exception as exc:  # 이 진단도 status 자체를 죽이면 안 된다
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
        """훅이 설치돼 있는데 실제로 돈 적이 없는지 행태로 진단한다.

        정적 신호(hooks.json 존재)만으로는 신뢰 여부를 알 수 없다 — codex-cli
        0.155.1 은 신뢰되지 않은 훅을 메시지도 원장 행도 없이 건너뛴다. 그래서
        설치 이후 Codex 세션이 실제로 omhc mark 를 남겼는지를 원장과 대조한다.
        무엇이 잘못돼도 status 자체가 죽으면 안 되므로(진단 도구), 통째로
        감싸 실패는 ok=None(미판정)으로 열화시킨다.

        ok=None(`----`, AGENTS.md 의 status 규약)은 "아직 아무것도 판정할 수
        없다"는 세 번째 상태다 — 대화형 Codex 세션이 install 이후 하나도 없거나
        헤드리스(`codex exec`)뿐이면 PASS 도 FAIL 도 아니다.
        """
        try:
            if not self.hook_is_installed(repo_root):
                # 훅을 설치한 적 없는 사용자에게 매번 행을 보여주는 건 소음이다
                # — "설치 안 됨" 은 이제 `<adapter-id> hooks` 행(hookconf 기반,
                # cmd_status)이 이미 말해준다.
                return ()
            source = self._install_source(repo_root)
            if source is None:
                # hook_is_installed(repo_root) 는 True 라고 했는데 그 파일(들)을
                # 다시 stat 하지 못했다 — 방금 지워졌거나 하는 경합. 판정 불가.
                return (("codex hook", None,
                         "unknown (installed hook file could not be stat'd)"),)
            install_epoch, install_path = source
            install_date = time.strftime("%Y-%m-%d %H:%M %z", time.localtime(install_epoch))

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
                if row.get("event") != "start":
                    # "훅이 돌았다" 는 세션 시작 행의 존재로만 증명된다 — 계획된
                    # pull 행 등 다른 event 는 훅이 돌았다는 증거가 아니다.
                    continue
                sid = row.get("session")
                if sid:
                    ran_sessions.add(sid)

            def _post_install(entries):
                """entries: (session_id, cwd, meta) 튜플들. repo 소속과 설치
                이후 시작 시각으로 걸러 (started, session_id, meta) 를 만든다."""
                out = []
                for session_id, cwd, meta in entries:
                    if not self._repo_matches(repo_root, cwd):
                        continue
                    # 파일 두 개(hooks.json mtime, rollout 의
                    # session_meta.timestamp)의 시각을 비교한다 — 한쪽이 mtime
                    # 이라 invariant 6 이 금지하는 "순서의 근거"가 아니라
                    # 일회성 진단이라 허용한다(이 비교 결과로 이벤트를 정렬하지
                    # 않는다). cli._backfill_foreign_sessions 가 하는 비교와는
                    # 다르다 — 거기는 두 세션 시작 epoch(둘 다
                    # session_meta.timestamp 계열, mtime 아님)를 비교해 원장
                    # append 순서를 정하는, invariant 6 이 허용하는 예외다.
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
                # OMHC_ALLOW_HEADLESS=1 이면 list_sessions() 자체가 헤드리스도
                # 후보에 넣으므로 여기 온 시점엔 이미 진짜로 아무것도 없다 —
                # 이 헤드리스 전용 재스캔은 env 와 무관하게 존재 여부만 본다.
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

            # 신뢰는 config.toml 을 바꾸지, hooks.json 을 바꾸지 않는다 — 신뢰
            # 이전 세션은 install_epoch 이후라도 영원히 "안 돈 것"으로 남는다.
            # 그래서 전체 개수가 아니라 **가장 최신** 세션의 행태로 판정한다:
            # 최신이 돌았으면 그 시점부터는 신뢰가 성립한 것이므로 PASS, 아니면
            # 최신에서부터 거슬러 연속으로 안 돈 세션 수를 센다.
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

            # UNVERIFIED: Codex Desktop/IDE 세션(originator 예: codex_work_desktop)이
            # 이 훅을 애초에 전혀 돌리지 않을 수 있다 — 확인된 바 없다. 오진단이
            # 눈에 보이도록 가장 최신 미실행 세션의 originator 를 detail 에 남긴다.
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
            # else: project-level 설치 — 그 신뢰는 `[projects...] trust_level`
            # 로 이미 확인됐다(여기 오려면 그게 trusted 여야 한다, _hook_layers)
            # — hooks.state 힌트는 이 층과 무관하니 덧붙이지 않는다.
            # 훅 백필(cmd_mark) 덕에 Codex→Claude 방향은 이 FAIL 과 무관하게
            # 산다 — 끊긴 건 Claude→Codex 뿐이라는 걸 명시한다.
            detail += (" — Claude→Codex is not delivered; Codex→Claude still works "
                       "via Claude's mark backfill")
            return (("codex hook", False, detail),)
        except Exception as exc:  # 진단이 status 자체를 죽이면 안 된다 (invariant 7)
            return (("codex hook", None, "unknown ({})".format(exc)),)
