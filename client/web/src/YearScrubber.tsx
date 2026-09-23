import { useEffect, useMemo, useRef, useState } from 'react'
import type { PhotoMeta } from './db'

// v4: fisheye timeline with a fixed indicator and warping slots - see
// docs/decisions/timeline-scrubber-v4-plan.md for the full design this
// implements. Unlike v3 (a moving indicator over a mostly-static label
// list), the indicator now stays roughly still and a fixed number of label
// *slots* change what they show as the user scrolls - closer to iOS
// Photos' own year scrubber.

export interface MonthBucket {
  year: number
  month: number // 1-12
  rowIndex: number
  photoCount: number
  topMoment: { label: string; rowIndex: number; photoCount: number } | null
}

// One entry per calendar month that has at least one photo - not one per
// moment, since the tier-selection rules below operate at month/year
// granularity ("show the two months either side", "one month per year for
// distant years"). topMoment is still tracked per month so a big moment
// can earn its own slot even in the mid/far tiers.
export function computeMonthBuckets(photos: PhotoMeta[], rowIndexByMoment: Map<string, number>): MonthBucket[] {
  const momentPhotoCounts = new Map<string, number>()
  // Bucket a moment by the date in its own FOLDER NAME, not by its
  // earliest photo's EXIF date. The two genuinely disagree: gap-based
  // grouping means a moment named 2024-09-30-moment can hold photos
  // starting 2024-10-04, so keying on the photo date filed it under
  // October while the grid still positions it among September's moments -
  // which rendered as an out-of-sequence label in the scrubber
  // ("Nov, Aug, Oct, Sep" instead of "Nov, Oct, Sep, Aug"), confirmed by
  // dumping each slot's resolved rowIndex against the grid's real order.
  // The folder name is what determines grid position, so it's what the
  // scrubber's month buckets have to agree with.
  const momentDate = new Map<string, string>()
  for (const photo of photos) {
    momentPhotoCounts.set(photo.moment, (momentPhotoCounts.get(photo.moment) ?? 0) + 1)
    if (!momentDate.has(photo.moment)) {
      const fromFolder = /^(\d{4})-(\d{2})-\d{2}/.exec(photo.moment)
      momentDate.set(photo.moment, fromFolder ? `${fromFolder[1]}-${fromFolder[2]}-01` : photo.date)
    }
  }

  interface Acc {
    year: number
    month: number
    rowIndex: number
    photoCount: number
    topMoment: { label: string; rowIndex: number; photoCount: number } | null
  }
  const byKey = new Map<string, Acc>()

  for (const [moment, date] of momentDate) {
    const year = Number(date.slice(0, 4))
    const month = Number(date.slice(5, 7))
    const key = `${year}-${month}`
    const rowIndex = rowIndexByMoment.get(moment) ?? 0
    const photoCount = momentPhotoCounts.get(moment) ?? 0

    const existing = byKey.get(key)
    if (!existing) {
      byKey.set(key, {
        year,
        month,
        rowIndex,
        photoCount,
        topMoment: { label: moment, rowIndex, photoCount },
      })
      continue
    }
    existing.rowIndex = Math.min(existing.rowIndex, rowIndex)
    existing.photoCount += photoCount
    if (!existing.topMoment || photoCount > existing.topMoment.photoCount) {
      existing.topMoment = { label: moment, rowIndex, photoCount }
    }
  }

  return [...byKey.values()].sort((a, b) => a.rowIndex - b.rowIndex)
}

export type ScrubberLabel = {
  key: string
  text: string
  /** 0-100, vertical position on the track. */
  top: number
  kind: 'month' | 'year'
  bold: boolean
  rowIndex: number
}

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

// The indicator is hard-pinned here and never moves - labels move around
// it instead (explicit user decision: "indicator should stay exactly in
// place at 50%").
export const INDICATOR_TOP_PCT = 50

// How far the indicator may drift from INDICATOR_TOP_PCT when one side of
// the rail has no more years to show. Set so the pointer stays within
// roughly the middle 70% of the track (15%..85%), which always leaves room
// for the month band on the shorter side - user: "should stay within 70%
// roughly of screen so theres always space for the months".
const INDICATOR_DRIFT_PCT = 42

