import { useCallback, useEffect, useMemo, useState } from 'react'
import type { MonthBucket } from '../YearScrubber'
import { RAIL_WIDTH, TimelineRail } from './TimelineRail'
import { toAbsMonth } from './layout'

export { RAIL_WIDTH }

export interface RailScrubberProps {
  /** Month buckets from computeMonthBuckets() - the library's real density. */
  buckets: MonthBucket[]
  /**
   * Fractional row currently at the top of the viewport, from the virtualizer.
   *
   * Rows have very different heights (a moment header is 29px, a photo row
   * ~200px), so deriving a row from scrollTop/scrollHeight is off by months -
   * the virtualizer is the only thing that knows the real mapping.
   */
  firstVisibleRow: number
  /**
   * The grid's scroll container, and a way to resolve its offset to a
   * fractional row.
   *
   * The rail subscribes to scroll itself rather than relying on re-renders of
   * the grid: React only re-renders the grid when the virtualizer's item set
   * changes, i.e. once per row boundary, which freezes the rail's labels for
   * a dozen frames at a time between steps.
   */
  scrollElement: HTMLElement | null
  rowAtOffset: (offset: number) => number
  /** Phone layout: collapse to a handle that expands while dragging. */
  narrow?: boolean
  /** Jump to an exact virtualizer row. */
  onJumpToRow: (rowIndex: number) => void
}

/**
 * Adapter between the grid and the rail.
 *
 * The rail knows only about calendar time (a fractional absolute-month `pos`);
 * the grid knows only about virtualizer rows. Everything that translates
 * between the two lives here, so the rail stays a pure function of `pos` and
 * can be swapped for a different implementation without touching Timeline.tsx.
 *
 * The mapping is currently linear in calendar time (an empty month still
 * occupies rail space). Making it density-weighted - so months with many
 * photos get more of the rail - means replacing posToRow/rowToPos below with a
 * monotonic CDF over bucket.photoCount, and nothing else.
 */
export function RailScrubber({
  buckets,
  firstVisibleRow,
  scrollElement,
  rowAtOffset,
  onJumpToRow,
  narrow = false,
}: RailScrubberProps) {
  // The rail spans whole calendar years covering the library, so the year
  // stack always starts and ends on a real year boundary.
  const range = useMemo(() => {
    if (buckets.length === 0) {
      const y = new Date().getFullYear()
      return { minYear: y, maxYear: y }
    }
    let minYear = Infinity
    let maxYear = -Infinity
    for (const b of buckets) {
      if (b.year < minYear) minYear = b.year
      if (b.year > maxYear) maxYear = b.year
    }
    return { minYear, maxYear }
  }, [buckets])

  /**
   * Anchors mapping calendar position -> row index, ascending by row.
   *
   * MonthBucket.month is 1-12 but the rail's absolute-month index is 0-based,
   * hence the -1. Getting this wrong shifts the whole rail by a month, which
   * looks plausible enough to miss on a read-through.
   */
  const anchors = useMemo(
    () =>
      buckets
        .map((b) => ({ pos: toAbsMonth(b.year, b.month - 1), row: b.rowIndex }))
        // Sorted by ROW, which runs newest -> oldest. Sorting by pos instead
        // reverses the two scans below relative to the grid and pins the rail
        // to the wrong end of the library entirely.
        .sort((a, b) => a.row - b.row),
    [buckets],
  )

  /**
   * Row for `pos` - the bucket closest to it in time, either side.
   *
   * Nearest, not "the newest at or before". A year label means 1 Jan of that
   * year, and a library rarely has a photo in that exact month, so the
   * at-or-before rule landed in the PREVIOUS year: tapping "2020" jumped to
   * December 2019. Nearest lands on whichever real month is closest, which for
   * a year label is the start of that year whenever the library has anything
   * near it.
   */
  const posToRow = useCallback(
    (pos: number) => {
      if (anchors.length === 0) return 0
      let best = anchors[0]
      let bestDist = Math.abs(best.pos - pos)
      for (const a of anchors) {
        const d = Math.abs(a.pos - pos)
        if (d < bestDist) {
          bestDist = d
          best = a
        }
      }
      return best.row
    },
    [anchors],
  )

  /**
   * Continuous position for a fractional row.
   *
   * Interpolated between the two surrounding month anchors rather than snapped
   * to the nearest one: the rail's whole rolodex effect comes from the
   * fractional part of `pos` (layout.ts's `drift`), which slides labels between
   * slots and fades them across the band edges. Returning a whole month index
   * makes every scroll a discrete jump from one month to the next, with no
   * motion in between.
   */
  const rowToPos = useCallback(
    (row: number) => {
      if (anchors.length === 0) return toAbsMonth(range.maxYear, 11)
      if (row <= anchors[0].row) return anchors[0].pos

      for (let i = 1; i < anchors.length; i++) {
        const prev = anchors[i - 1]
        const next = anchors[i]
        if (row <= next.row) {
          const span = next.row - prev.row
          // Anchors sharing a row would divide by zero; they also carry no
          // information about where between them we are.
          if (span <= 0) return next.pos
          const t = (row - prev.row) / span
          return prev.pos + t * (next.pos - prev.pos)
        }
      }
      return anchors[anchors.length - 1].pos
    },
    [anchors, range.maxYear],
  )

  // While dragging, the rail owns the position; otherwise it follows the grid.
  const [dragPos, setDragPos] = useState<number | null>(null)

  // Row tracked at scroll-event rate. Seeded from the prop so the first paint
  // is right, then updated by the rail's own scroll listener - the grid's
  // render cycle is far too coarse to animate against (see scrollElement).
  const [scrolledRow, setScrolledRow] = useState(firstVisibleRow)
  useEffect(() => {
    if (!scrollElement) return
    let frame = 0
    const onScroll = () => {
      // Coalesce to one read per frame: scroll fires more often than paint,
      // and each update re-lays out ~35 absolutely positioned labels.
      if (frame) return
      frame = requestAnimationFrame(() => {
        frame = 0
        const next = rowAtOffset(scrollElement.scrollTop)
        // Quantize: a re-render lays out ~35 absolutely positioned labels, and
        // at full scroll-event rate that lands inside the frame budget (p90
        // measured 68ms vs 36ms). A month slot is ~22px tall, so rounding the
        // position to 1/64 of a row still moves labels by well under a pixel
        // per step - visually continuous, a fraction of the renders.
        setScrolledRow((prev) =>
          Math.abs(next - prev) < 1 / 64 ? prev : next,
        )
      })
    }
    onScroll()
    scrollElement.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      if (frame) cancelAnimationFrame(frame)
      scrollElement.removeEventListener('scroll', onScroll)
    }
  }, [scrollElement, rowAtOffset])

  const pos = dragPos ?? rowToPos(scrolledRow)

  const handleSeek = useCallback(
    (next: number) => {
      setDragPos(next)
      onJumpToRow(posToRow(next))
    },
    [onJumpToRow, posToRow],
  )

  // Releasing hands control back to the grid, which by now has scrolled to the
  // row this drag asked for.
  const handleSeekEnd = useCallback(
    (next: number) => {
      onJumpToRow(posToRow(next))
      setDragPos(null)
    },
    [onJumpToRow, posToRow],
  )

  return (
    <TimelineRail
      pos={pos}
      onSeek={handleSeek}
      onSeekEnd={handleSeekEnd}
      collapsed={narrow}
      config={{ minYear: range.minYear, maxYear: range.maxYear }}
    />
  )
}
