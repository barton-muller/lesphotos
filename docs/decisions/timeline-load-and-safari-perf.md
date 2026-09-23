# Decision: splitting thumbnails out of `photos`, and Safari's per-row IndexedDB cost

## Request

User reports, in order: the Timeline took ~15s to load on a ~26,000-photo real library;
returning to the app (foreground resync) was also slow; a Dexie `bulkPut` occasionally failed
with a Blob/File serialization error and the status bar showed "Offline" even though the
server was reachable; thumbnails flickered to a broken-image icon while scrolling; and,
specifically on Safari (desktop and iPad), the Timeline still took 10-15s to load even after
the first three fixes landed - a symptom Chrome never showed at all.

## Root cause 1: `thumbnailBlob` living inline on the `photos` row

`db.ts`'s `CachedPhoto` had an optional `thumbnailBlob?: Blob` field on the same row as every
metadata field. A comment above `PhotoMeta = Omit<CachedPhoto, 'thumbnailBlob'>` claimed this
"lets the Timeline load all ~28,000 rows at once... while thumbnails stream in" - but that's a
TypeScript-level type alias, not a schema-level separation. `Timeline.tsx`'s
`db.photos.toArray()` and `sync.ts`'s `db.photos.toArray()` (built for diffing on every sync,
including routine foreground ones) both deserialized every row's full `thumbnailBlob` off
IndexedDB before either function discarded it in a `.map()` or ignored it entirely. At real
scale, with `thumbnailBlob` populated on most/all rows, this dragged the whole thumbnail
library through deserialization on every Timeline render and every foreground resync.

**Fix:** a genuine schema split (`db.ts` v9). `thumbnailBlob` moved into its own `thumbnails`
table (`{ id, blob }`), and `CachedPhoto` is metadata-only. Every call site
(`Timeline.tsx`, `App.tsx`'s status-bar counts, `sync.ts`'s diffing and bulk-download writes,
`thumbnails.ts`'s per-tile read/write) was updated to read/write the new table. The v9
migration **clears** `photos` on upgrade rather than migrating blobs across, matching this
project's own v4-v8 precedent - an in-place migration would mean reading ~26,000 blobs into
the browser's JS heap during the upgrade transaction, on a device that might be a phone, for a
one-time, one-directional benefit. The next sync repopulates metadata (now cheap - JSON only)
and thumbnails refetch lazily per tile exactly as a first-ever sync already does.

This fix alone dropped the Timeline load from ~15s to under 1s **in Chrome**.

## Root cause 2: `bulkPut` carrying a stale Blob into a metadata-only write

`sync.ts`'s "updated" branch spread `...cached` (a full row, including whatever
`thumbnailBlob` it had) into the object passed to `bulkPut`, so a metadata-only change (a
rename, a favorite toggle) silently re-serialized an unrelated, already-cached thumbnail.
Structured-clone of certain Blob shapes (specifically ones built from a `Uint8Array` view over
a shared zip-response `ArrayBuffer`, see root cause 5 below) could fail mid-sync with
"Error preparing Blob/File data to be stored in object store."

Separately, `App.tsx`'s `runSync` catch block treated **any** non-401 error as
"server unreachable," including this purely local IndexedDB write failure - so a local storage
error showed up to the user as a false "Offline" indicator.

**Fix:** the schema split above makes the stale-Blob-in-bulkPut bug structurally impossible
(`CachedPhoto` has no blob field to carry). Separately, `sync.ts` gained
`isLocalStorageError()`, keyed on `err.name` (Dexie throws `BulkError`/`DexieError`
subclasses, not fetch/network errors), and `App.tsx`/`StatusBar.tsx` gained a distinct
`localStorageError` state, shown as "Local storage error: ..." rather than "Offline."

## Root cause 3: thumbnail eviction with no regard for what's on screen

`thumbnails.ts`'s in-memory object-URL cache (`entries`, keyed by photo id) evicted purely by
LRU sequence, with no check against `waiters` (the refcount of currently-mounted tiles).
During a fast scroll, the prefetch runway (~72 tiles either side) could insert enough new
entries between two visits to the same tile to push it out of pure-LRU order while it was
still on screen - `evictIfNeeded` would then `URL.revokeObjectURL` a URL an already-rendered
`<img src="blob:...">` was still pointing at, with nothing forcing that component to
re-render, so it showed the browser's broken-image icon until an unrelated state change
happened to redraw it.

