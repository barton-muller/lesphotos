import { db } from './db'

/**
 * Object-URL store for thumbnails, loaded on demand rather than up front.
 *
 * The grid used to render straight from `photo.thumbnailBlob`, which meant the
 * Timeline query had to pull every blob in the library into one array before it
 * could lay anything out. That coupled scroll smoothness to image data: layout
 * (which only needs id/date/moment/width/height, a couple hundred bytes a row)
 * waited on pixels (megabytes), and `useLiveQuery` re-ran the whole thing on any
 * write. At the ~28,000-photo target it would not have fit in memory at all.
 *
 * Here blobs are fetched per photo, keyed by id, and evicted LRU-style with
 * their object URLs revoked - the old module-level Map in Timeline.tsx never
 * revoked anything, so URLs accumulated for the whole session.
 */

/**
 * How many thumbnail object URLs to keep alive at once.
 *
 * Each live URL pins its blob in memory, so this can't just be made large: at
 * 600 a full sweep of the test library held ~11MB more than letting the browser
 * lazy-load <img> tags did, which is the wrong trade on a 1GB DS124 and gets
 * worse at the 28,000-photo target.
 *
 * But a fixed budget is wrong in the other direction too. It has to cover the
 * working set - everything on screen plus the prefetch runway - and that scales
 * with the grid density: at 20 columns a tall viewport shows ~240 tiles, which
 * was the entire budget, so tiles were evicted while still visible. That is
 * what made photos load, turn grey, then load again during a fast scroll.
 *
 * So the budget is set by the grid (see setThumbnailBudget) and only bounded
 * here. The floor keeps small grids from thrashing; the ceiling is what stops a
 * dense grid on a tall screen from pinning the whole library.
 */
const MIN_BUDGET = 240

/**
 * Ceiling, scaled to the device.
 *
 * Client memory is not a concern on a desktop - 975 resident thumbnails
 * measured 34MB, barely above the 29MB of a 6-column grid, because thumbnails
 * are small. A phone is the real constraint: less RAM, a stricter renderer
 * memory cap, and a browser that will discard the tab rather than swap.
 *
 * navigator.deviceMemory is a coarse hint (Chrome/Android only, rounded down to
 * a power of two, absent on Safari) - so it is used only to lower the ceiling
 * when the device says it is small, never to raise it beyond what a desktop
 * gets. A device that reports nothing gets the middle tier.
 */
function deviceCeiling(): number {
  const nav = navigator as Navigator & { deviceMemory?: number }
  const gb = nav.deviceMemory
  if (typeof gb === 'number') {
    if (gb <= 1) return 400
    if (gb <= 2) return 700
    if (gb <= 4) return 1100
  }
  // Small viewports are phones even when deviceMemory is unavailable (Safari).
  const small = Math.min(window.innerWidth, window.innerHeight) < 500
  return small ? 900 : 1600
}

const MAX_BUDGET = deviceCeiling()

let maxUrls = MIN_BUDGET

/**
 * Size the cache to what's actually on screen.
 *
 * Called by the grid whenever its density or viewport changes. `visibleTiles`
 * is the real count the grid can show at once; the multiplier is the runway
 * that keeps a photo scrolled just off-screen from being dropped before the
 * user scrolls back to it.
 */
export function setThumbnailBudget(visibleTiles: number) {
  // 1.5x the mounted set: enough runway that scrolling back a screen finds
  // its tiles still resident, without pinning several screens of blobs.
  const next = Math.round(visibleTiles * 1.5)
  maxUrls = Math.min(MAX_BUDGET, Math.max(MIN_BUDGET, next))
}

interface Entry {
  url: string
  /** Bumped on each access so the LRU sweep can find the coldest entries. */
  seq: number
}

const entries = new Map<string, Entry>()
const inflight = new Map<string, Promise<string | null>>()
let seqCounter = 0

/**
 * Ids whose IndexedDB row had no blob, so we don't re-query them every frame.
 *
 * Deliberately NOT permanent. A tile can mount before sync has written that
 * photo's blob - which is normal during the first sync, when the timeline
 * renders as rows arrive - and a permanent negative cache left those tiles
 * showing their alt text (the raw photo id) until a full page reload. Entries
 * expire so a retry happens on a later frame, by which time the blob has
 * usually landed.
 */
const MISSING_TTL_MS = 2000
const missing = new Map<string, number>()

function isMissing(id: string): boolean {
  const at = missing.get(id)
  if (at === undefined) return false
  if (Date.now() - at > MISSING_TTL_MS) {
    missing.delete(id)
    return false
  }
  return true
}

function evictIfNeeded() {
  if (entries.size <= maxUrls) return
  // Sort by access order and drop the coldest quarter, so eviction runs
  // rarely rather than on nearly every insert once the cache is full.
  const sorted = [...entries.entries()].sort((a, b) => a[1].seq - b[1].seq)
  const drop = Math.max(1, Math.floor(entries.size - maxUrls * 0.75))
  for (let i = 0; i < drop; i++) {
    const [id, entry] = sorted[i]
    URL.revokeObjectURL(entry.url)
    entries.delete(id)
  }
}

/** Synchronous peek: the URL if it's already resident, else null. */
export function peekThumbnailUrl(id: string): string | null {
  const entry = entries.get(id)
  if (!entry) return null
  entry.seq = ++seqCounter
  return entry.url
}

/**
 * Load one thumbnail's object URL, reading the blob from IndexedDB if needed.
 * Concurrent calls for the same id share one read.
 */
export function loadThumbnailUrl(id: string): Promise<string | null> {
  const existing = peekThumbnailUrl(id)
  if (existing) return Promise.resolve(existing)
  if (isMissing(id)) return Promise.resolve(null)

  const pending = inflight.get(id)
  if (pending) return pending

  const promise = db.photos
    .get(id)
    .then((row) => {
      if (!row?.thumbnailBlob) {
        missing.set(id, Date.now())
        return null
      }
      // Another caller may have won the race while this read was in flight.
      const already = entries.get(id)
      if (already) {
        already.seq = ++seqCounter
        return already.url
      }
      const url = URL.createObjectURL(row.thumbnailBlob)
      entries.set(id, { url, seq: ++seqCounter })
      evictIfNeeded()
      return url
    })
    .catch(() => null)
    .finally(() => {
      inflight.delete(id)
    })

  inflight.set(id, promise)
  return promise
}

/**
 * Forget a photo's cached URL - call after its blob is replaced, so the next
 * render reads the new one instead of the stale object URL.
 */
export function invalidateThumbnail(id: string) {
  const entry = entries.get(id)
  if (entry) {
    URL.revokeObjectURL(entry.url)
    entries.delete(id)
  }
  missing.delete(id)
}

/**
 * Warm a batch of thumbnails, newest request first.
 *
 * Called with the rows just outside the rendered window so a fling has blobs
 * ready before those tiles mount. Reads are issued through the same inflight
 * map as loadThumbnailUrl, so a prefetch and a real render never duplicate work.
 */
export function prefetchThumbnails(ids: string[]) {
  for (const id of ids) {
    if (entries.has(id) || inflight.has(id) || isMissing(id)) continue
    void loadThumbnailUrl(id)
  }
}

