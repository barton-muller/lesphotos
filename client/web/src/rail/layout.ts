/**
 * Pure layout for the timeline rail. No React, no DOM — given a continuous
 * position in "absolute months" and a rail height, produce the ticks to draw.
 *
 * The whole design rests on one idea: months live in a fixed-size band whose
 * slots never move (labels scroll *through* them), and years are placed by a
 * linear year->pixel map with a hole cut out of it exactly the height of that
 * band. The hole slides down the rail as you scroll back in time, which is
 * what keeps the years evenly spaced on each side while the band pushes them
 * apart.
 */

export const MONTH_NAMES = [
  'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
  'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC',
]

export interface RailConfig {
  /** First year on the rail, inclusive. */
  minYear: number
  /** Last year on the rail, inclusive. */
  maxYear: number
  /** How many month slots either side of the centre one. 3 or 4. */
  radius: number
  /** Vertical gap between adjacent month slots, px. */
  monthSpacing: number
  /** Px kept clear at the top and bottom of the rail. */
  padding: number
  /** Minimum px between year ticks; below this, distant years are dropped. */
  minYearGap: number
}

export const DEFAULT_CONFIG: RailConfig = {
  minYear: 2000,
  maxYear: 2026,
  radius: 3,
  monthSpacing: 26,
  padding: 14,
  minYearGap: 20,
}

/**
 * Pick a config that fits the rail height. The month band is a fixed pixel cost
 * (2*radius*monthSpacing) regardless of range, so on a short rail it's the band
 * — not the year count — that starves the years of room. Shrinking the radius
 * and spacing first is what buys the years their space back; thinning years
 * alone can't, because the band's height doesn't depend on them.
 */
export function configFor(height: number, base: RailConfig = DEFAULT_CONFIG): RailConfig {
  if (height >= 700) return base
  if (height >= 520) {
    return { ...base, radius: 3, monthSpacing: 22, minYearGap: 17 }
  }
  // Very short rail: 2 months either side, tighter everything.
  return { ...base, radius: 2, monthSpacing: 20, minYearGap: 16, padding: 10 }
}


/** Absolute month index: months since Jan of year 0. Fractional between months. */
export function toAbsMonth(year: number, month: number): number {
  return year * 12 + month
}

export function yearOf(abs: number): number {
  return Math.floor(abs / 12)
}

export function monthOf(abs: number): number {
  return ((Math.floor(abs) % 12) + 12) % 12
}

export interface MonthTick {
  kind: 'month'
  /** Integer absolute month index this label represents. */
  abs: number
  label: string
  /** Year to draw in the right-hand column, or null if this isn't a JAN. */
  yearLabel: number | null
  y: number
  opacity: number
  /** True for the slot under the pointer marker. */
  current: boolean
}

export interface YearTick {
  kind: 'year'
  year: number
  label: string
  y: number
  opacity: number
}

export type Tick = MonthTick | YearTick

export interface RailLayout {
  ticks: Tick[]
  /** Pixel y of the centre month slot — where the ▶ marker sits. */
  markerY: number
  /** Top and bottom edges of the month band, for drawing a bracket. */
  bandTop: number
  bandBottom: number
}

/**
 * The band's centre travels linearly down the rail from `maxYear` (top) to
 * `minYear` (bottom), so scrolling always moves the marker in the direction
 * you'd expect even though the years around it are being redistributed.
 */
function bandCentreY(pos: number, cfg: RailConfig, height: number): number {
  // Reserve room beyond the band for the years that must always be shown (the
  // +-NEAR window plus the range endpoint). Without this the band pins flush to
  // the rail edge at the extremes of the range, leaving a couple of pixels into
  // which those mandatory years get crushed on top of each other — which is a
  // blur of overlapping labels, the exact thing being fixed here.
  const half = cfg.radius * cfg.monthSpacing
  const reserve = Math.min(
    3 * cfg.minYearGap,
    Math.max(0, (height - 2 * cfg.padding - 2 * half) / 2),
  )
  const top = cfg.padding + half + reserve
  const bottom = height - cfg.padding - half - reserve
  const newest = toAbsMonth(cfg.maxYear, 11)
  const oldest = toAbsMonth(cfg.minYear, 0)
  const f = (newest - pos) / (newest - oldest)
  return top + clamp(f, 0, 1) * (bottom - top)
}

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v
}


