from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import List, Optional

from . import adapters, agents_md, brief, due, gate, index, ledger, locate, pin, watch
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
    # 어떤 omhc 호출에서든 오래된 AGENTS.md 구간을 붕괴시킨다.
    try:
        agents_md.collapse(root)
    except Exception:
        pass
    if args.verbose:
        out.write("marked {} {} in {}\n".format(args.harness, args.event, key))
    return 0


# --- note -------------------------------------------------------------------


def cmd_note(args, *, home=None, out=sys.stdout) -> int:
    _root, _key, state = _state_for(home)
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


# --- log --------------------------------------------------------------------


def _index_files(state: str) -> List[str]:
    directory = os.path.join(state, "index")
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [os.path.join(directory, n) for n in names if n.endswith(".idx")]


def cmd_log(args, *, home=None, out=sys.stdout) -> int:
    _root, _key, state = _state_for(home)
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

    for session, row in rows:
        out.write(
            "#{} {} {} {} {}\n".format(
                row.seq,
                session[:8],
                row.verb,
                "ok" if row.ok else "FAIL",
                row.arg or ",".join(row.paths),
            )
        )
    if not rows:
        out.write("no events (run a session in another harness first)\n")
    return 0


# --- show -------------------------------------------------------------------


def _pinned_path(state: str, session_id: str, fallback: str) -> str:
    pinned = os.path.join(pin.pinned_dir(state, session_id), "source.jsonl")
    return pinned if os.path.exists(pinned) else fallback


def cmd_show(args, *, home=None, out=sys.stdout) -> int:
    _root, _key, state = _state_for(home)
    target = args.target.strip()
    refs = index.read_refs(state)

    entry = refs.get(target) or refs.get(target.upper())
    if entry is None and target.startswith("#"):
        try:
            seq = int(target[1:])
        except ValueError:
            seq = None
        if seq is not None:
            for path in _index_files(state):
                row = index.find(path, seq)
                if row:
                    session = os.path.basename(path)[: -len(".idx")]
                    entry = {"session_id": session, "source_path": "",
                             "offset": row.offset, "length": row.length, "seq": seq}
                    break
    if entry is None:
        out.write("unknown reference {!r}; try `omhc log --last 30`\n".format(target))
        return 1

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
    return 0


# --- status -----------------------------------------------------------------


def _check(out, label: str, ok: bool, detail: str) -> bool:
    out.write("{:<4} {:<22} {}\n".format("PASS" if ok else "FAIL", label, detail))
    return ok


def cmd_status(args, *, home=None, out=sys.stdout) -> int:
    root, key, state = _state_for(home)
    installed = adapters.present(now=time.time)
    rows = [r for r in ledger.read(home=home) if r.get("repo") == key]
    artifact = os.path.join(state, ARTIFACT_NAME)

    # watch.lag 가 정확히 이 계산을 소유한다. 두 벌로 두면 고정 레이아웃이
    # 바뀔 때 한쪽만 고쳐진다.
    lag_rows = watch.lag(state)

    pulls = len([r for r in rows if r.get("event") == "pull"])
    injections = 0
    delivered = os.path.join(state, due.DELIVERED_NAME)
    if os.path.exists(delivered):
        with open(delivered, encoding="utf-8", errors="replace") as fh:
            injections = len([line for line in fh if line.strip()])

    if args.json:
        out.write(json.dumps({
            "repo_root": root, "repo_key": key, "state_dir": state,
            "adapters": installed, "ledger_rows": len(rows),
            "archive": lag_rows,
            "injections": injections, "pulls": pulls,
            "off": due.is_off(state), "watcher_pid": watch.read_lock(state),
        }, ensure_ascii=False, indent=2) + "\n")
        return 0

    out.write("repo   {}\nkey    {}\nstate  {}\n\n".format(root, key, state))
    ok = True
    ok &= _check(out, "adapters", bool(installed), ", ".join(installed) or "none found")
    ok &= _check(out, "ledger", bool(rows),
                 "{} rows for this repo".format(len(rows)))
    ok &= _check(out, "archive", bool(lag_rows),
                 "; ".join("{} tail={}B".format(r["session"][:8], r["lag_bytes"])
                           for r in lag_rows)
                 or "nothing pinned yet")
    ok &= _check(out, "off switch", not due.is_off(state),
                 "off" if due.is_off(state) else "on")
    # 항상 참인 항목을 ok 에 접으면 독자가 리터럴 True 를 추적해야 안다.
    # watcher 줄처럼 정보로만 출력한다.
    _check(out, "pull rate", True,
           "pulled {} of {} injections".format(pulls, injections))
    watcher = watch.read_lock(state)
    _check(out, "watcher (optional)", True,
           "running pid {}".format(watcher) if watcher
           else "not running — brief falls back to inline parsing")
    out.write("\nartifact {}\n".format(
        "{}B".format(os.path.getsize(artifact)) if os.path.exists(artifact)
        else "none"))
    return 0 if ok else 1


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
    p.add_argument("target")
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

    return parser


def main(argv=None, *, home=None, out=None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not getattr(args, "func", None):
        parser.print_help(out or sys.stdout)
        return 0
    stream = out or sys.stdout
    try:
        return args.func(args, home=home, out=stream)
    except AdapterUnavailable as exc:
        stream.write("{}\n".format(exc))
        return 1
    except BrokenPipeError:
        return 0
