import {
  fetchMediaTimelinePaged,
  fetchTimelineDeltaPaged,
  fetchTimelineMaxSeq,
  thumbsBatchUrl,
  thumbShardsManifestUrl,
  thumbShardUrl,
  type TimelineEntry,
} from './api'
import { db, type CachedPhoto } from './db'
import { readStoredZip } from './miniZip'
import { formatBytes, hasRoomFor, requestPersistentStorage } from './storage'
import { invalidateThumbnail } from './thumbnails'

// Mean measured across 2,041 real photos in the test library (the synthetic
// 160x120 fakes are excluded - they average 0.2KB and would skew this low).
// Only used to size the pre-download quota check in downloadAllThumbnails,
// so being roughly right is enough; the check has its own 10% headroom.
const APPROX_THUMB_BYTES = 20 * 1024

export class QuotaError extends Error {}

// Tells apart a local IndexedDB write failure from a network/server
// problem, so a sync's catch block doesn't mislabel one as the other (see
// App.tsx's runSync). Keyed on err.name - Dexie throws BulkError for a
// partial bulkPut/bulkUpdate failure and various DexieError subclasses
// (ConstraintError, DataError, ...) for other IndexedDB failures - rather
// than matching err.message text, which is browser/engine-dependent and
// not something to rely on for classification.
export function isLocalStorageError(err: unknown): boolean {
  return (
    err instanceof Error &&
    (err.name === 'BulkError' ||
      err.name.includes('Dexie') ||
      /object store|IDBDatabase|QuotaExceededError|Blob\/File data/i.test(err.message))
  )
}

// Last changed_seq seen from the server (see docs/decisions/delta-sync.md),
// persisted so both the short-circuit check and the choice of delta-vs-full
// sync work from a cold page load too, not just within one session.
// Replaces the old count-only LAST_COUNT_KEY: count missed a same-count
// change (a favorite toggle, a rename) entirely, where changed_seq is
// bumped by every write that matters to another device.
const LAST_SEQ_KEY = 'lesphotos-last-changed-seq'

function readLastSeq(): number | null {
  try {
    const v = localStorage.getItem(LAST_SEQ_KEY)
    return v === null ? null : Number(v)
  } catch {
    return null
  }
}

function writeLastSeq(n: number) {
  try {
    localStorage.setItem(LAST_SEQ_KEY, String(n))
  } catch {
    // Loses the skip optimisation only, never correctness - the next sync
    // just does a full relist as if this were the first one.
  }
}

// A photo's own moment never changes because of trash, but its path does:
// deleting it moves it under library/trash/YYYY/YYYY-MM-DD/ (see
// api.py's /photos/delete), which stays a fully ordinary media/photos row
// - same id, same delta-sync treatment as any other move - just one the
// default grid shouldn't render. Matches server-side filtering in
// api.py's /timeline (no `since`) so "in the grid" means the same thing
// whether a row arrived via full relist or delta. `path` is client-facing
// (reorganize.client_path's "/photos/..." prefix, see api.ts's toApiPath),
// not the bare library-relative path the server's own SQL filters on.
function isTrashed(path: string): boolean {
  return path.startsWith('/photos/trash/')
}

export interface SyncResult {
  added: number
  updated: number
  removed: number
  total: number
}

function toNewCachedPhoto(r: TimelineEntry) {
  return {
    id: r.id,
    path: r.path,
    moment: r.moment,
    momentPath: r.momentPath,
    momentDate: r.momentDate,
    date: r.date,
    favorite: r.favorite ? (1 as const) : (0 as const),
    // No blob yet - the tile fetches it on demand (useThumbnail) or
    // the user asks for everything at once (downloadAllThumbnails).
    width: r.width ?? 0,
    height: r.height ?? 0,
    isVideo: r.isVideo ? (1 as const) : (0 as const),
    hasMotion: r.hasMotion ? (1 as const) : (0 as const),
    duration: r.duration,
  }
}

