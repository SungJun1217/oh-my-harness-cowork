from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import List, Optional

from . import (
    adapters, agents_md, brief, due, gate, hookconf, index, ledger, locate,
    managed_block, pin, watch,
)
from .adapter import AdapterUnavailable

PROG = "omhc"
NOTES_NAME = "notes.txt"
ARTIFACT_NAME = "omhc.txt"


def _stdin_text() -> str:
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        return sys.stdin.read()
    except Exception:
        return ""


def _state_for(home: Optional[str], start: Optional[str] = None):
    root = locate.resolve_repo_root(start)
    key = locate.repo_key(root)
    return root, key, locate.state_dir(key, home=home)


# --- mark -------------------------------------------------------------------


# 신뢰되지 않은 Codex 훅은 mark 를 조용히 건너뛴다(실측, codex_cli.py 참고).
# 그러면 그 Codex 세션은 원장에 영영 없고, due() 는 원장만 읽으므로 Claude
# 쪽 훅이 멀쩡히 돌아도 Codex→Claude 가 죽는다. 그래서 **Claude 의** mark 가
# 얹혀서 다른 하네스(Codex)의 세션을 원장에 채운다 — due() 자체는 안 바뀐다.
BACKFILL_CAP = 5
# 훅 예산(150ms) 의 일부만 쓴다. 비싼 부분은 discover() 자체(Codex 는 날짜
# 디렉터리 스캔)이므로 이 deadline 을 discover() 에도 그대로 넘겨 어댑터가
# 스스로 스캔을 끊게 한다 — 여기서만 재고 있으면 discover() 호출 자체가
# 늦게 끝나 이 mark 호출이 세션 시작을 지연시킬 수 있다.
BACKFILL_TIME_BUDGET = 0.08


def _ref_repo_key(ref) -> Optional[str]:
    """이 ref 가 실제로 속한 레포 키. `locate.owning_repo_key` 로 위임한다
    (원래 이 함수에 있던 로직 — codex_cli.health() 도 같은 필터를 쓴다)."""
    return locate.owning_repo_key(ref.cwd)


def _backfill_foreign_sessions(harness: str, root: str, key: str, state: str,
                                home, now: float) -> None:
    deadline = time.time() + BACKFILL_TIME_BUDGET
    rows = ledger.read(repo_key=key, home=home)
    for adapter_id in sorted(adapters.REGISTRY):
        if adapter_id == harness:
            continue
        if time.time() > deadline:
            break
        try:
            refs = list(adapters.get(adapter_id, home=home).discover(
                root, deadline=deadline))
        except Exception:
            continue
        if not refs:
            continue
        own_rows = [r for r in rows if r.get("harness") == adapter_id]
        known_sessions = {str(r.get("session")) for r in own_rows if r.get("session")}
        newest_start = 0.0
        for r in own_rows:
            if r.get("event") == "start":
                newest_start = max(newest_start, float(r.get("epoch") or 0.0))

        # 먼저 자격 있는 것만 걸러 **전체를 놓고** 정렬한다 — 오래된 것부터
        # 자르면(리뷰 결함) 8개 중 5개가 죄다 옛것이 되어 due() 가 최신 대신
        # 4번째로 최신인 세션을 돌려준다. 최신 N개를 골라야 한다.
        eligible = []
        for ref in refs:
            if not ref.session_id or ref.session_id in known_sessions:
                continue
            if ref.epoch <= newest_start:
                continue
            if now and (now - ref.epoch) > due.MAX_AGE_SECONDS:
                continue
            if _ref_repo_key(ref) != key:
                continue
            eligible.append(ref)
        eligible.sort(key=lambda r: r.epoch)
        selected = eligible[-BACKFILL_CAP:]

        # 고른 뒤에는 **오름차순으로 붙인다** — 원장의 append 순서가 시작
        # 순서와 일치해야 due() 가(원장을 거꾸로 읽어 "가장 최근"을 고른다)
        # 진짜 최신 세션을 돌려준다. 이미 최신 N개로 골랐으므로 여기서부터는
        # 시간 예산으로 중간에 끊지 않는다 — 끊으면 방금 고른 최신 세션이
        # 아니라 그보다 오래된 것만 남을 수 있다(리뷰 결함). 어차피 최대
        # BACKFILL_CAP 줄만 쓰므로 비용은 무시할 만하다.
        for ref in selected:
            row = {
                "repo": key,
                "harness": adapter_id,
                "session": ref.session_id,
                "event": "start",
                "epoch": ref.epoch,
                "path": ref.source_path,
                "cwd": root,
                # health() 가 이 값을 보고 "훅이 실제로 돌았다" 는 증거에서 뺀다
                # (codex_cli.py) — 백필이 신뢰 없는 훅을 가려버리면 안 된다.
                "via": "scan",
            }
            ledger.append(row, home=home)


