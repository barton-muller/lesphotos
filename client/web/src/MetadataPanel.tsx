import { useEffect, useState } from 'react'
import type { PhotoMeta } from './db'
import { fetchMediaMetadata, type MediaMetadata } from './api'

function formatExposure(seconds: number | null): string {
  if (seconds === null) return '—'
  if (seconds >= 1) return `${seconds}s`
  return `1/${Math.round(1 / seconds)}s`
}

function formatFileSize(bytes: number | undefined): string {
  if (!bytes) return '—'
  const mb = bytes / 1e6
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${(bytes / 1e3).toFixed(0)} KB`
}

function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

interface Row {
  label: string
  value: string
}

function buildRows(photo: PhotoMeta, detail: MediaMetadata): Row[] {
  const rows: Row[] = []

  if (detail.dateShot) rows.push({ label: 'Date taken', value: new Date(detail.dateShot).toLocaleString() })
  if (detail.camera) rows.push({ label: 'Camera', value: [detail.maker, detail.camera].filter(Boolean).join(' ') })
  if (detail.lens) rows.push({ label: 'Lens', value: detail.lens })
  if (detail.aperture) rows.push({ label: 'Aperture', value: `f/${detail.aperture}` })
  if (detail.exposure) rows.push({ label: 'Shutter speed', value: formatExposure(detail.exposure) })
  if (detail.iso) rows.push({ label: 'ISO', value: String(detail.iso) })
  if (detail.focalLength) rows.push({ label: 'Focal length', value: `${detail.focalLength}mm` })
  // Guard the values, not just the object: a photo with no GPS tags can
  // still come back with a coordinates object holding nulls, and .toFixed()
  // on null throws and takes the whole panel down with it.
  if (detail.coordinates?.latitude != null && detail.coordinates?.longitude != null) {
    rows.push({
      label: 'GPS',
      value: `${detail.coordinates.latitude.toFixed(5)}, ${detail.coordinates.longitude.toFixed(5)}`,
    })
  }

  if (detail.isVideo && detail.duration) {
    rows.push({ label: 'Duration', value: formatDuration(detail.duration) })
  }
  rows.push({ label: 'Resolution', value: `${detail.width ?? photo.width}×${detail.height ?? photo.height}` })
  rows.push({ label: 'File size', value: formatFileSize(detail.fileSize) })
  rows.push({ label: 'Moment', value: photo.moment })
  return rows
}

export function MetadataPanel({ photo, onClose }: { photo: PhotoMeta; onClose: () => void }) {
  const [detail, setDetail] = useState<MediaMetadata | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setDetail(null)
    setError(null)
    fetchMediaMetadata(photo.id)
      .then((d) => {
        if (!cancelled) setDetail(d)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      })
    return () => {
      cancelled = true
    }
  }, [photo.id])

  return (
    <div
      data-testid="metadata-panel"
      style={{
        position: 'absolute',
        top: 0,
        right: 0,
        bottom: 0,
        width: 280,
        background: 'rgba(20,20,20,0.92)',
        color: 'white',
        padding: 16,
        overflowY: 'auto',
        fontSize: 13,
      }}
      onClick={(e) => e.stopPropagation()}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 12 }}>
        <strong>Info</strong>
        <button type="button" onClick={onClose} style={{ background: 'none', border: 'none', color: 'white', cursor: 'pointer' }}>
          ✕
        </button>
      </div>
      {error && <div style={{ color: '#f88' }}>Failed to load: {error}</div>}
      {!detail && !error && <div>Loading…</div>}
      {detail && (
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <tbody>
            {buildRows(photo, detail).map((row) => (
              <tr key={row.label}>
                <td style={{ padding: '4px 0', opacity: 0.6, verticalAlign: 'top' }}>{row.label}</td>
                <td style={{ padding: '4px 0', textAlign: 'right' }}>{row.value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