// Compares a fresh server row against what's cached and returns the row to
// write, or null if nothing actually changed. Shared between the full
// relist and the delta path so "what counts as changed" can't drift
// between the two - see each field's own comment for why it's checked.
function diffRow(cached: CachedPhoto | undefined, r: TimelineEntry): CachedPhoto | null {
  if (!cached) return toNewCachedPhoto(r)
  const changed =
    cached.path !== r.path ||
    cached.moment !== r.moment ||
    cached.momentPath !== r.momentPath ||
    // A photo's own moment doesn't change, but a sibling landing in
    // its month bucket rebalances moment_date for the whole bucket
    // (indexer.rebalance_month_bucket_dates) - without this, an
    // existing cached row in that bucket would keep its stale sort
    // key forever, the same class of bug this field exists to fix.
    cached.momentDate !== r.momentDate ||
    cached.date !== r.date ||
    cached.favorite !== (r.favorite ? 1 : 0) ||
    // Without this a photo that gains motion on a re-index would keep
    // its stale row forever - the id is unchanged, so presence alone
    // says nothing (see the note above).
    cached.hasMotion !== (r.hasMotion ? 1 : 0)
  if (!changed) return null
  // Safe to spread the whole cached row now by construction, not by
  // luck: CachedPhoto has no blob field to accidentally carry along
  // into this bulkPut (thumbnailBlob lives in its own `thumbnails`
  // table - db.ts's v9 schema). A metadata-only change no longer
  // risks re-serializing an unrelated cached thumbnail.
  return {
    ...cached,
    path: r.path,
    moment: r.moment,
    momentPath: r.momentPath,
    momentDate: r.momentDate,
    date: r.date,
    favorite: r.favorite ? (1 as const) : (0 as const),
    isVideo: r.isVideo ? (1 as const) : (0 as const),
    hasMotion: r.hasMotion ? (1 as const) : (0 as const),
    duration: r.duration,
  }
}

// The delta path: fetch only what changed since the last sync (see
// docs/decisions/delta-sync.md) instead of paging the whole library. Used
// whenever a watermark is already cached; syncTimeline falls back to
// fullRelist otherwise. A trashed row (isTrashed) is written like any
// other move and then immediately deleted locally - it's a real,
// delta-visible row server-side (api.py's /timeline?since= does not
// filter it out), just one this client doesn't render, matching the
// default listing's own server-side filter.
async function deltaSync(
  since: number,
  onProgress?: (done: number, total: number) => void,
): Promise<SyncResult> {
  let added = 0
  let updated = 0
  let removed = 0
  let done = 0

  const maxSeq = await fetchTimelineDeltaPaged(since, async (page: TimelineEntry[]) => {
    const toWrite: CachedPhoto[] = []
    const toDelete: string[] = []
    for (const r of page) {
      if (isTrashed(r.path)) {
        toDelete.push(r.id)
        continue
      }
      const cached = await db.photos.get(r.id)
      const row = diffRow(cached, r)
      if (row) {
        if (cached) updated += 1
        else added += 1
        toWrite.push(row)
      }
    }
    if (toWrite.length) await db.photos.bulkPut(toWrite)
    if (toDelete.length) {
      removed += toDelete.length
      await db.photos.bulkDelete(toDelete)
    }
    done += page.length
    onProgress?.(done, done)
  })

  writeLastSeq(maxSeq)
  return { added, updated, removed, total: await db.photos.count() }
}

// The full-relist path: pages the entire visible timeline and diffs every
// row against IndexedDB. Used for a device's first sync (no cached
// watermark yet) or when the server has no maxSeq to offer (an old server
// predating delta sync) - see syncTimeline.
async function fullRelist(onProgress?: (done: number, total: number) => void): Promise<SyncResult> {
  // Cached rows, not just their keys: photo_id is stable across moves by
  // design, so "already cached" does NOT imply "unchanged". A renamed
  // moment keeps every photo's id while changing its moment/path, and an
  // id-presence check alone would skip the row and leave it showing the old
  // name forever - the exact bug that made rename appear to do nothing
  // under the PhotoView backend (see CLAUDE.md).
  const cachedById = new Map((await db.photos.toArray()).map((p) => [p.id, p]))
  const remoteIds = new Set<string>()

  let added = 0
  let updated = 0
  // Total is unknown until the last (short) page arrives, so progress is
  // reported as "done so far" against itself until then - the caller
  // renders that as "N photos" rather than "N/M", which is honest about
  // what is actually known partway through a stream.
  let done = 0

  await fetchMediaTimelinePaged(async (page: TimelineEntry[]) => {
    const toWrite: CachedPhoto[] = []
    for (const r of page) {
      remoteIds.add(r.id)
      const row = diffRow(cachedById.get(r.id), r)
      if (row) {
        if (cachedById.has(r.id)) updated += 1
        else added += 1
        toWrite.push(row)
      }
    }
    if (toWrite.length) await db.photos.bulkPut(toWrite)
    done += page.length
    onProgress?.(done, done)
  })

  const removedIds = [...cachedById.keys()].filter((id) => !remoteIds.has(id))
  if (removedIds.length) await db.photos.bulkDelete(removedIds)

  return { added, updated, removed: removedIds.length, total: remoteIds.size }
}

