from __future__ import annotations

import glob
import json
import os
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
from . import _register, install_state_artifact, iso_epoch

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

# --- 실측 (codex-cli 0.156.1, 2026-09-23) -----------------------------------
# 셸 한 번은 레코드 두 개를 남긴다:
#   response_item/custom_tool_call name="exec"  ← 모델이 쓴 JS 코드. 명령도
#       종료 코드도 없고 출력은 성공·실패 모두 "Script completed" 로 시작한다.
#   event_msg/item_completed item.type="CommandExecution"  ← argv, exit_code,
#       status, cwd(file:// URI), parsed_cmd(Codex 자신의 분류)
# 편집은 item.type="FileChange", changes={절대경로: {type, unified_diff}}.
# 그래서 사실은 item 에서 읽고 JS 래퍼는 부기로 센다(둘 다 세면 이중 계상).
_JS_WRAPPER_TOOL = "exec"
# parsed_cmd 가 전부 이 종류면 읽기다. Codex 에는 읽기 전용 도구가 따로 없어서
# 이걸 안 쓰면 Codex 세션에 inspected 가 영영 없다.
_INSPECT_KINDS = frozenset({"read", "list_files", "search"})
_SHELL_FLAGS = frozenset({"-c", "-lc"})

# --- UNVERIFIED (이전/다른 버전) --------------------------------------------
# 0.156.1 은 function_call 을 쓰지 않는다. 아래는 Rust serde 필드명 기준 추정이고
# 실물로 확인된 적이 없다. 모르는 모양은 예외 대신 unparsed 로 계상된다.
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


def _arg_and_paths(payload: dict) -> Tuple[str, Tuple[str, ...]]:
    """UNVERIFIED: function_call.arguments 는 JSON 문자열로 온다고 가정한다."""
    raw = payload.get("arguments")
    parsed = None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return raw.strip()[:ARG_LIMIT], ()
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
    return guard.redact_b64(arg)[:ARG_LIMIT], paths


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
        return verb, ok, guard.redact_b64(arg)[:ARG_LIMIT], paths
    if kind == "FileChange":
        changes = item.get("changes")
        paths = tuple(str(k) for k in changes) if isinstance(changes, dict) else ()
        return "modified", ok, (paths[0] if paths else "")[:ARG_LIMIT], paths
    return None


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
                            events[idx] = events[idx]._replace(ok=False)
                    bump("tool_output")
                    continue

                if kind == "reasoning":
                    bump("reasoning")
                    continue

                # response_item 인데 모양을 모른다 → 조용히 버리지 않는다.
                unparsed += 1

        return SessionRead(ref=ref, events=tuple(events), unparsed=unparsed,
                           dropped=dropped)

    def classify(self, source_path: str) -> bool:
        """Codex rollout 에는 비대화형 표식이 없다.

        session_meta 를 읽을 수 있으면 사람이 시작한 세션으로 본다. Claude 의
        entrypoint 어휘를 여기서 찾는 것은 무의미하고(필드가 없다) 실제로 그렇게
        하면 필터가 조용히 no-op 가 된다.
        """
        return session_meta(source_path) is not None

    def ref_for_path(self, source_path: str, session_id: str,
                     cwd: Optional[str] = None) -> Optional[SessionRef]:
        meta = session_meta(source_path)
        if meta is None:
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
