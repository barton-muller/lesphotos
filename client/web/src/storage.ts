/**
 * Browser storage-quota guards for the thumbnail cache.
 *
 * The cache is a real dependency, not a nicety: after the first sync the whole
 * grid renders from IndexedDB with no network at all (measured: 60 screens of
 * scrolling, 0 requests). Two failure modes of browser storage would silently
 * break that, and both are invisible without asking:
 *
 *   Eviction. Storage is "best-effort" by default, meaning the browser may
 *   discard it under disk pressure with no warning and no event. The user
 *   would just find an empty grid and a full resync running.
 *
 *   Exhaustion. Exceeding the origin's quota throws QuotaExceededError from
 *   whichever db.photos.put() happens to be running, mid-sync, with a message
 *   that says nothing useful about what went wrong.
 *
 * Neither is close at today's scale - ~90MB used against a 3GB quota on this
 * machine, and a 100GB library projects to ~390MB (thumbnails scale with file
 * COUNT, not bytes, and ~8% of files being video makes a video-heavy library
 * much cheaper per GB). These exist so that if it ever does bite, it surfaces
 * as a clear message rather than as a mystery.
 */

/**
 * Ask the browser to keep this origin's storage rather than treating it as
 * discardable cache.
 *
 * Best-effort by design: Chrome grants it silently for an installed PWA or a
 * site with engagement history, Firefox prompts, and Safari ignores the request
 * on iOS. A `false` return is not an error - it means the cache is still
 * eviction-eligible, which is exactly how it behaved before this existed.
 * Called once per session; the browser remembers the grant.
 */
export async function requestPersistentStorage(): Promise<boolean> {
  if (!navigator.storage?.persist) return false
  try {
    if (await navigator.storage.persisted()) return true
    return await navigator.storage.persist()
  } catch {
    return false
  }
}

export interface QuotaInfo {
  usageBytes: number
  quotaBytes: number
  /** Fraction of quota used, 0-1. 0 when the browser reports no quota. */
  ratio: number
}

export async function getQuota(): Promise<QuotaInfo | null> {
  if (!navigator.storage?.estimate) return null
  try {
    const { usage = 0, quota = 0 } = await navigator.storage.estimate()
    return { usageBytes: usage, quotaBytes: quota, ratio: quota > 0 ? usage / quota : 0 }
  } catch {
    return null
  }
}

/**
 * Whether a sync that needs `additionalBytes` more is likely to fit.
 *
 * The 0.9 headroom is deliberate. Browsers enforce quota approximately and some
 * start evicting other origins' data before hard-failing, so aiming for the
 * last 10% trades a clear "not enough space" message for a mid-write
 * QuotaExceededError. A browser that reports no quota at all (older Safari)
 * returns `true` - refusing to sync on missing information would be worse than
 * trying and reporting a real failure.
 */
export async function hasRoomFor(additionalBytes: number): Promise<{ ok: boolean; info: QuotaInfo | null }> {
  const info = await getQuota()
  if (!info || info.quotaBytes === 0) return { ok: true, info }
  return { ok: info.usageBytes + additionalBytes <= info.quotaBytes * 0.9, info }
}

export function formatBytes(bytes: number): string {
  if (bytes >= 2 ** 30) return `${(bytes / 2 ** 30).toFixed(1)}GB`
  if (bytes >= 2 ** 20) return `${Math.round(bytes / 2 ** 20)}MB`
  return `${Math.round(bytes / 1024)}KB`
}