// Metadata only, streamed one server page at a time rather than buffered
// whole. The previous shape fetched every /timeline page before writing
// anything, so a device with a few thousand photos sat on a blank "Syncing…"
// for 30-60s and then wrote the whole library in one pass - which also
// meant thumbnails for old years never got a chance to start loading until
// that pass reached them, showing as bands of grey tiles days apart from
// where the network activity actually was. Writing each page as it lands
// means the grid has rows to render, and therefore tiles to lazily fetch
// thumbnails for, within the first ~100ms page instead of after the last.
//
// Thumbnail PIXELS are a separate, explicit action - see
// downloadAllThumbnails. A tile with no thumbnailBlob renders grey and
// fetches its own thumbnail when it scrolls into view (useThumbnail), which
// is what makes metadata sync fast regardless of library size: it is JSON,
// never image bytes.
export async function syncTimeline(
  onProgress?: (done: number, total: number) => void,
): Promise<SyncResult> {
  // Ask once per session for the cache not to be treated as discardable.
  // Deliberately not awaited into a branch: a refusal is normal (Safari
  // always refuses on iOS) and changes nothing about how sync proceeds - the
  // cache just stays eviction-eligible, as it was before this call existed.
  void requestPersistentStorage()

  // Cheap check first (see docs/decisions/delta-sync.md): most syncs - the
  // foreground-refresh ones especially - find nothing changed at all, and
  // a full relist is a real cost even now that changes fetch is possible
  // (~700KB / one request per 500 photos - measured 10s over Tailscale at
  // 4,000 photos). maxSeq catches every write that matters to another
  // device (a favorite toggle, a rename, not just additions/deletions -
  // the gap the old count-only check had). A locally-cached seq that
  // predates any photos existing (null) never short-circuits the very
  // first sync, since a fresh device also has nothing in IndexedDB to skip
  // to - the emptiness would be self-inconsistent, not just stale.
  const lastSeq = readLastSeq()
  const cachedCount = await db.photos.count()
  if (lastSeq !== null && cachedCount > 0) {
    const remoteMaxSeq = await fetchTimelineMaxSeq()
    if (remoteMaxSeq === lastSeq) {
      return { added: 0, updated: 0, removed: 0, total: cachedCount }
    }
    // Something changed since lastSeq - fetch only that delta rather than
    // the whole library. This is the fix for "adding one photo costs a
    // full relist": O(what changed), not O(library size).
    return deltaSync(lastSeq, onProgress)
  }

  // No cached watermark: first sync on this device (or old cached state
  // from before delta sync existed). Full relist is the only option - it
  // also establishes the watermark for every sync after this one.
  const result = await fullRelist(onProgress)
  const maxSeq = await fetchTimelineMaxSeq()
  writeLastSeq(maxSeq)
  return result
}

