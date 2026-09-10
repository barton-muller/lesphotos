import { previewUrl } from './api'
import type { PhotoMeta } from './db'
import { db } from './db'

// See docs/decisions/fullscreen-viewer-cache.md - bounded LRU cache for
// full-resolution images, kept separate from the always-wanted thumbnail
// cache. Capped by entry count, not total bytes: photo file sizes vary too
// much for a byte cap to be a meaningful thing to size around in the UI.
const MAX_CACHED_FULLRES = 50

export type FullResSource = 'local' | 'server'

export interface FullResResult {
  blob: Blob
  source: FullResSource
}

async function evictOldestIfOverCap() {
  const count = await db.fullres.count()
  if (count <= MAX_CACHED_FULLRES) return
  const toEvict = await db.fullres.orderBy('lastAccessed').limit(count - MAX_CACHED_FULLRES).primaryKeys()
  await db.fullres.bulkDelete(toEvict)
}

// Checks the local bounded cache first (marking it as recently accessed on
// a hit, for the LRU), falling back to fetching the server's preview
// derivative on a miss. The URL is now deterministic from photo_id rather
// than a stored, generated-suffix PhotoView URL, so there is no longer a
// "photo with no full-res URL" case to defend against.
export async function getFullRes(photo: PhotoMeta): Promise<FullResResult | null> {
  const cached = await db.fullres.get(photo.id)
  if (cached) {
    db.fullres.update(photo.id, { lastAccessed: Date.now() }).catch(() => {})
    return { blob: cached.blob, source: 'local' }
  }

  const resp = await fetch(previewUrl(photo.id), { credentials: 'include' })
  if (!resp.ok) throw new Error(`failed to fetch full-res image (${resp.status})`)
  const blob = await resp.blob()

  await db.fullres.put({ id: photo.id, blob, lastAccessed: Date.now() })
  evictOldestIfOverCap().catch(() => {})

  return { blob, source: 'server' }
}
