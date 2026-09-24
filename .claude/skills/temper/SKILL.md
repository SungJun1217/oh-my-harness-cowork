---
name: temper
description: Develop one unit of work end-to-end in omhc — analyze (if needed) → implement → review → commit. Use when the user runs /temper <task>. Never pushes.
argument-hint: <what to build or fix>
disable-model-invocation: true
---

# /temper — one unit of work, hardened by review, then committed

Task: $ARGUMENTS

You (the main session) orchestrate and write the commit. Talk to the user in Korean.

## 0. Scope check

- If `$ARGUMENTS` is empty or too vague to define "done", ask the user one focused question and stop.
- If it is clearly several independent units, propose the split and ask which to do first. One `/temper` = one commit.
- If the working tree already has uncommitted changes unrelated to this task, stop and ask — do not fold them into this commit.

## 1. Analyze (only when needed)

Skip this when the task already names the files and the intended behavior.
Otherwise spawn `omhc-analyst` with the task and whatever you know. Use its conclusion,
evidence, and invariants-touched list to write a precise brief for step 2.
If it reports "needs re-discussion" against a settled decision, stop and bring that to the user.

## 2. Implement

Spawn `omhc-implementer` with a concrete brief: files, intended behavior, invariants to watch,
and the analyst's evidence if any. If it stops because the task is ambiguous or out of scope,
resolve that (yourself or with the user) and continue the same agent with SendMessage.

## 3. Review

Spawn `omhc-reviewer` with one line of intent and the implementer's test result. Tell it the
suite is already green, so it runs tests only to reproduce a specific finding.

- **CONFIRMED findings and high-severity PLAUSIBLE ones** → send them verbatim to the *same*
  implementer (SendMessage, not a new agent), then continue the *same* reviewer with the prior
  findings and only what changed since: check the fixes and any new hunks, not the whole diff again.
- **Everything else** → your call; list anything you skip in the final report.
- **Max 2 fix rounds.** If findings remain after the second re-review, stop without committing:
  show the user the remaining findings and the current diff summary, and ask how to proceed.

## 4. Commit

- Stage only the files this unit touched (`git add <paths>`, never `-A`).
- Start from the implementer's suggested message and check its type against `git log --oneline -20`.
  The body is Korean: *why* the change was made (the cause, usually a measured fact), then what
  changed, wrapped at ~80 columns. End with the attribution trailer from the current system instructions.
- The pre-commit gate (`.claude/hooks/pre_commit_gate.py`) may block the commit. Treat that as a
  new finding: the same implementer fixes it, the same reviewer checks only that fix, then commit again.
  If it blocks a second time, stop and report.
- **Do not push.** Pushing is outward-facing; ask the user separately.

## 5. Report (short)

- What was done, in one or two sentences
- The commit hash and subject
- Review rounds used, and any findings deliberately left unfixed
- That it has not been pushed
