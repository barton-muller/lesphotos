# Decision: full-screen photo viewer — progressive load + bounded full-res cache

> **Partly superseded.** The bounded-LRU design is current and still in `fullres.ts`, but
> full-res now comes from our own `/media/{id}/preview` rather than PhotoView's highRes URL,
> so there is no stored generated-suffix URL and no "photo with no full-res URL" case.

## Requirements (from the user)

- Clicking a photo opens it full-screen.
- Shows the already-cached thumbnail immediately (zoomed in), then swaps to full resolution
  once it loads — never a blank/loading screen when a thumbnail is already on-device.
- Before requesting the full-res image from the server, check if it's already available
  locally (the "original already on device" shortcut design, from the very first planning
  pass — client/sync_client.py already modeled this with local_originals/mark-local).
- Left/right arrow navigation between photos while in full-screen view.
- Cache *some* full-res images locally, but not unboundedly — expectation is that most
  recently-viewed photos are already on-device from normal browsing, so this is a bounded
  LRU-style cache, not "cache everything ever opened."
- A simple visible indicator of where the currently-shown image is actually coming from
  (server vs. local) — the user wants to be able to tell at a glance.

## Design

**Three-tier resolution ladder per photo:**
1. **Thumbnail (already in IndexedDB from sync)** — shown immediately on open, scaled up to
   fill the screen. This is what makes opening a photo feel instant even before anything else
   loads.
2. **Full-resolution original** — fetched from PhotoView's `highRes` URL (confirmed, in
   the archived plan.md's schema research, to serve the original file bytes directly, no per-request
   decode cost server-side) if not already cached locally.
3. **On-device original** (future — camera-roll-integration territory, not buildable yet
   without a real device/native layer) — the client/sync_client.py `local_originals` idea. Not
   reachable from a browser PWA today (no filesystem access to the phone's camera roll), so
   this tier is modeled in the cache design but the *source* of "already on device" for now is
   only "did we cache the full-res image ourselves," not "did the phone capture this photo."

**Bounded full-res cache**: a second Dexie table, `fullres`, separate from the existing
`photos` (thumbnail) table — keeping them apart means the always-wanted thumbnail cache is
never evicted to make room for full-res images. Simple LRU: track `lastAccessed` per cached
full-res blob, cap total entries (not total bytes, for simplicity - photo file sizes vary
too much for a byte cap to be a meaningful UX signal to size around) at a configurable number
(default 50 - "most recently viewed" is the expectation, not a large fraction of the library),
evict the least-recently-accessed entry when the cap is exceeded on insert.

**Source indicator**: a small badge/icon in the viewer's corner - "📱 Local" if serving from
the `fullres` cache (or, later, an actual on-device original), "☁️ Server" if it just fetched
from PhotoView and is showing that. Simple text/icon, not elaborate - the ask was for
something simple, not a detailed sync-status panel.

**Left/right navigation**: operates over the same flattened, date-ordered photo list the
Timeline already queries from IndexedDB (not a separate query) - opening photo N and pressing
right moves to photo N+1 in that same ordering, so viewer navigation always matches whatever
the grid was showing (respects the starred-only filter if it's active, for instance).

## What got built

- `client/web/src/db.ts`: new `fullres` table (`id`, `blob`, `lastAccessed`), v3 schema bump.
- `client/web/src/fullres.ts`: `getFullRes(photo)` - checks the `fullres` cache first, fetches
  from PhotoView's `highRes` URL on a miss, writes into the bounded cache, evicts oldest entries
  over the cap. Returns both the blob and which tier served it (for the source indicator).
- `client/web/src/PhotoViewer.tsx`: full-screen overlay component - shows the thumbnail blob
  immediately, kicks off `getFullRes()`, swaps the `<img>` src when it resolves, wires
  ArrowLeft/ArrowRight keyboard navigation and on-screen prev/next buttons, shows the source
  badge.
- `Timeline.tsx`: thumbnails become clickable, opening `PhotoViewer` at that photo's index
  within the currently-visible (possibly starred-only-filtered) ordered list.
