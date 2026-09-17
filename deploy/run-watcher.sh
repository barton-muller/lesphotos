#!/bin/sh
# Starts the upload watcher. Used by its own DSM scheduled task, separate
# from run.sh/the API server - see deploy/README.md and server/watcher.py's
# own docstring for why this is a second process rather than folded into
# the server.
#
# sh, not bash: DSM's task scheduler runs a minimal shell and its
# environment is not a login shell, so nothing from .profile is set -
# including PATH, which is why uv is called by absolute path below.
set -e

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$APP_DIR/deploy/lesphotos.env"

[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (copy lesphotos.env.example)" >&2; exit 1; }

# shellcheck disable=SC1090
. "$ENV_FILE"
# The watcher only ever calls auth.py/indexer.py directly, never the API
# over HTTP, so it needs LESPHOTOS_DATA_DIR (to find auth.sqlite3 and each
# user's watch_paths) but none of the API-serving variables.
export LESPHOTOS_DATA_DIR

UV="${UV:-$HOME/.local/bin/uv}"
[ -x "$UV" ] || { echo "uv not found at $UV - see deploy/README.md" >&2; exit 1; }

cd "$APP_DIR"
exec "$UV" run --directory server python3 watcher.py
