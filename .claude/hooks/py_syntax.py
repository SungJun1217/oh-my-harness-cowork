#!/usr/bin/env python3
"""PostToolUse(Edit|Write): 고친 .py 파일이 지원 하한(MIN_PY) 문법으로 파싱되는지 본다.

실행하는 인터프리터 버전이 아니라 feature_version 으로 검사한다. 개발 머신마다
python3 가 다르므로(이 맥 3.9.6, 다른 곳은 3.12…) 로컬 버전에 기대면 3.12 머신에서
match 가 그냥 통과한다. feature_version 은 CPython 도 best-effort 라 명시하므로
최종 방어선은 하한 버전에서 도는 테스트다.

jq 에 의존하지 않는다 — python3 는 이 프로젝트의 유일한 전제 조건이다.
"""
import ast
import json
import sys

MIN_PY = (3, 9)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    path = (payload.get("tool_input") or {}).get("file_path") or (
        payload.get("tool_response") or {}
    ).get("filePath")
    if not path or not path.endswith(".py"):
        return 0
    try:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
    except OSError:
        return 0
    try:
        ast.parse(src, path, feature_version=MIN_PY)
    except SyntaxError as exc:
        sys.stderr.write(
            "Python {}.{} syntax check failed for {}:{}: {}\n".format(
                MIN_PY[0], MIN_PY[1], path, exc.lineno, exc.msg
            )
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
