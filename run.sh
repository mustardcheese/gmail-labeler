#!/usr/bin/env bash
#
# One-command entry point for gmail-labeler. Bootstraps the virtualenv,
# installs dependencies, runs first-time setup if needed, then runs the
# labeler. Safe to re-run: each step is skipped when it's already done.
#
#   ./run.sh                              # process new mail once (default)
#   ./run.sh watch                        # poll continuously
#   ./run.sh watch --interval 300         # ...every 5 minutes
#   ./run.sh run --dry-run                # classify + log, touch nothing
#   ./run.sh setup                        # re-run interactive setup
#   ./run.sh clear                        # wipe stored API keys, then re-setup
#   ./run.sh backfill --query "in:inbox newer_than:6m"
#
# Force a dependency reinstall with:  REINSTALL=1 ./run.sh
#
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/.venv"
PY="$VENV/bin/python"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/gmail-labeler/config.json"

cd "$DIR"  # so `python -m gmail_labeler` finds the package

# 0. Load a local .env file (KEY=value) if present, so a project-local API key
#    reaches the labeler without exporting it in your shell. `[ -f ]` guards
#    against a stray `.env/` directory. See .env.example.
if [ -f "$DIR/.env" ]; then
    set -a
    . "$DIR/.env"
    set +a
fi

# 1. Ensure the virtualenv exists and dependencies are installed.
if [ ! -x "$PY" ]; then
    echo ">> Creating virtualenv at $VENV"
    python3 -m venv "$VENV"
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install --quiet -r "$DIR/requirements.txt"
elif [ "${REINSTALL:-}" = "1" ]; then
    echo ">> Reinstalling dependencies"
    "$PY" -m pip install --quiet -r "$DIR/requirements.txt"
fi

# 2. First run (no config yet): walk through interactive setup, unless the
#    user is already invoking a config-management command (setup/clear).
if [ ! -f "$CONFIG" ] && [ "${1:-}" != "setup" ] && [ "${1:-}" != "clear" ]; then
    echo ">> No config found at $CONFIG; starting one-time setup."
    "$PY" -m gmail_labeler setup
fi

# 3. Run the labeler. With no arguments, do a single pass ("run").
if [ "$#" -eq 0 ]; then
    exec "$PY" -m gmail_labeler run
fi
exec "$PY" -m gmail_labeler "$@"