/**
 * Lay out the years for both sides of the band.
 *
 * Rank is over *kept* years, not calendar years: once decimation is in play,
 * spacing by calendar distance would leave visible holes where dropped years
 * used to be, which reads as a bug rather than as thinning. Spacing by kept
 * rank keeps the surviving ticks evenly pitched.
 */
/**
 * The rail's time axis: pixel y for any absolute month.
 *
 * Months and years both sit on this, which is what makes a year label land
 * level with its own JAN. Inside the band a month slot is `monthSpacing` tall,
 * so the axis is linear there; the year stack further out is thinned rather
 * than compressed, so the same scale carries all the way.
 *
 * Derived from the month band's own formula: layoutRail draws slot k at
 * `centre - k * monthSpacing + drift`, where k is (abs - round(pos)) and drift
 * is the fractional part - which reduces to exactly this.
 */
function monthToY(
  abs: number,
  pos: number,
  cfg: RailConfig,
  height: number,
): number {
  const centre = bandCentreY(pos, cfg, height)
  const delta = abs - pos
  const bandMonths = cfg.radius + 1

  // Linear inside the band, compressed outside it.
  //
  // A purely linear axis would be correct but useless: at 22px a month, a year
  // away is 264px, so only two or three years fit on the rail at all and the
  // rest of the library falls off the ends. Beyond the band the scale is
  // squeezed so the whole range always fits, which is the same thing the month
  // band and year stack were always doing - just expressed as one function
  // instead of two layouts that had to be reconciled at the seam.
  if (Math.abs(delta) <= bandMonths) return centre - delta * cfg.monthSpacing

  const dir = delta > 0 ? 1 : -1
  const edge = centre - dir * (bandMonths * cfg.monthSpacing)
  // Months from the band's edge to the far end of the library on this side.
  const newest = toAbsMonth(cfg.maxYear, 11)
  const oldest = toAbsMonth(cfg.minYear, 0)
  const far = dir > 0 ? newest - pos : pos - oldest
  const remaining = Math.max(1, far - bandMonths)
  // Pixels available between the band's edge and the rail's end.
  const room =
    dir > 0
      ? edge - cfg.padding
      : height - cfg.padding - edge
  const past = Math.abs(delta) - bandMonths
  return edge - dir * Math.min(past / remaining, 1) * Math.max(0, room)
}

/**
 * Inverse of monthToY: which absolute month a pixel y corresponds to.
 *
 * Clicking or dragging the rail has to invert the SAME axis the labels are
 * drawn on. It previously used bandCentreY's linear range mapping instead,
 * which is a different curve entirely - so a tap on "2015" jumped somewhere
 * else, and the further the label sat from the band the further out the jump
 * was. Anything that turns a pointer position into a time must come through
 * here.
 */
export function yToMonth(
  y: number,
  pos: number,
  cfg: RailConfig,
  height: number,
): number {
  const centre = bandCentreY(pos, cfg, height)
  const bandMonths = cfg.radius + 1
  const bandPx = bandMonths * cfg.monthSpacing

  // Inside the band the axis is linear, so invert directly.
  if (Math.abs(y - centre) <= bandPx) {
    return pos - (y - centre) / cfg.monthSpacing
  }

  // Outside it, undo the compression on whichever side the point falls.
  // dir > 0 is newer (up the rail, smaller y).
  const dir = y < centre ? 1 : -1
  const edge = centre - dir * bandPx
  const newest = toAbsMonth(cfg.maxYear, 11)
  const oldest = toAbsMonth(cfg.minYear, 0)
  const far = dir > 0 ? newest - pos : pos - oldest
  const remaining = Math.max(1, far - bandMonths)
  const room = dir > 0 ? edge - cfg.padding : height - cfg.padding - edge
  if (room <= 0) return pos + dir * far
  const frac = Math.min(Math.abs(edge - y) / room, 1)
  return pos + dir * (bandMonths + frac * remaining)
}