def cmd_mark(args, *, home=None, out=sys.stdout) -> int:
    """세션 시작을 원장에 남긴다. 훅이 부른다. 약 220바이트 한 줄."""
    raw = args.stdin if args.stdin is not None else _stdin_text()
    payload = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except ValueError:
            payload = {}
    start = str(payload.get("cwd") or "") or None
    root, key, state = _state_for(home, start)
    if locate.refused_root(root):
        # 훅 경로다 — 원장에 아무것도 남기지 않고 조용히 나간다(invariant 2).
        return 0
    session = gate.session_id_from_hook_payload(raw) or ""
    row = {
        "repo": key,
        "harness": args.harness,
        "session": session,
        "event": args.event,
        "epoch": round(time.time(), 0),
        "path": str(payload.get("transcript_path") or ""),
        "cwd": root,
    }
    # 사람이 대화한 세션인지 **여기서** 판정해 기록한다. 훅 stdin 페이로드에는
    # 그 정보가 없으므로 어댑터가 트랜스크립트를 보고 판단한다. 기록하지 않으면
    # due() 의 비대화형 차단이 프로덕션에서 죽은 코드가 된다 — 테스트만 그 필드를
    # 손으로 넣어 통과하고, 실제로는 남의 도구가 남긴 sdk 세션이 핸드오프된다.
    #
    # 판정은 하네스별 지식이므로 어댑터가 소유한다. 코어가 어휘를 들고 있으면
    # 새 하네스를 붙일 때 코어를 고쳐야 한다.
    if row["path"]:
        try:
            if not adapters.get(args.harness, home=home).classify(row["path"]):
                row["interactive"] = False
        except Exception:
            pass
    ledger.append(row, home=home)
    # 다른 하네스의 세션을 원장에 백필한다(위 주석). 훅 경로이므로 실패해도
    # mark 자체는 항상 exit 0, 빈 stdout 이어야 한다(invariant 2).
    try:
        if not due.is_off(state):
            _backfill_foreign_sessions(args.harness, root, key, state, home,
                                       row["epoch"])
    except Exception:
        pass
    # 어떤 omhc 호출에서든 오래된 AGENTS.md 구간을 붕괴시킨다.
    try:
        agents_md.collapse(root)
    except Exception:
        pass
    if args.verbose:
        out.write("marked {} {} in {}\n".format(args.harness, args.event, key))
    return 0


# --- note -------------------------------------------------------------------


def cmd_note(args, *, home=None, out=sys.stdout, err=sys.stderr) -> int:
    root, _key, state = _state_for(home)
    reason = locate.refused_root(root)
    if reason:
        err.write("{}\n".format(reason))
        return 2
    os.makedirs(state, exist_ok=True)
    path = os.path.join(state, NOTES_NAME)
    text = " ".join(args.text).strip()
    if not text:
        out.write("nothing to note\n")
        return 0
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text + "\n")
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = [line for line in fh if line.strip()]
    out.write("noted ({} notes, {}B)\n".format(len(lines), os.path.getsize(path)))
    return 0


# --- pull rate ---------------------------------------------------------------


def _record_pull(key: str, state: str, via: str, home, session: Optional[str] = None,
                 tag: Optional[str] = None) -> None:
    """`show`/`log` 로 산출물을 인출했다는 행을 원장에 남긴다. §9 인출률 회계.

    `harness` 키를 절대 넣지 않는다 — due() 는 event=="start" 만 보고,
    backfill 은 harness+start 로 own_rows 를 거르고, codex health() 도
    event=="start" 만 "훅이 돌았다"는 증거로 센다. 이 세 곳 중 어디에도
    pull 행이 섞여 들면 안 된다. 실패는 show/log 의 결과에 영향을 주면
    안 되므로 통째로 삼킨다(훅 경로는 아니지만 fail-open 을 유지한다).

    show 는 실제로 읽은 세션을 넘긴다. `#N` 은 옛 세션의 색인에 떨어질 수 있어
    "가장 최근 전달" 로 두면 보지 않은 세션의 인출률이 오른다. 대상이 하나로
    정해지지 않는 log 만 가장 최근 전달 세션(delivered.tsv 마지막 줄)에 돌린다."""
    try:
        session = session or due.last_delivered(state)
        if not session:
            return
        row = {"repo": key, "event": "pull", "via": via, "session": session,
               "epoch": round(time.time(), 0)}
        if tag:
            row["tag"] = tag
        ledger.append(row, home=home)
    except Exception:
        pass


# --- log --------------------------------------------------------------------


def _index_files(state: str) -> List[str]:
    directory = os.path.join(state, "index")
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [os.path.join(directory, n) for n in names if n.endswith(".idx")]


def _all_session_ids(state: str) -> List[str]:
    return [os.path.basename(p)[: -len(".idx")] for p in _index_files(state)]


