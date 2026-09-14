import { fetchMediaTimelinePaged, fetchTimelineCount, thumbUrl, type TimelineEntry } from './api'
import { db } from './db'
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
export async function downloadAllThumbnails(
  onProgress?: (done: number, total: number) => void,
): Promise<{ downloaded: number; failed: number; total: number }> {
  const rows = await db.photos.toArray()
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
  for (const r of need) {
    const resp = await fetch(thumbUrl(r.id), { credentials: 'include' })
    if (resp.ok) {
      await db.photos.update(r.id, { thumbnailBlob: await resp.blob() })
      // Drop any negative-cache entry recorded while this row had no blob
      // yet, so the tile picks the new one up instead of waiting for the
      // TTL (see thumbnails.ts).
      invalidateThumbnail(r.id)
    } else {
      failed += 1
    }
    done += 1
    onProgress?.(done, need.length)
  }
  return { downloaded: done - failed, failed, total: need.length }
}
