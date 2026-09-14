export type SyncState = 'idle' | 'syncing' | 'done' | 'error'

/**
 * Persistent bottom bar saying what the app is doing.
 *
 * Replaces the sidebar's sync text, which vanished with the left panel. A
 * fixed strip is the right home for it: sync now runs on its own (on login
 * and on regaining focus), so the user never triggers it and needs somewhere
 * consistent to look rather than a message that appears next to a button
 * they did not press.
 *
 * Always shows the metadata/thumbnail counts, not just while something is
 * active: distinguishing "the grid is complete but thumbnails are still
 * arriving as I scroll" from "the whole library isn't here yet" was
 * previously only guessable from watching the grid itself, made worse by
 * metadata sync writing nothing to IndexedDB until the whole library had
 * been fetched - the counts stayed blank for the entire wait.
 *
 * Stays mounted at all times so the bar never changes the page's height as
 * messages come and go.
 */
export function StatusBar({
  syncStatus,
  syncMessage,
  photoCount,
  thumbnailCount,
  downloadingThumbs,
  thumbDownloadMessage,
  offline,
}: {
  syncStatus: SyncState
  syncMessage: string
  photoCount: number | null
  // Rows that have a thumbnailBlob - i.e. actually browsable offline right
  // now, as opposed to just known about. null while the count itself is
  // loading, same convention as photoCount.
  thumbnailCount: number | null
  downloadingThumbs: boolean
  thumbDownloadMessage: string
  // Set when the server can't be reached but the session was kept. The
  // grid still works from IndexedDB, so this is a state to explain, not an
  // error to shout about - "Sync failed: Failed to fetch" reads like the
  // app is broken when the NAS is merely asleep.
  offline?: boolean
}) {
  const counts =
    photoCount === null
      ? ''
      : `${photoCount.toLocaleString()} photos, ${(thumbnailCount ?? 0).toLocaleString()} thumbs`

  const activity =
    syncStatus === 'syncing'
      ? `Loading metadata… ${syncMessage}`
      : downloadingThumbs
        ? `Downloading thumbnails… ${thumbDownloadMessage}`
        : offline
          ? 'Offline'
          : syncStatus === 'error'
            ? `Sync failed: ${syncMessage}`
            : thumbDownloadMessage
              ? thumbDownloadMessage
              : ''

  const text = activity ? `${activity}${counts ? ` — ${counts}` : ''}` : counts

  const active = syncStatus === 'syncing' || downloadingThumbs

  return (
    <div
      data-testid="status-bar"
      style={{
        flexShrink: 0,
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        // Room for the scrubber rail's handle on the right.
        padding: '0 40px 0 12px',
        height: 28,
        fontSize: 12,
        color: syncStatus === 'error' && !offline ? 'var(--danger)' : 'var(--text-muted)',
        borderTop: '1px solid var(--border)',
        background: 'var(--surface)',
        whiteSpace: 'nowrap',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
      }}
    >
      {active && (
        <span
          data-testid="sync-spinner"
          style={{
            width: 8,
            height: 8,
            borderRadius: '50%',
            background: 'var(--accent)',
            flexShrink: 0,
            // Pulses rather than spins: a spinner at this size reads as a
            // smudge, and this needs no keyframe cleanup when it unmounts.
            animation: 'lesphotos-pulse 1.2s ease-in-out infinite',
          }}
        />
      )}
      <span data-testid="status-text">{text}</span>
    </div>
  )
}
