import { thumbUrl } from './api'
import { db, type CachedThumbnail } from './db'

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

/**
 * Abort handles for network fetches, and how many mounted tiles still want
 * each one.
 *
 * Flinging through a large library mounts and unmounts thousands of tiles.
 * Without this, every one of them leaves a request in flight that nobody is
 * waiting for any more - measured on a 2,298-photo library: a fast scroll
 * issued 519 requests during the fling and kept going until all 2,298 had
 * been fetched, long after the tiles were gone. Cancelling on the last
 * release keeps the traffic proportional to what is actually on screen.
 */
const fetchControllers = new Map<string, AbortController>()
const waiters = new Map<string, number>()
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
  // Never evict an id a mounted tile still wants. useThumbnail peeks its
  // object URL synchronously during render (deliberately, so scrolling back
  // doesn't flash a placeholder - see useThumbnail.ts), so revoking a
  // waited-on entry here breaks an already-rendered <img src="blob:...">
  // out from under it: nothing forces that component to re-render and
  // re-peek, so it keeps pointing at a dead URL and shows the browser's
  // broken-image icon until some unrelated state change happens to redraw
  // it. Reproduced during a fast fling: prefetch's ~72-tile runway inserts
  // enough new entries between two visits to the same tile to push it out
  // of pure-LRU order while it's still on screen.
  const candidates = [...entries.entries()].filter(([id]) => !waiters.has(id))
  if (candidates.length === 0) {
    // Every resident entry is currently on screen - nothing is safe to
    // evict this pass. Bounded, not a leak: `waiters` shrinks as tiles
    // unmount (releaseThumbnail), and maxUrls is already sized off the
    // same visible-tile count that bounds waiters.size (setThumbnailBudget's
    // 1.5x runway assumes it covers the mounted set), so entries.size can
    // only exceed maxUrls by however much is genuinely visible right now.
    return
  }
  // Sort by access order and drop the coldest quarter of what's actually
  // evictable, so eviction runs rarely rather than on nearly every insert
  // once the cache is full.
  const sorted = candidates.sort((a, b) => a[1].seq - b[1].seq)
  const drop = Math.max(1, Math.floor(candidates.length - maxUrls * 0.75))
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
/**
 * One thumbnail from the server, or null if it cannot be had.
 *
 * Same URL and credentials as the bulk sync uses - this is the same
 * request, made one tile at a time instead of all at once.
 */
async function fetchThumbnailBlob(id: string): Promise<Blob | null> {
  const controller = new AbortController()
  fetchControllers.set(id, controller)
  try {
    const resp = await fetch(thumbUrl(id), {
      credentials: 'include',
      signal: controller.signal,
    })
    return resp.ok ? await resp.blob() : null
  } catch {
    // Includes the AbortError from releaseThumbnail. Returning null marks
    // the id missing for MISSING_TTL_MS (2s), so a tile scrolled back to
    // simply asks again.
    return null
  } finally {
    fetchControllers.delete(id)
  }
}


/**
 * Note that a mounted tile wants this photo. Pair every call with
 * releaseThumbnail.
 */
export function retainThumbnail(id: string) {
  waiters.set(id, (waiters.get(id) ?? 0) + 1)
}


/**
 * The tile is gone. When nothing else wants the photo, abort any fetch
 * still running for it.
 */
export function releaseThumbnail(id: string) {
  const n = (waiters.get(id) ?? 0) - 1
  if (n > 0) {
    waiters.set(id, n)
    return
  }
  waiters.delete(id)
  fetchControllers.get(id)?.abort()
}


/**
 * Coalesces same-tick db.thumbnails.get(id) calls into one bulkGet.
 *
 * The initial mount asks for every visible tile plus the prefetch runway
 * (~150+ ids) within the same render pass, and each used to open its own
 * IndexedDB transaction. Measured on a real ~26,000-photo Safari session:
 * 164 individual get() calls spread across a ~10s stall with the main
 * thread completely idle between them - Safari appears to serialize
 * concurrent IndexedDB transactions against the same store far less
 * efficiently than Chrome does, so a burst of one-row reads that's
 * essentially free in Chrome became the actual bottleneck here. Batching
 * into a single bulkGet - one transaction regardless of how many ids
 * arrive in the same tick - sidesteps that entirely.
 */
const pendingGets = new Map<string, { resolve: (row: CachedThumbnail | undefined) => void }[]>()
let getFlushScheduled = false

