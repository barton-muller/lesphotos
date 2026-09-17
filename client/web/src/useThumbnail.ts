import { useEffect, useState } from 'react'
import { loadThumbnailUrl, peekThumbnailUrl, releaseThumbnail, retainThumbnail } from './thumbnails'

/**
 * The thumbnail URL for one photo, or null while it loads.
 *
 * The resident URL is read during render rather than synced through an effect:
 * scrolling back over photos just seen must not flash a placeholder, and this
 * runs once per visible tile per scroll frame, so an extra render each would be
 * the most expensive thing on the path.
 *
 * `loaded` exists only to wake the component when an async read lands; the URL
 * itself always comes from the store, which is the single source of truth for
 * what is resident.
 */
export function useThumbnail(id: string): string | null {
  const [, setLoaded] = useState(0)
  const resident = peekThumbnailUrl(id)

  // Retain for as long as this tile is showing THIS photo, keyed on id
  // alone. Keying on `resident` too would release and re-retain every time
  // the URL resolves, dropping the count to zero while the tile is still
  // mounted - which aborts the very fetch it is waiting for.
  useEffect(() => {
    retainThumbnail(id)
    return () => releaseThumbnail(id)
  }, [id])

  useEffect(() => {
    if (resident) return
    let cancelled = false
    // allowNetwork: false - the app is designed to run fully offline
    // (per "Download All" / downloadAllThumbnails), so a tile scrolling
    // into view reads only what's already in IndexedDB rather than
    // hitting the network itself. Network-driven scroll-loading also
    // directly competed with a running bulk download: both write
    // db.photos.update() for the same row, and IndexedDB serializes
    // readwrite transactions per store, so scrolling during "Download
    // All" measurably slowed it down. A tile with nothing resident
    // just stays a placeholder until Download All (or a future sync)
    // fills it in.
    loadThumbnailUrl(id, { allowNetwork: false }).then(() => {
      // The tile may have been recycled onto a different photo mid-read.
      if (!cancelled) setLoaded((n) => n + 1)
    })
    return () => {
      cancelled = true
    }
  }, [id, resident])

  return resident
}