**Fix:** `evictIfNeeded` filters eviction candidates to `!waiters.has(id)` first. If every
resident entry is currently on screen, it skips the eviction pass entirely rather than evict a
live tile - bounded by viewport size (via `waiters`, which shrinks as tiles unmount), so this
can't grow unbounded independent of screen real estate.

## Root cause 4: Safari's IndexedDB has a real per-row cost that Chrome doesn't

Even after the schema split, the user reported the Timeline still took 10-15s on Safari
(desktop and iPad), though Chrome was now under 1s. Measured directly:

- Raw `IndexedDB.getAll()` over all 26,423 rows: **13.5s** in real Safari (26.5.2), vs. under
  300ms in Chrome and in Playwright's bundled WebKit (which did **not** reproduce the slowdown
  - a real difference between installed Safari and Playwright's WebKit build, not something
  automatable tests could catch).
- Cursor iteration (`openCursor`, one row at a time): **13.8s** - essentially identical to
  `getAll()`, ruling out "bulk API vs. cursor" as the variable.
- A 500-row `getAll()`: **265ms** - linear (26,423 / 500 x 265ms ~ 14s matches the full-table
  measurement almost exactly), confirming a genuine **per-row** cost in Safari's IndexedDB
  backend at this table size, not a fixed per-transaction overhead. Extensions, iCloud Private
  Relay, and Web Inspector being open were all ruled out as causes.

