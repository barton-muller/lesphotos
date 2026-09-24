import { useVirtualizer } from '@tanstack/react-virtual'
import { memo, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'
import { createMomentFromPhotos, isStaleEditError, mergeMoments, renameMoment, suggestMomentName } from './api'
import { db, getPhotosVersion, subscribePhotosVersion, type PhotoMeta } from './db'
import { toggleFavorite } from './favorites'
import { useThumbnail } from './useThumbnail'
import { usePinchZoom } from './usePinchZoom'
import { StarIcon } from './StarIcon'
import { RangeIcon } from './RangeIcon'
import { prefetchThumbnails, setThumbnailBudget } from './thumbnails'
import { PhotoViewer } from './PhotoViewer'
import { computeMonthBuckets, YearScrubber } from './YearScrubber'
import { RailScrubber } from './rail/RailScrubber'

const GAP = 4
// Measured from the rendered <h3>, not estimated - see estimateSize below.
const HEADER_HEIGHT = 29
// Matches YearScrubber's own layout (right: 16, width: 124) plus a little
// breathing room, so the grid's last column never sits flush against the
// scrubber track - confirmed necessary via a real screenshot at grid
// density 15/row, where the rightmost thumbnails visibly overlapped it.
const SCRUBBER_RESERVED_WIDTH = 148

// Width the collapsed rail handle occupies on a phone. Wide enough to be an
// easy thumb target at the screen edge without eating grid width.
const NARROW_RAIL_WIDTH = 28

// Breathing room at the grid's left edge, so tiles aren't flush to the window.
const GRID_EDGE_PADDING = 8

// Which scrubber implementation renders. 'v5' is the month-band rail in
// src/rail/ (self-contained: layout maths, rendering and the grid adapter all
// live there); 'v4' is the original fisheye YearScrubber. Kept switchable so
// the two can be compared against a real library without a checkout.
const SCRUBBER: 'v4' | 'v5' = 'v5'

// Persisted so the rail can show a stable start-to-end range from the very
// first render on any load after the first - see oldestMomentYear's own
// comment at its declaration.
const OLDEST_MOMENT_YEAR_KEY = 'lesphotos-oldest-moment-year'

// Rows rendered beyond the viewport on each side. Also feeds the thumbnail
// cache budget below - these rows are mounted, so they demand thumbnails.
const OVERSCAN_ROWS = 12

interface MomentGroup {
  moment: string
  momentPath: string
  photos: PhotoMeta[]
}

// Flattened row list the virtualizer scrolls over: a moment header is its
// own row, and each moment's photos are chunked into fixed-column rows.
// This is the "bucket by moment, virtualize the rows within/across
// buckets" pattern from the research (see plan.md's virtualization
// section) - simpler than Immich's month/day hierarchy since a "moment"
// here is already a bounded, pre-grouped unit from the server side.
type TimelineRow =
  | { type: 'header'; moment: string; momentPath: string }
  | { type: 'photos'; photos: PhotoMeta[] }

function groupByMoment(photos: PhotoMeta[]): MomentGroup[] {
  const groups = new Map<string, MomentGroup>()
  for (const photo of photos) {
    const existing = groups.get(photo.moment)
    if (existing) {
      existing.photos.push(photo)
    } else {
      groups.set(photo.moment, { moment: photo.moment, momentPath: photo.momentPath, photos: [photo] })
    }
  }
  // Photos already come back date-descending from PhotoView's timeline;
  // Map preserves insertion order, so groups stay in that same order.
  return [...groups.values()]
}

function chunkRows(groups: MomentGroup[], columns: number): TimelineRow[] {
  const rows: TimelineRow[] = []
  for (const group of groups) {
    rows.push({ type: 'header', moment: group.moment, momentPath: group.momentPath })
    for (let i = 0; i < group.photos.length; i += columns) {
      rows.push({ type: 'photos', photos: group.photos.slice(i, i + columns) })
    }
  }
  return rows
}

const MONTH_FULL = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
]

function ordinal(day: number): string {
  // 11th/12th/13th are the exceptions to the 1st/2nd/3rd pattern.
  if (day % 100 >= 11 && day % 100 <= 13) return `${day}th`
  return `${day}${['th', 'st', 'nd', 'rd'][day % 10] ?? 'th'}`
}

/**
 * "2025-03-15-spain-holiday" -> "2025 March 15th - Spain Holiday".
 * Folder names are the storage format; this is the display format. A
 * folder without a recognisable date prefix (or without a label after it)
 * degrades to showing whatever it does have rather than inventing one.
 */
export function formatMomentTitle(folderName: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:-(.*))?$/.exec(folderName)
  if (!m) return folderName
  const [, year, mon, day, rawLabel] = m
  const date = `${year} ${MONTH_FULL[Number(mon) - 1] ?? mon} ${ordinal(Number(day))}`
  if (!rawLabel) return date
  // "moment" is this app's own placeholder for an unnamed moment - showing
  // "- Moment" would be noise, so a placeholder collapses to just the date.
  if (rawLabel === 'moment') return date
  const label = rawLabel
    .split('-')
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ')
  return `${date} - ${label}`
}

interface ThumbnailProps {
  photo: PhotoMeta
  tileSize: number
  onOpen: () => void
  selectionMode: boolean
  selected: boolean
  onToggleSelect: () => void
  /** Extend the selection from the anchor to this photo. */
  onSelectRangeTo: () => void
  /** True once an anchor exists, so extending is meaningful. */
  canExtend: boolean
  /** The range's own start: extending "to" it would be a no-op. */
  isAnchor: boolean
}

// Object URLs are cached per photo id and deliberately never revoked
// during a session. The virtualizer unmounts and remounts tiles constantly
// while scrolling; creating a fresh URL each time both leaked one URL per
// remount (nothing revoked them) and forced the browser to decode the same
// JPEG again from scratch, which showed up directly in frame timings -
function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const sec = Math.round(seconds % 60)
  return `${m}:${sec.toString().padStart(2, '0')}`
}

/**
 * Renders the grid's own controls into the app header.
 *
 * A portal rather than lifting this state into App: the selection and merge
 * state is used entirely inside Timeline, and hoisting it would spread one
 * feature across two components purely to satisfy where its buttons are drawn.
 *
 * The slot is looked up in an effect, not during render - it belongs to a
 * parent that has not necessarily committed to the DOM when this first runs.
 */
