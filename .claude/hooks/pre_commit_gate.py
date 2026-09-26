#!/usr/bin/env python3
"""PreToolUse(Bash): runs unittest + smoke right before git commit, and blocks
it on failure (exit 2).

Placed at the commit boundary rather than every turn because the suite takes ~12s.
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
