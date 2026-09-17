import {
  fetchMediaTimelinePaged,
  fetchTimelineCount,
  thumbsBatchUrl,
  thumbShardsManifestUrl,
  thumbShardUrl,
  type TimelineEntry,
} from './api'
import { db } from './db'
import { readStoredZip } from './miniZip'
import { formatBytes, hasRoomFor, requestPersistentStorage } from './storage'
import { invalidateThumbnail } from './thumbnails'

// Mean measured across 2,041 real photos in the test library (the synthetic
// 160x120 fakes are excluded - they average 0.2KB and would skew this low).
// Only used to size the pre-download quota check in downloadAllThumbnails,
// so being roughly right is enough; the check has its own 10% headroom.
const APPROX_THUMB_BYTES = 20 * 1024

export class QuotaError extends Error {}

// Last count seen from /timeline/count, persisted so the skip works from
// a cold page load too, not just within one session.
const LAST_COUNT_KEY = 'lesphotos-last-timeline-count'

function readLastCount(): number | null {
  try {
    const v = localStorage.getItem(LAST_COUNT_KEY)
    return v === null ? null : Number(v)
  } catch {
    return null
  }
}

function writeLastCount(n: number) {
  try {
    localStorage.setItem(LAST_COUNT_KEY, String(n))
  } catch {
    // Loses the skip optimisation only, never correctness - the next sync
    // just does a full relist as if this were the first one.
  }
}

export interface SyncResult {
  added: number
  updated: number
  removed: number
  total: number
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

  // Cheap check first: a full relist is a real cost (~700KB / one request
  // per 500 photos - measured 10s over Tailscale at 4,000 photos), and
  // most syncs - the foreground-refresh ones especially - find nothing
  // changed. Skips the relist when the count matches; does NOT catch a
  // favorite toggle or rename with no count change (those still show up
  // on the next add/delete, or via "Sync now"). A locally-cached count
  // that predates any photos existing (0) never short-circuits the very
  // first sync, since a fresh device also has nothing in IndexedDB to skip
  // to - the emptiness would be self-inconsistent, not just stale.
  const lastCount = readLastCount()
  const cachedCount = await db.photos.count()
  if (lastCount !== null && cachedCount > 0) {
    const remoteCount = await fetchTimelineCount()
    if (remoteCount === lastCount && remoteCount === cachedCount) {
      return { added: 0, updated: 0, removed: 0, total: remoteCount }
    }
  }

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
    const rows = await Promise.all(
      page.map(async (r) => {
        remoteIds.add(r.id)
        const cached = cachedById.get(r.id)
        if (!cached) {
          added += 1
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
        updated += 1
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
      }),
    )
    const toWrite = rows.filter((r): r is NonNullable<typeof r> => r !== null)
    if (toWrite.length) await db.photos.bulkPut(toWrite)
    done += page.length
    onProgress?.(done, done)
  })

  const removedIds = [...cachedById.keys()].filter((id) => !remoteIds.has(id))
  if (removedIds.length) await db.photos.bulkDelete(removedIds)

  writeLastCount(remoteIds.size)
  return { added, updated, removed: removedIds.length, total: remoteIds.size }
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
  const need = rows.filter((r) => !r.thumbnailBlob)

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
      await db.photos.bulkUpdate(
        relevant.map((e) => ({ key: e.name, changes: { thumbnailBlob: new Blob([e.data]) } })),
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
  let nextIndex = 0

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
    await db.photos.bulkUpdate(
      entries.map((e) => ({ key: e.name, changes: { thumbnailBlob: new Blob([e.data]) } })),
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