def _unique_prefix_len(ids: List[str], minlen: int = 8) -> int:
    """이 id 들을 서로 구분하는 가장 짧은 접두 길이(>=minlen). Codex 의 UUIDv7 은
    앞 8자가 ~65초마다만 바뀌어 고정폭 8자 자르기로는 같은 레포에서 짧게 연달아
    시작한 세션들이 자주 충돌한다(#15c). 13자쯤이 보기 좋은 상한이지만 그건
    표시상 취향일 뿐이다 — 그 안에서 안 갈리면 유일해질 때까지 계속 늘린다.
    안 그러면 log 가 찍은 ref 를 show 가 "모호하다"며 거부하면서, 정작 후보
    목록에는 똑같은 문자열이 두 번 찍히는 리뷰 결함이 생긴다."""
    uniq = list(dict.fromkeys(ids))
    n = minlen
    longest = max((len(i) for i in uniq), default=minlen)
    while n < longest and len({i[:n] for i in uniq}) < len(uniq):
        n += 1
    return n


def cmd_log(args, *, home=None, out=sys.stdout) -> int:
    _root, key, state = _state_for(home)
    _record_pull(key, state, "log", home)
    rows = []
    for path in _index_files(state):
        session = os.path.basename(path)[: -len(".idx")]
        for row in index.rows(path):
            rows.append((session, row))
    rows.sort(key=lambda pair: (pair[1].epoch, pair[1].seq))

    if args.verb:
        rows = [r for r in rows if r[1].verb == args.verb]
    if args.grep:
        needle = args.grep.lower()
        rows = [r for r in rows if needle in r[1].arg.lower()]
    if args.file:
        rows = [r for r in rows if any(args.file in p for p in r[1].paths)]
    # falsy-zero 검사를 쓰면 `--last 0` 이 전부를 쏟는다 — F7 이 막으려던 바로 그
    # 무한 출력이다. 음수도 앞에서 자르는 엉뚱한 동작이 된다.
    if args.last is not None and args.last >= 0:
        rows = rows[len(rows) - args.last :] if args.last else []

    # 이 배치가 아니라 상태 디렉터리 전체에서 유일하게 만든다 — 안 그러면
    # 필터링으로 짧아진 접두사가 화면 밖의 다른 세션과 겹칠 수 있고, 그 ref 를
    # `show` 에 그대로 넘기면 모호해진다(#10).
    n = _unique_prefix_len(_all_session_ids(state)) if rows else 8

    for session, row in rows:
        ref = "{}#{}".format(session[:n], row.seq)
        content = row.arg or ",".join(row.paths)
        if not content and row.verb == "said":
            # index 는 본문을 담지 않는다(아카이브 이중화 방지) — 빈 줄 대신
            # 힌트를 보여준다(#15a).
            content = "(text: omhc show {})".format(ref)
        out.write(
            "{} {} {} {}\n".format(
                ref,
                row.verb,
                "ok" if row.ok else "FAIL",
                content,
            )
        )
    if not rows:
        out.write("no events (run a session in another harness first)\n")
    return 0


# --- show -------------------------------------------------------------------


def _pinned_path(state: str, session_id: str, fallback: str) -> str:
    pinned = os.path.join(pin.pinned_dir(state, session_id), "source.jsonl")
    return pinned if os.path.exists(pinned) else fallback


_SEQ_REF_RE = re.compile(r"^([^#]*)#(\d+)$")


def _default_log_session(state: str) -> Optional[str]:
    """pull 회계가 `log` 를 돌리는 세션 — 가장 최근 전달된 세션(due.last_delivered,
    §9). `log` 자체엔 "기본 세션" 이 없다(색인 전부를 나열한다); `#N` 만 받았을 때
    그 회계 규칙을 그대로 재사용해 하나로 좁힌다(#10)."""
    return due.last_delivered(state)


def _resolve_seq_ref(state: str, prefix: str, seq: int):
    """`#N` 또는 `<prefix>#N` 을 (entry, note) 로 푼다. note 는 자동으로 고른
    세션을 사람에게 알려줄 문구, 없으면 None. 못 풀면 (None, error message)."""
    ids = _all_session_ids(state)
    note = None
    if prefix:
        matches = [sid for sid in ids if sid.startswith(prefix)]
        if not matches:
            return None, "unknown session prefix {!r}".format(prefix)
        if len(matches) > 1:
            # 접두사로 줄이면 그 자체가 다시 모호해질 수 있다(리뷰 결함) — 후보는
            # 항상 전체 id 로 보여준다.
            candidates = ", ".join(sorted(matches))
            return None, "ambiguous session prefix {!r}; candidates: {}".format(
                prefix, candidates)
        session = matches[0]
    else:
        session = _default_log_session(state)
        if session is None:
            return None, ("no default session yet (nothing delivered here) — "
                          "use `<session-prefix>#{}` or `omhc log --last 30`".format(seq))
        if session not in ids:
            return None, ("most recently delivered session {} has no index yet — "
                          "use `<session-prefix>#{}` or `omhc log --last 30`".format(
                              session, seq))
        n = _unique_prefix_len(ids)
        note = "{} -> {}#{} (most recently delivered session)".format(
            "#{}".format(seq), session[:n], seq)

    path = os.path.join(state, "index", session + ".idx")
    row = index.find(path, seq)
    if row is None:
        return None, "no event #{} in session {}".format(seq, session)
    entry = {"session_id": session, "source_path": "",
             "offset": row.offset, "length": row.length, "seq": seq}
    return entry, note


