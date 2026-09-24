---
name: temper
description: Develop one unit of work end-to-end in omhc — analyze (if needed) → implement → review → commit. Use when the user runs /temper <task>. Never pushes.
argument-hint: <what to build or fix>
disable-model-invocation: true
---

# /temper — one unit of work, hardened by review, then committed

Task: $ARGUMENTS

You (the main session) orchestrate. The subagents do the work; you hold the context,
make the judgment calls, and write the commit. Talk to the user in Korean.

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
and the analyst's evidence if any. It edits and runs the tests; it does not commit.
If it stops because the task is ambiguous or out of scope, resolve that (yourself or with the user) and re-brief.

## 3. Review

Spawn `omhc-reviewer` on the uncommitted diff. Pass one line of intent so it knows what the change is for.

- **Verdict ship / no findings** → go to 4.
- **CONFIRMED findings, or PLAUSIBLE high-severity ones** → send them back to `omhc-implementer` (the findings verbatim, plus which to fix), then review again.
- **Low-severity PLAUSIBLE findings** → your call; mention the ones you skipped in the final report.
- **Max 2 fix rounds.** If findings remain after the second re-review, stop without committing: show the user the remaining findings and the current diff summary, and ask how to proceed.

## 4. Commit

- Stage only the files this unit touched (`git add <paths>`, never `-A` blindly). Fixtures under `tests/fixtures/` are never staged.
- Message format, matching this repo's log:
  - Subject: `type(scope): 한국어 요약` — types in use: `feat`, `fix`, `refactor`, `perf`, `test`, `docs`, `chore`, `ci`
  - Body in Korean: *why* the change was made (the cause, usually a measured fact), then what changed. Wrap at ~80 columns.
  - End with the attribution trailer from the current system instructions.
- The pre-commit hook runs the unit tests and `tests/smoke.sh`. If it blocks, treat the failure as a new finding: one more implementer → reviewer pass, then commit. If it still fails, stop and report.
- **Do not push.** Pushing is outward-facing; ask the user separately.

## 5. Report (Korean, short)

- What was done, in one or two sentences
- The commit hash and subject
- Review rounds used, and any findings deliberately left unfixed
- That it has not been pushed
