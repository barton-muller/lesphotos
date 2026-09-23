import Dexie, { type EntityTable } from 'dexie'

// Local thumbnail cache - the whole point of the "keep the complete
// thumbnail library on-device" design (see plan.md). Keyed by the server's
// own stable photo_id (server/appdb, see docs/decisions/stable-photo-id.md)
// - NOT PhotoView's own media id, which is MD5(path)-derived and changes on
// every move/rename (confirmed: this caused moved photos to render twice,
// once under the stale old-id row and once under the new one, until the
// next full resync happened to prune the orphan - not reliably, in
// practice). photo_id survives moves/renames/merges since the server mints
// it once per photo and keeps it forever.
/**
 * Everything about a photo except its image data.
 *
 * The grid lays out from this alone. `thumbnailBlob` used to live inline on
 * this same row (see v9's migration note below) - that made every full-table
 * read (Timeline's live query, a routine foreground resync's cachedById
 * diff) deserialize every cached thumbnail just to discard it a moment
 * later, which was the actual cost behind both a slow Timeline load and a
 * slow "check for new photos" at the real ~28,000-photo/user target. Blobs
 * now live in their own `thumbnails` table, so `CachedPhoto` itself is
 * metadata-only and this alias is just for readability at call sites that
 * only ever wanted the metadata shape.
 */
export type PhotoMeta = CachedPhoto

export interface CachedPhoto {
  id: string
  path: string
  moment: string
  momentPath: string
  // The server's own moment sort key (media.moment_date) - not always
  // derivable from `moment` alone: a month-only bucket folder ("2026-09")
  // has its moment_date rebalanced to its newest photo's date rather than
  // defaulting to day=1 (see indexer.rebalance_month_bucket_dates
  // server-side). null only when the moment has no parseable date prefix
  // at all. Timeline.tsx sorts on this, not on `moment` text.
  momentDate: string | null
  date: string
  // 0 | 1, not boolean: IndexedDB's native key types (see IndexableType)
  // don't include boolean at all, so a boolean-typed field can't be used
  // with .where('favorite').equals(...) - matches server/appdb's own
  // `favorite INTEGER` column for the same reason.
  favorite: 0 | 1
  width: number
  height: number
  // Videos render as a normal tile with a play badge and duration; the
  // original file streams from /media/<id>/original via range requests
  // rather than being cached as a blob (see PhotoViewer).
  isVideo: 0 | 1
  duration: number | null
  // A still with a short clip attached - iPhone Live Photo or Pixel
  // Motion Photo. 0|1, not boolean: IndexedDB cannot index booleans, so
  // a boolean-typed field silently cannot be queried by .equals().
  hasMotion: 0 | 1
}

// Bounded full-resolution image cache, kept in a separate table from the
// always-wanted thumbnail cache so a full-res LRU eviction never touches
// the thumbnails - see docs/decisions/fullscreen-viewer-cache.md.
export interface CachedFullRes {
  id: string
  blob: Blob
  lastAccessed: number
}

// A row exists here only once its pixels have actually been fetched - see
// thumbnails.ts. A photo with no row here yet is not an error, just not
// downloaded; the grid fetches it lazily as its tile scrolls into view.
export interface CachedThumbnail {
  id: string
  blob: Blob
}

export const db = new Dexie('lesphotos') as Dexie & {
  photos: EntityTable<CachedPhoto, 'id'>
  fullres: EntityTable<CachedFullRes, 'id'>
  thumbnails: EntityTable<CachedThumbnail, 'id'>
}

db.version(1).stores({
  photos: 'id, moment, date',
})

// v2: index `favorite` so a starred-only filter (planned - see plan.md's
// "Future UI requirements") can use db.photos.where('favorite').equals(1)
// instead of a full-table .filter() scan. Fine either way at today's
// ~2,000-photo test scale, but worth having in place before the real
// ~28,000-photo/user target makes the difference actually matter.
//
// The .upgrade() step matters, confirmed by testing: rows written under v1
// stored `favorite` as a real boolean, and IndexedDB's native key types
// don't include boolean at all - a plain schema bump does NOT retroactively
// fix existing row values, only adds the index. Without this migration, any
// pre-existing true/false row is silently excluded from every
// .where('favorite').equals(1) query even though it's still sitting right
// there in a .toArray()/.getAll() scan - reproduced directly against a
// browser seeded with old-shaped data before adding this.
db.version(2)
  .stores({
    photos: 'id, moment, date, favorite',
  })
  .upgrade(async (tx) => {
    await tx
      .table('photos')
      .toCollection()
      .modify((photo) => {
        photo.favorite = photo.favorite ? 1 : 0
      })
  })

// v3: adds the bounded full-resolution cache for the full-screen photo
// viewer (see docs/decisions/fullscreen-viewer-cache.md). `lastAccessed` is
// indexed so the LRU eviction in fullres.ts can find the oldest entry
// without a full scan.
db.version(3).stores({
  photos: 'id, moment, date, favorite',
  fullres: 'id, lastAccessed',
})

// v4: `id` now means the server's stable photo_id, not PhotoView's own
// (path-derived, move-unstable) id - see docs/decisions/stable-photo-id.md.
// Existing v3 rows are keyed by the old id scheme and have no valid mapping
// to the new one, so this clears the table rather than trying to migrate
// row-by-row - a thumbnail cache, not a source of truth, so losing it just
// means the next sync re-downloads everything (cheap, and the fix this
// migration exists for: the bug it fixes was exactly rows that a normal
// sync wasn't reliably clearing).
db.version(4)
  .stores({
    photos: 'id, moment, date, favorite',
    fullres: 'id, lastAccessed',
  })
  .upgrade(async (tx) => {
    await tx.table('photos').clear()
  })

