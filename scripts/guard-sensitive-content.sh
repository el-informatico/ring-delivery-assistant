#!/bin/sh
# Sensitive-content guard for ring-delivery-assistant: refuses a commit
# whose STAGED ADDED LINES contain configured sensitive tokens —
# user-home paths, Windows-mount paths, sibling/local-infra project
# identifiers. Invoked by scripts/hooks/pre-commit (core.hooksPath
# wiring).
#
# INVARIANT ENFORCED: no NEW commit history may introduce configured
# sensitive tokens. Only ADDED lines of the staged diff are scanned, so
# a file that already carries a token under its current tracked form
# can still be committed as long as the staged change adds no new
# token-bearing line — pre-existing occurrences are handled by repo
# history hygiene, not by this guard.
#
# TOKEN LIST: literal fixed strings, one per line, blank lines ignored,
# matched case-insensitively as substrings, read from
#   $(git rev-parse --absolute-git-dir)/sensitive-tokens
# The tokens ARE the sensitive strings, so the list must never live in
# tracked content. The git dir is per-clone and never tracked; reseed
# it with a plain copy after a fresh clone (the seed file is kept
# outside the repository). A file rather than multi-valued git config
# so the seed copy can be diffed and cp'd back verbatim.
#
# EXIT CODES: Violation => exit 1. Guard-internal failure (no git dir,
# mktemp failure, diff listing failure, or tokens path present but not
# a readable regular file — chmod 000, a directory, a dangling symlink)
# => exit 2, failing closed per design. Clean, or check disabled
# (tokens file absent/empty) => exit 0 — fail-open bootstrap.
#
# HONEST BOUNDARY: `git commit --no-verify` bypasses it (it binds every
# session committing here otherwise); binary staged content is not
# scanned (git emits no content lines for it); a rename whose content
# changed surfaces as added lines and can trip it while a pure rename
# does not; a linked worktree would carry a different git dir and thus
# a different token list; the staged-file list is newline-delimited
# (POSIX sh has no NUL-safe read), so a staged path containing an
# embedded newline is not scanned — accepted limit; matching content is
# never printed or logged — file names and counts only.
#
# POSIX sh: no bashisms — runs under dash/busybox as well as bash.

set -u

GIT_DIR="$(git rev-parse --absolute-git-dir)" || {
  echo "guard-sensitive-content: cannot determine git dir" >&2
  exit 2
}
TOKENS="$GIT_DIR/sensitive-tokens"

if [ ! -e "$TOKENS" ] && [ ! -L "$TOKENS" ]; then
  echo "guard-sensitive-content: no sensitive-tokens list in the git dir; check disabled" >&2
  exit 0
fi
if [ ! -f "$TOKENS" ] || [ ! -r "$TOKENS" ]; then
  echo "guard-sensitive-content: sensitive-tokens exists but is not a readable file; failing closed per design" >&2
  exit 2
fi

PATTERNS="$(mktemp)" || {
  echo "guard-sensitive-content: mktemp failed; failing closed per design" >&2
  exit 2
}
NAMES="$(mktemp)" || {
  rm -f "$PATTERNS"
  echo "guard-sensitive-content: mktemp failed; failing closed per design" >&2
  exit 2
}
trap 'rm -f "$PATTERNS" "$NAMES"' EXIT
# Strip CR (the list may be edited from the Windows side) and blanks —
# a blank pattern would match everything.
if ! tr -d '\r' < "$TOKENS" | sed -e '/^[[:space:]]*$/d' > "$PATTERNS"; then
  echo "guard-sensitive-content: cannot sanitize token list; failing closed per design" >&2
  exit 2
fi
if [ ! -s "$PATTERNS" ]; then
  echo "guard-sensitive-content: sensitive-tokens carries no usable lines; check disabled" >&2
  exit 0
fi
# Staged files to scan (Added/Copied/Modified/Renamed). Newline-
# delimited: see the honest-boundary note above.
if ! git diff --cached --name-only --diff-filter=ACMR > "$NAMES"; then
  echo "guard-sensitive-content: cannot list staged files; failing closed per design" >&2
  exit 2
fi

fail=0
blocked=0
while IFS= read -r f || [ -n "$f" ]; do
  [ -n "$f" ] || continue
  # Added lines only, taken from INSIDE the hunks (everything after the
  # first @@ line — this diff is per-file, so exactly one +++ b/<path>
  # header set exists and it always precedes the first hunk): the
  # header can never be mistaken for content — and genuine added lines
  # that BEGIN with '+' (quoted diff text: content "++ token" reaches
  # the diff as "+++ token", exactly header-shaped) are still scanned.
  # Strip exactly the leading '+' from each. -i: tokens are stored
  # lowercase and matched case-insensitively (so any capitalization of
  # a token is caught); -a: never let content be treated as opaque
  # binary.
  added="$(git diff --cached -U0 -- "$f" \
           | awk '/^@@/{inhunk=1; next} inhunk && /^\+/{sub(/^./,""); print}')"
  [ -n "$added" ] || continue
  if printf '%s\n' "$added" | grep -iaFqf "$PATTERNS"; then
    echo "guard-sensitive-content: BLOCKED — staged additions in $f" >&2
    echo "  contain configured sensitive tokens." >&2
    fail=1
    blocked=$((blocked + 1))
  fi
done < "$NAMES"

if [ "$fail" -ne 0 ]; then
  echo "  Rewrite the flagged additions with tokenized/category references" >&2
  echo "  and re-stage; do not use --no-verify. Rule: scripts/hooks/README.md" >&2
  exit 1
fi
exit 0