function Toolbar({ children }: { children: React.ReactNode }) {
  // eslint-disable-next-line react-hooks/set-state-in-effect -- the slot is a
  // DOM node owned by a parent that has not necessarily committed when this
  // first renders, so it genuinely cannot be read during render.
  const [slot, setSlot] = useState<HTMLElement | null>(null)
  useEffect(() => {
    setSlot(document.getElementById('lesphotos-toolbar-slot'))
  }, [])
  if (!slot) return null
  return createPortal(children, slot)
}

const Thumbnail = memo(function Thumbnail({ photo, tileSize, onOpen, selectionMode, selected, onToggleSelect, onSelectRangeTo, canExtend, isAnchor }: ThumbnailProps) {
  const url = useThumbnail(photo.id)
  // Hover reveals the range control on desktop. Touch has no hover, which is
  // why showRange also covers the range's own ends - see ThumbnailProps.
  const [hovered, setHovered] = useState(false)
  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        position: 'relative',
        width: '100%',
        height: tileSize,
        // Grey placeholder behind every tile. The box is already the right
        // size from the grid, so a thumbnail arriving never shifts layout -
        // it just replaces the grey.
        background: url ? undefined : 'var(--surface-hover, #f0f0f0)',
        borderRadius: 4,
      }}
    >
      {url ? (
      <img
        src={url}
        // The moment name, not photo.id: alt text is what the browser
        // renders when the image has no source, so a raw UUID was appearing
        // in the grid whenever a blob wasn't resident yet.
        alt={photo.moment}
        onClick={(e) => {
          if (!selectionMode) return onOpen()
          // Shift-click is the desktop idiom for "everything up to here".
          if (e.shiftKey) return onSelectRangeTo()
          onToggleSelect()
        }}
        style={{
          width: '100%',
          height: tileSize,
          objectFit: 'cover',
          borderRadius: 4,
          cursor: 'pointer',
          // Selection takes priority when both are true (selection is a
          // transient action-in-progress state, favorite is a persistent
          // photo property) - a gold border replaces the earlier ★/☆ badge
          // button (see docs/decisions/favorite-border.md): the star
          // glyph's visual weight isn't centered in its own em box, which
          // read as visibly off-center in a small circular badge - a border
          // has no such centering problem and is less visually intrusive.
          outline: selected ? '3px solid var(--selection)' : photo.favorite ? '3px solid var(--accent)' : 'none',
          outlineOffset: -3,
        }}
        loading="lazy"
        // Lets the browser decode off the main thread instead of
        // synchronously during the frame that paints the tile - the
        // decode work is the same, but it stops landing inside the
        // scroll frame's budget.
        decoding="async"
        // The blob's real pixel dimensions, so the browser can size and
        // decode without a layout round-trip. Not for layout (the style
        // above fixes the box) - purely to give the decoder what it needs
        // up front.
        width={photo.width}
        height={photo.height}
      />
      ) : (
        // The placeholder carries the same click target and selection/favorite
        // outline as the real tile, so a fast scroll stays fully interactive
        // instead of dead until its thumbnail arrives.
        <div
          onClick={(e) => {
            if (!selectionMode) return onOpen()
            if (e.shiftKey) return onSelectRangeTo()
            onToggleSelect()
          }}
          style={{
            width: '100%',
            height: tileSize,
            borderRadius: 4,
            cursor: 'pointer',
            outline: selected
              ? '3px solid var(--selection)'
              : photo.favorite
                ? '3px solid var(--accent)'
                : 'none',
            outlineOffset: -3,
          }}
        />
      )}
      {photo.isVideo ? (
        // Video tiles are otherwise identical to photo tiles - same
        // thumbnail pipeline, same grid - so a badge is what distinguishes
        // them. pointerEvents:'none' so the badge never eats the click that
        // opens or selects the tile underneath it.
        <div
          data-testid="video-badge"
          style={{
            position: 'absolute',
            bottom: 6,
            right: 6,
            display: 'flex',
            alignItems: 'center',
            gap: 4,
            padding: '2px 6px 2px 4px',
            borderRadius: 10,
            background: 'rgba(0,0,0,0.55)',
            color: 'white',
            fontSize: 11,
            fontVariantNumeric: 'tabular-nums',
            pointerEvents: 'none',
          }}
        >
          <span style={{ fontSize: 9 }}>▶</span>
          {photo.duration ? formatDuration(photo.duration) : null}
        </div>
      ) : null}
      {selectionMode ? (
        <div
          data-testid="selection-checkmark"
          style={{
            position: 'absolute',
            top: 4,
            right: 4,
            border: '2px solid white',
            background: selected ? 'var(--selection)' : 'rgba(0,0,0,0.4)',
            borderRadius: '50%',
            width: 24,
            height: 24,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: 'white',
            fontSize: 14,
            pointerEvents: 'none',
          }}
        >
          {selected ? '✓' : ''}
        </div>
      ) : null}

      {/* Touch equivalent of shift-click: no press-and-hold, no drag. Tap one
          photo, then tap this on another and everything between is selected.

          Shown on SELECTED photos too, not just unselected ones: the range is
          set rather than grown, so tapping a nearer photo pulls it back. That
          makes overshooting one tap to fix instead of something to undo, which
          is the actual annoyance with hold-and-drag range selection.

          An icon rather than a "to here" pill so it fits a small tile without
          covering the photo, and reads the same on every platform. */}
      {selectionMode && canExtend && !isAnchor ? (
        <button
          type="button"
          data-testid="range-extend"
          onClick={(e) => {
            e.stopPropagation()
            onSelectRangeTo()
          }}
          aria-label="Select from the first photo through to here"
          title="Select through to here"
          style={{
            position: 'absolute',
            left: 3,
            bottom: 3,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            width: 22,
            height: 22,
            padding: 0,
            color: 'white',
            // Deliberately quiet. This sits on every tile in selection mode,
            // because touch has no hover to reveal it on demand - so it has to
            // be legible without competing with the photo. A dark disc at low
            // opacity in the corner opposite the checkmark does that; at full
            // strength on every tile the grid was unreadable.
            opacity: hovered || selected ? 0.95 : 0.5,
            background: 'rgba(0,0,0,0.45)',
            border: 'none',
            borderRadius: '50%',
            cursor: 'pointer',
            transition: 'opacity 120ms ease',
          }}
        >
          <RangeIcon size={13} />
        </button>
      ) : null}

      {!selectionMode ? (
        <button
          type="button"
          className="favorite-toggle"
          onClick={() => toggleFavorite(photo)}
          aria-label={photo.favorite ? 'Unfavorite' : 'Favorite'}
          style={{
            position: 'absolute',
            top: 2,
            right: 2,
            border: 'none',
            // No circular chip behind the star. The chip was what made the
            // glyph look off-centre - a star's ink isn't centred in its em
            // box, so it always sat visibly high/left inside a circle, and
            // the error grew more obvious the further a tile was from the
            // eye's reference point. A bare glyph has nothing to be
            // off-centre *within*.
            background: 'none',
            padding: 4,
            lineHeight: 1,
            width: 'auto',
            height: 'auto',
            cursor: 'pointer',
            color: photo.favorite ? 'var(--accent)' : 'white',
            // A drop-shadow keeps a white star legible on pale photos now
            // that there's no dark chip behind it.
            filter: 'drop-shadow(0 1px 2px rgba(0,0,0,0.65))',
            // A ★/☆ text glyph rendered differently on every platform (and as
            // colour emoji on some Android builds); the SVG below is identical
            // everywhere. See StarIcon.
            display: 'flex',
            // The gold border (above) is now the primary "is this
            // favorited" signal - this button just needs to stay
            // discoverable/clickable, not loudly visible on every tile.
            // Always-subtle when already favorited; only shown on hover
            // when not, via the .favorite-toggle CSS rule in index.css
            // (inline styles can't express :hover).
            opacity: photo.favorite ? 0.85 : undefined,
          }}
        >
          <StarIcon filled={!!photo.favorite} size={16} />
        </button>
      ) : null}
    </div>
  )
})

