import { useEffect, useRef, useState } from 'react'
import type { PhotoMeta } from './db'
import { compatUrl, motionUrl, originalUrl } from './api'
import { shareOrDownloadPhoto } from './download'
import { getFullRes, type FullResSource } from './fullres'
import { MetadataPanel } from './MetadataPanel'
import { useThumbnail } from './useThumbnail'
import { useBackToClose } from './useBackToClose'

interface PhotoViewerProps {
  photos: PhotoMeta[]
  index: number
  onClose: () => void
  onNavigate: (index: number) => void
}

// Full-screen overlay: shows the already-cached thumbnail immediately
// (zoomed to fill the screen), then swaps to full resolution once it
// loads - see docs/decisions/fullscreen-viewer-cache.md for the full
// design and why. Never a blank screen while a thumbnail is on-device.
export function PhotoViewer({ photos, index, onClose, onNavigate }: PhotoViewerProps) {
  const photo = photos[index]

  // Reset per photo, keyed on photo.id rather than the photo object:
  // Timeline builds fresh objects every render, so an object dep would
  // reset this constantly (the same trap that once broke the full-res
  // fetch - see CLAUDE.md). Without the reset, one unplayable video would
  // pin every subsequent one to the lower-resolution transcode.
  const [useCompat, setUseCompat] = useState(false)

  // Live Photo / Motion Photo playback. Two independent gestures:
  //   - the play button LATCHES a loop on, and stops it when pressed again
  //   - press-and-hold plays only while held
  // They are separate state rather than one flag, so releasing a hold
  // never cancels a loop the button had started.
  const [motionLatched, setMotionLatched] = useState(false)
  const [motionHeld, setMotionHeld] = useState(false)
  const motionPlaying = motionLatched || motionHeld
  const motionRef = useRef<HTMLVideoElement | null>(null)
  // When a press-and-hold started, so the click that ends it can be told
  // apart from a normal tap. Tapping the image still closes the viewer;
  // holding it to watch the motion must not, which is what made the clip
  // vanish the instant you let go.
  const holdStartedAt = useRef(0)
  useEffect(() => {
    setUseCompat(false)
    setMotionLatched(false)
    setMotionHeld(false)
  }, [photo.id])

  // Restart from the beginning each time playback starts: a Live Photo is
  // ~1s, so resuming mid-clip would usually mean showing almost nothing.
  useEffect(() => {
    const el = motionRef.current
    if (!el) return
    if (motionPlaying) {
      el.currentTime = 0
      // play() rejects if the browser blocks autoplay or cannot decode the
      // clip (Google motion photos are HEVC, which Firefox will not play).
      // Failing back to the still is the right outcome, not an error.
      el.play().catch(() => {
        setMotionLatched(false)
        setMotionHeld(false)
      })
    } else {
      el.pause()
    }
  }, [motionPlaying, photo.id])
  const videoSrc = useCompat ? compatUrl(photo.id) : originalUrl(photo.id)
  // Shared with the grid, so opening a photo reuses the thumbnail already
  // resident rather than minting a second object URL for the same blob (the
  // old useMemo here also never revoked its URL).
  const thumbnailUrl = useThumbnail(photo.id)

  const [fullResUrl, setFullResUrl] = useState<string | null>(null)
  const [fullResBlob, setFullResBlob] = useState<Blob | null>(null)
  const [source, setSource] = useState<FullResSource | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showInfo, setShowInfo] = useState(false)
  // Android's back gesture closes the photo instead of leaving the app.
  useBackToClose(true, onClose)
  const [shareStatus, setShareStatus] = useState('')

  // The object URL is revoked when the NEXT one is ready (or on unmount),
  // not in this effect's own cleanup. Revoking in cleanup tore down a
  // freshly-created blob URL every time the effect re-ran, and the re-run's
  // own `cancelled` flag then discarded its result - so the full-res image
  // silently never appeared and the source badge never rendered.
  //
  // The effect re-runs more than you would expect for two reasons, which is
  // why this was intermittent rather than always broken:
  //   - StrictMode double-invokes every effect (setup, cleanup, setup).
  //   - The dep list is photo.id, NOT the `photo` object: Timeline builds
  //     fresh CachedPhoto objects on each render, so an object-identity dep
  //     re-fired this on every unrelated re-render (a sync tick, a hover),
  //     cancelling the in-flight fetch each time.
  // Both predate the backend switch. PhotoView's slower highRes fetch just
  // meant a later run usually still won the race; serving previews from a
  // local cache made the fetch fast enough to lose, which is how it
  // surfaced - a timing change exposing the bug, not causing it.
  const lastObjectUrl = useRef<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setFullResUrl(null)
    setSource(null)
    setError(null)

    setFullResBlob(null)
    setShareStatus('')

    if (photo.isVideo) return

    getFullRes(photo)
      .then((result) => {
        if (cancelled || !result) return
        if (lastObjectUrl.current) URL.revokeObjectURL(lastObjectUrl.current)
        const objectUrl = URL.createObjectURL(result.blob)
        lastObjectUrl.current = objectUrl
        setFullResUrl(objectUrl)
        setFullResBlob(result.blob)
        setSource(result.source)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      })

    return () => {
      cancelled = true
    }
  }, [photo.id, photo.isVideo])

  // Revoke the last outstanding blob URL when the viewer closes for good.
  // Empty deps deliberately: this must run only on unmount, not per photo -
  // per-photo revocation is handled above, when the replacement is ready.
  useEffect(() => {
    return () => {
      if (lastObjectUrl.current) URL.revokeObjectURL(lastObjectUrl.current)
    }
  }, [])

  async function handleShare() {
    if (!fullResBlob) return
    setShareStatus('…')
    try {
      const result = await shareOrDownloadPhoto(photo, fullResBlob)
      setShareStatus(result === 'shared' ? 'Shared' : result === 'saved-mirrored' ? 'Saved' : 'Downloaded')
      setTimeout(() => setShareStatus(''), 2000)
    } catch (err) {
      setShareStatus(`Failed: ${err instanceof Error ? err.message : String(err)}`)
    }
  }

  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
      else if (e.key === 'ArrowLeft' && index > 0) onNavigate(index - 1)
      else if (e.key === 'ArrowRight' && index < photos.length - 1) onNavigate(index + 1)
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [index, photos.length, onClose, onNavigate])

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        background: 'black',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 1000,
      }}
      onClick={onClose}
    >
      <div
        data-testid="viewer-moment-title"
        style={{
          position: 'absolute',
          top: 12,
          left: '50%',
          transform: 'translateX(-50%)',
          color: 'white',
          fontSize: 14,
          fontWeight: 600,
          textShadow: '0 1px 3px rgba(0,0,0,0.8)',
          maxWidth: '60%',
          textAlign: 'center',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
          pointerEvents: 'none',
        }}
      >
        {photo.moment}
      </div>

      {photo.isVideo ? (
        // Streamed from the server rather than served from a cached blob:
        // the originals are large and unmodified (no transcoding - see
        // docs/decisions/replacing-photoview.md), and FileResponse handles
        // range requests, so <video> can seek without downloading the whole
        // file first. The already-cached thumbnail is the poster, so the
        // frame is filled instantly rather than showing black while the
        // first bytes arrive.
        //
        // ~95% of the real library is HEVC, which Safari/iOS decode
        // natively and Firefox does not. Rather than sniff the browser
        // server-side, play the original and swap to the host's H.264
        // transcode only once playback has actually failed - so the
        // majority who can play the original keep its full resolution
        // (1920x1440 against the transcode's 720x960), and the rest get
        // something that plays. /compat 404s where no transcode exists,
        // which leaves the element in its normal error state.
        <video
          key={videoSrc}
          src={videoSrc}
          poster={thumbnailUrl ?? undefined}
          data-testid="viewer-video"
          controls
          autoPlay
          playsInline
          style={{ maxWidth: '100%', maxHeight: '100%' }}
          onClick={(e) => e.stopPropagation()}
          onError={() => setUseCompat(true)}
        />
      ) : (
        <div
          style={{
            position: 'relative',
            display: 'flex',
            // Fixed to the full viewport (not shrink-wrapped to the child
            // <img>'s intrinsic size) so the img's own width/height: 100%
            // below has something stable to fill - see the img's comment.
            width: '100vw',
            height: '100vh',
          }}
          // Press-and-hold plays for as long as the press lasts. Pointer
          // events rather than touch/mouse pairs so one handler covers
          // finger, pen and mouse. onPointerLeave/Cancel matter: a finger
          // sliding off the image, or the browser stealing the gesture for
          // a scroll, both need to stop playback or it sticks on.
          onPointerDown={
            photo.hasMotion
              ? () => {
                  holdStartedAt.current = Date.now()
                  setMotionHeld(true)
                }
              : undefined
          }
          onPointerUp={photo.hasMotion ? () => setMotionHeld(false) : undefined}
          onPointerLeave={photo.hasMotion ? () => setMotionHeld(false) : undefined}
          onPointerCancel={photo.hasMotion ? () => setMotionHeld(false) : undefined}
          onClick={(e) => {
            // A hold ends in a click, which would otherwise bubble to the
            // backdrop and close the viewer. 250ms separates a deliberate
            // hold from a tap; below that, let the click through so a plain
            // tap on the image still closes as it always did.
            if (photo.hasMotion && Date.now() - holdStartedAt.current > 250) {
              e.stopPropagation()
            }
          }}
        >
        <img
          src={fullResUrl ?? thumbnailUrl ?? undefined}
          alt={photo.moment}
          data-testid="viewer-image"
          data-loaded={fullResUrl ? 'fullres' : 'thumbnail'}
          style={{
            // Explicit width/height, not just max-width/max-height: without
            // these the box sizes to the <img>'s intrinsic pixel dimensions,
            // which are tiny for the thumbnail and huge for the full-res
            // photo - so swapping src from one to the other visibly grew the
            // image on screen. Forcing both sources into the same
            // fit-to-screen box makes the full-res swap a pure sharpen in
            // place.
            width: '100%',
            height: '100%',
            maxWidth: '100%',
            maxHeight: '100%',
            objectFit: 'contain',
            // Kept mounted under the clip rather than swapped out, so the
            // frame never flashes empty between still and video.
            visibility: motionPlaying ? 'hidden' : 'visible',
          }}
          onClick={(e) => e.stopPropagation()}
        />
        {photo.hasMotion && (
          <video
            ref={motionRef}
            src={motionUrl(photo.id)}
            data-testid="motion-video"
            loop
            muted
            playsInline
            preload="none"
            style={{
              position: 'absolute',
              inset: 0,
              width: '100%',
              height: '100%',
              objectFit: 'contain',
              display: motionPlaying ? 'block' : 'none',
              pointerEvents: 'none',
            }}
          />
        )}
        </div>
      )}

      {source && (
        <div
          data-testid="source-badge"
          style={{
            position: 'absolute',
            top: 12,
            left: 12,
            background: 'rgba(0,0,0,0.6)',
            color: 'white',
            padding: '4px 10px',
            borderRadius: 12,
            fontSize: 12,
          }}
        >
          {source === 'local' ? '📱 Local' : '☁️ Server'}
        </div>
      )}

      {error && (
        <div
          style={{
            position: 'absolute',
            bottom: 12,
            left: 12,
            background: 'rgba(200,0,0,0.7)',
            color: 'white',
            padding: '4px 10px',
            borderRadius: 8,
            fontSize: 12,
          }}
        >
          Full-res load failed: {error}
        </div>
      )}

      <button
        type="button"
        data-testid="share-button"
        onClick={(e) => {
          e.stopPropagation()
          handleShare()
        }}
        disabled={!fullResBlob}
        style={{ ...closeButtonStyle, right: 100 }}
      >
        ⇪
      </button>
      {shareStatus && (
        <div
          data-testid="share-status"
          style={{
            position: 'absolute',
            top: 14,
            right: 140,
            color: 'white',
            fontSize: 12,
          }}
        >
          {shareStatus}
        </div>
      )}

      {photo.hasMotion && (
        <button
          type="button"
          data-testid="motion-button"
          aria-pressed={motionLatched}
          title={motionLatched ? 'Stop' : 'Play Live Photo'}
          onClick={(e) => {
            e.stopPropagation()
            // Latches: this toggle is independent of press-and-hold, so
            // letting go of a hold never stops a loop started here.
            setMotionLatched((v) => !v)
          }}
          style={{
            ...closeButtonStyle,
            right: 100,
            // Explicit centring: the glyph is a text character, not an
            // emoji, and without flex it sits off-centre in the circle.
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: 14,
            // Variation selector 15 (below) asks for the TEXT rendering of
            // these characters. Left to itself Safari draws them as
            // full-width emoji, which overflows a 36px circle and collides
            // with the neighbouring button.
            fontVariantEmoji: 'text',
          }}
        >
          {motionLatched ? '\u23F8\uFE0E' : '\u25B6\uFE0E'}
        </button>
      )}

      <button
        type="button"
        data-testid="info-button"
        onClick={(e) => {
          e.stopPropagation()
          setShowInfo((v) => !v)
        }}
        style={{ ...closeButtonStyle, right: 56 }}
      >
        ⓘ
      </button>

      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation()
          onClose()
        }}
        style={closeButtonStyle}
      >
        ✕
      </button>

      {showInfo && <MetadataPanel photo={photo} onClose={() => setShowInfo(false)} />}

      {index > 0 && (
        <button
          type="button"
          data-testid="viewer-prev"
          onClick={(e) => {
            e.stopPropagation()
            onNavigate(index - 1)
          }}
          style={{ ...navButtonStyle, left: 12 }}
        >
          ‹
        </button>
      )}
      {index < photos.length - 1 && (
        <button
          type="button"
          data-testid="viewer-next"
          onClick={(e) => {
            e.stopPropagation()
            onNavigate(index + 1)
          }}
          style={{ ...navButtonStyle, right: 12 }}
        >
          ›
        </button>
      )}
    </div>
  )
}

const closeButtonStyle: React.CSSProperties = {
  position: 'absolute',
  top: 12,
  right: 12,
  background: 'rgba(0,0,0,0.6)',
  color: 'white',
  border: 'none',
  borderRadius: '50%',
  width: 36,
  height: 36,
  fontSize: 18,
  cursor: 'pointer',
}

const navButtonStyle: React.CSSProperties = {
  position: 'absolute',
  top: '50%',
  transform: 'translateY(-50%)',
  background: 'rgba(0,0,0,0.4)',
  color: 'white',
  border: 'none',
  borderRadius: '50%',
  width: 48,
  height: 48,
  fontSize: 28,
  cursor: 'pointer',
}