def cmd_show(args, *, home=None, out=sys.stdout, err=sys.stderr) -> int:
    _root, key, state = _state_for(home)
    target = args.target.strip()
    refs = index.read_refs(state)

    entry = refs.get(target) or refs.get(target.upper())
    note = None
    if entry is None:
        m = _SEQ_REF_RE.match(target)
        if m:
            entry, err_or_note = _resolve_seq_ref(state, m.group(1), int(m.group(2)))
            if entry is None:
                out.write("{}\n".format(err_or_note))
                return 1
            note = err_or_note
    if entry is None:
        out.write("unknown reference {!r}; try `omhc log --last 30`\n".format(target))
        return 1

    if note:
        # stdout 은 원본 바이트 그대로여야 한다(`omhc show '#3' --full | jq .` 가
        # 깨지면 안 된다) — 자동으로 고른 세션을 알리는 메모는 stderr 로만 보낸다.
        err.write("# {}\n".format(note))

    source = _pinned_path(state, entry["session_id"], entry.get("source_path") or "")
    if not source or not os.path.exists(source):
        out.write("source bytes are gone for {} (session {})\n".format(
            target, entry["session_id"]))
        return 1
    with open(source, "rb") as fh:
        fh.seek(entry["offset"])
        raw = fh.read(entry["length"] if not args.full else -1)
    out.write(raw.decode("utf-8", "replace"))
    if not raw.endswith(b"\n"):
        out.write("\n")
    _record_pull(key, state, "show", home, session=entry["session_id"], tag=target)
    return 0


# --- status -----------------------------------------------------------------


# 세 값만 쓴다: True(PASS, 게이팅), False(FAIL, 게이팅), None(`----`, 게이팅
# 안 함). SKIP 이 아니다 — "아직 아무 일도 안 일어났다"를 실패로도 성공으로도
# 위장하지 않고 그대로 보여주려는 세 번째 라벨이다(#8).
def _verdict_word(verdict: Optional[bool]) -> str:
    if verdict is True:
        return "PASS"
    if verdict is False:
        return "FAIL"
    return "----"


def _check(out, label: str, verdict: Optional[bool], detail: str) -> None:
    out.write("{:<4} {:<22} {}\n".format(_verdict_word(verdict), label, detail))


