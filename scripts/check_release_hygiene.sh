#!/usr/bin/env bash
# OSS-001 Deliverable: automated release hygiene checks.
#
# Usage: check_release_hygiene.sh <export-dir>
# Scans a public-export candidate tree. Exit 0 = clean, exit 1 = violations.
# Run locally before repo creation and in every public repo's CI.
#
# Checks:
#   H1 committed secrets / private keys
#   H2 forbidden files (runtime .env, SQL backups/dumps with data, runtime state)
#   H3 dependency/build directories
#   H4 production domains, emails, identifiers, fixture markers
#   H5 license allowlist (requires license tooling; skipped with warning if absent)
#   H6 public-to-Enterprise imports

set -u
TARGET="${1:?usage: check_release_hygiene.sh <export-dir>}"
cd "$TARGET" || exit 2
FAIL=0

say()  { printf '%s\n' "$*"; }

# Reviewed exceptions: a .hygiene-allow file at the export root may contain
# lines of the form "H6 path/to/file" to suppress one check for one file.
allowed() { # $1=check $2=file
  [ -f .hygiene-allow ] && grep -qxF "$1 $2" .hygiene-allow
}
viol() { # $1=check $2=message $3=file(optional, for allowlist)
  if [ -n "${3:-}" ] && allowed "$1" "$3"; then
    say "ALLOWED [$1] $3 (listed in .hygiene-allow)"
    return
  fi
  FAIL=1; say "VIOLATION [$1] $2"
}

# File list: tracked files if a git repo, else all files.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  FILES=$(git ls-files)
else
  FILES=$(find . -type f | sed 's|^\./||')
fi

# --- H1: secrets / private keys -------------------------------------------
# POSIX ERE only (BSD grep has no -P). amp_k_test_ fixture keys are allowed.
H1_HARD='-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY|AKIA[0-9A-Z]{16}|xox[baprs]-[0-9A-Za-z-]{10,}|sk-[A-Za-z0-9_-]{20,}|re_[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{36}'
while IFS= read -r f; do
  [ -f "$f" ] || continue
  if grep -IEq "$H1_HARD" "$f" 2>/dev/null; then
    viol H1 "possible secret in $f" "$f"
  elif grep -IEo 'amp_k_[A-Za-z0-9_]{10,}' "$f" 2>/dev/null | grep -vq '^amp_k_test_'; then
    viol H1 "possible AMP API key in $f" "$f"
  fi
done <<< "$FILES"

# --- H2: forbidden file patterns ------------------------------------------
H2_HITS=$(printf '%s\n' "$FILES" | grep -Ei '(^|/)\.env(\..+)?$|(^|/)(backups?|db-backups?)/|(^|/)[^/]*(backup|dump)[^/]*\.(sql|dump|bak|backup|mysqldump)(\.(gz|bz2|xz|zip|zst))?$|\.(mysqldump|sql\.(gz|bz2|xz|zip|zst))$|\.sqlite3?$|(^|/)state\.json$|\.pem$|\.key$' | grep -vE '\.env\.example$|\.env\.template$')
[ -n "$H2_HITS" ] && while IFS= read -r f; do viol H2 "forbidden file: $f" "$f"; done <<< "$H2_HITS"
# SQL files containing row data are forbidden even if not named "backup"
for f in $(printf '%s\n' "$FILES" | grep -E '\.sql$'); do
  grep -qE '^\s*INSERT INTO' "$f" 2>/dev/null && viol H2 "SQL file contains row data (INSERT): $f" "$f"
done

# --- H3: dependency / build directories -----------------------------------
H3_HITS=$(printf '%s\n' "$FILES" | grep -E '(^|/)(node_modules|__pycache__|\.venv|venv|dist|build)/' )
[ -n "$H3_HITS" ] && viol H3 "dependency/build artifacts tracked: $(printf '%s\n' "$H3_HITS" | head -3 | tr '\n' ' ')... ($(printf '%s\n' "$H3_HITS" | wc -l | tr -d ' ') files)"

# --- H4: production markers -----------------------------------------------
H4_PATTERN='inquiryon\.com|jeshua[._-]?cheng|amp_proper_backup|amp_manual_backup'
H4_HITS=$(printf '%s\n' "$FILES" | xargs -I{} grep -lIE "$H4_PATTERN" "{}" 2>/dev/null)
[ -n "$H4_HITS" ] && while IFS= read -r f; do viol H4 "production marker in $f" "$f"; done <<< "$H4_HITS"

# --- H5: license allowlist ------------------------------------------------
ALLOWED='MIT|Apache-2.0|BSD-2-Clause|BSD-3-Clause|ISC|CC-BY-4.0|CC0-1.0|Unlicense|Python-2.0'
if [ -f package.json ] && command -v npx >/dev/null 2>&1; then
  BAD=$(npx --yes license-checker --production --onlyAllow "$(echo $ALLOWED | tr '|' ';')" 2>&1 >/dev/null | grep -iv '^npm warn' | grep -i 'license\|not allowed' | head -3)
  [ -n "$BAD" ] && viol H5 "npm dependency license violation: $BAD"
elif [ -f pyproject.toml ] || [ -f requirements.txt ]; then
  if command -v pip-licenses >/dev/null 2>&1; then
    BAD=$(pip-licenses --format=csv 2>/dev/null | tail -n +2 | grep -vE "$ALLOWED" || true)
    [ -n "$BAD" ] && viol H5 "python dependency license violation: $BAD"
  else
    say "WARN [H5] pip-licenses not installed; python license check skipped"
  fi
else
  say "INFO [H5] no dependency manifest found; license check not applicable"
fi

# --- H6: public-to-Enterprise imports -------------------------------------
H6_PATTERN='@amp/rlhf-events|file:\.\./rlhf|from rlhf|import rlhf|progressive[_-]autonomy|autonomy_ops_catalog|inquiryon_amp_backend'
H6_HITS=$(printf '%s\n' "$FILES" | grep -E '\.(js|ts|jsx|tsx|py|json)$' | xargs -I{} grep -lIE "$H6_PATTERN" "{}" 2>/dev/null)
[ -n "$H6_HITS" ] && while IFS= read -r f; do viol H6 "possible Enterprise import/reference in $f" "$f"; done <<< "$H6_HITS"

# ---------------------------------------------------------------------------
if [ "$FAIL" -eq 0 ]; then
  say "OK: release hygiene checks passed for $TARGET"
else
  say "FAILED: release hygiene violations found in $TARGET"
fi
exit $FAIL