// Share of the rail given to the current year's own 12 months. The rest
// is split between the neighbouring years (one marker each) and every
// remaining year, evenly spaced.
const CURRENT_YEAR_BAND_PCT = 33
// When one side of the rail has no year labels to place, the month band
// stretches to fill it rather than leaving dead space - measured a 37.5%
// empty gap between the pinned "2026" boundary and the first month when
// scrolled near the newest photos, because no newer year existed to
// occupy that region.
const MAX_BAND_STRETCH_PCT = 72

// The single month shown for each immediately-neighbouring year, as a
// mid-year anchor between the current year's detail and the evenly-spaced
// far years.
const NEIGHBOUR_MONTH = 6 // June

// Minimum vertical space a year label needs to stay legible, as a % of
// track height. Years are thinned to fit this before rendering.
const MIN_YEAR_LABEL_GAP_PCT = 4.5


/** Continuous time position: 2024-07 -> 2024.5. */
function timeOf(year: number, month: number): number {
  return year + (month - 1) / 12
}

/**
 * Deterministic zone layout, replacing the earlier continuous fisheye
 * curve. See docs/decisions/timeline-scrubber-v4.md.
 *
 * Positions come from CALENDAR TIME, never from grid row index. Row index
 * is proportional to photo count, so a heavy month (2025-08 has 383
 * photos; 2025-12 has 1) occupied ~64 rows against 1 and shoved everything
 * below it far down the rail - producing both wildly uneven gaps and, when
 * a sparse month sat above a dense earlier one, labels rendering out of
 * chronological order ("Dec" above "Jun"). Calendar time gives every month
 * equal weight, which is what makes the layout predictable.
 *
 * Zones, from the current position outward:
 *  - Current year: all 12 months, evenly spaced across CURRENT_YEAR_BAND_PCT.
 *  - Neighbour years (+/-1): a single June marker each.
 *  - All remaining years: evenly spaced across whatever rail is left.
 *  - Boundary markers: newestYear+1 pinned at 0%, oldest year at 100%, so
 *    both ends of the range are unambiguous.
 */