function batchedGetThumbnail(id: string): Promise<CachedThumbnail | undefined> {
  return new Promise((resolve) => {
    const waiting = pendingGets.get(id)
    if (waiting) {
      waiting.push({ resolve })
      return
    }
    pendingGets.set(id, [{ resolve }])
    if (!getFlushScheduled) {
      getFlushScheduled = true
      queueMicrotask(flushPendingGets)
    }
  })
}

function flushPendingGets() {
  getFlushScheduled = false
  const ids = [...pendingGets.keys()]
  // A COPY, not an alias: pendingGets.clear() below empties the same Map
  // object a plain `const resolvers = pendingGets` would still be pointing
  // at, so every resolvers.get() after the clear silently returned
  // undefined and no waiter was ever resolved - found by tracing a
  // never-settling promise all the way back to this line; nothing threw,
  // nothing logged, because "resolve nothing" isn't an error, just silence.
  const resolvers = new Map(pendingGets)
  pendingGets.clear()
  db.thumbnails
    .bulkGet(ids)
    .then((rows) => {
      rows.forEach((row, i) => {
        for (const w of resolvers.get(ids[i]) ?? []) w.resolve(row)
      })
    })
    .catch(() => {
      for (const list of resolvers.values()) {
        for (const w of list) w.resolve(undefined)
      }
    })
}

export function loadThumbnailUrl(
  id: string,
  { allowNetwork = true }: { allowNetwork?: boolean } = {},
): Promise<string | null> {
  const existing = peekThumbnailUrl(id)
  if (existing) return Promise.resolve(existing)
  if (isMissing(id)) return Promise.resolve(null)

  const pending = inflight.get(id)
  if (pending) return pending

  const promise = batchedGetThumbnail(id)
    .then(async (row) => {
      if (!row?.blob) {
        // No blob on device yet. Fetch it and keep it, so a device that
        // has only synced metadata fills in as you scroll instead of
        // needing the whole library downloaded up front.
        //
        // Failure is deliberately not special-cased: MISSING_TTL_MS is
        // 2s, so a tile that failed simply retries the next time it is
        // scrolled past. Most of the time the library is fully synced and
        // this path never runs at all.
        if (!allowNetwork) {
          // Prefetch warms from IndexedDB only. Not marked missing: the
          // tile itself will ask for real when it mounts, and recording a
          // miss here would suppress that for MISSING_TTL_MS.
          return null
        }
        const blob = await fetchThumbnailBlob(id)
        if (!blob) {
          missing.set(id, Date.now())
          return null
        }
        // Store it so the next open reads from IndexedDB, and so a photo
        // viewed once is offline-capable thereafter. A write failure (quota)
        // still yields a usable URL for this session. put, not update - a
        // thumbnails row doesn't already exist on a first-time fetch.
        db.thumbnails.put({ id, blob }).catch(() => {})
        const already = entries.get(id)
        if (already) {
          already.seq = ++seqCounter
          return already.url
        }
        const url = URL.createObjectURL(blob)
        entries.set(id, { url, seq: ++seqCounter })
        evictIfNeeded()
        return url
      }
      // Another caller may have won the race while this read was in flight.
      const already = entries.get(id)
      if (already) {
        already.seq = ++seqCounter
        return already.url
      }
      const url = URL.createObjectURL(row.blob)
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
  const waiting = invalidationListeners.get(id)
  if (waiting) for (const cb of [...waiting]) cb()
}

const invalidationListeners = new Map<string, Set<() => void>>()

/**
 * Call back when this photo's thumbnail is invalidated - i.e. its blob was just
 * written. A mounted tile that read IndexedDB before "Download all" reached it
 * is parked as missing and would otherwise stay grey until remounted.
 * Returns the unsubscribe function.
 */
export function subscribeThumbnailInvalidated(id: string, cb: () => void): () => void {
  let set = invalidationListeners.get(id)
  if (!set) invalidationListeners.set(id, (set = new Set()))
  set.add(cb)
  return () => {
    set.delete(cb)
    if (!set.size) invalidationListeners.delete(id)
  }
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
    // Local reads only. This fires on every scroll position change over a
    // runway of ~72 tiles either side, which is right for cheap IndexedDB
    // reads but would put the whole library on the wire during a fling -
    // measured: all 2,298 photos requested while flinging through a
    // 2,298-photo library. Tiles that actually mount do the fetching.
    void loadThumbnailUrl(id, { allowNetwork: false })
  }
}

