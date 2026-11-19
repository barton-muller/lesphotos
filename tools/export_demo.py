#!/usr/bin/env python3
"""
Crawls a running lesphotos server and writes a static snapshot the browser-only
demo (client built with VITE_DEMO=1, see client/web/src/demo/demoServer.ts)
serves in place of the real API.

    python tools/export_demo.py --base http://127.0.0.1:8001 \
        --user demo --password '...' --out docs/demo-data

Output layout:
    timeline.json          every row /timeline returns, newest first
    photo-ids.json         path -> photo_id, as /photo-ids returns it
    metadata.json          photo_id -> /media/{id}/metadata
    thumbs/<id>.webp       one per photo
    previews/<id>.webp     one per photo
"""

import argparse
import http.cookiejar
import json
import shutil
import urllib.request
from pathlib import Path

PAGE = 500


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8001")
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    api = args.base.rstrip("/") + "/lesphotos-api"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(path: str, body=None) -> bytes:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            api + path, data=data, headers={"Content-Type": "application/json"} if data else {}
        )
        with opener.open(req) as r:
            return r.read()

    call("/login", {"username": args.user, "password": args.password})

    rows, offset = [], 0
    while True:
        page = json.loads(call(f"/timeline?limit={PAGE}&offset={offset}"))
        rows += page
        if len(page) < PAGE:
            break
        offset += PAGE

    out = args.out
    if out.exists():
        shutil.rmtree(out)
    for sub in ("thumbs", "previews"):
        (out / sub).mkdir(parents=True)

    metadata = {}
    for i, row in enumerate(rows, 1):
        pid = row["id"]
        (out / "thumbs" / f"{pid}.webp").write_bytes(call(f"/media/{pid}/thumb"))
        (out / "previews" / f"{pid}.webp").write_bytes(call(f"/media/{pid}/preview"))
        metadata[pid] = json.loads(call(f"/media/{pid}/metadata"))
        if i % 20 == 0 or i == len(rows):
            print(f"{i}/{len(rows)}")

    for row in rows:
        row["changedSeq"] = 1
    (out / "timeline.json").write_text(json.dumps(rows, separators=(",", ":")))
    (out / "photo-ids.json").write_text(call("/photo-ids").decode())
    (out / "metadata.json").write_text(json.dumps(metadata, separators=(",", ":")))
    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"{len(rows)} photos, {total / 1e6:.1f} MB -> {out}")


if __name__ == "__main__":
    main()