No specific WebKit bug report matches this exact symptom (checked bugs.webkit.org and
Dexie's own Safari-quirks doc); it matches the general, long-documented pattern that Safari's
IndexedDB (SQLite-backed) scales worse than Chrome's (LevelDB-backed) at large row counts, and
that cursor/paginated reads are the standard community mitigation - not because they're
individually faster (they aren't, per the measurement above), but because they let the caller
render progressively instead of blocking on the whole table.

**Fix - first attempt (reverted):** chunked ALL ~26,000 rows into ~1,000-row pages via
`.offset().limit()`, re-sorting the whole accumulated array and calling `setPhotos()` after
every chunk (~27 times). This helped Safari (grid fills in progressively instead of one long
blank stall) but **regressed Chrome**: ~27 full-array sorts plus ~27 grid re-renders cost far
more than Chrome's already-fast read ever did, measured turning a ~1s load into ~6s.

**Fix - final:** a **two-phase** load instead of N chunks. Phase 1 reads just the newest
`FIRST_PAGE_SIZE` (500) rows via `orderBy('date').reverse().limit(500)` and commits
immediately - something on screen fast, in either browser, and ordered newest-first so the
common case (checking what was just added) is satisfied by phase 1 alone. Phase 2 reads
everything else in **one** bulk call (`orderBy('date').reverse().offset(500)`), same shape as
the original single-`toArray()` approach, and commits once. This keeps Chrome at its original
~1s (one extra small read plus one extra render, negligible) while giving Safari something
stable to look at within the time it takes to read 500 rows (~265ms) rather than the full
13.5s.

## A genuine bug found during this investigation: `Map` aliasing, not the Blob type

While debugging why "download all" thumbnails rendered as `[?]`/blank tiles after the schema
split, the actual cause turned out to be unrelated to any of the above: `thumbnails.ts`'s
`flushPendingGets()` (the function that coalesces same-tick `db.thumbnails.get(id)` calls into
one `bulkGet`, added to fix a separate Safari concurrency issue - see below) did:

```ts
const resolvers = pendingGets
pendingGets.clear()
```

`resolvers` was an **alias**, not a copy - `pendingGets.clear()` emptied the very same `Map`
object `resolvers` pointed to, so every `resolvers.get(id)` in the `.then()` callback that ran
moments later silently returned `undefined`, and no waiter's promise was ever resolved. Every
`loadThumbnailUrl()` call hung forever with no error, no rejection, and no console output -
"resolve nothing" isn't a throw. Traced by adding temporary logging at every step of the
promise chain until the exact line where execution silently stopped was found. Fixed with
`const resolvers = new Map(pendingGets)` - an actual snapshot taken before the clear.

## A second, real Safari-only concurrency cost: many small transactions

Separately from the per-row `getAll()` cost, an initial Timeline mount asks for every visible
tile plus the prefetch runway (~150+ ids) within the same render pass. Each `useThumbnail`
call used to open its own `db.thumbnails.get(id)` transaction - measured as 164 individual
transactions spread across a ~10s stall in real Safari, with the main thread's CPU usage
sitting at a flat **0%** for ~8.5 seconds between them (confirmed via a Safari Web Inspector
Timelines recording) - genuine idle time waiting on Safari's IndexedDB, not blocked
JavaScript. Batching these into one `bulkGet()` per tick (via `batchedGetThumbnail`/
`flushPendingGets`, `queueMicrotask`-scheduled) collapsed 164 individual transactions into 2
batched ones. This didn't by itself fix the Timeline's ~10-15s load (that was root cause 4,
the two-phase load above), but it does matter for scroll-time thumbnail loading, where a burst
of tile mounts still happens on every fling.

## The oldest-moment-year rail range: `date` disagreeing with `momentDate`

The two-phase load's "newest 500 first" ordering exposed a UI tell: the scrubber rail's
year range (`RailScrubber.tsx`'s `minYear`/`maxYear`, derived from `computeMonthBuckets`'
output) is computed from whatever's currently in `photos`, so it visibly grew from "just this
year" to the library's real span as phase 2 landed a few seconds later.

The first attempt at a fix queried `db.photos.orderBy('date').first()` - a single indexed
lookup - to get the true oldest date up front. This surfaced a real, pre-existing data issue:
an old camera with a wrong clock had `date` (EXIF capture time) of 1970, while its `moment`
(the folder it was correctly sorted into, established at index time) was `2001-01` - `date`
and `momentDate` can genuinely disagree, and `momentDate` is what the grid and scrubber
actually use for ordering. There is no index on `momentDate`, so finding the true oldest one
cheaply (without a full scan) isn't possible up front.

**Fix:** persist the real oldest `momentDate`'s year to `localStorage`
(`lesphotos-oldest-moment-year`) once phase 2 finishes and the whole library is in memory -
the same pattern `sync.ts` already uses for `lesphotos-last-timeline-count`. Every load after
the first uses the persisted value from the very first render, so the rail shows a stable
range immediately with no clamp or guess involved. A brand-new library (nothing persisted
yet) falls back to the rail's original behaviour - computing its range from whatever's loaded
so far - until phase 2 completes and writes the real value for next time. `RailScrubber`'s
`minYear` prop only ever widens the range downward from what `buckets` already shows, never
shrinks it, so a stale or wrong persisted value can't hide real data.

## Files touched

- `client/web/src/db.ts` - v9 schema (thumbnails table split), `subscribePhotosVersion`/
  `getPhotosVersion` (a manual per-write change signal, since this app doesn't use Dexie's
  `dexie-observable` addon that `useLiveQuery` would otherwise need to see table-level
  changes from hooks).
- `client/web/src/Timeline.tsx` - two-phase load, persisted oldest-moment-year.
- `client/web/src/sync.ts` - `isLocalStorageError`, thumbnails-table read/write sites.
- `client/web/src/thumbnails.ts` - `evictIfNeeded`'s waiter-aware eviction, batched
  `bulkGet`, the `Map`-aliasing fix.
- `client/web/src/App.tsx`, `client/web/src/StatusBar.tsx` - `localStorageError` state and
  display.
- `client/web/src/rail/RailScrubber.tsx`, `client/web/src/YearScrubber.tsx` - `minYear`
  override.

## Verified

- Chrome: Timeline load ~1s (both fresh and cached), matching pre-regression baseline.
- Safari (real device, not Playwright's WebKit): first 500 photos visible within ~300ms of
  sync completing; full library fills in over the remaining ~10-13s (root cause 4's
  per-row cost is a genuine platform limitation, not something client code can fix - only
  make less painful to wait through).
- "Download all for offline" thumbnails render correctly after the `Map`-aliasing fix, in
  both Chrome and Safari.
- Fast scroll-stress test: no broken-image icons observed after the waiter-aware eviction fix.
- Rail range: stable across reloads once one full load has completed and persisted the real
  oldest year; correctly reflects `momentDate` (2001) rather than a broken camera clock's
  EXIF `date` (1970).

## When to revisit

Root cause 4 (Safari's per-row IndexedDB cost) is a platform limitation, not a bug in this
codebase - if Safari ever fixes this class of slowdown, the two-phase load can simply be
reverted to a single bulk read without losing correctness, since phase 2's query is already
the same shape the original single read used.
