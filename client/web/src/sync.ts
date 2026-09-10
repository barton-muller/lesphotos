import { fetchMediaTimeline, thumbUrl } from './api'
import { db } from './db'
import { formatBytes, hasRoomFor, requestPersistentStorage } from './storage'
import { invalidateThumbnail } from './thumbnails'

// Mean measured across 2,041 real photos in the test library (the synthetic
// 160x120 fakes are excluded - they average 0.2KB and would skew this low).
// Only used to size the pre-sync quota check, so being roughly right is
// enough; the check has its own 10% headroom on top.
const APPROX_THUMB_BYTES = 20 * 1024

// Full-relist + diff, against server/api.py's own /timeline (see
// docs/decisions/replacing-photoview.md). Simpler than the PhotoView
// version it replaces: that one had to fetch a separate /photo-ids map and
// resolve every timeline entry's stable id by path, because PhotoView's own
// id is MD5(path)-derived and churns on every move. /timeline returns
// photo_id directly, so the whole resolve-and-filter step is gone.
//
// Still a full relist rather than a changes-since query: the server has no
// change feed, and at the scale this runs today the relist is ~50ms. If the
// library grows to where that stops being true, the fix is a windowed sync
// (recent months resident, older fetched on demand + LRU evicted) rather
// than a smarter diff - see the decision doc.
export class QuotaError extends Error {}

export async function syncTimeline(
  onProgress?: (done: number, total: number) => void,
  { thumbnails = true }: { thumbnails?: boolean } = {},
) {
  // Ask once per session for the cache not to be treated as discardable.
  // Deliberately not awaited into a branch: a refusal is normal (Safari
  // always refuses on iOS) and changes nothing about how sync proceeds - the
  // cache just stays eviction-eligible, as it was before this call existed.
  void requestPersistentStorage()

  const remote = await fetchMediaTimeline()
  const remoteIds = new Set(remote.map((r) => r.id))

  // Cached rows, not just their keys: photo_id is stable across moves by
  // design, so "already cached" does NOT imply "unchanged". A renamed
  // moment keeps every photo's id while changing its moment/path, and an
  // id-presence check alone would skip the row and leave it showing the old
  // name forever - the exact bug that made rename appear to do nothing
  // under the PhotoView backend (see CLAUDE.md).
  const cachedById = new Map((await db.photos.toArray()).map((p) => [p.id, p]))

  // Split the work: only genuinely-new photos need their thumbnail bytes
  // downloaded. Photos already cached whose metadata drifted just need a
  // cheap in-place field update, reusing the blob already on device.
  const toFetch = remote.filter((r) => !cachedById.has(r.id))
  const toUpdate = remote.filter((r) => {
    const c = cachedById.get(r.id)
    if (!c) return false
    return (
      c.path !== r.path ||
      c.moment !== r.moment ||
      c.momentPath !== r.momentPath ||
      c.date !== r.date ||
      c.favorite !== (r.favorite ? 1 : 0) ||
      // Without this a photo that gains motion on a re-index would keep
      // its stale row forever - the id is unchanged, so presence alone
      // says nothing (see the note above).
      c.hasMotion !== (r.hasMotion ? 1 : 0)
    )
  })

  // Check before writing rather than discovering it mid-loop: exceeding quota
  // throws QuotaExceededError from whatever put() happens to be running, which
  // surfaces as an opaque failure partway through a sync. This turns that into
  // a message naming the actual numbers.
  const needed = toFetch.length * APPROX_THUMB_BYTES
  const { ok, info } = await hasRoomFor(needed)
  if (!ok && info) {
    throw new QuotaError(
      `not enough browser storage: need ~${formatBytes(needed)} more, ` +
        `${formatBytes(info.usageBytes)} of ${formatBytes(info.quotaBytes)} already used`,
    )
  }

  // Both loops report into one total: the update pass is real work the
  // user is waiting on, and counting only downloads meant a re-sync after
  // a rename sat on "0/0" while plainly doing something.
  const total = toFetch.length + toUpdate.length
  let done = 0
  for (const r of toFetch) {
    // thumbnails:false stores metadata only - the timeline appears at once
    // and the grid pulls each thumbnail as it scrolls into view. That is
    // what makes a new device usable immediately instead of after a full
    // library download; "Sync all" then runs this with thumbnails:true to
    // make the whole library offline-capable.
    let blob: Blob | undefined
    if (thumbnails) {
      const resp = await fetch(thumbUrl(r.id), { credentials: 'include' })
      if (!resp.ok) continue
      blob = await resp.blob()
    }
    await db.photos.put({
      id: r.id,
      path: r.path,
      moment: r.moment,
      momentPath: r.momentPath,
      date: r.date,
      favorite: r.favorite ? 1 : 0,
      thumbnailBlob: blob,
      width: r.width ?? 0,
      height: r.height ?? 0,
      isVideo: r.isVideo ? 1 : 0,
      hasMotion: r.hasMotion ? 1 : 0,
      duration: r.duration,
    })
    // Drop any negative-cache entry recorded while this row had no blob yet,
    // so the tile picks the new one up instead of waiting for the TTL.
    invalidateThumbnail(r.id)
    done += 1
    onProgress?.(done, total)
  }

  for (const r of toUpdate) {
    await db.photos.update(r.id, {
      path: r.path,
      moment: r.moment,
      momentPath: r.momentPath,
      date: r.date,
      favorite: r.favorite ? 1 : 0,
      isVideo: r.isVideo ? 1 : 0,
      hasMotion: r.hasMotion ? 1 : 0,
      duration: r.duration,
    })
    done += 1
    onProgress?.(done, total)
  }

  const removedIds = [...cachedById.keys()].filter((id) => !remoteIds.has(id))
  if (removedIds.length) await db.photos.bulkDelete(removedIds)

  return { added: toFetch.length, updated: toUpdate.length, removed: removedIds.length, total: remote.length }
}
