#!/bin/sh
# History guard for ring-delivery-assistant: refuses a push when the
# repository HISTORY carries configured sensitive tokens. The full
# `git log --all -p` dump — every commit message plus every diff of
# every revision reachable from any ref — is scanned, not just the
# working tree or the staged diff. Invoked by scripts/hooks/pre-push
# (core.hooksPath wiring); also the manual full-audit entry point:
# run it directly before any publication-grade push.
#
# WHY A HISTORY GUARD: the tree guard (guard-sensitive-content.sh)
# sees only STAGED ADDED lines, and no diff scanner at commit time
# covers commit MESSAGES. A token that entered history before that
# guard existed, or travelled in a message body, stays invisible to
# it while remaining fully present in every clone. This script closes
# that class: a configured token anywhere in any revision or any
# commit message blocks the push. Removing the token in a LATER
# commit is not enough — the old revision still carries it — so a
# hit means rewrite history (project runbook: git filter-repo) or
# reconsider the token list.
#
# TOKEN LIST: same file, same semantics as guard-sensitive-content.sh:
# $(git rev-parse --absolute-git-dir)/sensitive-tokens — literal fixed
# strings, one per line, blank lines ignored, matched
# case-insensitively as substrings. The list never lives in tracked
# content, so this script's own history introduces no self-matches.
#
# EXIT CODES: token found in history => exit 1 (push refused).
# Guard-internal failure (no git dir, mktemp failure, history dump
# failure, or a tokens path present but not a readable regular file)
# => exit 2, failing closed per design. Clean, or check disabled
# (tokens file absent/empty) => exit 0 — fail-open bootstrap, same
# as the tree guard.
#
# HONEST BOUNDARY: `git push --no-verify` bypasses the hook (running
# this script directly remains the audit-grade check); tokens split
# across a binary/encoded boundary are not caught (grep -a forces
# text treatment of what it can see); attribution is commit-level —
# the offending SHA and a per-commit match count are printed, never
# the matching content, and file/line attribution needs the offline
# attributed scanner; refs not reachable from any local ref (e.g.
# objects only in someone else's clone) are out of scope by
# definition.
#
# POSIX sh: no bashisms — runs under dash/busybox as well as bash.

set -u

GIT_DIR="$(git rev-parse --absolute-git-dir)" || {
  echo "guard-history: cannot determine git dir" >&2
  exit 2
}
TOKENS="$GIT_DIR/sensitive-tokens"

if [ ! -e "$TOKENS" ] && [ ! -L "$TOKENS" ]; then
  echo "guard-history: no sensitive-tokens list in the git dir; check disabled" >&2
  exit 0
fi
if [ ! -f "$TOKENS" ] || [ ! -r "$TOKENS" ]; then
  echo "guard-history: sensitive-tokens exists but is not a readable file; failing closed per design" >&2
  exit 2
fi

PATTERNS="$(mktemp)" || {
  echo "guard-history: mktemp failed; failing closed per design" >&2
  exit 2
}
DUMP="$(mktemp)" || {
  rm -f "$PATTERNS"
  echo "guard-history: mktemp failed; failing closed per design" >&2
  exit 2
}
trap 'rm -f "$PATTERNS" "$DUMP"' EXIT
# Strip CR (the list may be edited from the Windows side) and blanks —
# a blank pattern would match everything.
if ! tr -d '\r' < "$TOKENS" | sed -e '/^[[:space:]]*$/d' > "$PATTERNS"; then
  echo "guard-history: cannot sanitize token list; failing closed per design" >&2
  exit 2
fi
if [ ! -s "$PATTERNS" ]; then
  echo "guard-history: sensitive-tokens carries no usable lines; check disabled" >&2
  exit 0
fi

# Full-history dump: messages + diffs of every revision reachable from
# any ref (local branches, tags, remotes). --no-color keeps the bytes
# grep sees identical to the blob bytes.
if ! git log --all -p --no-color > "$DUMP" 2>/dev/null; then
  echo "guard-history: cannot dump history; failing closed per design" >&2
  exit 2
fi

# Verdict first (grep -q: fixed strings, case-insensitive, text
# forced). If it fires, count matching lines and attribute them to
# commits: `git log` output opens each commit with a "commit <sha>"
# line, so the sha current at match time owns the match. Content is
# never printed.
if ! grep -iaFqf "$PATTERNS" "$DUMP"; then
  exit 0
fi
total="$(grep -iaFf "$PATTERNS" "$DUMP" | wc -l | tr -d '[:space:]')"
attrib="$(awk -v pat="$PATTERNS" '
  BEGIN {
    while ((getline t < pat) > 0) { npat++; low = tolower(t); if (low != "") pats[npat] = low }
    cur = "(before first commit header)"
  }
  /^commit / { cur = $2; next }
  {
    low = tolower($0)
    for (i = 1; i <= npat; i++) if (index(low, pats[i]) > 0) { hits[cur]++; break }
  }
  END { for (c in hits) print c, hits[c] }
' "$DUMP")"

echo "guard-history: BLOCKED — configured sensitive tokens appear in repository history" >&2
echo "  matching lines in the full log dump: $total" >&2
echo "  offending commits (sha + match count; message or diff):" >&2
printf '%s\n' "$attrib" | while IFS= read -r line; do
  [ -n "$line" ] && echo "    $line" >&2
done
echo "  Deleting the token in a later commit does not clean history —" >&2
echo "  rewrite it (git filter-repo runbook) or review the token list." >&2
echo "  Rule: scripts/hooks/README.md" >&2
exit 1
