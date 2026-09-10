#!/usr/bin/env bash
# Measures PhotoView's RSS through an idle baseline, a full scan of the
# scaletest library, and post-scan settle - same methodology as the earlier
# 97-photo test, extended to ~2000 real, full-size phone photos.
set -euo pipefail

CONTAINER=server-photoview-1
API=http://localhost:8000/api/graphql

rss() {
  docker exec "$CONTAINER" sh -c "grep VmRSS /proc/1/status" | awk '{print $2}'
}

echo "== baseline (idle) =="
echo "RSS: $(rss) kB"

echo "== logging in as admin =="
TOKEN=$(curl -s "$API" -H "Content-Type: application/json" -d '{"query":"mutation($u:String!,$p:String!){ authorizeUser(username:$u,password:$p){token} }","variables":{"u":"admin","p":"admintestpass123"}}' | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['authorizeUser']['token'])")

echo "== triggering scanAll =="
curl -s "$API" -H "Content-Type: application/json" -H "Cookie: auth-token=$TOKEN" -d '{"query":"mutation{ scanAll{ finished success message } }"}'
echo ""

echo "== sampling RSS every 5s for up to 10 minutes, until it stabilizes =="
prev=""
stable_count=0
for i in $(seq 1 120); do
  current=$(rss)
  echo "t=$((i*5))s RSS: ${current} kB"
  if [ "$current" == "$prev" ]; then
    stable_count=$((stable_count+1))
  else
    stable_count=0
  fi
  if [ "$stable_count" -ge 6 ]; then
    echo "stable for 30s, stopping"
    break
  fi
  prev="$current"
  sleep 5
done

echo "== final docker stats =="
docker stats "$CONTAINER" --no-stream
