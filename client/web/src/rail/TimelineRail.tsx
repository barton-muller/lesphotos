import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  DEFAULT_CONFIG,
  configFor,
  layoutRail,
  toAbsMonth,
  yToMonth,
  type RailConfig,
} from './layout'

export interface TimelineRailProps {
  /** Continuous position, in absolute months (year*12 + month). */
  pos: number
  /** Called as the user drags or clicks the rail. */
  onSeek: (pos: number) => void
  /** Called on release, so the caller can settle onto an exact row. */
  onSeekEnd?: (pos: number) => void
  config?: Partial<RailConfig>
  /**
   * Phone layout: show only a slim handle at the screen edge, and reveal the
   * full rail while the user is dragging it.
   *
   * The labels are always laid out and always mounted - only their opacity and
   * the panel behind them change - so expanding costs no work and the drag that
   * triggered it is never interrupted by a remount.
   */
  collapsed?: boolean
}

/** Rail width including the label columns; the grid reserves this much. */
export const RAIL_WIDTH = 124

/**
 * The scrubber rail: a month band whose slots stay put while labels flow
 * through them, over a year stack that thins with distance. All layout maths
 * lives in layout.ts — this file is only rendering and pointer handling, so the
 * two can be developed (or replaced) independently.
 */
export function TimelineRail({
  pos,
  onSeek,
  onSeekEnd,
  config,
  collapsed = false,
}: TimelineRailProps) {
  const [height, setHeight] = useState(0)
  const elRef = useRef<HTMLDivElement | null>(null)

  // Density is chosen from the measured height, then caller overrides win.
  //
  // Memoised on its inputs rather than rebuilt each render: posFromY closes
  // over it, so a fresh object every render gave that callback a new identity
  // every render too, re-registering the drag listeners mid-gesture.
  const cfg: RailConfig = useMemo(
    () => ({
      ...configFor(height, { ...DEFAULT_CONFIG, ...config }),
      ...config,
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [height, config?.minYear, config?.maxYear, config?.minYearGap],
  )

  // Callback ref, not useRef+effect: the node may not exist on first render,
  // and an effect that saw null would never get a second chance.
  const attach = useCallback((node: HTMLDivElement | null) => {
    elRef.current = node
    if (!node) return
    setHeight(node.clientHeight)
    const ro = new ResizeObserver(() => setHeight(node.clientHeight))
    ro.observe(node)
  }, [])

  /** Map a pointer y back to a position, inverting bandCentreY. */
  /**
   * Map a pointer y back to a position, inverting the axis the labels are
   * drawn on.
   *
   * This used to invert bandCentreY's linear range mapping instead - a
   * different curve from the one layoutRail places labels with - so tapping a
   * label jumped somewhere else, and the further that label sat from the band
   * the worse the miss. yToMonth is the exact inverse of monthToY, so a tap
   * now lands on whatever was tapped.
   */
  // The position the current gesture started from.
  //
  // yToMonth inverts an axis that is itself defined relative to `pos`, so
  // feeding it the live position moves the axis under the finger: holding
  // still, the rail drifts continuously (measured 2022 -> 2025 over eight
  // frames without moving). Freezing the frame of reference for the duration
  // of a gesture makes a held finger hold its place, and a drag track it.
  const gestureFrom = useRef(pos)

  const posFromY = useCallback(
    (clientY: number, from: number) => {
      const node = elRef.current
      if (!node) return pos
      const rect = node.getBoundingClientRect()
      const newest = toAbsMonth(cfg.maxYear, 11)
      const oldest = toAbsMonth(cfg.minYear, 0)
      const next = yToMonth(clientY - rect.top, from, cfg, rect.height)
      return next < oldest ? oldest : next > newest ? newest : next
    },
    [cfg, pos],
  )

  const [dragging, setDragging] = useState(false)
  useEffect(() => {
    if (!dragging) return
    const move = (e: PointerEvent) =>
      onSeek(posFromY(e.clientY, gestureFrom.current))
    const up = (e: PointerEvent) => {
      setDragging(false)
      onSeekEnd?.(posFromY(e.clientY, gestureFrom.current))
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    return () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
    }
  }, [dragging, onSeek, onSeekEnd, posFromY])

  const { ticks, markerY } = layoutRail(pos, height, cfg)

  // Collapsed until touched. `expanded` drives only opacity and the backdrop,
  // never whether the labels exist, so a drag is never interrupted.
  const expanded = !collapsed || dragging

  return (
    <div
      ref={attach}
      data-testid="timeline-rail"
      data-expanded={expanded ? 'true' : 'false'}
      onPointerDown={(e) => {
        // Freeze the axis at the position the gesture starts from; every move
        // in this drag is measured against that same frame.
        gestureFrom.current = pos
        setDragging(true)
        onSeek(posFromY(e.clientY, pos))
      }}
      style={{
        // Absolute within the grid's own scroll container, not fixed to the
        // viewport: as `fixed` it floated over the app's header and swallowed
        // clicks on the buttons there, including the account menu.
        position: 'absolute',
        top: 8,
        // Flush to the edge when collapsed: the handle is a thumb target, and
        // an inset one is easy to miss and easy to trigger by accident.
        right: collapsed ? 0 : 16,
        bottom: 8,
        width: collapsed && !dragging ? 28 : RAIL_WIDTH,
        cursor: 'grab',
        userSelect: 'none',
        touchAction: 'none',
        // Only while expanded on a phone: the labels would otherwise sit
        // unreadably over the photos they are floating above.
        background:
          collapsed && dragging ? 'var(--surface)' : 'transparent',
        borderRadius: collapsed && dragging ? 'var(--radius) 0 0 var(--radius)' : 0,
        boxShadow: collapsed && dragging ? '0 0 20px rgba(0,0,0,0.22)' : 'none',
        zIndex: 30,
      }}
    >
      {/* Track pill: the scrollbar-like affordance and the drag target. */}
      <div
        style={{
          position: 'absolute',
          top: 0,
          bottom: 0,
          right: 0,
          width: 8,
          borderRadius: 4,
          background: 'var(--scrubber-track, #d0d0d0)',
        }}
      />
      <div
        style={{
          position: 'absolute',
          top: markerY,
          right: 0,
          width: 16,
          height: 2,
          marginTop: -1,
          borderRadius: 1,
          background: 'var(--scrubber-indicator, #af3029)',
        }}
      />
      {ticks.map((t) =>
        t.kind === 'month' ? (
          <div
            key={`m${t.abs}`}
            style={{
              position: 'absolute',
              top: t.y,
              right: 62,
              transform: 'translateY(-50%)',
              opacity: expanded ? t.opacity : 0,
              transition: 'opacity 140ms ease',
              whiteSpace: 'nowrap',
              pointerEvents: 'none',
              fontSize: 12,
              fontWeight: 600,
              letterSpacing: 1,
              fontVariantNumeric: 'tabular-nums',
              color: t.current
                ? 'var(--scrubber-indicator, #af3029)'
                : 'var(--text)',
            }}
          >
            {t.label}
          </div>
        ) : (
          <div
            key={`y${t.year}`}
            style={{
              position: 'absolute',
              top: t.y,
              right: 26,
              transform: 'translateY(-50%)',
              opacity: expanded ? t.opacity : 0,
              transition: 'opacity 140ms ease',
              whiteSpace: 'nowrap',
              pointerEvents: 'none',
              fontSize: 11,
              fontVariantNumeric: 'tabular-nums',
              color: 'var(--text-muted, #767676)',
            }}
          >
            {t.label}
          </div>
        ),
      )}
    </div>
  )
}