function MomentHeader({
  moment,
  momentPath,
  onNeedsSync,
  mergeMode,
  mergeSelected,
  onToggleMergeSelect,
}: {
  moment: string
  momentPath: string
  onNeedsSync: () => void
  mergeMode: boolean
  mergeSelected: boolean
  onToggleMergeSelect: () => void
}) {
  // "2025-03-15-spain-holiday" -> date "2025-03-15", label "spain-holiday".
  // A folder without a recognisable date prefix (shouldn't happen given
  // _with_date_prefix server-side, but the client shouldn't crash on one)
  // edits as a label-only moment, same as before this split existed.
  const dateMatch = /^(\d{4}-\d{2}-\d{2})(?:-(.*))?$/.exec(moment)
  const initialDate = dateMatch ? dateMatch[1] : ''
  const initialLabel = dateMatch ? (dateMatch[2] ?? '') : moment

  const [editing, setEditing] = useState(false)
  const [date, setDate] = useState(initialDate)
  const [label, setLabel] = useState(initialLabel)
  const [status, setStatus] = useState('')

  // Folder names are the storage format (CLAUDE.md: moments are
  // "YYYY-MM-DD-Label" directories) - a label typed with spaces or
  // punctuation would otherwise produce a bad folder name, so mistakes are
  // normalized rather than rejected: lowercase, spaces/underscores become
  // hyphens, anything else not safe in a folder name is dropped, and
  // repeated/edge hyphens are collapsed.
  function sanitizeLabel(raw: string): string {
    return raw
      .trim()
      .toLowerCase()
      .replace(/[\s_]+/g, '-')
      .replace(/[^a-z0-9-]/g, '')
      .replace(/-+/g, '-')
      .replace(/^-|-$/g, '')
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
      setStatus('Date must be YYYY-MM-DD')
      return
    }
    setStatus('Renaming…')
    try {
      await renameMoment(momentPath, sanitizeLabel(label), date)
      setStatus('Renamed')
      setEditing(false)
      // PhotoView identifies media by MD5(path) (see CLAUDE.md invariant
      // #1) - a rename is a delete+recreate under a new id, so the client's
      // IndexedDB cache holds stale rows for these photos until the next
      // sync. Trigger it automatically instead of leaving a note asking
      // the user to remember to do it, which left stale/duplicate-looking
      // rows on screen.
      onNeedsSync()
    } catch (err) {
      // A stale path means another device already changed this moment -
      // resync so this client catches up to the real state instead of
      // just showing a dead-end error for a "failure" that already
      // succeeded elsewhere.
      if (isStaleEditError(err)) {
        setStatus('This moment changed elsewhere - refreshing…')
        onNeedsSync()
      } else {
        setStatus(`Rename failed: ${err instanceof Error ? err.message : String(err)}`)
      }
    }
  }

  async function suggest() {
    setStatus('Looking up location…')
    try {
      const result = await suggestMomentName(momentPath)
      if (result.suggestion) {
        setLabel(result.suggestion)
        setStatus('')
      } else {
        setStatus('No GPS data found for this moment')
      }
    } catch (err) {
      setStatus(`Suggestion failed: ${err instanceof Error ? err.message : String(err)}`)
    }
  }

  if (editing && !mergeMode) {
    return (
      <form onSubmit={submit} style={{ display: 'flex', gap: 8, alignItems: 'center', margin: '8px 0 4px' }}>
        <input
          data-testid="rename-date-input"
          type="date"
          value={date}
          onChange={(e) => setDate(e.target.value)}
        />
        <input data-testid="rename-input" value={label} onChange={(e) => setLabel(e.target.value)} />
        <button type="submit">Save</button>
        <button type="button" onClick={() => setEditing(false)}>
          Cancel
        </button>
        <button type="button" data-testid="suggest-name-button" onClick={suggest} style={{ fontSize: 12 }}>
          📍 Suggest name
        </button>
        <span>{status}</span>
      </form>
    )
  }

  return (
    <h3
      style={{
        // Margins are part of the row's reserved height (HEADER_HEIGHT).
        // The previous 12px/6px pair pushed the real footprint past the
        // reserved row, so headers overlapped the photos below them.
        margin: 0,
        height: HEADER_HEIGHT,
        display: 'flex',
        gap: 8,
        alignItems: 'center',
        fontSize: 14,
        fontWeight: 600,
        color: 'var(--text)',
        // No uppercase transform - a formatted title like "2025 March 15th
        // - Spain Holiday" is meant to read as prose, not a label.
        letterSpacing: 0,
        cursor: mergeMode ? 'pointer' : 'default',
        overflow: 'hidden',
        whiteSpace: 'nowrap',
      }}
      onClick={mergeMode ? onToggleMergeSelect : undefined}
    >
      {mergeMode && (
        <input
          type="checkbox"
          data-testid="merge-select-checkbox"
          checked={mergeSelected}
          onChange={onToggleMergeSelect}
          onClick={(e) => e.stopPropagation()}
        />
      )}
      {formatMomentTitle(moment)}
      {!mergeMode && (
        <button type="button" onClick={() => setEditing(true)} style={{ fontSize: 12 }}>
          Rename
        </button>
      )}
      {status && <span style={{ fontSize: 12, fontWeight: 'normal' }}>{status}</span>}
    </h3>
  )
}

