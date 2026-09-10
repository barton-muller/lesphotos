#!/bin/sh
# Starts the server. Used by the DSM scheduled task (see deploy/README.md).
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
export LESPHOTOS_SECRET LESPHOTOS_DATA_DIR LESPHOTOS_CLIENT_DIST LESPHOTOS_CORS_ORIGINS

UV="${UV:-$HOME/.local/bin/uv}"
[ -x "$UV" ] || { echo "uv not found at $UV - see deploy/README.md" >&2; exit 1; }

cd "$APP_DIR"
# --app-dir server so `import api` resolves; root_app (not app) is the one
# that serves both the API and the built client.
exec "$UV" run uvicorn api:root_app \
    --app-dir server \
    --host 0.0.0.0 \
    --port "${LESPHOTOS_PORT:-8001}"
