# Decision: a server-less browser demo, published from `docs/`

## Question

Can the project have a try-it-now demo on GitHub Pages, which only serves static files?

## Decision

Yes: the unmodified production client runs against a stand-in for `server/api.py` that
lives in the page (`client/web/src/demo/demoServer.ts`). It patches `window.fetch`, so every
call to `/lesphotos-api/*` is answered from a snapshot instead of the network. Nothing in the
real app's code paths is forked; the demo only exists when the client is built with `VITE_DEMO=1`.

- **Snapshot**: `tools/export_demo.py` crawls a real running server and writes `docs/demo-data/`
  (timeline, photo ids, metadata, one thumbnail and preview per photo), so the demo shows exactly
  what the real API returns.
- **Edits**: favorite, rename, merge, create-moment and delete are applied to an in-memory copy
  and saved to `localStorage`. They survive a reload and never leave the browser. "Reset demo"
  in the corner badge clears them.
- **Published from `docs/`**: `client/web/scripts/build-demo.sh` builds the client into `docs/`
  (base path `/lesphotos/`) next to `docs/demo-data/`. In the repo's Pages settings choose
  *Deploy from a branch -> /docs*. `docs/.nojekyll` stops Jekyll touching the Markdown here.

## Rebuilding

```sh
# a real server with the demo library running on :8001
python tools/export_demo.py --base http://127.0.0.1:8001 --user demo --password '...' --out docs/demo-data
client/web/scripts/build-demo.sh
```

## What the demo is not

It shows the client, not the server's ~50MB footprint. Moment suggestions, auto-generate,
re-indexing and video/Live Photo playback answer 501/404 ("not available in the browser demo").

## Licensing

The snapshot contains Wikimedia Commons and Unsplash photos, credited in
[docs/images/CREDITS.md](../images/CREDITS.md), plus the author's own Thailand and Laos photos.
Previews are resized and carry no EXIF or GPS.