export function Timeline({
  starredOnly,
  columns,
  onNeedsSync,
  onColumnsChange,
  narrow = false,
}: {
  starredOnly: boolean
  columns: number
  onNeedsSync: () => void
  /** Pinch-to-zoom changes grid density; there is no slider any more. */
  onColumnsChange: (next: number) => void
  /** Phone layout: the rail collapses to a touch handle, see RailScrubber. */
  narrow?: boolean
}) {
  // starredOnly uses the `favorite` index added in db.ts's v2 schema
  // (db.photos.where('favorite').equals(1)) rather than loading everything
  // and filtering in JS - matters once the library is large (the real
  // ~28,000-photo/user target), not so much at today's test scale, but
  // cheap to do correctly from the start.
  // Metadata only - thumbnailBlob lives in its own `thumbnails` table (see
  // db.ts's v9 schema), so this read never touches image bytes at all.
  //
  // Loading blobs in this query used to couple scroll smoothness to image
  // data: the grid could not lay out until every thumbnail in the library
  // was in one JS array, and useLiveQuery re-ran the whole thing on any
  // write. Thumbnails now stream in per visible tile via useThumbnail();
  // this query stays small enough to hold the whole library (a few hundred
  // bytes a row).
  //
  // Read in TWO PHASES, not one bulk toArray()/where().equals() - measured
  // on a real ~26,000-photo library: Safari's IndexedDB backend pays a
  // per-row cost that Chrome's doesn't (confirmed linear: 500 rows in
  // 265ms, 26,423 rows in ~13.5s in raw getAll() AND cursor iteration -
  // same cost either way, so it isn't the bulk-vs-cursor API choice, it's
  // Safari's per-row overhead at this table size). A single bulk read means
  // the whole grid sits behind one blank "Loading…" for that entire
  // stretch.
  //
  // Phase 1 reads just FIRST_PAGE_SIZE rows (newest-date-first) and commits
  // them immediately, so there is something on screen fast regardless of
  // library size or browser - the case that matters most in practice
  // (landing here to see what was just added). Phase 2 then reads
  // everything else in ONE bulk call, same as the original single-toArray()
  // approach, and commits once. An earlier version chunked ALL ~26,000 rows
  // into ~27 pieces and re-sorted+re-rendered after every one - safe for
  // Safari's slow backend, but on Chrome (where the read itself is already
  // fast) that turned a ~1s load into ~6s: 27 full-array sorts plus 27 grid
  // re-renders cost far more than the read ever did. Two phases keeps
  // Safari's "something to look at almost immediately" benefit without
  // taxing Chrome's already-fast path with per-chunk overhead it doesn't
  // need.
  const FIRST_PAGE_SIZE = 500
  const [photos, setPhotos] = useState<PhotoMeta[] | undefined>(undefined)
  // The oldest MOMENT year, persisted across page loads so the scrubber can
  // show the library's true start-to-end range from phase 1 onward instead
  // of growing as older rows land in phase 2 - a visibly shifting range was
  // the tell that gave away the two-phase load entirely.
  //
  // Deliberately momentDate, not the indexed `date` field: an old camera
  // with a wrong clock can have date=1970 while momentDate (the folder it
  // was correctly sorted into) says 2001 - `date` is EXIF capture time,
  // unrelated to where the photo actually sits in the grid. momentDate has
  // no index, so finding it cheaply requires having already scanned the
  // library once - hence persisting it, not querying for it fresh each
  // load. A brand new library naturally has no persisted value yet, in
  // which case the rail simply falls back to computing its range from
  // whatever's loaded so far (its original behaviour, pre-dating this
  // feature) until phase 2 lands and this gets written for next time.
  const [oldestMomentYear, setOldestMomentYear] = useState<number | undefined>(() => {
    try {
      const v = localStorage.getItem(OLDEST_MOMENT_YEAR_KEY)
      return v === null ? undefined : Number(v)
    } catch {
      return undefined
    }
  })
  // Not db.photos.count(): a rename, favorite toggle, or hasMotion flip
  // changes a row's contents without changing the row count, and this must
  // re-trigger the read below just the same - see subscribePhotosVersion's
  // own comment in db.ts.
  const tableVersion = useSyncExternalStore(subscribePhotosVersion, getPhotosVersion)
  useEffect(() => {
    let cancelled = false
    setPhotos(undefined)
    // Sorted by MOMENT date, matching /timeline's own ORDER BY, then by
    // capture time within the moment. Sorting on `date` alone here would
    // silently undo the server's ordering: a photo whose EXIF disagrees
    // with its folder would be pulled out of its own moment and dropped
    // wherever its capture time happened to fall.
    //
    // momentDate comes straight from the server (media.moment_date), not
    // re-derived from `moment` text - a month-only bucket folder
    // ("2026-09") has its moment_date rebalanced to its newest photo's
    // date rather than defaulting to day=1 (see indexer's
    // rebalance_month_bucket_dates), which a client-side regex on the
    // folder name has no way to recover. null sorts last, same as the old
    // ''-sorts-last behaviour for an unparseable moment.
    function byMomentThenDate(a: PhotoMeta, b: PhotoMeta) {
      const am = a.momentDate ?? ''
      const bm = b.momentDate ?? ''
      if (am !== bm) return am < bm ? 1 : -1
      return a.date < b.date ? 1 : a.date > b.date ? -1 : 0
    }
    async function run() {
      if (starredOnly) {
        // Small set - no need for a first-page split.
        const rows = await db.photos.where('favorite').equals(1).toArray()
        if (!cancelled) setPhotos(rows.sort(byMomentThenDate))
        return
      }
      // Newest-`date`-first, not primary-key order: a UUID primary key
      // scatters recent photos more or less randomly, so the person who
      // just landed here to see today's upload would otherwise wait for
      // the whole library before it's visible. orderBy uses the `date`
      // index directly (present since v1). Not exactly the grid's own sort
      // key (momentDate, then date) since Dexie can only drive a query off
      // one index at a time - but `date` and `momentDate` are close enough
      // for recent content that this alone gets today's photos into the
      // first page, which is the actual goal.
      const firstPage = await db.photos.orderBy('date').reverse().limit(FIRST_PAGE_SIZE).toArray()
      if (cancelled) return
      setPhotos([...firstPage].sort(byMomentThenDate))
      const rest = await db.photos.orderBy('date').reverse().offset(FIRST_PAGE_SIZE).toArray()
      if (cancelled) return
      const all = [...firstPage, ...rest]
      setPhotos(all.sort(byMomentThenDate))
      // Now that the whole library is in memory, find the real oldest
      // momentDate and persist its year for next load - see
      // oldestMomentYear's own comment above for why this can't be a cheap
      // indexed query up front.
      let oldestYear: number | undefined
      for (const p of all) {
        if (!p.momentDate) continue
        const y = Number(p.momentDate.slice(0, 4))
        if (oldestYear === undefined || y < oldestYear) oldestYear = y
      }
      if (oldestYear !== undefined) {
        setOldestMomentYear(oldestYear)
        try {
          localStorage.setItem(OLDEST_MOMENT_YEAR_KEY, String(oldestYear))
        } catch {
          // Loses the stable-range nicety for next load only, never correctness.
        }
      }
    }
    void run()
    return () => {
      cancelled = true
    }
  }, [starredOnly, tableVersion])
  // Callback ref, not useRef+useEffect([]): the scroll container doesn't
  // exist in the DOM until the `!photos` loading branch below stops
  // rendering (that early-return's JSX has no ref at all) - a mount-once
  // effect fired against a still-null ref before the real grid ever
  // mounted, then never ran again, silently leaving containerWidth stuck
  // at 0 forever. A callback ref fires exactly when the node attaches,
  // whichever render that happens to be.
  const [scrollElement, setScrollElement] = useState<HTMLDivElement | null>(null)
  const parentRef = (node: HTMLDivElement | null) => {
    if (node) setScrollElement(node)
  }

  // Square tiles at any density (fixing the "grid size leads to rectangles"
  // report): tileSize is derived from the actual container width and
  // column count, rather than a fixed ROW_HEIGHT independent of the
  // computed column width - a ResizeObserver keeps it correct across
  // sidebar/window resizes, not just the initial mount width.
  const [containerWidth, setContainerWidth] = useState(0)
  useEffect(() => {
    if (!scrollElement) return
    const observer = new ResizeObserver((entries) => {
      setContainerWidth(entries[0].contentRect.width)
    })
    observer.observe(scrollElement)
    return () => observer.disconnect()
  }, [scrollElement])
  const tileSize = containerWidth > 0 ? (containerWidth - GAP * (columns - 1)) / columns : 160

  const rows = useMemo(() => {
    if (!photos) return []
    return chunkRows(groupByMoment(photos), columns)
  }, [photos, columns])

  // Row index of each moment's header - used by the year scrubber to jump
  // directly there via scrollToIndex, see YearScrubber.tsx.
  const rowIndexByMoment = useMemo(() => {
    const map = new Map<string, number>()
    rows.forEach((row, i) => {
      if (row.type === 'header') map.set(row.moment, i)
    })
    return map
  }, [rows])

  const monthBuckets = useMemo(
    () => (photos ? computeMonthBuckets(photos, rowIndexByMoment) : []),
    [photos, rowIndexByMoment],
  )

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollElement,
    // Header height measured directly from the DOM (29px), not guessed.
    // The previous 40px estimate was 11px too tall, so every header that
    // scrolled into view corrected itself and shifted all content below -
    // which is exactly the "jumps with photo loading" the user reported.
    // It looked like a loading problem but was a measurement problem:
    // scrollHeight stayed stable, individual rows moved.
    estimateSize: (index) => (rows[index]?.type === 'header' ? HEADER_HEIGHT : tileSize + GAP),
    // Deliberately generous. Rows mounting exactly at the viewport edge
    // have no time to decode before they're painted, which showed up as
    // intermittent 40ms+ frames during fast scrolling even after the
    // object-URL and memo fixes. More overscan buys decode runway; the
    // cost is bounded (rows, not the whole library) and each extra tile is
    // an <img> pointing at an already-cached blob URL.
    overscan: OVERSCAN_ROWS,
  })

  // Pinch (touch) or ctrl+wheel (trackpad) changes how many photos per row.
  // Clamped to the same 3-20 the removed slider used.
  const handleZoom = useCallback(
    (delta: number) => {
      const next = Math.min(20, Math.max(3, columns + delta))
      if (next !== columns) onColumnsChange(next)
    },
    [columns, onColumnsChange],
  )
  usePinchZoom(scrollElement, handleZoom)

  // Fractional index of the row at the top of the viewport: the row's own
  // index plus how far the scroll has travelled through it.
  //
  // A whole-number row makes the rail step discretely from one month to the
  // next - the rolodex drift that slides month labels between slots is driven
  // by the fractional part of the position, so an integer input freezes it.
  // The first row whose bottom edge is past the scroll offset, not
  // getVirtualItems()[0], which includes overscan rows above the viewport.
  const rowAtOffset = useCallback(
    (offset: number) => {
      // getOffsetForIndex/measurements are the virtualizer's own view of row
      // geometry, so this stays correct as rows are measured and re-measured.
      const measurements = virtualizer.measurementsCache
      if (measurements.length === 0) return 0
      // Binary search for the row containing `offset`.
      let lo = 0
      let hi = measurements.length - 1
      while (lo < hi) {
        const mid = (lo + hi) >> 1
        if (measurements[mid].end > offset) hi = mid
        else lo = mid + 1
      }
      const row = measurements[lo]
      const size = row.end - row.start
      const within = size > 0 ? (offset - row.start) / size : 0
      return row.index + Math.min(Math.max(within, 0), 1)
    },
    [virtualizer],
  )

  const fractionalRow = rowAtOffset(virtualizer.scrollOffset ?? 0)

  // Survive a browser refresh at the same scroll position instead of
  // dumping the user back at the top (today's photos). Row index, not raw
  // pixel offset - row height depends on `columns`, which can differ
  // between the save and the restore. sessionStorage rather than
  // localStorage: this is "stay where I was this visit", not a permanent
  // bookmark that should still apply after the app has been closed for a
  // week and today's photos are what's actually relevant again.
  const SCROLL_STORAGE_KEY = 'lesphotos-scroll-row'
  const restoredRef = useRef(false)
  useEffect(() => {
    if (restoredRef.current) return
    if (!scrollElement || rows.length === 0) return
    restoredRef.current = true
    const saved = sessionStorage.getItem(SCROLL_STORAGE_KEY)
    if (!saved) return
    const rowIndex = Number(saved)
    if (!Number.isFinite(rowIndex) || rowIndex <= 0) return
    virtualizer.scrollToIndex(Math.min(rowIndex, rows.length - 1), { align: 'start' })
  }, [scrollElement, rows.length, virtualizer])

  const fractionalRowRef = useRef(fractionalRow)
  fractionalRowRef.current = fractionalRow

  // Moment at the top of the viewport, kept live on every render so it's
  // never more than one render stale. Header rows carry `moment` directly;
  // photo rows only carry it via their photos.
  const topMomentRef = useRef<string | null>(null)
  const topRow = rows[Math.floor(fractionalRow)]
  if (topRow) {
    topMomentRef.current = topRow.type === 'header' ? topRow.moment : (topRow.photos[0]?.moment ?? null)
  }

  // Preserve scroll position across a starredOnly round trip: entering
  // favourites mode saves where you were in the FULL grid, leaving it
  // restores you there - not a symmetric "remember wherever you just were",
  // which would instead save the starred-only position on the way out and
  // hand it right back on the way back in, losing the original spot.
  // Keyed on `moment` (stable across the filter) rather than row index
  // (isn't - the two lists have different lengths), same idea as the
  // refresh-restore above.
  //
  // The saved moment must be frozen the instant starredOnly flips to true,
  // not read later from topMomentRef inside an effect: useLiveQuery's
  // re-query is async, so several renders happen (still on the OLD rows,
  // then eventually the NEW ones) before an effect would fire, and
  // topMomentRef keeps tracking whatever's on screen through all of them -
  // by the time an effect ran it would already be reading the post-switch
  // top row. Detecting the transition during render itself, before
  // topMomentRef is overwritten for this render, is what actually captures
  // the pre-switch moment.
  const prevStarredOnlyRef = useRef(starredOnly)
  const savedMomentRef = useRef<string | null>(null)
  const pendingRestoreRef = useRef<string | null>(null)
  if (prevStarredOnlyRef.current !== starredOnly) {
    prevStarredOnlyRef.current = starredOnly
    if (starredOnly) {
      // Entering: remember the full-grid position, don't touch scroll now
      // (starred-only's own row list decides where to land, same as any
      // other filter change).
      savedMomentRef.current = topMomentRef.current
    } else {
      // Leaving: hand back whatever was saved on the way in.
      pendingRestoreRef.current = savedMomentRef.current
      savedMomentRef.current = null
    }
  }
  useEffect(() => {
    const moment = pendingRestoreRef.current
    if (!moment) return
    const rowIndex = rowIndexByMoment.get(moment)
    if (rowIndex === undefined) return
    pendingRestoreRef.current = null
    virtualizer.scrollToIndex(rowIndex, { align: 'start' })
  }, [rowIndexByMoment, virtualizer])

  useEffect(() => {
    if (!scrollElement) return
    const onScroll = () => {
      sessionStorage.setItem(SCROLL_STORAGE_KEY, String(Math.floor(fractionalRowRef.current)))
    }
    scrollElement.addEventListener('scroll', onScroll, { passive: true })
    return () => scrollElement.removeEventListener('scroll', onScroll)
  }, [scrollElement])

  // Size the thumbnail cache to what this grid can actually show at once.
  //
  // A fixed budget is wrong at both ends of the density slider: at 20 columns a
  // tall viewport shows ~240 tiles, which was the entire cache, so tiles were
  // evicted while still on screen - photos loaded, went grey, then loaded again
  // during a fast scroll.
  useEffect(() => {
    const viewportHeight = scrollElement?.clientHeight ?? 0
    if (viewportHeight === 0 || tileSize <= 0) return
    const rowsOnScreen = Math.ceil(viewportHeight / (tileSize + GAP)) + 1
    // Overscan counts: those rows are really mounted, so their tiles really ask
    // the cache for a URL. Sizing to the visible rows alone left the budget
    // below what a single frame demanded at high density, so eviction ran
    // constantly against tiles that were still on screen.
    setThumbnailBudget((rowsOnScreen + 2 * OVERSCAN_ROWS) * columns)
  }, [scrollElement, tileSize, columns, containerWidth])

  // Warm thumbnails for rows just beyond the rendered window, so a fast fling
  // finds blobs already read rather than starting an IndexedDB round-trip at
  // the moment each tile mounts. Scrolling never waits on this - tiles render
  // grey immediately and swap in when their blob lands.
  const virtualItems = virtualizer.getVirtualItems()
  const firstRendered = virtualItems[0]?.index ?? 0
  const lastRendered = virtualItems[virtualItems.length - 1]?.index ?? 0
  useEffect(() => {
    if (rows.length === 0) return
    // Runway measured in TILES, not rows. As rows, a fixed count meant 12 rows
    // either side - fine at 6 columns (~144 thumbnails), but 240 either side at
    // 20 columns, which on its own exceeded the whole cache budget. Every
    // prefetch then evicted entries the next prefetch immediately re-read:
    // measured 7,527 reloads and 7,917 evictions while scrolling back and forth
    // over one region, which is the load-grey-load flicker.
    const AHEAD = Math.max(2, Math.ceil(72 / columns))
    const ids: string[] = []
    for (
      let i = Math.max(0, firstRendered - AHEAD);
      i <= Math.min(rows.length - 1, lastRendered + AHEAD);
      i++
    ) {
      const row = rows[i]
      if (row?.type === 'photos') for (const p of row.photos) ids.push(p.id)
    }
    prefetchThumbnails(ids)
  }, [firstRendered, lastRendered, rows, columns])

  // estimateSize is captured by closure and TanStack Virtual doesn't
  // automatically re-measure when the value it depends on changes -
  // without this, resizing the sidebar or moving the density slider would
  // leave stale (too-tall-or-short) row measurements until the next
  // scroll-triggered remeasure.
  useEffect(() => {
    virtualizer.measure()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tileSize, columns])

  // Index into `photos` (the same flat, ordered, possibly starred-filtered
  // list the grid renders) - viewer navigation always matches whatever the
  // grid is currently showing, per docs/decisions/fullscreen-viewer-cache.md.
  const [viewerIndex, setViewerIndex] = useState<number | null>(null)

  // Selection mode for "create a new moment from selected photos" - see
  // docs/decisions/range-select-new-moment.md. That doc rejected shift-click
  // ranges as touch-hostile and ambiguous under grid reflow; both are now
  // addressed, so ranges are supported alongside plain tapping:
  //
  //   - reflow: the range runs over the PHOTO LIST order, not screen position,
  //     so changing density or rotating the phone can't change what a range
  //     means. A rectangle in a virtualized grid would have neither property.
  //   - touch: no press-and-hold and no dragging. Tap the first photo, then
  //     tap "to here" on the last one. Both taps are ordinary taps, and
  //     overshooting is fixed by tapping a different end photo rather than by
  //     starting the whole selection again.
  const [selectionMode, setSelectionMode] = useState(false)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [newMomentLabel, setNewMomentLabel] = useState('')
  const [createStatus, setCreateStatus] = useState('')
  // Where the next range starts: the last photo tapped without extending.
  const [anchorId, setAnchorId] = useState<string | null>(null)

  const photoOrder = useMemo(() => {
    const map = new Map<string, number>()
    photos?.forEach((p, i) => map.set(p.id, i))
    return map
  }, [photos])

  /**
   * Make the selection run from the anchor to `id` inclusive.
   *
   * Sets the range rather than only growing it: the photos the previous range
   * covered are dropped first, so tapping a nearer photo pulls the range BACK.
   * That is what makes overshooting free - the fix is one tap on where you
   * actually meant, not undoing tile by tile - and it is why the control
   * appears on selected photos too, not only unselected ones.
   *
   * Selections made outside this range (individual taps elsewhere) are kept;
   * only the previous range's own photos are given up.
   */
  const lastRangeRef = useRef<string[]>([])
  const selectRangeTo = useCallback(
    (id: string) => {
      if (!photos) return
      const to = photoOrder.get(id)
      const from = anchorId != null ? photoOrder.get(anchorId) : undefined
      if (to === undefined) return
      // No anchor yet (or it was filtered away): behave like a plain tap.
      if (from === undefined) {
        setSelectedIds((prev) => new Set(prev).add(id))
        setAnchorId(id)
        return
      }
      const [lo, hi] = from <= to ? [from, to] : [to, from]
      const ids: string[] = []
      for (let i = lo; i <= hi; i++) ids.push(photos[i].id)
      setSelectedIds((prev) => {
        const next = new Set(prev)
        for (const old of lastRangeRef.current) next.delete(old)
        for (const add of ids) next.add(add)
        // The anchor is always part of its own range.
        if (anchorId) next.add(anchorId)
        return next
      })
      lastRangeRef.current = ids
      // Anchor stays put, so a second tap re-extends from the same start
      // rather than chaining - which is what makes overshooting cheap
      // to correct instead of something you have to undo.
    },
    [anchorId, photoOrder, photos],
  )

  function toggleSelect(id: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
    setAnchorId(id)
    // A fresh anchor starts a fresh range; the old one becomes a plain part of
    // the selection rather than something the next range can take back.
    lastRangeRef.current = []
  }

  async function createMoment() {
    if (!photos || selectedIds.size === 0 || !newMomentLabel.trim()) return
    setCreateStatus('Creating…')
    try {
      const selectedPaths = photos.filter((p) => selectedIds.has(p.id)).map((p) => p.path)
      const result = await createMomentFromPhotos(selectedPaths, newMomentLabel.trim())
      setCreateStatus(`Created "${result.new_path}" with ${result.moved_count} photo(s)`)
      setSelectedIds(new Set())
      setSelectionMode(false)
      setNewMomentLabel('')
      // Same reasoning as MomentHeader's rename: the moved photos now have
      // new PhotoView ids, so the client cache holds stale rows for them
      // until a sync runs - do it automatically rather than relying on the
      // user to remember, which was leaving old-id rows rendered alongside
      // the new ones (visible to the user as apparent "duplicates").
      onNeedsSync()
    } catch (err) {
      setCreateStatus(`Failed: ${err instanceof Error ? err.message : String(err)}`)
    }
  }

  // Merge mode: select 2+ whole moments (not individual photos, unlike
  // selectionMode above) and combine them into one. Mutually exclusive
  // with selectionMode - toggling one off the other avoids a confusing UI
  // where both a photo-level and moment-level selection are live at once.
  const [mergeMode, setMergeMode] = useState(false)
  const [selectedMomentPaths, setSelectedMomentPaths] = useState<Set<string>>(new Set())
  const [mergeLabel, setMergeLabel] = useState('')
  const [mergeStatus, setMergeStatus] = useState('')

  function toggleMergeSelect(momentPath: string) {
    setSelectedMomentPaths((prev) => {
      const next = new Set(prev)
      if (next.has(momentPath)) next.delete(momentPath)
      else next.add(momentPath)
      return next
    })
  }

  async function merge() {
    if (selectedMomentPaths.size < 2 || !mergeLabel.trim()) return
    setMergeStatus('Merging…')
    try {
      const result = await mergeMoments([...selectedMomentPaths], mergeLabel.trim())
      setMergeStatus(`Merged into "${result.new_path}"`)
      setSelectedMomentPaths(new Set())
      setMergeMode(false)
      setMergeLabel('')
      onNeedsSync()
    } catch (err) {
      if (isStaleEditError(err)) {
        setMergeStatus('One of these moments changed elsewhere - refreshing…')
        onNeedsSync()
      } else {
        setMergeStatus(`Failed: ${err instanceof Error ? err.message : String(err)}`)
      }
    }
  }

  if (!photos) return <div>Loading…</div>
  if (photos.length === 0) {
    return <div style={{ padding: 16 }}>No photos cached yet - run a sync.</div>
  }

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <Toolbar>
        <button
          type="button"
          data-testid="selection-mode-toggle"
          disabled={mergeMode}
          onClick={() => {
            setSelectionMode((v) => !v)
            setSelectedIds(new Set())
            setAnchorId(null)
            setCreateStatus('')
          }}
        >
          {selectionMode ? 'Cancel selection' : 'Select photos'}
        </button>
        {selectionMode && selectedIds.size > 0 && (
          <>
            <span>{selectedIds.size} selected</span>
            <input
              data-testid="new-moment-label-input"
              placeholder="New moment name"
              value={newMomentLabel}
              onChange={(e) => setNewMomentLabel(e.target.value)}
            />
            <button type="button" data-testid="create-moment-button" onClick={createMoment} disabled={!newMomentLabel.trim()}>
              Create moment
            </button>
          </>
        )}
        {createStatus && <span style={{ fontSize: 12 }}>{createStatus}</span>}

        <button
          type="button"
          data-testid="merge-mode-toggle"
          disabled={selectionMode}
          onClick={() => {
            setMergeMode((v) => !v)
            setSelectedMomentPaths(new Set())
            setMergeStatus('')
          }}
        >
          {mergeMode ? 'Cancel merge' : 'Merge moments'}
        </button>
        {mergeMode && selectedMomentPaths.size > 0 && (
          <>
            <span>{selectedMomentPaths.size} moment(s) selected</span>
            <input
              data-testid="merge-label-input"
              placeholder="Merged moment name"
              value={mergeLabel}
              onChange={(e) => setMergeLabel(e.target.value)}
            />
            <button
              type="button"
              data-testid="merge-button"
              onClick={merge}
              disabled={selectedMomentPaths.size < 2 || !mergeLabel.trim()}
            >
              Merge
            </button>
          </>
        )}
        {mergeStatus && <span style={{ fontSize: 12 }}>{mergeStatus}</span>}
      </Toolbar>

      {/* Positioning context for the rail: it must stay put while the grid
          scrolls under it, without being fixed to the viewport - as `fixed` it
          floated over the app header and swallowed clicks on the buttons
          there. */}
      <div style={{ flex: 1, minHeight: 0, position: 'relative' }}>
      <div
        ref={parentRef}
        style={{
          position: 'absolute',
          inset: 0,
          overflow: 'auto',
          // On a phone the rail is a collapsed handle that overlays the grid
          // while dragging, so only the handle's own width is reserved -
          // reserving the full 148px would give back the space the drawer
          // change just freed up.
          paddingRight: narrow ? NARROW_RAIL_WIDTH : SCRUBBER_RESERVED_WIDTH,
          // Photos sat flush against the window edge, which reads as clipped
          // and puts the leftmost tile's controls under the screen bezel on a
          // phone. Matches the gap between tiles so the grid looks inset
          // rather than shifted.
          paddingLeft: GRID_EDGE_PADDING,
        }}
      >
      <div
        style={{
          height: virtualizer.getTotalSize(),
          position: 'relative',
          width: '100%',
        }}
      >
        {virtualizer.getVirtualItems().map((virtualRow) => {
          const row = rows[virtualRow.index]
          return (
            <div
              key={virtualRow.key}
              style={{
                position: 'absolute',
                top: 0,
                left: 0,
                width: '100%',
                height: virtualRow.size,
                transform: `translateY(${virtualRow.start}px)`,
              }}
            >
              {row.type === 'header' ? (
                <MomentHeader
                  moment={row.moment}
                  momentPath={row.momentPath}
                  onNeedsSync={onNeedsSync}
                  mergeMode={mergeMode}
                  mergeSelected={selectedMomentPaths.has(row.momentPath)}
                  onToggleMergeSelect={() => toggleMergeSelect(row.momentPath)}
                />
              ) : (
                <div style={{ display: 'grid', gridTemplateColumns: `repeat(${columns}, 1fr)`, gap: GAP }}>
                  {row.photos.map((photo) => (
                    <Thumbnail
                      key={photo.id}
                      photo={photo}
                      tileSize={tileSize}
                      // By id, not indexOf: the photos array is rebuilt (and
                      // re-sorted into new objects) on every useLiveQuery run,
                      // so reference identity can silently stop matching and
                      // indexOf returns -1 - which renders no viewer at all.
                      onOpen={() => {
                        const i = photos.findIndex((p) => p.id === photo.id)
                        if (i >= 0) setViewerIndex(i)
                      }}
                      selectionMode={selectionMode}
                      selected={selectedIds.has(photo.id)}
                      onToggleSelect={() => toggleSelect(photo.id)}
                      onSelectRangeTo={() => selectRangeTo(photo.id)}
                      canExtend={anchorId !== null}
                      isAnchor={anchorId === photo.id}
                    />
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>
      </div>

      {/* Sibling of the scroll container, not a child: inside it the rail
          scrolled away with the content. */}
      {SCRUBBER === 'v5' ? (
        <RailScrubber
          narrow={narrow}
          buckets={monthBuckets}
          firstVisibleRow={fractionalRow}
          scrollElement={scrollElement}
          rowAtOffset={rowAtOffset}
          onJumpToRow={(rowIndex) => virtualizer.scrollToIndex(rowIndex, { align: 'start' })}
          minYear={oldestMomentYear}
        />
      ) : (
        <YearScrubber
          buckets={monthBuckets}
          totalRows={rows.length}
          scrollElement={scrollElement}
          onJumpToFraction={(fraction) => virtualizer.scrollToOffset(fraction * virtualizer.getTotalSize())}
          onJumpToRow={(rowIndex) => virtualizer.scrollToIndex(rowIndex, { align: 'start' })}
        />
      )}
      </div>

      {viewerIndex !== null && (
        <PhotoViewer
          photos={photos}
          index={viewerIndex}
          onClose={() => setViewerIndex(null)}
          onNavigate={setViewerIndex}
        />
      )}
    </div>
  )
}