def cmd_status(args, *, home=None, out=sys.stdout) -> int:
    root, key, state = _state_for(home)
    reason = locate.refused_root(root)
    if reason:
        # `/` 에서의 status 는 오사용이다 — SKIP 이 아니라 게이팅되는 FAIL 로
        # 보여준다. state 가 이미 있다면(예전에 잘못 돈 흔적) 고아라고 알린다.
        # 정상 경로의 나머지 진단(ledger.read, adapters.present, health, ...)은
        # 전부 `root` 에 걸려 있어 `/` 에서 의미가 없다 — text/json 이 여기서만
        # 갈라지는 최소한의 필드(refused, orphaned_state)를 덧붙인다.
        orphaned = state if os.path.isdir(state) else None
        if args.json:
            out.write(json.dumps({
                "repo_root": root, "repo_key": key, "state_dir": state,
                "refused": reason, "orphaned_state": orphaned,
                "rows": [{"label": "root", "verdict": "fail", "detail": reason}],
            }, ensure_ascii=False, indent=2) + "\n")
        else:
            _check(out, "root", False, reason)
            if orphaned:
                out.write("orphaned state dir: {}\n".format(orphaned))
        return 1
    installed = adapters.present(now=time.time)
    # repo_key= 를 쓴다 — read() 는 limit(기본 2000, 머신 전체 공유)보다 먼저
    # repo 필터를 적용하므로, 여러 레포를 오가는 사람에게서 이 레포의 행이
    # 슬라이스 밖으로 밀려나지 않는다(ledger.read 문서 참고).
    rows = ledger.read(home=home, repo_key=key)
    artifact = os.path.join(state, ARTIFACT_NAME)

    # watch.lag 가 정확히 이 계산을 소유한다. 두 벌로 두면 고정 레이아웃이
    # 바뀔 때 한쪽만 고쳐진다.
    lag_rows = watch.lag(state)

    # X = 최소 한 번 인출된 "전달받은 세션"의 수(중복 제거), N = 전달 횟수.
    # 같은 세션을 두 번 show 해도 X 는 한 번만 세고, injections 는 delivered.tsv
    # 줄 수 그대로 둔다(§9 "pulled X of N injections" — N 은 전달 횟수다).
    pull_sessions = {r.get("session") for r in rows
                      if r.get("event") == "pull" and r.get("session")}
    injections = 0
    delivered_sessions = set()
    delivered = os.path.join(state, due.DELIVERED_NAME)
    if os.path.exists(delivered):
        with open(delivered, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                injections += 1
                session = line.split("\t", 1)[0]
                if session:
                    delivered_sessions.add(session)
    pulls = len(delivered_sessions & pull_sessions)

    # AGENTS.md 가 CLAUDE.md 와 공유되면 Codex Path B 는 절대 쓰면 안 된다 —
    # 그 파일을 공유 배선 만들기 *전에* 심어 둔 낡은 관리 구간만 실패 사유다.
    shared = agents_md.shared_with_claude(root)
    leaked = bool(shared) and managed_block.installed_captured_at(
        agents_md.path_for(root)) is not None

    # 선택적 어댑터 진단(예: codex 신뢰 안 된 훅). 세션 id 는 전역 유일이므로
    # 레포로 거르지 않은 원장을 넘긴다 — 위 rows 처럼 이 레포로 미리 거르면
    # 자기 .git 을 가진 중첩 워크트리·서브모듈에서 시작한 세션이 다른 repo 키로
    # 기록돼 여기서 영원히 "안 돈 것"으로 보인다. limit(기본 2000, 머신 전체
    # 공유)이 14일 창을 못 덮을 수 있다는 게 알려진 한계다 — 개인용 도구고
    # status 는 훅 경로가 아니므로 필요하면 여기서만 무제한으로 읽는다.
    # 한 어댑터가 죽어도 나머지 status 가 죽으면 안 되므로 어댑터별로 감싼다.
    all_rows = ledger.read(home=home, limit=0)
    health_rows = []
    for adapter_id in installed:
        try:
            inst = adapters.get(adapter_id, home=home)
        except Exception:
            # 어댑터 생성 자체가 안 되면 health 를 판정할 근거가 없다.
            continue
        try:
            health_rows.extend(getattr(inst, "health", lambda *a: ())(root, all_rows))
        except Exception:
            pass

    # hooks 행은 `installed`(detect() 로 감지된 것)보다 넓다 — curl 설치
    # 직후, 하네스가 한 번도 안 돌아 detect() 가 보는 세션 디렉터리가 아직
    # 없어도 설정 디렉터리(~/.claude, ~/.codex)는 있을 수 있고, 그 경우도
    # "설치됐는지" 는 여전히 보여줘야 한다(hook_config_targets, #7 리뷰 1).
    # health 와 독립이다 — 한쪽이 죽어도 다른 쪽 행은 여전히 나와야 한다
    # (리뷰 결함: 예전엔 health 의 예외가 hooks 판정 자체를 건너뛰었다).
    hook_rows = []
    for adapter_id in hook_config_targets(home):
        try:
            inst = adapters.get(adapter_id, home=home)
            hc = getattr(inst, "hook_config", lambda: None)()
            if hc is not None:
                fragment = hookconf.load_fragment(hc.fragment_name)
                ok, detail = hookconf.inspect(hc.config_path, fragment, inst.home)
                hook_rows.append(("{} hooks".format(adapter_id), ok, detail))
        except Exception as exc:
            # 조용히 버리지 않는다 — 판정이 죽었다는 사실 자체가 FAIL 행이다
            # (예: hooks/ 디렉터리가 없어 load_fragment 가 실패한 경우).
            hook_rows.append(("{} hooks".format(adapter_id), False,
                              "cannot check hooks ({})".format(exc)))
    watcher = watch.read_lock(state)

    # 행을 한 번만 만들고 텍스트·JSON 이 같은 목록을 렌더한다 — 따로 만들면
    # 한쪽만 고쳐질 수 있다(#8, status --json 이 항상 exit 0 이던 결함).
    checks = []
    checks.append(("adapters", bool(installed), ", ".join(installed) or "none found"))
    checks.append(("ledger", None,
                   "{} rows for this repo".format(len(rows)) if rows
                   else "no sessions recorded here yet — start either harness in this repo"))

    # lag_rows 의 size/lag_bytes 는 pinned/<sid>/source.jsonl 이 없어도 0 으로
    # 나온다 — "size 0" 과 "고정 성공, 꼬리 0바이트" 를 구분 못 하면 고정이
    # 실패한 세션도 archive PASS 로 보인다(리뷰 결함). watch.lag 의 `pinned` 로
    # 실제 존재 여부를 본다.
    pinned_rows = [r for r in lag_rows if r.get("pinned")]
    unpinned_rows = [r for r in lag_rows if not r.get("pinned")]
    # 고정폭 8자는 Codex UUIDv7 앞 8자가 ~65초마다만 바뀌어 자주 충돌한다
    # (#15c) — 이 레포에 지금 보이는 id 들 사이에서만 유일하면 된다.
    archive_n = _unique_prefix_len([r["session"] for r in lag_rows]) if lag_rows else 8
    if pinned_rows:
        archive_verdict = True
        archive_detail = "; ".join(
            "{} tail={}B".format(r["session"][:archive_n], r["lag_bytes"])
            for r in pinned_rows)
        if unpinned_rows:
            archive_detail += "; unpinned: " + ", ".join(
                r["session"][:archive_n] for r in unpinned_rows)
    elif injections:
        # 핀은 brief.compute 가 전달 *후에만* 만든다(brief.py) — 원장 행은
        # 있지만 아직 한 번도 전달받지 못한 사람에게 archive 를 영영 FAIL 로
        # 두면 안 되지만, 전달은 됐는데(injections>0) 핀이 하나도 없다면
        # 진짜 결함이다.
        archive_verdict = False
        archive_detail = "{} injections but nothing pinned".format(injections)
        log_path = os.path.join(locate.omhc_root(home), brief.GUARD_LOG)
        if os.path.exists(log_path):
            archive_detail += "; details may be in {}".format(log_path)
    else:
        archive_verdict = None
        archive_detail = "nothing handed off to this repo yet"
    checks.append(("archive", archive_verdict, archive_detail))

    off_reason = due.off_reason(state)
    checks.append(("off switch", None,
                   "on" if off_reason is None else "off ({})".format(off_reason)))

    if leaked:
        checks.append(("instruction files", False,
                       "{}; stale omhc block in AGENTS.md would leak into Claude — "
                       "run `omhc clear`".format(shared)))
    elif shared:
        checks.append(("instruction files", True,
                       "{} -> Codex Path B disabled, falls to .omhc/outbox".format(shared)))
    else:
        checks.append(("instruction files", True,
                       "AGENTS.md not shared with CLAUDE.md"
                       if os.path.exists(agents_md.path_for(root))
                       else "no AGENTS.md"))

    for label, health_ok, detail in health_rows:
        checks.append((label, health_ok, detail))
    for label, hook_ok, detail in hook_rows:
        checks.append((label, hook_ok, detail))

    checks.append(("pull rate", None,
                   "pulled {} of {} injections".format(pulls, injections)))
    checks.append(("watcher (optional)", None,
                   "running pid {}".format(watcher) if watcher
                   else "not running — brief falls back to inline parsing"))

    code = 1 if any(verdict is False for _label, verdict, _detail in checks) else 0

    if args.json:
        def _verdict_json(verdict: Optional[bool]) -> Optional[str]:
            if verdict is True:
                return "pass"
            if verdict is False:
                return "fail"
            return None

        out.write(json.dumps({
            "repo_root": root, "repo_key": key, "state_dir": state,
            "adapters": installed, "ledger_rows": len(rows),
            "archive": lag_rows,
            "injections": injections, "pulls": pulls,
            "off": due.is_off(state), "watcher_pid": watcher,
            "instruction_files": {"shared": shared, "stale_block": leaked},
            "health": [{"label": label, "ok": ok, "detail": detail}
                       for label, ok, detail in health_rows],
            "rows": [{"label": label, "verdict": _verdict_json(verdict), "detail": detail}
                     for label, verdict, detail in checks],
        }, ensure_ascii=False, indent=2) + "\n")
        return code

    out.write("repo   {}\nkey    {}\nstate  {}\n\n".format(root, key, state))
    for label, verdict, detail in checks:
        _check(out, label, verdict, detail)
    verbs = {}
    for path in _index_files(state):
        for row in index.rows(path):
            verbs[row.verb] = verbs.get(row.verb, 0) + 1
    if verbs:
        out.write("\nevents  {}\n".format(
            " ".join("{}={}".format(k, verbs[k]) for k in sorted(verbs))))
    out.write("artifact {}\n".format(
        "{}B".format(os.path.getsize(artifact)) if os.path.exists(artifact)
        else "none"))
    return code


# --- brief ------------------------------------------------------------------


def cmd_brief(args, *, home=None, out=sys.stdout) -> int:
    return brief.emit(
        harness=args.harness,
        stdin_text=args.stdin if args.stdin is not None else _stdin_text(),
        budget=args.budget,
        wire=args.wire,
        force=args.force,
        as_text=args.text or args.dry_run,
        home=home,
        out=out,
    )


# --- hooks --------------------------------------------------------------


def _adapters_with_hook_config(home) -> List[str]:
    """`hook_config()` 를 구현한(=SessionStart 훅 개념이 있는) 등록된 어댑터
    id 전부. 감지 여부와 무관하다 — `--harness` 없이 아무 대상도 못 찾았을 때
    "이 중에서 골라라" 로 보여줄 목록이다."""
    ids = []
    for adapter_id in sorted(adapters.REGISTRY):
        try:
            inst = adapters.get(adapter_id, home=home)
        except Exception:
            continue
        if getattr(inst, "hook_config", lambda: None)() is not None:
            ids.append(adapter_id)
    return ids


def hook_config_targets(home) -> List[str]:
    """`hook_config()` 가 있고, 이 머신에서 그 하네스가 감지됐거나(`detect()`)
    설정 디렉터리가 이미 있는 어댑터 id 들. `omhc status` 의 `<adapter-id>
    hooks` 행과 `omhc hooks install` 의 기본 대상이 이 규칙을 공유한다.

    curl 설치 직후, 어느 하네스도 아직 한 번도 안 돈 시점에는 `detect()` 가
    보는 세션 디렉터리(`~/.claude/projects`, `~/.codex/sessions`)가 없다 —
    하지만 하네스 자신의 설정 디렉터리(`~/.claude`, `~/.codex`)는 그 하네스를
    한 번이라도 실행했거나 사람이 미리 만들어 뒀다면 존재할 수 있다. 이 규칙이
    없으면 첫 사용자에게 `hooks install` 이 "찾은 게 없다"며 조용히 아무 일도
    안 하고, `status` 도 훅 행 자체를 안 보여준다(#7 리뷰 1)."""
    ids = []
    for adapter_id in sorted(adapters.REGISTRY):
        try:
            inst = adapters.get(adapter_id, home=home)
        except Exception:
            continue
        hc = getattr(inst, "hook_config", lambda: None)()
        if hc is None:
            continue
        try:
            detected = inst.detect().present
        except Exception:
            detected = False
        if detected or os.path.isdir(os.path.dirname(hc.config_path)):
            ids.append(adapter_id)
    return ids


def cmd_hooks(args, *, home=None, out=sys.stdout) -> int:
    """`omhc hooks install|uninstall`. status 의 `<adapter-id> hooks` 행이
    가리키는 그 설치를 실제로 한다. 코어는 벤더 이름을 모른다 — 대상은
    `hook_config()` 를 구현한, 이 머신에 감지됐거나 설정 디렉터리가 있는
    어댑터들이다."""
    if not getattr(args, "hooks_action", None):
        out.write("usage: omhc hooks install|uninstall [--harness ID]\n")
        return 0

    targets = [args.harness] if args.harness else hook_config_targets(home)
    if not targets:
        known = _adapters_with_hook_config(home)
        out.write("no harness found -- run with --harness <id> ({})\n".format(
            ", ".join(known) if known else "no adapter declares a hook config"))
        return 1

    had_error = False
    for adapter_id in targets:
        try:
            inst = adapters.get(adapter_id, home=home)
        except AdapterUnavailable as exc:
            out.write("{}\n".format(exc))
            had_error = True
            continue

        hc = getattr(inst, "hook_config", lambda: None)()
        if hc is None:
            if args.harness:
                out.write("{}: no hook config for this harness\n".format(adapter_id))
            continue

        try:
            if args.hooks_action == "install":
                fragment = hookconf.load_fragment(hc.fragment_name)
                had_backup = os.path.exists(hc.config_path)
                changed = hookconf.merge(hc.config_path, fragment, inst.home)
                if changed:
                    out.write("{}: installed -> {}\n".format(adapter_id, hc.config_path))
                    if had_backup:
                        out.write("{}: backup {}\n".format(
                            adapter_id, hc.config_path + ".omhc-bak"))
                    if hc.post_write_note:
                        out.write("{}: {}\n".format(adapter_id, hc.post_write_note))
                else:
                    out.write("{}: already up to date\n".format(adapter_id))
                ok, detail = hookconf.inspect(hc.config_path, fragment, inst.home)
                out.write("{}: {} -- {}\n".format(
                    adapter_id, "PASS" if ok else "FAIL", detail))
                if not ok:
                    # 파일은 이미 (다시) 쓰였다 — 그런데도 재검사가 FAIL 이면
                    # (예: 바이너리를 아직 못 찾음) 사람이 고쳐야 할 문제가
                    # 남아 있다는 뜻이므로 exit code 로도 알린다(#7 리뷰 2).
                    had_error = True
            else:
                changed = hookconf.strip(hc.config_path)
                if changed:
                    out.write("{}: removed from {}\n".format(adapter_id, hc.config_path))
                    out.write("{}: backup {}\n".format(
                        adapter_id, hc.config_path + ".omhc-bak"))
                else:
                    out.write("{}: nothing to remove\n".format(adapter_id))
        except hookconf.HookConfigError as exc:
            out.write("{}: {}\n".format(adapter_id, exc))
            had_error = True
        except Exception as exc:  # 트레이스백은 절대 안 보여준다 — 훅 경로는 아니지만 이 명령도 사람용이다.
            out.write("{}: unexpected error ({})\n".format(adapter_id, exc))
            had_error = True

    return 1 if had_error else 0


# --- clear ------------------------------------------------------------------


def cmd_clear(args, *, home=None, out=sys.stdout) -> int:
    root, _key, state = _state_for(home)
    removed = []
    if agents_md.collapse(root, force=True):
        removed.append(agents_md.path_for(root))
    artifact = os.path.join(state, ARTIFACT_NAME)
    if os.path.exists(artifact):
        os.unlink(artifact)
        removed.append(artifact)
    out.write("cleared {}\n".format(", ".join(removed) if removed else "nothing"))
    return 0


# --- watch ------------------------------------------------------------------


def cmd_watch(args, *, home=None, out=sys.stdout) -> int:
    """가속기 데몬. 정확성을 담당하지 않으므로 죽어도 결과가 바뀌지 않는다."""
    root, _key, state = _state_for(home)
    reason = locate.refused_root(root)
    if reason:
        out.write("{}\n".format(reason))
        return 1
    if args.stop:
        pid = watch.read_lock(state)
        if pid is None:
            out.write("no watcher running\n")
            return 0
        import signal as _signal

        try:
            os.kill(pid, _signal.SIGTERM)
        except OSError as exc:
            out.write("could not stop {}: {}\n".format(pid, exc))
            return 1
        out.write("stopped {}\n".format(pid))
        return 0
    if args.once:
        written = watch.sweep(root, state, home=home)
        out.write("indexed {} new events\n".format(written))
        return 0
    try:
        return watch.run(root, home=home, poll=args.poll,
                         idle_exit=args.idle_exit)
    except watch.LockBusy:
        out.write("watcher already running (pid {})\n".format(
            watch.read_lock(state)))
        return 1


# --- parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="하네스 간 작업 이어가기")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("brief", help="훅 경로: 전달할 표식을 stdout 으로")
    p.add_argument("--harness", required=True)
    p.add_argument("--budget", type=int, default=brief.mint.BUDGET)
    p.add_argument("--wire", default="", choices=("", "claude", "cursor", "sdk"),
                   help="주입 JSON 형식. 기본값은 --harness 에서 유도한다")
    p.add_argument("--force", action="store_true")
    p.add_argument("--text", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--stdin", default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("mark", help="세션 시작을 원장에 기록")
    p.add_argument("--harness", required=True)
    p.add_argument("--event", default="start", choices=("start", "end"))
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--stdin", default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("show", help="표식의 태그로 원본 바이트를 조회")
    p.add_argument("target", help="E1 같은 표식 태그, 또는 `omhc log` 가 출력한 "
                   "<session>#N / #N 참조")
    p.add_argument("--full", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("log", help="색인된 이벤트를 한 줄씩")
    p.add_argument("--last", type=int, default=30)
    p.add_argument("--grep", default="")
    p.add_argument("--verb", default="")
    p.add_argument("--file", default="")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("note", help="메모를 남긴다 (에이전트도 부를 수 있다)")
    p.add_argument("text", nargs="+")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("status", help="유일한 사람용 대시보드")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("watch", help="가속기 데몬 (선택. 없어도 결과는 같다)")
    p.add_argument("--stop", action="store_true")
    p.add_argument("--once", action="store_true", help="한 번만 훑고 끝낸다")
    p.add_argument("--poll", type=float, default=watch.POLL_SECONDS)
    p.add_argument("--idle-exit", type=float, default=watch.IDLE_EXIT_SECONDS,
                   dest="idle_exit")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("clear", help="설치된 표식을 제거")
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("hooks", help="omhc 자신의 SessionStart 훅을 설치/제거")
    p.set_defaults(func=cmd_hooks, hooks_action=None)
    hooks_sub = p.add_subparsers(dest="hooks_action")
    p_install = hooks_sub.add_parser("install", help="감지된 하네스에 훅을 병합")
    p_install.add_argument("--harness", default=None)
    p_install.set_defaults(func=cmd_hooks)
    p_uninstall = hooks_sub.add_parser("uninstall", help="omhc 자신의 훅만 제거")
    p_uninstall.add_argument("--harness", default=None)
    p_uninstall.set_defaults(func=cmd_hooks)

    return parser


def main(argv=None, *, home=None, out=None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not getattr(args, "func", None):
        parser.print_help(out or sys.stdout)
        return 0
    stream = out or sys.stdout
    # --harness 를 **여기서** 해소한다. 자유 문자열로 흘려보내면 오타가 세 깊이에서
    # 서로 다르게 조용히 열화된다 — 와이어 표는 기본값으로 떨어지고, 같은 벤더
    # 단축이 매칭을 멈추고, adapters.get 은 brief 의 bare except 안에서 터져
    # 아무것도 출력하지 않는다. 경계에서 한 번 실패하는 것이 낫다.
    harness = getattr(args, "harness", None)
    if harness:
        try:
            adapters.get(harness, home=home)
        except AdapterUnavailable as exc:
            stream.write("{}\n".format(exc))
            return 1
    try:
        return args.func(args, home=home, out=stream)
    except AdapterUnavailable as exc:
        stream.write("{}\n".format(exc))
        return 1
    except BrokenPipeError:
        return 0
