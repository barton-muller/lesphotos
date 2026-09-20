import { setFavorite } from './api'
import { db, type PhotoMeta } from './db'

// Shared by the grid tile's star button and the viewer's - both need the same
// optimistic-local-then-sync-server behaviour, not two copies of it.
export async function toggleFavorite(photo: PhotoMeta) {
  const nextBool = !photo.favorite
  // Optimistic local update first: the appdb-backed API call is the source
  // of truth server-side, but updating IndexedDB immediately keeps the UI
  // responsive - if the API call fails, this re-syncs back on next sync
  // rather than needing a rollback here.
  await db.photos.update(photo.id, { favorite: nextBool ? 1 : 0 })
  await setFavorite(photo.path, nextBool)
}
