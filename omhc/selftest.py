"""Opt-in installation probes, confined to disposable homes; no model calls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

from . import adapters, hookconf, mint


def _commands(inst):
    config = getattr(inst, "hook_config", lambda: None)()
    if config is None:
        return None, []
    fragment = hookconf.load_fragment(config.fragment_name)
    commands = [h["command"] for group in fragment.get("SessionStart", [])
                for h in group.get("hooks", []) if h.get("type") == "command"]
    return config, commands


def run():
    launcher = Path(__file__).resolve().parent.parent / "bin" / "omhc"
    checks = []
    ids = sorted(adapters.REGISTRY)
    for source in ids:
        factory = getattr(adapters.get(source), "diagnostic_sample", None)
        if not callable(factory):
            checks.append({"label": source + " sample", "ok": None,
                           "detail": "No diagnostic sample declared by this adapter."})
            continue
        for target in ids:
            if source == target:
                continue
            label = source + " -> " + target
            try:
                with tempfile.TemporaryDirectory(prefix="omhc-self-test-") as temporary:
                    home = Path(temporary) / "Test Home"
                    repo = home / "repo"
                    repo.mkdir(parents=True)
                    (repo / ".omhc-root").touch()
                    binary = home / ".local" / "bin" / "omhc"
                    binary.parent.mkdir(parents=True)
                    binary.symlink_to(launcher)
                    env = dict(os.environ, HOME=str(home))
                    for key in ("OMHC_OFF", "OMHC_LIVE", "OMHC_ALLOW_HEADLESS"):
                        env.pop(key, None)
                    instances = {i: adapters.get(i, home=str(home)) for i in (source, target)}
                    configs = {i: _commands(instances[i]) for i in instances}
                    if any(config is None or not commands for config, commands in configs.values()):
                        checks.append({"label": label, "ok": None,
                                       "detail": "A participating adapter has no SessionStart hooks."})
                        continue
                    for config, _commands_unused in configs.values():
                        hookconf.merge(config.config_path,
                                       hookconf.load_fragment(config.fragment_name), str(home))
                    sample = factory(str(repo))
                    transcript = home / "source.jsonl"
                    raw = "".join(json.dumps(row) + "\n" for row in sample["rows"]).encode()
                    transcript.write_bytes(raw)

                    def execute(argv, payload=None):
                        return subprocess.run(argv, input=payload, capture_output=True,
                                              cwd=str(repo), env=env, timeout=15)

                    source_payload = json.dumps({"cwd": str(repo), "session_id": "self-test-source",
                        "transcript_path": str(transcript), "source": "startup"}).encode()
                    source_mark = next(c for c in configs[source][1]
                                       if hookconf._parse_call(c).sub == "mark")
                    result = execute(["/bin/sh", "-c", source_mark], source_payload)
                    if result.returncode or result.stdout:
                        raise ValueError("Source mark did not exit silently with code 0.")
                    payload = json.dumps({"cwd": str(repo), "session_id": "self-test-target",
                                          "source": "startup"}).encode()

                    def chain():
                        outputs = []
                        for command in configs[target][1]:
                            result = execute(["/bin/sh", "-c", command], payload)
                            if result.returncode:
                                raise ValueError("A shipped hook command failed: " + result.stderr.decode(errors="replace"))
                            if result.stdout:
                                outputs.append(json.loads(result.stdout))
                        return outputs

                    outputs = chain()
                    if len(outputs) != 1:
                        raise ValueError("Expected exactly one handoff wire payload.")
                    wire = outputs[0]
                    body = (wire.get("hookSpecificOutput", {}).get("additionalContext")
                            or wire.get("additionalContext") or wire.get("additional_context") or "")
                    if "GOAL  " + sample["goal"] not in body or "FAIL" not in body:
                        raise ValueError("Expected human goal and synthetic failure were not relayed.")
                    if not 0 < len(body.encode("utf-8")) <= mint.BUDGET:
                        raise ValueError("Handoff exceeded its byte budget or was empty.")
                    if chain():
                        raise ValueError("Duplicate SessionStart emitted a second handoff.")
                    before = execute([str(binary), "show", "E1"])
                    transcript.unlink()
                    after = execute([str(binary), "show", "E1"])
                    if (before.returncode or after.returncode or not before.stdout
                            or before.stdout != after.stdout or before.stdout not in raw.splitlines(keepends=True)):
                        raise ValueError("Archived raw-byte lookup did not survive deleting the synthetic source.")
                    checks.append({"label": label, "ok": True,
                        "detail": "Shipped shell hooks, human goal, FAIL, byte budget, duplicate gate, and archived show passed."})
            except Exception as exc:
                checks.append({"label": label, "ok": False, "detail": str(exc)})
    return {"isolated": True, "model_receipt": "unverified",
            "scope": "Synthetic local pipeline only; no harness or model is launched, and host hook trust is not verified.",
            "checks": checks,
            "ok": any(r["ok"] is True for r in checks) and not any(r["ok"] is False for r in checks)}
