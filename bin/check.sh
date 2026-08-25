#!/usr/bin/env bash
# Everything that must hold before a commit. Run it by hand any time:
#
#   bin/check.sh
#
# Installed as a pre-commit hook by:
#
#   git config core.hooksPath bin/githooks
#
# Three layers, cheapest first. The type check is the one that would have
# caught a renamed store method left behind at a call site; the import check
# is the one that proves the package still loads at all.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PY=.venv/bin/python
RUFF=.venv/bin/ruff
PYRIGHT=.venv/bin/basedpyright

if [ ! -x "$PY" ]; then
    echo "❌ no .venv — create one: uv venv && uv pip install -r requirements.txt -r requirements-dev.txt"
    exit 1
fi

missing=""
[ -x "$RUFF" ] || missing="$missing ruff"
[ -x "$PYRIGHT" ] || missing="$missing basedpyright"
if [ -n "$missing" ]; then
    echo "❌ missing tools:$missing"
    echo "   uv pip install -r requirements-dev.txt"
    exit 1
fi

fail=0

echo "── ruff"
"$RUFF" check . || fail=1

echo "── basedpyright"
"$PYRIGHT" --outputjson >/tmp/ccbot-pyright.json 2>/dev/null
"$PY" - <<'INNER' || fail=1
import json, sys
with open("/tmp/ccbot-pyright.json") as fh:
    d = json.load(fh)
errs = [x for x in d["generalDiagnostics"] if x["severity"] == "error"]
for x in errs:
    f = x["file"].split("claude-tg-bot/")[-1]
    print(f"{f}:{x['range']['start']['line'] + 1}  {x['message'].splitlines()[0]}")
print(f"{len(errs)} errors")
sys.exit(1 if errs else 0)
INNER

echo "── import"
"$PY" -c "import main, ccbot.bot, ccbot.watcher, ccbot.state, ccbot.screen" || fail=1

# The bot reads the compiled .mo, never the .po a human edited. An edited .po
# left uncompiled is therefore invisible until someone switches language and
# sees the old wording, which is exactly the kind of silence to catch here.
echo "── locales"
for po in locales/*/LC_MESSAGES/bot.po; do
    [ -e "$po" ] || continue
    mo="${po%.po}.mo"
    name=$(basename "$(dirname "$(dirname "$po")")")
    if [ ! -f "$mo" ]; then
        echo "$name: bot.mo is missing — run: .venv/bin/pybabel compile -d locales -D bot"
        fail=1
    elif [ "$po" -nt "$mo" ]; then
        echo "$name: bot.po is newer than bot.mo — run: .venv/bin/pybabel compile -d locales -D bot"
        fail=1
    fi
    if grep -q '^#, fuzzy' "$po"; then
        echo "$name: fuzzy entries left in bot.po — review and drop the flag"
        fail=1
    fi
done
[ "$fail" -eq 0 ] && echo "locales ok"

if [ "$fail" -ne 0 ]; then
    echo "❌ checks failed — commit aborted"
    exit 1
fi
echo "✅ all clean"
