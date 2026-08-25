#!/bin/bash
# Tee the status-line payload, then hand it to the real status line unchanged.
#
# Claude Code feeds the status line a JSON document with exact numbers
# (context_window.used_percentage, rate_limits.*.used_percentage/resets_at).
# Scraping those figures back out of the rendered bars is lossy, so the bot
# reads this file instead.
#
# Install: point statusLine.command in ~/.claude/settings.json at this script,
# or just run ./install.sh, which does it and keeps a backup.
#
# If you already have a status line, set CCBOT_REAL_STATUSLINE to it (or leave
# the default path) and it keeps working untouched. Since statusLine.command is
# not guaranteed to be run through a shell, the installer does not inline that
# variable there: it generates a one-line wrapper in ~/.config/ccbot/ and points
# the setting at that instead.
#
# Every failure path is non-fatal: whatever happens, the status line still
# renders and Claude Code never sees an error from here.

REAL="${CCBOT_REAL_STATUSLINE:-}"
# The conventional path, used only when it actually exists: an unset variable
# must fall through to the minimal status line, not to a missing file.
if [ -z "$REAL" ] && [ -x "$HOME/.claude/statusline-command.sh" ]; then
    REAL="$HOME/.claude/statusline-command.sh"
fi
DIR="${XDG_CACHE_HOME:-$HOME/.cache}/ccbot/status"

input=$(cat)

{
    sid=$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null)
    if [ -n "$sid" ] && [ "$sid" != "null" ]; then
        mkdir -p "$DIR"
        printf '%s' "$input" > "$DIR/$sid.json.tmp" \
            && mv "$DIR/$sid.json.tmp" "$DIR/$sid.json"
    fi
} 2>/dev/null || true

if [ -x "$REAL" ]; then
    printf '%s' "$input" | "$REAL"
elif [ -n "$REAL" ]; then
    # Not a plain executable: a status line may legitimately be a command with
    # arguments ("npx ccusage statusline"), so fall back to a shell.
    printf '%s' "$input" | sh -c "$REAL"
else
    # No status line of your own: print a minimal one rather than nothing.
    printf '%s' "$input" | jq -r '
        [ (.model.display_name // empty),
          (if .context_window.used_percentage then
             "ctx \(.context_window.used_percentage | floor)%" else empty end)
        ] | map(select(. != "")) | join(" | ")' 2>/dev/null
fi
