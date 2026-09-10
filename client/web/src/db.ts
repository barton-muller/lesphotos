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
 * The grid lays out from this alone. Keeping it separate from `thumbnailBlob`
 * is what lets the Timeline load all ~28,000 rows at once (a couple hundred
 * bytes each) while thumbnails stream in per visible row - see thumbnails.ts.
 */
export type PhotoMeta = Omit<CachedPhoto, 'thumbnailBlob'>

export interface CachedPhoto {
  id: string
  path: string
  moment: string
  momentPath: string
  date: string
  // 0 | 1, not boolean: IndexedDB's native key types (see IndexableType)
  // don't include boolean at all, so a boolean-typed field can't be used
  // with .where('favorite').equals(...) - matches server/appdb's own
  // `favorite INTEGER` column for the same reason.
  favorite: 0 | 1
  // Optional: a metadata-only sync stores the row without pixels and the
  // grid fills them in as tiles come into view (see thumbnails.ts). A row
  // that has one is offline-capable; one that does not needs the server.
  thumbnailBlob?: Blob
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

export const db = new Dexie('lesphotos') as Dexie & {
  photos: EntityTable<CachedPhoto, 'id'>
  fullres: EntityTable<CachedFullRes, 'id'>
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
