#!/usr/bin/env python3
"""PreToolUse(Bash): git commit 직전에 unittest + smoke 를 돌리고, 실패하면 막는다(exit 2).

매 턴이 아니라 커밋 경계에 둔 것은 스위트가 약 12초라서다.
"""
import json
import os
import re
import subprocess
import sys

_COMMIT = re.compile(r"(^|[;&|\s])git(\s+-C\s+\S+)?\s+commit(\s|$)")

SUITES = (
    ("unit tests", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-q"]),
    ("tests/smoke.sh", ["bash", "tests/smoke.sh"]),
)


def main() -> int:
    try:
        cmd = (json.load(sys.stdin).get("tool_input") or {}).get("command") or ""
    except ValueError:
        return 0
    if not _COMMIT.search(cmd):
        return 0
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    for name, argv in SUITES:
        proc = subprocess.run(
            argv, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        if proc.returncode != 0:
            tail = "\n".join(proc.stdout.splitlines()[-40:])
            sys.stderr.write("Commit blocked: {} failed.\n{}\n".format(name, tail))
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