function layoutYears(
  pos: number,
  cfg: RailConfig,
  height: number,
  minGap: number,
  /** Years already drawn beside their JAN, with the y they were drawn at. */
  drawnInBand: Map<number, number>,
): { year: number; y: number }[] {
  const top = cfg.padding
  const bottom = height - cfg.padding

  // One axis, not two stacks.
  //
  // A year label IS a point in time - 1 Jan of that year - and belongs wherever
  // that instant falls on the rail, exactly like a month label. It is above the
  // marker when Jan Y is later than the current position and below when it is
  // earlier; there is no separate "newer stack" and "older stack" to assign it
  // to, and nothing has to decide which side the year you are currently in
  // belongs on.
  //
  // The previous model built two stacks either side of the band and forced the
  // boundary year onto one of them. Every version of that was wrong somewhere:
  // ceil/floor put the current year below the band for all but its first month
  // (Sep 2025 read as 2024), and splitting on the calendar year instead just
  // moved the error to May, when Jan really is behind you.
  const yearY = (year: number) => monthToY(toAbsMonth(year, 0), pos, cfg, height)

  // Which years could be drawn at all: those inside the library's own span. A
  // library ending in 2025 has no Jan 2026, so nothing is drawn above the band
  // when you are late in 2025 - the axis simply stops there.
  const candidates: { year: number; y: number }[] = []
  for (let year = cfg.minYear; year <= cfg.maxYear; year++) {
    // Drawn beside its JAN in the band already; a second copy in the stack
    // would land a few pixels away and read as a smudge.
    if (drawnInBand.has(year)) continue
    const y = yearY(year)
    if (y < top || y > bottom) continue
    candidates.push({ year, y })
  }

  // Thin by a fixed STRIDE from the current year, not by a greedy nearest-first
  // fit.
  //
  // Greedy selection made the whole stack flip whenever a year crossed into or
  // out of the month band: the band year gave up its slot, the next candidate
  // suddenly fitted, and every year below it shifted. Stepping from 2024-04 to
  // 2024-05 turned "2024 2022 2020 2018 ..." into "2023 2021 2019 2017 ...", so
  // every label below the band moved at once and half of them vanished - the
  // jitter, and the reason a year could disappear as its neighbour arrived.
  //
  // A stride anchored on the current year has none of that. Which years are
  // shown depends only on the current year and the stride, so a label enters or
  // leaves only when the stride itself changes, and the ones that stay keep
  // their identity while sliding smoothly along the axis.
  const centre = bandCentreY(pos, cfg, height)
  // Space the YEAR stack actually has: the rail minus the month band, which
  // sits in the middle and holds no year labels of its own.
  //
  // Measuring against the whole rail overstated it badly - 27 years "fitted"
  // in 34 slots, so stride 1 was chosen, all 27 were selected, and the spacing
  // filter then dropped roughly half of them by position. Which half changed
  // as the axis shifted, so the visible set flipped parity wholesale (23 labels
  // at once) instead of thinning to a stable scale.
  const bandPx = 2 * (cfg.radius + 1) * cfg.monthSpacing
  const capacity = Math.max(4, Math.floor((bottom - top - bandPx) / minGap))

  // Years drawn in the band occupy real space the stack must clear, or the
  // first stack label lands a few pixels under the one beside JAN (measured
  // 4.4px). They are not in `kept` - the band owns them - so their positions
  // are seeded here as obstacles.
  const obstacles = [...drawnInBand.values()]

  // Coarsest stride that fits the WHOLE library in the space available.
  //
  // Derived from the library's range and the rail's height - both fixed while
  // you scroll - and deliberately not from the distance to the current year.
  // A position-dependent span made the stride itself flip at each January
  // (2 -> 1 between Dec 2009 and Jan 2010), which re-selected every label at
  // once: the single biggest source of the jitter, and invisible from the
  // stride rule alone because it looked stable within any one year.
  //
  // Calendar-shaped steps rather than arbitrary ones, so the labels read as a
  // deliberate scale.
  const STRIDES = [1, 2, 5, 10, 20, 25, 50, 100]
  const totalYears = cfg.maxYear - cfg.minYear
  let stride = STRIDES[STRIDES.length - 1]
  for (const s of STRIDES) {
    // +2 for the endpoints, which are forced in below whatever the stride says.
    if (totalYears / s + 2 <= capacity) {
      stride = s
      break
    }
  }

  // Multiples of the stride on the ABSOLUTE calendar - 2024, 2022, 2020 at
  // stride 2 - which is what the original mockup did (`y % stride === 0`) and
  // what got lost when pickYears was replaced.
  //
  // The phase has to be independent of where you are looking. Anchoring on
  // currentYear re-phases the whole stack every January, and anchoring on
  // minYear re-phases it whenever the library's oldest photo changes; either
  // way every label below the band jumps at once and half of them are replaced.
  // A calendar-absolute stride is the only choice that is stable under both.
  const onStride = (y: number) => y % stride === 0

  // The years either side of the current one survive whatever the stride says.
  // Without this the year you have just scrolled out of vanishes the moment it
  // leaves the band - at stride 2 from May 2025, "2024" disappeared as soon as
  // JAN 2025 left the month band, which is the label most worth keeping.
  // (The original mockup's pickYears had the same rule, as `mustKeep`.)
  // Measured in PIXELS from the marker, not in whole years from currentYear.
  //
  // A year-count window steps when currentYear does, so its membership changes
  // at every January - which is the same jump this stride was chosen to avoid,
  // just moved from the stride to the exception. A pixel window moves smoothly
  // with the scroll, so a label enters or leaves it one at a time, gradually,
  // and never at a boundary shared with every other label.
  const NEAR_PX = 3 * minGap
  const mustKeep = (c: { y: number }) => Math.abs(c.y - centre) <= NEAR_PX

  // On-stride years are placed FIRST, then the near-marker extras fill in
  // around them.
  //
  // Taking candidates in year order let an off-stride year claim a pixel that
  // an on-stride one then could not use - so at the compressed end of the range
  // the whole scale flipped parity (2023 2021 2019 ... became 2024 2022 2020
  // ...) as one label displaced another. The stride is the scale; the near
  // window is an addition to it, and must never displace it.
  const clearsAll = (c: { y: number }, placed: { y: number }[]) =>
    obstacles.every((o) => Math.abs(o - c.y) >= minGap) &&
    placed.every((k) => Math.abs(k.y - c.y) >= minGap)

  const kept: { year: number; y: number }[] = []
  for (const c of candidates) {
    if (onStride(c.year) && clearsAll(c, kept)) kept.push(c)
  }
  for (const c of candidates) {
    if (!onStride(c.year) && mustKeep(c) && clearsAll(c, kept)) kept.push(c)
  }
  // Nearest the marker wins when the rail still cannot hold them all.
  kept.sort((a, b) => Math.abs(a.y - centre) - Math.abs(b.y - centre))
  kept.splice(capacity)

  // The range's endpoints anchor the rail: without them it looks like the
  // library stops at whatever year happened to survive thinning. Added even if
  // they crowd a neighbour, then de-overlapped below.
  for (const edge of [cfg.minYear, cfg.maxYear]) {
    if (drawnInBand.has(edge)) continue
    if (kept.some((k) => k.year === edge)) continue
    const y = clamp(yearY(edge), top, bottom)
    // Evict whatever it would land on rather than stacking on top of it. This
    // injection bypasses the thinning above, so without this the endpoint sat
    // fractions of a pixel from a kept year (measured 0.1px on a 70-year range)
    // - and the spacing sweeps below cannot separate them, because both are
    // pinned: one to the rail's end, the other by its own neighbours.
    for (let i = kept.length - 1; i >= 0; i--) {
      if (Math.abs(kept[i].y - y) < minGap) kept.splice(i, 1)
    }
    kept.push({ year: edge, y })
  }

  kept.sort((a, b) => a.y - b.y)

  // Final spacing guarantee: no two labels closer than minGap, none off-rail.
  //
  // Done as a few relaxation sweeps rather than one directional pass. A single
  // pass outwards from the marker cannot fix crowding it has already walked
  // past, and clamping the ends back onto the rail can re-crowd labels the pass
  // already spaced - the two fight, and pairs as close as 0.1px survived. Each
  // sweep pushes overlapping neighbours apart and pulls strays back inside;
  // repeating converges because every push is bounded by the rail's ends.
  // Clamp first, then relax. Clamping after the sweeps re-crowds exactly the
  // pair the sweeps just separated - the endpoint gets pinned to the rail's end
  // while its neighbour keeps the position it was spread to, which is how the
  // newest year ended up 4px from the year below it.
  for (const k of kept) k.y = clamp(k.y, top, bottom)

  for (let sweep = 0; sweep < 12; sweep++) {
    let moved = false
    for (let i = 1; i < kept.length; i++) {
      const gap = kept[i].y - kept[i - 1].y
      if (gap < minGap) {
        const push = (minGap - gap) / 2
        kept[i - 1].y -= push
        kept[i].y += push
        moved = true
      }
    }
    // Pull the ends back on-rail; the next sweep re-spaces anything this
    // crowded.
    if (kept.length > 0) {
      const first = kept[0]
      const last = kept[kept.length - 1]
      if (first.y < top) {
        const shift = top - first.y
        for (const k of kept) k.y += shift
        moved = true
      }
      if (last.y > bottom) {
        const shift = last.y - bottom
        for (const k of kept) k.y -= shift
        moved = true
      }
    }
    if (!moved) break
  }

  return kept.filter((k) => k.y >= top - 0.5 && k.y <= bottom + 0.5)
}