export function selectLabels(
  buckets: MonthBucket[],
  currentRowIndex: number,
  totalRows: number,
  // Overrides the oldest year derived from `buckets` when given - lets the
  // caller show the library's TRUE start-to-end range immediately, before
  // every month's buckets have actually loaded (Timeline's two-phase read:
  // the newest photos render first, older years fill in a few seconds
  // later). Without this the rail's own range visibly grew as older rows
  // arrived, which is exactly the tell that gave the staged load away.
  // Ignored if it's newer than what `buckets` already has, so a caller that
  // doesn't know better can never SHRINK the range below what's real.
  minYear?: number,
): { labels: ScrubberLabel[]; indicatorTop: number } {
  if (buckets.length === 0 || totalRows <= 1) return { labels: [], indicatorTop: INDICATOR_TOP_PCT }

  // Buckets are ordered newest-first (grid order), so the newest year is
  // at the head and the oldest at the tail.
  const newestYear = buckets[0].year
  const oldestYear = Math.min(buckets[buckets.length - 1].year, minYear ?? Infinity)

  // Which bucket is the cursor currently on? Buckets are sorted by
  // rowIndex ascending, so this is a plain binary search.
  let curIdx = 0
  {
    let lo = 0
    let hi = buckets.length - 1
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1
      if (buckets[mid].rowIndex <= currentRowIndex) lo = mid
      else hi = mid - 1
    }
    curIdx = lo
  }
  const currentYear = buckets[curIdx].year
  const currentMonthOfCursor = buckets[curIdx].month
  const currentTime = timeOf(buckets[curIdx].year, buckets[curIdx].month)

  // Time -> row lookup for labels that map onto a real bucket.
  const rowByTime = new Map<number, number>()
  for (const b of buckets) rowByTime.set(timeOf(b.year, b.month), b.rowIndex)

  const out: ScrubberLabel[] = []
  const push = (key: string, text: string, top: number, kind: 'month' | 'year', bold: boolean, time: number) => {
    // Years get CLAMPED into the track; months are dropped when they fall
    // outside it. A year running off the end is still a needed navigation
    // target (the minimum-step floor pushes distant years past 0%, and
    // silently discarding them is what made "2024"/"2025" vanish when
    // scrolled into 2002 - they were never created, so no amount of
    // collision priority could save them). A month off the end is just
    // detail belonging to a part of the timeline that isn't in view.
    if (top < 0 || top > 100) return
    // Year labels resolve by YEAR, never by an exact time key. Keying on
    // timeOf(y, N) made a year label inherit whatever month bucket
    // happened to match that key, which produced row indexes belonging to
    // a neighbouring year - "2023" claimed row 356 and "2022" row 358, two
    // rows apart despite a full year of photos between them, so clicking
    // them landed in the wrong year.
    const resolved =
      kind === 'year'
        ? firstRowOfYear(Math.round(time)) ?? nearestRow(time)
        : rowByTime.get(time) ?? nearestRow(time)
    out.push({ key, text, top, kind, bold, rowIndex: resolved })
  }

  // Time -> row for a label that has no bucket of its own (a boundary
  // marker, or a June in a year whose June is empty): fall back to the
  // closest bucket in time, so clicking it still lands somewhere sensible.
  // Year labels resolve against timeOf(y, 1) - the START of the year, not
  // December. timeOf(y, 12) is y+11/12, which sits closer to the following
  // January than to any of its own year's months, so every year label
  // resolved one year too recent (clicking "2019" landed in 2020). Only
  // the pinned boundary years were correct, because they bypass this
  // lookup entirely - which is why earlier fixes to nearestRow had no
  // visible effect: these labels were hitting an exact rowByTime match,
  // never the fallback.
  // Lowest (newest) row index among buckets belonging to a given year -
  // the row the grid actually starts that year at.
  function firstRowOfYear(year: number): number | null {
    let best: number | null = null
    for (const b of buckets) {
      if (b.year !== year) continue
      if (best === null || b.rowIndex < best) best = b.rowIndex
    }
    return best
  }

  function nearestRow(time: number): number {
    // For a YEAR label, snap to that year's own newest bucket rather than
    // to whatever month is numerically closest. Year labels are pushed
    // with timeOf(y, 1) (December), but a sparse library often has no
    // December, so the plain nearest-in-time search jumped into an
    // adjacent year - clicking "2020" landed on 2022, "2008" on 2010.
    const wantYear = Math.floor(time)
    const inYear = buckets.filter((b) => b.year === wantYear)
    if (inYear.length > 0) {
      return inYear.reduce((a, b) => (a.rowIndex <= b.rowIndex ? a : b)).rowIndex
    }
    let best = buckets[0]
    let bestD = Infinity
    for (const b of buckets) {
      const d = Math.abs(timeOf(b.year, b.month) - time)
      if (d < bestD) {
        bestD = d
        best = b
      }
    }
    return best.rowIndex
  }

  // --- Zone 1: current year's months, evenly spaced around the cursor ---
  // The band is centred on the indicator, and the cursor's own month sits
  // exactly at INDICATOR_TOP_PCT. Newer months are above (smaller top%),
  // older below, matching the grid's newest-first order.
  let yearsNewer = 0
  let yearsOlder = 0
  for (let y = newestYear + 1; y >= oldestYear; y--) {
    if (y >= currentYear - 1 && y <= currentYear + 1) continue
    if (y > currentYear) yearsNewer++
    else yearsOlder++
  }
  // Rail a year zone needs. Deliberately modest per year and capped: an
  // over-generous reservation left large dead gaps (measured 37.5% of the
  // track empty between the "2026" boundary and the first month, because
  // the band was held away from an area no year label actually used).
  const zoneNeed = (n: number) => (n === 0 ? 0 : Math.min(26, 4 + n * 1.6))
  const needNewer = zoneNeed(yearsNewer)
  const needOlder = zoneNeed(yearsOlder)

  // The indicator sits at 50% for the vast majority of scrolling, but is
  // allowed to drift toward whichever edge has run out of years to show.
  // Kept inside roughly the middle 70% of the track so there is always
  // room for the month band on the shorter side.
  // Stretch the band into whatever rail the year zones don't need, capped
  // so months never become absurdly far apart on a short library.
  const availableForBand = Math.max(0, 100 - needNewer - needOlder)
  const bandPct = Math.min(MAX_BAND_STRETCH_PCT, Math.max(CURRENT_YEAR_BAND_PCT, availableForBand))
  // Where the cursor's month should sit. The band holds the months of the
  // current year, so the space ABOVE the cursor can only ever be filled by
  // months newer than it - in December there are none, which is what left
  // a large dead gap under the pinned newest-year marker.
  //
  // So position the band by how many months actually fall on each side of
  // the cursor, not by splitting the rail evenly. In December the band
  // rides high (nothing above), in January it rides low.
  const monthsNewer = 12 - currentMonthOfCursor
  const monthsOlder = currentMonthOfCursor - 1
  const bandTopNeed = needNewer + monthsNewer * (bandPct / 12)
  const bandBottomNeed = needOlder + monthsOlder * (bandPct / 12)
  const idealCentre =
    bandTopNeed + bandBottomNeed === 0
      ? INDICATOR_TOP_PCT
      : (bandTopNeed / (bandTopNeed + bandBottomNeed)) * 100
  const bandCentre = Math.max(
    INDICATOR_TOP_PCT - INDICATOR_DRIFT_PCT,
    Math.min(INDICATOR_TOP_PCT + INDICATOR_DRIFT_PCT, idealCentre),
  )

  // Bounds of everything placed so far - the year zones fill the rail
  // above and below the month band.

  const monthStepPct = bandPct / 12

  // Zone 1: the current year's 12 months, evenly spaced, centred so the
  // cursor's own month lands exactly on bandCentre (where the indicator
  // is drawn). Every month renders, including empty ones - skipping them
  // left holes in what is meant to be an evenly spaced band, and an empty
  // month still resolves to a sensible jump target via nearestRow().
  for (let m = 1; m <= 12; m++) {
    const monthsFromCursor = timeOf(currentYear, m) - currentTime
    const top = bandCentre - monthsFromCursor * 12 * monthStepPct
    push(
      `m${currentYear}-${m}`,
      MONTH_NAMES[m - 1],
      top,
      'month',
      timeOf(currentYear, m) === currentTime,
      timeOf(currentYear, m),
    )
  }

  // The year label marks the START of its year, so in a newest-first
  // timeline it sits BELOW that year's January - "2025" reads as the
  // boundary you cross going backwards into 2025.
  const curYearTop = bandCentre - (timeOf(currentYear, 0.5) - currentTime) * 12 * monthStepPct
  push(`y${currentYear}`, String(currentYear), curYearTop, 'year', true, timeOf(currentYear, 1))

  // Bounds of everything placed so far (the month band plus the current
  // year's own label). The far-year zones start outside this, so a far
  // year can never tie with, or cross, a label belonging to the band -
  // which is what produced "2014 2016" and "2007 2006 2009".
  const zoneTop = Math.min(...out.map((l) => l.top), bandCentre)
  const zoneBottom = Math.max(...out.map((l) => l.top), bandCentre)

  // Where the month band's centre sits. Normally that's the indicator, so
  // the cursor's own month is at 50%. But near either end of the library
  // the band would otherwise run off the rail and squash the year zone on
  // that side into nothing (e.g. scrolled to the oldest photos, every year
  // piled above and "2001@77.8 -> 2000@100" left a 22% hole below). Clamp
  // the centre so both year zones always keep at least MIN_YEAR_ZONE_PCT.
  // How many distinct years sit on each side of the current one? A side
  // with nothing to show needs no rail space, so the month band should
  // expand into it rather than leaving a hole - scrolled to the oldest
  // photos, "2001@77.8 -> 2000@100" left 22% of the rail empty purely
  // because the older zone had no years left to place.
  // Years outside the neighbour range are laid out by RANK within their
  // side, not by a compression curve over elapsed time.
  //
  // The curve approach (sqrt over time distance, with assorted floors and
  // clamps layered on) kept producing years that overflowed the zone and
  // were silently discarded by push()'s range check - most visibly, from
  // 2002 the labels for 2024 and 2025 vanished entirely, leaving no way to
  // navigate back to recent photos. Several attempts to rescue it with
  // minimum-step floors, clamping, and collision priorities each fixed one
  // position and broke another, because the underlying positions were
  // still allowed to leave the track.
  //
  // Even rank spacing can't overflow: n years always fit in n slots. It
  // gives up strict time-proportionality between distant years, which is a
  // fair trade for "every year is reachable, always" - and years nearest
  // the band still get the curve's spacing via the neighbour markers and
  // the month band itself.
  const newerList: number[] = []
  const olderList: number[] = []
  for (let y = newestYear + 1; y >= oldestYear; y--) {
    if (y >= currentYear - 1 && y <= currentYear + 1) continue
    if (y > currentYear) newerList.push(y)
    else olderList.push(y)
  }

  // More years than fit legibly get THINNED here, deterministically,
  // rather than left to the collision pass. Collision culling drops
  // whichever label loses a proximity contest, which alternated
  // arbitrarily as the cursor moved (2025 appearing and disappearing).
  // Thinning by a fixed stride instead keeps a stable, predictable set,
  // and always keeps index 0 - the year nearest the band - so the route
  // back toward recent photos is never removed.
  const thin = (list: number[], span: number): number[] => {
    const maxFit = Math.max(2, Math.floor(span / MIN_YEAR_LABEL_GAP_PCT))
    if (list.length <= maxFit) return list
    const stride = Math.ceil(list.length / maxFit)
    // Anchor the stride at the END of the list (the year nearest the
    // month band) rather than the start. Striding from index 0 - the
    // furthest year - dropped whichever years didn't land on the stride,
    // which consistently removed the newest year (2025) while keeping
    // 2024/2022/2020. The years adjacent to where you're looking are the
    // ones most worth keeping.
    const kept = list.filter((_, i) => (list.length - 1 - i) % stride === 0)
    // Always retain both ends: the furthest year so the span reads
    // end-to-end, and the nearest so the route back toward the cursor is
    // unbroken. Re-sorted afterwards - appending an end AFTER positions
    // were assigned by index put it visually out of sequence
    // ("2026 2023 2025 2020"), because position is derived from array
    // index, so the array must be ordered before positions are computed.
    const set = new Set(kept)
    set.add(list[0])
    set.add(list[list.length - 1])
    return [...set].sort((a, b) => list.indexOf(a) - list.indexOf(b))
  }

  // newerList runs newest -> oldest, i.e. top of the track downward.
  // Far zones stop short of the neighbour-year labels placed just outside
  // the band, so the two groups can't overlap or tie.
  const farTop = Math.max(0, zoneTop - monthStepPct)
  const farBottom = Math.min(100, zoneBottom + monthStepPct)
  const newerKept = thin(newerList, Math.max(0, farTop))
  newerKept.forEach((y, i) => {
    const span = Math.max(0, farTop)
    // newerList runs NEWEST -> oldest. The track is newest-at-top, so
    // index 0 (newest) maps to 0% and later indexes descend toward the
    // month band. Reversing this (tried) made years ascend down the track,
    // which is backwards for a newest-first timeline.
    const top = newerKept.length === 1 ? span / 2 : (i / (newerKept.length - 1)) * span
    push(`y${y}`, String(y), top, 'year', true, timeOf(y, 1))
  })

  // olderList runs newest -> oldest, i.e. below the band downward to 100%.
  const olderKept = thin(olderList, Math.max(0, 100 - farBottom))
  olderKept.forEach((y, i) => {
    const span = Math.max(0, 100 - farBottom)
    const top = olderKept.length === 1 ? farBottom + span / 2 : farBottom + ((i + 1) / olderKept.length) * span
    push(`y${y}`, String(y), top, 'year', true, timeOf(y, 1))
  })

  // The neighbouring years (+/-1) get a June marker as a mid-year anchor.
  // Their YEAR labels are placed by the same sqrt curve as every other
  // year above - placing them by a separate "just outside the band" rule
  // let them cross their neighbours (seen as "2004@14.3" rendering above
  // "2005@17.7", because 2004 used the band rule and 2005 used the curve).
  for (const ny of [currentYear + 1, currentYear - 1]) {
    if (ny > newestYear || ny < oldestYear) continue
    const isNewer = ny > currentYear
    // Half a month-step outside the band, so a neighbour year sits between
    // the month band and the far-year zone rather than exactly ON the
    // zone edge. Placing it at zoneTop/zoneBottom gave it the same
    // coordinate as the nearest far year, so the two tied and ordered
    // arbitrarily ("2026 2023 2025", "2022 2020 2018 2024").
    const yearTop = isNewer ? zoneTop - monthStepPct / 2 : zoneBottom + monthStepPct / 2
    if (yearTop < 0 || yearTop > 100) continue
    push(`y${ny}`, String(ny), yearTop, 'year', true, timeOf(ny, 1))

    // The June marker sits half a month-step outside its year label, and
    // only when nothing else has been placed in between - otherwise it
    // strands in a gap between two far-apart years, which is the "way too
    // much spacing" problem it exists to avoid.
    const junTop = isNewer ? yearTop - monthStepPct * 3 : yearTop + monthStepPct * 3
    if (junTop < 0 || junTop > 100) continue
    const crowded = out.some((l) => Math.abs(l.top - junTop) < monthStepPct * 2 && l.key !== `y${ny}`)
    if (crowded) continue
    // Don't render a second label with the same month name - the
    // neighbour year's June and the current year's June are different
    // dates but identical text, and showing both reads as a duplicate.
    if (out.some((l) => l.kind === 'month' && l.text === MONTH_NAMES[NEIGHBOUR_MONTH - 1])) continue
    push(
      `m${ny}-${NEIGHBOUR_MONTH}`,
      MONTH_NAMES[NEIGHBOUR_MONTH - 1],
      junTop,
      'month',
      false,
      timeOf(ny, NEIGHBOUR_MONTH),
    )
  }

  // Boundary markers, pinned hard so both ends of the range are always
  // explicit (user: "1st and last year should always be pinned at start
  // and end. maybe we should always add start of next year (2027) so end
  // points are clear").
  const withoutBoundaries = out.filter((l) => l.key !== `y${newestYear + 1}` && l.key !== `y${oldestYear}`)
  withoutBoundaries.push({
    key: `y${newestYear + 1}`,
    text: String(newestYear + 1),
    top: 0,
    kind: 'year',
    bold: true,
    rowIndex: buckets[0].rowIndex,
  })
  withoutBoundaries.push({
    key: `y${oldestYear}`,
    text: String(oldestYear),
    top: 100,
    kind: 'year',
    bold: true,
    rowIndex: buckets[buckets.length - 1].rowIndex,
  })

  // Drop anything that would collide with a neighbour, keeping the label
  // closest to the cursor (and years over months at equal distance).
  const MIN_GAP = 3.2
  const sorted = withoutBoundaries
    .map((l) => ({
      l,
      // Boundary markers are never dropped.
      // The cursor's own month outranks everything except the pinned
      // boundary markers. Years previously got a flat +1000 over months,
      // which meant a year label sitting near the cursor always evicted
      // the current month - so the arrow rendered with no label beside it
      // (measured: no month was ever bold at any scroll position).
      priority:
        l.top === 0 || l.top === 100
          ? 1e9

          : l.bold && l.kind === 'month'
            ? 1e8
            : (l.kind === 'year' ? 1000 : 0) - Math.abs(l.top - bandCentre),
    }))
    .sort((a, b) => b.priority - a.priority)

  const placed: ScrubberLabel[] = []
  for (const { l } of sorted) {
    if (placed.some((p) => Math.abs(p.top - l.top) < MIN_GAP)) continue
    placed.push(l)
  }
  // The indicator points at the CURRENT month's computed position, taken
  // straight from the same formula the month band uses rather than by
  // looking up the rendered label. Searching `placed` for the bold label
  // (tried first) silently failed whenever that label lost the collision
  // pass, leaving the arrow at the band centre while the current month
  // rendered elsewhere - the arrow sat at 36% with "Mar" at 50%.
  const indicatorTop = Math.max(0, Math.min(100, bandCentre))
  return { labels: placed.sort((a, b) => a.top - b.top), indicatorTop }
}


