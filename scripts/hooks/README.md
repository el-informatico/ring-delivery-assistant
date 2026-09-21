# Git hooks — triple validation at commit time, one at push time

This repository enforces three mechanical guards on every `git commit`,
plus one on every `git push`. They are tripwires, not containment:
`--no-verify` bypasses them, so the matching behavior rule (below)
matters as much as the hooks themselves.

## The three validations

1. **No AI attribution in commit messages** (`hooks/commit-msg`):
   rejects `Co-Authored-By:` trailers naming an AI vendor
   (an alternation of well-known AI vendor/product names),
   "(generated|written|authored|assisted) with/by <vendor>" phrasing,
   and the robot emoji. These patterns are shape-based: a subject
   *mentioning* a vendor and human `Co-Authored-By:` trailers pass the
   pattern check. (If a local token list also carries vendor names —
   a strict zero-mention policy — the message token check below blocks
   any mention; the list decides. This repo's seed list deliberately
   carries none — see the bootstrap note.)
2. **No host-internal paths in staged added lines**
   (`guard-sensitive-content.sh`): absolute paths from the developer's
   machine (`<home-dir>/<user>`, `<unix-windows-mount>/...`,
   `<drive>:\Users\...` and similar).
3. **No sibling/local-infra project names in staged added lines**
   (same guard): identifiers of other projects that live on the
   developer's machine.

Validations 2 and 3 read a list of literal tokens from
`$(git rev-parse --absolute-git-dir)/sensitive-tokens` — one string per
line, matched case-insensitively as substrings, only in ADDED lines of
the staged diff. That file lives inside `.git/`, so it is per-clone and
**never tracked or pushed**; the tokens themselves are the sensitive
strings and must not appear in the repository. The same list is also
checked against the commit *message* by `hooks/commit-msg`.

## Push-time validation

4. **No sensitive tokens anywhere in repository history**
   (`guard-history.sh`, run by `hooks/pre-push`): the full
   `git log --all -p` dump — every commit message and every revision
   diff reachable from any ref — is matched against the same local
   token list. This closes the two blind spots of validations 2–3:
   history that predates the tree guard, and commit messages (no
   commit-time diff scan covers them). A hit blocks the push even if
   a later commit deleted the token — the old revision still carries
   it, so cleaning up means a history rewrite, not a new commit.

## Files

| File | Role |
|---|---|
| `hooks/commit-msg` | validation 1 + sensitive tokens in the message |
| `hooks/pre-commit` | runs the tree guard below |
| `../guard-sensitive-content.sh` | validations 2 + 3 over staged added lines |
| `hooks/pre-push` | runs the history guard below |
| `../guard-history.sh` | validation 4 over the full history dump |

## Install (per clone)

```sh
git config core.hooksPath scripts/hooks
chmod +x scripts/hooks/commit-msg scripts/hooks/pre-commit \
         scripts/hooks/pre-push scripts/guard-sensitive-content.sh \
         scripts/guard-history.sh
```

Then create the local token list (adjust to your machine — every line
is a literal substring to block):

```sh
cat > "$(git rev-parse --absolute-git-dir)/sensitive-tokens" <<'EOF'
<home-dir>/<user>
<user>
<unix-windows-mount>
<drive>:\users
<sibling-project-name>
EOF
chmod 600 "$(git rev-parse --absolute-git-dir)/sensitive-tokens"
```

Seed files with real token lists stay OUTSIDE the repository (or inside
`.git/`); never commit one. Do NOT add AI-vendor names to the list:
`hooks/commit-msg` contains them in its detection regex, so seeding
them would block every future edit to the hook itself.

## Bootstrap note

The commit that introduced these guard scripts went through the hooks
themselves (no `--no-verify`): the sensitive-token seed deliberately
excludes AI-vendor words — which the commit-msg detection regex
necessarily contains — so the content guard does not fire on the
hooks' own source.

## Exit codes and failure mode

- `0` — clean, or check disabled (token list absent or empty:
  fail-open bootstrap, so a fresh clone without a list still commits).
- `1` — violation: rewrite the flagged content and re-stage.
- `2` — guard-internal failure (no git dir, `mktemp` failure, or a
  token list that exists but is not a readable regular file): the
  commit is REFUSED — fail-closed.

The push-time guard (validation 4) uses the same codes with the same
semantics — `1` = token in history, `2` = internal failure — and the
push is refused either way.

## Behavior rules (the hooks only tripwire these)

- Never use `git commit --no-verify` in this repository.
- Never write AI-attribution trailers or attribution footers into
  commits or content in the first place.
- Deliverables that must quote sensitive strings for audit/planning
  belong outside the working tree; in-repo text uses category
  references ("the developer's home directory", "a sibling project"),
  not the raw strings.

## Honest limits

`--no-verify` bypasses everything (`git commit --no-verify`, and for
the push-time guard `git push --no-verify`). Binary staged content is
not scanned. Only ADDED lines are checked — a file that already carries a
token under its tracked form is not re-flagged until a staged change
adds a new token-bearing line. The history guard scans the whole dump
as text, so a token split across a binary/encoded boundary escapes it,
and it attributes matches to commits (sha + count), never printing the
matched content; file/line attribution needs the offline attributed
scanner. The staged-file list is newline-delimited,
so a staged path containing an embedded newline is not scanned.
