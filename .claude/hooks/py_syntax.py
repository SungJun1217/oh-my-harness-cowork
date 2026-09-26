#!/usr/bin/env python3
"""PostToolUse(Edit|Write): checks that an edited .py file parses under the
supported minimum (MIN_PY) grammar.

Checked via feature_version, not the running interpreter's version. Since
python3 differs across dev machines (3.9.6 on this Mac, 3.12 elsewhere…),
relying on the local version would let `match` pass on a 3.12 machine. CPython
itself documents feature_version as best-effort, so the final line of defense
is the test suite running on the minimum version.

Doesn't depend on jq — python3 is this project's only prerequisite.
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