// v5: adds the required photoviewId field (see CachedPhoto's own comment).
// v4 rows have no value for it at all, not just a stale one - same
// disposable-cache reasoning as v4's own migration, clear and let the next
// sync repopulate everything with the new field present.
db.version(5)
  .stores({
    photos: 'id, moment, date, favorite',
    fullres: 'id, lastAccessed',
  })
  .upgrade(async (tx) => {
    await tx.table('photos').clear()
  })

// v6: the media backend is now server/api.py's own /timeline, not
// PhotoView (see docs/decisions/replacing-photoview.md). photoviewId is
// gone - nothing queries PhotoView by id any more - and highResUrl with
// it, since derivative URLs are now deterministic from photo_id
// (/media/<id>/preview) rather than carrying a generated suffix that had
// to be stored. isVideo/duration are new.
//
// Clears the table for the same reason v4 and v5 did: this is a thumbnail
// cache, not a source of truth, and existing rows lack the new fields
// entirely rather than merely holding stale values. A one-time full resync
// is cheap and correct; a per-row migration would be neither.
db.version(6)
  .stores({
    photos: 'id, moment, date, favorite',
    fullres: 'id, lastAccessed',
  })
  .upgrade(async (tx) => {
    await tx.table('photos').clear()
  })

// hasMotion is new. Clearing rather than back-filling: a bump only adds
// the index, never rewrites stored values (CLAUDE.md), and every existing
// row predates the field entirely - the next sync repopulates.
db.version(7)
  .stores({
    photos: 'id, moment, date, favorite',
    fullres: 'id, lastAccessed',
  })
  .upgrade(async (tx) => {
    await tx.table('photos').clear()
  })

// v8: momentDate is new (server started sending it in /timeline - see
// api.py's timeline()). Existing rows predate the field, so the Timeline's
// sort would fall back to `undefined` for every cached photo until the
// next sync - clearing rather than back-filling, same reasoning as v7.
db.version(8)
  .stores({
    photos: 'id, moment, date, favorite',
    fullres: 'id, lastAccessed',
  })
  .upgrade(async (tx) => {
    await tx.table('photos').clear()
  })

// v9: thumbnailBlob moves out of `photos` into its own `thumbnails` table,
// keyed by the same id, so a metadata-only read (Timeline's live query, a
// routine foreground resync's cachedById diff, the status bar's counts)
// never deserializes image bytes off IndexedDB - see CLAUDE.md's
// "thumbnailBlob is optional" invariant, which this makes structurally
// true rather than a JS-side convention a full-table read still paid for.
//
// Clearing rather than migrating existing blobs across, same reasoning as
// v4-v8: an in-place copy would mean reading all ~28,000 blobs into this
// tab's JS heap during the upgrade transaction, on whatever device happens
// to run it - including a phone. Letting the next sync (cheap, JSON-only
// after this fix) repopulate `photos` and each tile lazily refetch its own
// thumbnail costs a one-time re-download, not a migration bug surface.
db.version(9)
  .stores({
    photos: 'id, moment, date, favorite',
    fullres: 'id, lastAccessed',
    thumbnails: 'id',
  })
  .upgrade(async (tx) => {
    await tx.table('photos').clear()
  })

// A cheap "something changed" signal for `photos`, bumped by every
// create/update/delete regardless of whether row COUNT changed. Timeline.tsx
// reads this table in chunks now (see its own comment) rather than through
// a single useLiveQuery(() => db.photos.toArray()), so it needs its own
// trigger to know when to re-read - a plain row count isn't enough, since a
// rename, favorite toggle, or hasMotion flip changes a row's contents
// without changing how many rows exist, and CLAUDE.md's own sync.ts
// diffing exists precisely to catch that class of change.
//
// Dexie's requestBuild change tracking (the same mechanism useLiveQuery
// subscribes to) requires the `dexie-observable` addon to be enabled, which
// this app doesn't use - so this is a plain manual counter driven by
// Dexie's synchronous per-write hooks, wrapped in Dexie's own event emitter
// so components can subscribe with useSyncExternalStore instead of
// useLiveQuery (which needs the addon to see table-level changes at all).
const photosVersionListeners = new Set<() => void>()
let photosVersionValue = 0

export function subscribePhotosVersion(listener: () => void): () => void {
  photosVersionListeners.add(listener)
  return () => photosVersionListeners.delete(listener)
}

export function getPhotosVersion(): number {
  return photosVersionValue
}

// Dexie's per-row hooks fire once per row even inside a bulkPut/bulkDelete,
// so a 26,000-row sync would otherwise mean 26,000 individual notifications.
// Coalesced with a "notify pending" flag instead: any number of hook firings
// within the same microtask queue collapse into one bump and one round of
// listener calls, since all any subscriber needs to know is "changed since
// I last read", not how many times or how.
let notifyScheduled = false
function scheduleBump() {
  if (notifyScheduled) return
  notifyScheduled = true
  queueMicrotask(() => {
    notifyScheduled = false
    photosVersionValue++
    for (const listener of photosVersionListeners) listener()
  })
}

db.photos.hook('creating', scheduleBump)
db.photos.hook('updating', scheduleBump)
db.photos.hook('deleting', scheduleBump)
