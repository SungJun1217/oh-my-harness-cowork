## What and why

<!-- Same rule as commits: type(scope): English summary. The body explains *why*. -->

## Checks

- [ ] `python3 -m unittest discover -s tests -t . -q` passes
- [ ] `bash tests/smoke.sh` passes
- [ ] No `AGENTS.md` invariant is broken (especially the 900-byte cap and the never-raising hook path)
- [ ] No raw session content or fixtures committed
- [ ] Target branch is `develop`