interface YearScrubberProps {
  buckets: MonthBucket[]
  totalRows: number
  scrollElement: HTMLElement | null
  onJumpToFraction: (fraction: number) => void
  /** Jump to an exact virtualizer row - used for label clicks, where
   *  interpolating a fraction would land somewhere approximate. */
  onJumpToRow: (rowIndex: number) => void
  /** See selectLabels' own doc - keeps the rail's range stable during a
   *  staged load instead of visibly growing as older rows arrive. */
  minYear?: number
}

export function YearScrubber({
  buckets,
  totalRows,
  scrollElement,
  onJumpToFraction,
  onJumpToRow,
  minYear,
}: YearScrubberProps) {
  const trackRef = useRef<HTMLDivElement>(null)
  const [indicatorFraction, setIndicatorFraction] = useState(0)
  const [dragging, setDragging] = useState(false)

  // rAF-throttled scroll handling: coalesce bursts of scroll events into
  // at most one state update per paint frame, so the scrubber's own
  // re-render never competes with thumbnail paint work for main-thread
  // time - this (not any coupling to photo load state) is what "smooth
  // regardless of whether photos have loaded" actually depends on, since
  // scrollTop itself already updates independently of <img> load state.
  useEffect(() => {
    if (!scrollElement) return
    let rafId = 0
    let pending = false
    function handleScroll() {
      if (pending) return
      pending = true
      rafId = requestAnimationFrame(() => {
        pending = false
        const el = scrollElement!
        const maxScroll = el.scrollHeight - el.clientHeight
        setIndicatorFraction(maxScroll > 0 ? el.scrollTop / maxScroll : 0)
      })
    }
    handleScroll()
    scrollElement.addEventListener('scroll', handleScroll, { passive: true })
    return () => {
      scrollElement.removeEventListener('scroll', handleScroll)
      cancelAnimationFrame(rafId)
    }
  }, [scrollElement])

  const { labels, indicatorTop } = useMemo(() => {
    if (totalRows <= 1) return { labels: [] as ScrubberLabel[], indicatorTop: INDICATOR_TOP_PCT }
    const currentRow = indicatorFraction * (totalRows - 1)
    return selectLabels(buckets, currentRow, totalRows, minYear)
  }, [buckets, indicatorFraction, totalRows, minYear])

  // Pointer -> scroll fraction, resolved against the labels actually on
  // screen. Track position is a piecewise zone layout now, not an
  // invertible curve, so there's no formula to reverse - instead find the
  // two nearest labels above and below the pointer and interpolate
  // between their row indexes. Clicking a label therefore lands exactly on
  // that label's own date, and dragging between two labels lands
  // proportionally between them.
  function fractionFromPointer(clientY: number): number {
    const track = trackRef.current
    if (!track || buckets.length === 0) return 0
    const rect = track.getBoundingClientRect()
    const posPct = Math.max(0, Math.min(100, ((clientY - rect.top) / rect.height) * 100))

    if (labels.length === 0) return posPct / 100

    let above = labels[0]
    let below = labels[labels.length - 1]
    for (const l of labels) {
      if (l.top <= posPct && l.top >= above.top) above = l
      if (l.top >= posPct && l.top <= below.top) below = l
    }

    let targetRow: number
    if (below.top === above.top) {
      targetRow = above.rowIndex
    } else {
      const t = (posPct - above.top) / (below.top - above.top)
      targetRow = above.rowIndex + t * (below.rowIndex - above.rowIndex)
    }
    return Math.min(1, Math.max(0, targetRow / Math.max(1, totalRows - 1)))
  }

  function handlePointerDown(e: React.PointerEvent) {
    setDragging(true)
    ;(e.target as HTMLElement).setPointerCapture(e.pointerId)
    onJumpToFraction(fractionFromPointer(e.clientY))
  }

  function handlePointerMove(e: React.PointerEvent) {
    if (!dragging) return
    onJumpToFraction(fractionFromPointer(e.clientY))
  }

  if (totalRows === 0 || buckets.length === 0) return null

  return (
    <div
      ref={trackRef}
      data-testid="year-scrubber"
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={() => setDragging(false)}
      style={{
        position: 'fixed',
        top: 8,
        right: 16,
        bottom: 8,
        // Wide enough for a ~104px label column plus the 14px gap to the
        // track pill on the right - if this is narrower than the labels
        // need, they overflow leftward past the container and the track
        // itself gets pushed out of view (seen directly in testing).
        width: 124,
        cursor: 'grab',
        userSelect: 'none',
        touchAction: 'none',
      }}
    >
      {/* solid track pill - both the visual scrollbar-like affordance and
          the actual (wide) hit target for click/drag */}
      <div
        style={{
          position: 'absolute',
          right: 0,
          top: 0,
          bottom: 0,
          width: 8,
          borderRadius: 4,
          background: 'var(--scrubber-track, #d0d0d0)',
          boxShadow: 'inset 0 0 0 1px rgba(0,0,0,0.06)',
        }}
      />

      {/* Labels positioned by time-distance through the fisheye curve.
          Keyed by their own identity (not slot index) so React keeps the
          same DOM node for a given month/year as it moves - which is what
          lets the CSS `top` transition animate the warp instead of
          popping to a new position. */}
      {labels.map((l) => (
        <div
          key={l.key}
          data-testid="scrubber-slot"
          // Each label is directly clickable and jumps to its own exact
          // row, rather than relying on the track's pointer-position
          // interpolation - clicking "2019" should land on 2019, not on
          // whatever the interpolation between neighbouring labels
          // happens to resolve to. stopPropagation keeps the track's own
          // pointerdown from also firing and overriding it.
          onPointerDown={(e) => {
            e.stopPropagation()
            // rowIndex is a VIRTUALIZER ROW, not a photo index and not a
            // scroll fraction - rows and photos differ by the column count
            // plus header rows, so dividing by totalRows and treating the
            // result as a scroll fraction landed years away from the label
            // that was clicked (clicking "2018" jumped to 2024).
            onJumpToRow(l.rowIndex)
          }}
          style={{
            position: 'absolute',
            top: `${l.top}%`,
            right: 14,
            transform: 'translateY(-50%)',
            fontSize: l.bold ? 13 : l.kind === 'year' ? 12 : 11,
            fontWeight: l.bold || l.kind === 'year' ? 700 : 500,
            color: l.bold ? 'var(--text)' : 'var(--text-muted, #767676)',
            whiteSpace: 'nowrap',
            textAlign: 'right',
            cursor: 'pointer',
            // Padding widens the hit target well beyond the glyphs without
            // moving them, since these are small text targets.
            padding: '3px 4px',
            // Animating `top` is what produces the smooth warp as labels
            // slide around the pinned indicator. Disabled while dragging,
            // where the position should track the pointer exactly rather
            // than lag behind it.
            transition: dragging ? 'none' : 'top 0.12s linear, opacity 0.12s',
          }}
        >
          {l.text}
        </div>
      ))}

      {/* Position indicator: normally at INDICATOR_TOP_PCT, drifting up to
          INDICATOR_DRIFT_PCT toward whichever edge has run out of years
          (see selectLabels) so the rail stays full at the extremes. */}
      <div
        data-testid="scrubber-indicator"
        style={{
          position: 'absolute',
          right: -6,
          top: `${indicatorTop}%`,
          transform: `translateY(-50%) scale(${dragging ? 1.2 : 1})`,
          width: 0,
          height: 0,
          borderTop: '10px solid transparent',
          borderBottom: '10px solid transparent',
          borderLeft: '14px solid var(--scrubber-indicator, #af3029)',
          transition: 'top 0.15s, transform 0.1s',
          pointerEvents: 'none',
          filter: 'drop-shadow(0 1px 3px rgba(0,0,0,0.4))',
        }}
      />
    </div>
  )
}
