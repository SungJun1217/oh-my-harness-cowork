#!/bin/sh
# omhc install script. Unpacks a GitHub release source tree into
# ~/.local/share/omhc/<version> and symlinks ~/.local/bin/omhc to it — the
# same path the hooks/ fragments point at.
# Doesn't use pip — zero dependencies, so the source tree IS the install.
#
#   curl -fsSL https://raw.githubusercontent.com/SungJun1217/oh-my-harness-cowork/main/install.sh | sh
#   curl -fsSL .../install.sh | OMHC_VERSION=v0.1.0 sh      # a specific version
#
# Running it again is an update. Uninstall: curl -fsSL .../install.sh | sh -s -- --uninstall
set -eu

repo=SungJun1217/oh-my-harness-cowork
share=$HOME/.local/share/omhc
bin=$HOME/.local/bin

die() { echo "omhc install: $*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "python3 not found (3.9+ required)"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
  || die "python3 $(python3 -c 'import platform; print(platform.python_version())') — 3.9+ required"

# --uninstall: reverts the hook merge and removes the install. ~/.omhc (the
# archive) is left in place unless OMHC_PURGE=1 — so accidentally running
# --uninstall doesn't lose history. Must work even when omhc isn't installed
# (or wasn't before), so it can't depend on omhc itself — meaning it can't
# call hookconf.strip() and instead re-embeds the same logic as python inside
# the shell. tests/test_hooks_cmd.py's TestInstallShParity keeps the two
# implementations from drifting by checking they give the same output for the
# same input.
if [ "${1:-}" = "--uninstall" ]; then
  claude_settings=$HOME/.claude/settings.json
  codex_hooks=$HOME/.codex/hooks.json

  strip_omhc_hooks() {
    # A direct mirror of omhc/hookconf.py's strip_all()/_strip_hooks() — only
    # looks at hooks.SessionStart and hooks.UserPromptSubmit (v2 phase 2,
    # #42: `omhc turn`'s hook). Other events (Stop etc.) are left completely
    # untouched, so a user command like "omhc done" is never caught by
    # accident. Filters hook-by-hook and only removes a group/event when it's
    # left empty — doesn't remove a user hook hand-merged into the same
    # group. Command matching is narrowly scoped to the three commands
    # (mark/brief/turn) the harness fragments actually use. hookconf judges
    # argv structurally (_parse_call), while this is a string regex —
    # mistakes on the deletion side are tolerated broadly since this is
    # delete-only and safe, but a hook that only resembles the string
    # diverges between the two judgments. The three measured cases (`echo
    # 'omhc brief'`, `cd ~ && omhc brief …`·`/usr/bin/env omhc mark …`,
    # `'omhc' 'mark' …`) are in omhc/hookconf.py's module comment, and the
    # parity test deliberately excludes them.
    #
    # Exit codes: 0=changed and written, 2=error (die, stops before removing
    # the binary), 3=nothing to change.
    python3 - "$1" <<'PY'
import json, os, re, shutil, sys, tempfile

OMHC_CMD = re.compile(r'''(^|[/\s"'])omhc["']?\s+(mark|brief|turn)(\s|$|["'])''')
MANAGED_EVENTS = ("SessionStart", "UserPromptSubmit")

def is_omhc_hook(h):
    return isinstance(h, dict) and isinstance(h.get("command"), str) \
        and OMHC_CMD.search(h["command"]) is not None

def fail(msg):
    print("omhc uninstall: %s" % msg, file=sys.stderr)
    sys.exit(2)

def main():
    target = sys.argv[1]
    try:
        with open(target, encoding="utf-8") as fh:
            conf = json.load(fh)
    except (OSError, ValueError) as e:
        fail("failed to parse %s, leaving it untouched: %s" % (target, e))

    if not isinstance(conf, dict):
        fail("%s's top level is not an object, leaving it untouched" % target)

    hooks = conf.get("hooks")
    if hooks is None:
        sys.exit(3)
    if not isinstance(hooks, dict):
        fail("%s's hooks is not an object, leaving it untouched" % target)

    changed = False
    for event in MANAGED_EVENTS:
        groups = hooks.get(event)
        if groups is None:
            continue  # this event was never installed — nothing to strip from it.
        if not isinstance(groups, list):
            fail("%s's hooks.%s is not an array, leaving it untouched" % (target, event))

        kept_groups = []
        event_removed_any = False
        for g in groups:
            # If the shape deviates from expected (group is an object, hooks
            # is an array), fail-closed instead of safely skipping — same
            # principle as guard.py: don't leave or rewrite an unrecognized
            # mechanical shape on its own.
            if not isinstance(g, dict):
                fail("a %s group in %s is not an object, leaving it untouched" % (event, target))
            if not isinstance(g.get("hooks"), list):
                fail("a %s group's hooks in %s is not an array, leaving it untouched" % (event, target))
            kept_hooks = [h for h in g["hooks"] if not is_omhc_hook(h)]
            if len(kept_hooks) == len(g["hooks"]):
                # Nothing removed from this group — keep it exactly as-is,
                # even if it's empty (review #1 finding 8: a pre-existing
                # empty array/group this event never actually needed
                # touching must not be reassigned or dropped just because a
                # *different* managed event changed in this same pass).
                kept_groups.append(g)
                continue
            changed = True
            event_removed_any = True
            if kept_hooks:
                ng = dict(g)
                ng["hooks"] = kept_hooks
                kept_groups.append(ng)
            # else: the whole group was omhc hooks only — drop the whole group

        if not event_removed_any:
            continue  # this event's groups are untouched — leave hooks[event] exactly as it was.

        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]

    if not changed:
        sys.exit(3)  # nothing to change — skip backup/rewrite

    if not hooks:
        # If hooks held only omhc's own managed events, it's now an empty
        # object — leaving it would leave {"hooks": {}} as a trace in a
        # config that never installed anything.
        del conf["hooks"]

    shutil.copy2(target, target + ".omhc-bak")  # keeps permission bits matching the original too

    # Atomic swap: fully encode in memory first, so an encoding failure (e.g.
    # a surrogate) doesn't truncate the file, then write to a temp file in
    # the same directory and os.replace it. If target is a symlink, swap the
    # real file it points at via os.path.realpath, keeping the symlink itself intact.
    real_target = os.path.realpath(target)
    try:
        text = json.dumps(conf, indent=2, ensure_ascii=False) + "\n"
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(conf, indent=2, ensure_ascii=True) + "\n"
        data = text.encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(real_target) or ".", prefix=".omhc-tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(tmp_path, os.stat(real_target).st_mode & 0o777)  # mkstemp defaults to 0600 — don't lose the original permissions
        except OSError:
            pass
        os.replace(tmp_path, real_target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    sys.exit(0)

try:
    main()
except SystemExit:
    raise
except Exception as e:
    fail("unexpected error, may have left it untouched: %s" % e)
PY
  }

  for f in "$claude_settings" "$codex_hooks"; do
    if [ ! -f "$f" ]; then
      echo "omhc uninstall: $f not found, skipping"
      continue
    fi
    set +e
    strip_omhc_hooks "$f"
    rc=$?
    set -e
    case $rc in
      0) echo "omhc uninstall: removed omhc's hooks from $f, backed up to $f.omhc-bak" ;;
      3) echo "omhc uninstall: no omhc hook in $f" ;;
      *) echo "omhc uninstall: failed to process $f, left it untouched (binary not removed either)" >&2; exit 2 ;;
    esac
  done

  if [ -L "$bin/omhc" ]; then
    target=$(readlink "$bin/omhc" 2>/dev/null || true)
    case "$target" in
      "$share"/*) rm -f "$bin/omhc"; echo "omhc uninstall: removed $bin/omhc" ;;
      *) echo "omhc uninstall: $bin/omhc is not a symlink omhc created, skipping" ;;
    esac
  else
    echo "omhc uninstall: $bin/omhc not found, skipping"
  fi

  if [ -d "$share" ]; then
    rm -rf "$share"
    echo "omhc uninstall: removed $share"
  else
    echo "omhc uninstall: $share not found, skipping"
  fi

  if [ "${OMHC_PURGE:-}" = "1" ]; then
    rm -rf "$HOME/.omhc"
    echo "omhc uninstall: OMHC_PURGE=1 — also removed ~/.omhc (the archive)"
  else
    echo "omhc uninstall: keeping ~/.omhc (the archive) (set OMHC_PURGE=1 to remove it)"
  fi

  exit 0
fi

ver=${OMHC_VERSION:-}
if [ -z "$ver" ]; then
  ver=$(curl -fsSL "https://api.github.com/repos/$repo/releases/latest" \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1) || true
  [ -n "$ver" ] || die "could not find the latest release. Specify one like OMHC_VERSION=v0.1.0"
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# Don't unpack straight off a pipe: sh has no pipefail, so a curl failure would be hidden.
curl -fsSL -o "$tmp/src.tar.gz" "https://github.com/$repo/archive/refs/tags/$ver.tar.gz" \
  || die "failed to download $ver (check whether that tag exists)"
mkdir "$tmp/src"
tar -xzf "$tmp/src.tar.gz" -C "$tmp/src" || die "failed to unpack $ver"
set -- "$tmp/src"/*
[ -x "$1/bin/omhc" ] || die "the downloaded source has no bin/omhc"

mkdir -p "$share" "$bin"
rm -rf "$share/$ver"
mv "$1" "$share/$ver"
ln -sfn "$ver" "$share/current"
ln -sfn "$share/current/bin/omhc" "$bin/omhc"

# Clean up old versions: only keeps what current points at. Only deletes
# actual directories matching v[0-9]*, and never touches the current symlink
# itself (#15).
for d in "${share:?}"/v[0-9]*; do
  [ -d "$d" ] || continue
  [ "$(basename "$d")" = "$ver" ] && continue
  rm -rf "$d"
done

echo "omhc $ver installed → $bin/omhc"
case ":$PATH:" in
  *":$bin:"*) ;;
  *) echo "note: $bin is not in PATH. Add export PATH=\"\$HOME/.local/bin:\$PATH\" to your shell config" ;;
esac
echo "next: install the hooks with omhc hooks install, approve them for trust in Codex, then omhc status"
# install.sh is served from the main branch, but $ver may be an older
# release — since it may have downloaded a version that doesn't have hooks
# install yet, also show the manual-merge path (#7 review 7).
echo "      (if it's old enough to have no 'hooks install' command, hand-merge $share/current/hooks/*.json into your own settings)"