// The explicit, expensive action: download every thumbnail so the library
// is fully browsable offline. Everything else about the app works from
// metadata plus whichever thumbnails have actually been looked at - this
// is for "I am about to lose the connection" rather than routine use.
//
// Newest-first, matching the server's own derive-pending ordering ("the
// photos someone actually sees get pixels first") - a `date` index
// already exists for this (see db.ts's schema), so it's a plain reversed
// orderBy rather than a full-table sort in memory.
export async function downloadAllThumbnails(
  onProgress?: (done: number, total: number) => void,
  signal?: AbortSignal,
): Promise<{ downloaded: number; failed: number; total: number }> {
  const rows = await db.photos.orderBy('date').reverse().toArray()
  // Diffed against the thumbnails table's own keys, not a per-row field on
  // `photos` - thumbnailBlob moved to its own table (db.ts's v9 schema)
  // precisely so a read like this one doesn't have to deserialize every
  // cached blob just to test whether it's there. primaryKeys() reads only
  // ids, never blob bytes.
  const haveIds = new Set(await db.thumbnails.toCollection().primaryKeys())
  const need = rows.filter((r) => !haveIds.has(r.id))

  const needed = need.length * APPROX_THUMB_BYTES
  const { ok, info } = await hasRoomFor(needed)
  if (!ok && info) {
    throw new QuotaError(
      `not enough browser storage: need ~${formatBytes(needed)} more, ` +
        `${formatBytes(info.usageBytes)} of ${formatBytes(info.quotaBytes)} already used`,
    )
  }

  let done = 0
  let failed = 0
  const needIds = new Set(need.map((r) => r.id))

  // Shards (see thumb_shards.py) are assigned by DERIVATION order, oldest
  // shard number = oldest-derived content, current OPEN shard = newest.
  // So "newest first" here means: the open shard's content first (via
  // the per-id batch endpoint below, since it has no frozen zip yet),
  // THEN frozen shards newest-shard-number-first. Fetching frozen shards
  // in shard-0-first order would have shown the OLDEST derived content
  // before the newest, inverting the whole point of newest-first -
  // caught in review before this ever ran against a real download.
  let frozenShardsNewestFirst: { shard: number; frozen: boolean }[] = []
  // The still-being-filled shard's current member count, straight from
  // the manifest - used only to size the "definitely newer than any
  // frozen shard" slice of `need` that goes through the batch endpoint
  // first (see the ordering comment below). Not a promise that exactly
  // this many of `need`'s newest entries are unfetched - just a sizing
  // hint; a photo already on-device is filtered out before use.
  let openShardMemberCount = 0
  if (needIds.size > 0 && !signal?.aborted) {
    try {
      const manifestResp = await fetch(thumbShardsManifestUrl(), { credentials: 'include', signal })
      if (manifestResp.ok) {
        const { shards } = (await manifestResp.json()) as
          { shards: { shard: number; frozen: boolean; member_count: number }[] }
        frozenShardsNewestFirst = shards.filter((s) => s.frozen).sort((a, b) => b.shard - a.shard)
        openShardMemberCount = shards.find((s) => !s.frozen)?.member_count ?? 0
      }
      // A manifest fetch failure (offline, server hiccup, old server with
      // no shard endpoints yet) is not fatal - the per-id batch path below
      // still covers every photo, just without the shard-fetch shortcut.
    } catch (err) {
      if (signal?.aborted) return { downloaded: done - failed, failed, total: need.length }
    }
  }

  async function fetchFrozenShards() {
    for (const s of frozenShardsNewestFirst) {
      if (needIds.size === 0 || signal?.aborted) return
      const zipResp = await fetch(thumbShardUrl(s.shard), { credentials: 'include', signal })
      if (!zipResp.ok) continue
      const entries = readStoredZip(await zipResp.arrayBuffer())
      const relevant = entries.filter((e) => needIds.has(e.name))
      if (relevant.length === 0) continue
      // bulkPut, not bulkUpdate: a `thumbnails` row does not already exist
      // for a photo whose blob has never been fetched, unlike the old
      // `photos`-row-with-optional-field shape where the row was always
      // there from metadata sync. bulkUpdate silently no-ops on a missing
      // row, which would have made this a no-op for every first-time fetch.
      //
      // type: 'image/webp' explicitly - a Blob built from raw zip bytes has
      // no type unless given one (unlike fetchThumbnailBlob's resp.blob(),
      // which inherits it from the response's Content-Type header), and a
      // typeless Blob renders as nothing in an <img src="blob:...">: found
      // by inspecting IndexedDB directly after "download all" left every
      // tile grey with zero console errors - the blob was there (real size,
      // right bytes), just untyped. Derivatives are always webp - see
      // CLAUDE.md and indexer.py's _derive_one.
      await db.thumbnails.bulkPut(
        relevant.map((e) => ({ id: e.name, blob: new Blob([e.data], { type: 'image/webp' }) })),
      )
      for (const e of relevant) {
        invalidateThumbnail(e.name)
        needIds.delete(e.name)
        done += 1
      }
      onProgress?.(done, need.length)
    }
  }

  // History of this loop, cheapest fix first:
  //  1. One fetch + one IndexedDB write per photo: ~2/sec even against
  //     localhost. Not network-bound - 20 concurrent requests direct against
  //     this same server endpoint (curl, no browser involved) completed in
  //     0.06s.
  //  2. Concurrent fetches (worker pool) + batched writes (bulkUpdate every
  //     BATCH_SIZE photos instead of one transaction each): much better,
  //     but still capped by the browser's 6-connections-per-origin limit on
  //     HTTP/1.1, and every request still pays fixed per-request overhead
  //     (headers, the server's auth dependency, FastAPI routing) on top of
  //     a ~20KB payload.
  //  3. This version: HTTP_BATCH_SIZE ids per request, server bundles them
  //     into one ZIP_STORED response (see /media/thumbs-batch), client
  //     unzips (readStoredZip - webp is already compressed, so the server
  //     never DEFLATEs, and a full zip library would be overkill for a
  //     format this constrained) and writes the whole batch in one
  //     bulkUpdate. Cuts request count by ~HTTP_BATCH_SIZE, so per-request
  //     overhead and the 6-connection cap both stop being the limiting
  //     factor - the transfer itself (a few KB/photo) was never slow.
  const HTTP_BATCH_SIZE = 200
  const CONCURRENCY = 4 // batches in flight at once, not individual photos

  async function fetchAndStoreBatch(ids: string[]) {
    // Removed from needIds regardless of outcome (found or not) - this
    // function is called up to twice for the same photo across the three
    // ordering steps below (once in the newest-slice pass, once in the
    // final catch-all) ONLY if the first attempt didn't clear it here;
    // without this, step 3's catch-all would blindly re-fetch every id
    // step 1 already resolved, double-counting `done`/`failed` and
    // redoing real network work - caught in review, not live.
    for (const id of ids) needIds.delete(id)

    const resp = await fetch(thumbsBatchUrl(), {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ photo_ids: ids }),
      signal,
    })
    if (!resp.ok) {
      failed += ids.length
      done += ids.length
      onProgress?.(done, need.length)
      return
    }
    const entries = readStoredZip(await resp.arrayBuffer())
    const found = new Set(entries.map((e) => e.name))
    // bulkPut, not bulkUpdate - see the same note in fetchFrozenShards.
    await db.thumbnails.bulkPut(
      entries.map((e) => ({ id: e.name, blob: new Blob([e.data]) })),
    )
    for (const e of entries) invalidateThumbnail(e.name)
    // An id the server didn't return (no derivative yet, or a race with
    // deletion) still counts toward `done`, so progress reaches `total`
    // even when some photos are permanently thumbnail-less.
    failed += ids.length - found.size
    done += ids.length
    onProgress?.(done, need.length)
  }

  async function runBatchWorkers(targets: typeof need) {
    let idx = 0
    async function worker() {
      while (idx < targets.length) {
        if (signal?.aborted) return
        const batchIds = targets.slice(idx, idx + HTTP_BATCH_SIZE).map((r) => r.id)
        idx += HTTP_BATCH_SIZE
        try {
          await fetchAndStoreBatch(batchIds)
        } catch (err) {
          if (signal?.aborted) return
          throw err
        }
      }
    }
    const batchCount = Math.ceil(targets.length / HTTP_BATCH_SIZE)
    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, batchCount) }, worker))
  }

  // Ordering (newest content visible soonest, per this function's own
  // stated intent - see the comment above needIds):
  //  1. The current OPEN shard's photos have no frozen zip yet and are
  //     also the NEWEST derived content (shards fill oldest-to-newest) -
  //     both reasons point at fetching them via the plain batch endpoint
  //     first, not last. `need` is already sorted newest-first, so its
  //     leading slice up to openShardMemberCount approximates "the open
  //     shard's content" without needing per-photo shard membership on
  //     the client - approximates, not guarantees, which is fine since
  //     step 3 is a catch-all for whatever this estimate missed.
  //  2. Frozen shards, newest-shard-number-first (fetchFrozenShards).
  //  3. A final batch pass for anything neither step covered - a photo a
  //     frozen shard's zip happened to be missing (see thumb_shards.py's
  //     re-derive/skipped-file notes), or if step 1's size estimate was
  //     off (e.g. a shard filled and froze between the manifest fetch and
  //     now).
  const newestSlice = need.slice(0, openShardMemberCount).filter((r) => needIds.has(r.id))
  await runBatchWorkers(newestSlice)
  await fetchFrozenShards()
  await runBatchWorkers(need.filter((r) => needIds.has(r.id)))

  return { downloaded: done - failed, failed, total: need.length }
}