/** Inverse of bandCentreY, in whole years — which year the band is sitting on. */
function yearAtCentre(centreY: number, cfg: RailConfig, height: number): number {
  const half = cfg.radius * cfg.monthSpacing
  const top = cfg.padding + half
  const bottom = height - cfg.padding - half
  const f = bottom > top ? (centreY - top) / (bottom - top) : 0
  return cfg.maxYear - f * (cfg.maxYear - cfg.minYear)
}

/** Opacity falloff across the month band: full at centre, ~0 just past the edge. */
function monthOpacity(k: number, radius: number): number {
  const d = Math.abs(k) / (radius + 1)
  return clamp(1 - d * d, 0, 1)
}

export function layoutRail(
  pos: number,
  height: number,
  cfg: RailConfig = DEFAULT_CONFIG,
): RailLayout {
  const centre = bandCentreY(pos, cfg, height)
  const half = cfg.radius * cfg.monthSpacing
  const bandTop = centre - half
  const bandBottom = centre + half

  const ticks: Tick[] = []

  // --- months -------------------------------------------------------------
  // `pos` is fractional; the fraction is what slides labels between slots, so
  // an entering month fades in continuously rather than popping.
  const base = Math.round(pos)
  const drift = (pos - base) * cfg.monthSpacing
  for (let k = -cfg.radius - 1; k <= cfg.radius + 1; k++) {
    const abs = base + k
    if (abs < toAbsMonth(cfg.minYear, 0) || abs > toAbsMonth(cfg.maxYear, 11)) continue
    const m = monthOf(abs)
    // Scrolling forward in time moves labels *up*, so k maps to -k in pixels.
    const y = centre - k * cfg.monthSpacing + drift
    if (y < cfg.padding || y > height - cfg.padding) continue
    ticks.push({
      kind: 'month',
      abs,
      label: MONTH_NAMES[m],
      yearLabel: m === 0 ? yearOf(abs) : null,
      y,
      opacity: monthOpacity(k - (pos - base), cfg.radius),
      current: k === 0,
    })
  }

  // --- years --------------------------------------------------------------
  // Years thin out with distance from the band when there isn't room for all
  // of them (a 30-year range, or a short viewport). See layoutYears/pickYears.
  const bandYear = yearAtCentre(centre, cfg, height)

  // A year whose JAN is inside the month band is drawn in the year column at
  // JAN's own y — so as the year turns, its label rides up the rail alongside
  // JAN and slots into the stack, rather than popping between two columns.
  // A year is emitted at most once. The band-JAN pass and the stack pass can
  // otherwise both claim the same year (their notions of "in the band" differ
  // by a slot), which stacks two labels at almost the same y and reads as a
  // blur of overlapping text. Dedupe here rather than trying to keep the two
  // guards in agreement — this is the invariant that actually matters.
  const seen = new Set<number>()
  const pushYear = (year: number, y: number, opacity: number) => {
    if (seen.has(year)) return
    seen.add(year)
    ticks.push({ kind: 'year', year, label: String(year), y, opacity })
  }

  // Band-JAN years win: they're pinned to their JAN's y, which is the whole
  // point of the ride-up-with-JAN behaviour. Snapshot first — appending to an
  // array being iterated with for..of would revisit the new entries.
  const drawnInBand = new Map<number, number>()
  for (const t of [...ticks]) {
    if (t.kind === 'month' && t.yearLabel !== null) {
      pushYear(t.yearLabel, t.y, Math.max(0.85, t.opacity))
      drawnInBand.set(t.yearLabel, t.y)
    }
  }

  for (const { year, y } of layoutYears(pos, cfg, height, cfg.minYearGap, drawnInBand)) {
    pushYear(year, y, Math.abs(year - bandYear) < 1.5 ? 0.55 : 0.8)
  }

  return { ticks, markerY: centre, bandTop, bandBottom }
}
