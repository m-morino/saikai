#!/usr/bin/env sh
# check-history.sh — pre-publish gate on git HISTORY.
#
# Exits non-zero if ANY commit in the range has:
#   1. an author OR committer email NOT on the allowlist
#      (*@users.noreply.github.com  or  noreply@anthropic.com), or
#   2. an author OR committer name containing non-ASCII bytes or a "/", or
#   3. (optional) any pattern from the out-of-repo file named by
#      $RECAP_HISTORY_DENY, matched against commit messages AND patch text.
#
# The allowlist IS the design: it rejects corporate / personal identities
# WITHOUT naming any of them, so this script is safe to publish. Keep real
# codenames in $RECAP_HISTORY_DENY — a file kept OUTSIDE the repo.
#
# Usage:
#   scripts/check-history.sh [<git-range>]              # default: HEAD (all history)
#   RECAP_HISTORY_DENY=~/private/deny.txt scripts/check-history.sh origin/main..HEAD
#
# Enable as a push-time gate (opt-in, not auto-installed):
#   git config core.hooksPath scripts/hooks
set -u

RANGE="${1:-HEAD}"
TAB=$(printf '\t')
fail=0

# --- 1 + 2: identity allowlist + name sanity (one commit per line) -----------
IDENT=$(git log "$RANGE" --format="%ae${TAB}%ce${TAB}%an${TAB}%cn") || {
    echo "check-history: 'git log $RANGE' failed" >&2
    exit 2
}
# A here-doc (not a pipe) keeps the while loop in THIS shell, so $fail persists.
OLDIFS=$IFS
while IFS="$TAB" read -r ae ce an cn; do
    [ -z "${ae}${ce}${an}${cn}" ] && continue
    for em in "$ae" "$ce"; do
        case "$em" in
            *@users.noreply.github.com) : ;;
            noreply@anthropic.com)      : ;;
            "")                         : ;;
            *) echo "DENY  email not on allowlist: $em"; fail=1 ;;
        esac
    done
    for nm in "$an" "$cn"; do
        if printf '%s' "$nm" | LC_ALL=C grep -q '[^ -~]'; then
            echo "DENY  non-ASCII name: $nm"; fail=1
        fi
        case "$nm" in
            */*) echo "DENY  name contains '/': $nm"; fail=1 ;;
        esac
    done
done <<EOF
$IDENT
EOF
IFS=$OLDIFS

# --- 3: optional deny-pattern scan (codenames live OUTSIDE the repo) ----------
if [ -n "${RECAP_HISTORY_DENY:-}" ]; then
    if [ -f "$RECAP_HISTORY_DENY" ]; then
        if git log "$RANGE" -p --no-color 2>/dev/null \
             | grep -I -i -f "$RECAP_HISTORY_DENY" >/dev/null 2>&1; then
            echo "DENY  \$RECAP_HISTORY_DENY pattern found in history (message or diff)"
            fail=1
        fi
    else
        echo "WARN  RECAP_HISTORY_DENY=$RECAP_HISTORY_DENY not found — skipping pattern scan" >&2
    fi
fi

if [ "$fail" -ne 0 ]; then
    echo "history check FAILED for range: $RANGE" >&2
    exit 1
fi
echo "history check OK ($RANGE)"
