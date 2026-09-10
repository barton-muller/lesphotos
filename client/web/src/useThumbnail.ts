import { useEffect, useState } from 'react'
import { loadThumbnailUrl, peekThumbnailUrl } from './thumbnails'

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

  useEffect(() => {
    if (resident) return
    let cancelled = false
    loadThumbnailUrl(id).then(() => {
      // The tile may have been recycled onto a different photo mid-read.
      if (!cancelled) setLoaded((n) => n + 1)
    })
    return () => {
      cancelled = true
    }
  }, [id, resident])

  return resident
}
