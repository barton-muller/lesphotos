#!/usr/bin/env bash
# Builds the browser-only demo into the repo's docs/ folder, which GitHub
# Pages serves (Settings -> Pages -> Deploy from a branch -> /docs).
#
#   tools/export_demo.py  writes docs/demo-data/  (run it first, against a real server)
#   this script           writes the client itself next to it
#
# Only the files this script owns are replaced; docs/*.md, docs/decisions/,
# docs/images/ and docs/demo-data/ are left alone.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB="$(cd "$HERE/.." && pwd)"
DOCS="$(cd "$WEB/../.." && pwd)/docs"

if [ ! -f "$DOCS/demo-data/timeline.json" ]; then
  echo "docs/demo-data is missing - run tools/export_demo.py first" >&2
  exit 1
fi

cd "$WEB"
VITE_DEMO=1 npx tsc -b
rm -rf "$WEB/dist-demo"
VITE_DEMO=1 npx vite build --outDir dist-demo --emptyOutDir

rm -rf "$DOCS/assets" "$DOCS/index.html" "$DOCS/sw.js" "$DOCS/registerSW.js" \
       "$DOCS/manifest.webmanifest" "$DOCS"/workbox-*.js
cp -R "$WEB/dist-demo/." "$DOCS/"
rm -rf "$WEB/dist-demo"
touch "$DOCS/.nojekyll"
echo "demo written to $DOCS"
