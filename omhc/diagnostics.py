"""Read-only explanations of the real handoff decision path."""
from __future__ import annotations

import time

from . import adapters, brief, due, gate, hookconf, ledger, locate

REASONS = {
    "off": "Handoffs are disabled.",
    "same_session": "A session cannot hand off to itself.",
    "same_harness": "Use this harness's native resume instead.",
    "already_delivered": "Already delivered; older sessions are behind this boundary.",
    "too_old": "Older than the seven-day handoff window.",
    "excluded_by_adapter": "The adapter excluded this source (headless or subagent).",
    "missing_source": "The recorded transcript is missing and no replacement was found.",
    "unavailable_source": "The transcript is empty or its format is not recognized.",
    "source_boundary": "An unavailable newer source prevents reviving older work.",
    "reopened_boundary": "Reached the original start of an already delivered, reopened session.",
    "no_new_human_turn": "No new human turn since the previous delivery.",
    "no_human_turn": "No usable human turn for an older-session handoff.",
    "read_error": "Reading the source failed.",
    "read_time_limit": "Older-session read time budget was exhausted.",
    "session_limit": "The handoff already contains the maximum number of sessions.",
    "selection_limit": "Reached the candidate scan limit.",
    "gate_claimed": "This receiving session has already claimed its handoff.",
    "empty_body": "No handoff fits the content and byte-budget rules.",
    "preview_ready": "A handoff preview is ready; actual model receipt is unverified.",
    "no_candidates": "No recorded foreign session is due.",
    "outside_selection_window": "Not judged: behind a selection boundary or scan limit.",
    "included": "Included in the preview before byte-budget trimming.",
}
_PASS = {"candidate", "reading", "read", "selection_limit", "no_candidates"}


def _reason(events):
    useful = [e["reason"] for e in events if e["reason"] not in _PASS]
    # An eligibility callback can identify a more precise boundary before
    # due() reports its generic excluded result.
    if useful and useful[-1] == "excluded_by_adapter" and len(useful) > 1:
        return useful[-2]
    return useful[-1] if useful else "outside_selection_window"


def _hooks(harness, home, root):
    inst = adapters.get(harness, home=home)
    custom = getattr(inst, "hooks_status", None)
    if callable(custom):
        ok, detail = custom()
    else:
        config = getattr(inst, "hook_config", lambda: None)()
        if config is None:
            return {"ok": None, "detail": "This adapter declares no hook config."}
        ok, detail = hookconf.inspect(config.config_path,
                                      hookconf.load_fragment(config.fragment_name),
                                      home or inst.home)
    present = getattr(inst, "hook_is_installed", None)
    if ok is False and callable(present) and present(root):
        return {"ok": None, "detail": "A runnable repo hook exists; global config check: " + detail,
                "scope": "Global configuration check; project hook approval remains unverified."}
    return {"ok": ok, "detail": detail, "scope": "Global hook configuration; approval is unverified."}


def explain(root, harness, *, session="", home=None, now=None):
    stamp = time.time() if now is None else now
    key = locate.repo_key(root)
    events = []
    body = ""
    error = None
    try:
        body = brief.compute(my_harness=harness, my_session_id=session,
                             repo_root=root, home=home, now=stamp,
                             dry_run=True, trace=events.append)
    except Exception as exc:
        error = str(exc)
        reading = next((e for e in reversed(events) if e["reason"] == "reading"), {})
        due.report(events.append, "read_error", reading, detail=error)
    reasons = {e["reason"] for e in events}
    if body:
        reason = "gate_claimed" if "gate_claimed" in reasons else "preview_ready"
    else:
        reason = next((r for r in ("off", "read_error", "missing_source", "unavailable_source",
                                   "no_new_human_turn", "no_human_turn", "empty_body",
                                   "already_delivered", "too_old", "excluded_by_adapter",
                                   "same_session", "same_harness") if r in reasons), "no_candidates")
    try:
        hooks = _hooks(harness, home, root)
    except Exception as exc:
        hooks = {"ok": None, "detail": "Cannot judge hook config: {}".format(exc)}
    actions = []
    if hooks["ok"] is False:
        actions.append("omhc hooks install --harness " + harness)
    if reason == "off":
        actions.append("Check OMHC_OFF and the repo's off marker; enabling is a manual choice.")
    if reason in ("same_session", "same_harness"):
        actions.append("Use native resume in the source harness.")
    if reason in ("missing_source", "unavailable_source", "read_error"):
        actions.append("Run omhc status and inspect the source transcript.")
    return {"repo_root": root, "harness": harness, "session": session or None,
            "outcome": "error" if error else ("ready" if body and reason != "gate_claimed" else "nothing"),
            "reason": reason, "detail": REASONS[reason], "hooks": hooks,
            "gate_claimed": gate.is_claimed(locate.state_dir(key, home=home), harness, session) if session else None,
            "actions": actions, "events": events, "preview": body,
            "scope": "Read-only snapshot of recorded starts; no backfill, delivery, or hook-trust verification."}


def sessions(root, harness, *, session="", home=None, now=None, last=20):
    stamp = time.time() if now is None else now
    key = locate.repo_key(root)
    state = locate.state_dir(key, home=home)
    explanation = explain(root, harness, session=session, home=home, now=stamp)
    rows = []
    seen = set()
    for row in reversed(ledger.read(repo_key=key, home=home)):
        identity = (str(row.get("harness") or ""), str(row.get("session") or ""))
        if row.get("event") != "start" or not all(identity) or identity in seen:
            continue
        seen.add(identity)
        events = [e for e in explanation["events"]
                  if e.get("session") == identity[1] and e.get("harness") == identity[0]]
        reason = "off" if explanation["reason"] == "off" else _reason(events)
        record = {"harness": identity[0], "session": identity[1], "reason": reason,
                  "detail": REASONS.get(reason, reason), "goal": None,
                  "delivered": due.already_delivered(state, identity[1], harness),
                  "resume_hint": None}
        try:
            inst = adapters.get(identity[0], home=home)
            mark = due.Watermark(key, identity[0], identity[1], str(row.get("path") or ""),
                                 "start", float(row.get("epoch") or 0))
            refs = brief._ref_for(inst, mark, root)
            if refs:
                read = inst.read_session(refs[0])
                record["goal"] = next((e.text for e in read.events
                                       if e.author == "human" and e.verb == "said"), None)
                record["resume_hint"] = inst.native_resume_hint(refs[0])
        except Exception as exc:
            record["read_error"] = str(exc)
        rows.append(record)
        if len(rows) >= last:
            break
    return {"harness": harness, "sessions": rows,
            "scope": "Recorded starts only, newest ledger position first; unrecorded sessions are not listed."}
