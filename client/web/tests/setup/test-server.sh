#!/usr/bin/env bash
# Bootstraps and runs an isolated lesphotos instance for the @playwright/test
# suite: its own data dir, its own throwaway port (never :8001/:5173 - those
# are the developer's own long-running dev instances, see CLAUDE.md's "Dev
# server ports"), a small fakelib library, and one test account. Idempotent -
# safe to re-run; only regenerates what's missing.
#
# playwright.config.ts's webServer runs this directly. It builds and serves
# the real production client (api:root_app), not `vite dev` - matching how
# CLAUDE.md says validation should happen ("driving the real running app"),
# and sidestepping the vite proxy entirely (one process, one port).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../../.." && pwd)"
DATA_DIR="$REPO_ROOT/client/web/tests/.data"
PHOTOS_ROOT="$DATA_DIR/photos/e2e"
PORT="${LESPHOTOS_TEST_PORT:-8199}"

export LESPHOTOS_DATA_DIR="$DATA_DIR"
export LESPHOTOS_SECRET="e2e-test-secret-not-for-production-$(echo -n "$DATA_DIR" | md5sum | cut -c1-16)"
PY="$REPO_ROOT/.venv/bin/python"

mkdir -p "$DATA_DIR"

# Client: build once, or if the source is newer than the last build. The
# server (api:root_app) serves this built dist directly - see CLAUDE.md,
# "Serving is api:root_app ... not api:app".
cd "$REPO_ROOT/client/web"
if [ ! -f dist/index.html ] || find src -newer dist/index.html -print -quit | grep -q .; then
  echo "[test-server] building client..." >&2
  npm run build >&2
fi

cd "$REPO_ROOT/server"

# Fake library: only generate if missing. --moments-per-year/--photos-per-
# moment kept small - this is a functional smoke suite, not the volume
# benchmark (that's tools/bench.py against a real library, see
# docs/testing-guide.md).
if [ ! -d "$PHOTOS_ROOT" ]; then
  echo "[test-server] generating fakelib test library..." >&2
  "$PY" fakelib/generate_history_backfill.py \
    --user e2e --moments-per-year 3 --photos-per-moment 8 \
    2>&1 | while read -r line; do echo "  $line" >&2; done
  # generate_history_backfill.py writes under server/data/photos/<user> by
  # its own PHOTOS_ROOT constant (relative to fakelib/, not cwd or
  # LESPHOTOS_DATA_DIR) - move it into our isolated data dir so it never
  # touches (or gets confused with) the developer's real server/data.
  mkdir -p "$DATA_DIR/photos"
  mv "$REPO_ROOT/server/data/photos/e2e" "$PHOTOS_ROOT"
fi

# Account: idempotent. add-user exits non-zero if it already exists (see
# manage.py's cmd_add_user), which is expected on every re-run after the
# first - not a real failure, so don't let set -e kill the script over it.
"$PY" manage.py add-user e2e "$PHOTOS_ROOT" --password e2e-test-password-123 >&2 \
  || echo "[test-server] user e2e already exists, continuing" >&2

# Index (idempotent/incremental - see CLAUDE.md's "--no-derive run at
# filesystem speed" note; re-running after the first is fast either way).
echo "[test-server] indexing..." >&2
"$PY" indexer.py --user e2e >&2

echo "[test-server] starting on :$PORT (data dir: $DATA_DIR)" >&2
exec "$REPO_ROOT/.venv/bin/uvicorn" api:root_app --app-dir "$REPO_ROOT/server" \
  --host 127.0.0.1 --port "$PORT"
