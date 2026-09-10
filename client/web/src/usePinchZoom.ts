import { useEffect } from 'react'

/**
 * Pinch-to-zoom that changes grid density instead of scaling the page.
 *
 * Two gestures reach the same handler because browsers report them
 * differently:
 *
 *   - touch: two pointers, tracked by their own distance apart.
 *   - trackpad: a pinch arrives as a `wheel` event with ctrlKey set, which is
 *     also what a browser's own page zoom listens for. Both gestures must call
 *     preventDefault or the page zooms underneath the grid.
 *
 * Fewer columns means bigger tiles, so a spreading pinch (distance growing)
 * decreases the column count - the same direction as zooming into a photo.
 */
export function usePinchZoom(
  element: HTMLElement | null,
  onZoom: (delta: number) => void,
) {
  useEffect(() => {
    if (!element) return

    // --- trackpad / ctrl+wheel ------------------------------------------
    let wheelAccum = 0
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey) return
      // Without this the browser zooms the whole page instead.
      e.preventDefault()
      wheelAccum += e.deltaY
      // Threshold, not one step per event: a trackpad emits a stream of small
      // deltas, and stepping on each would race through the whole range.
      const STEP = 40
      while (Math.abs(wheelAccum) >= STEP) {
        onZoom(wheelAccum > 0 ? 1 : -1)
        wheelAccum -= Math.sign(wheelAccum) * STEP
      }
    }

    // --- touch ------------------------------------------------------------
    const active = new Map<number, { x: number; y: number }>()
    let baseline: number | null = null

    const distance = () => {
      const [a, b] = [...active.values()]
      return Math.hypot(a.x - b.x, a.y - b.y)
    }

    const onPointerDown = (e: PointerEvent) => {
      if (e.pointerType !== 'touch') return
      active.set(e.pointerId, { x: e.clientX, y: e.clientY })
      if (active.size === 2) baseline = distance()
    }

    const onPointerMove = (e: PointerEvent) => {
      if (e.pointerType !== 'touch' || !active.has(e.pointerId)) return
      active.set(e.pointerId, { x: e.clientX, y: e.clientY })
      if (active.size !== 2 || baseline === null) return
      const current = distance()
      // Ratio, not pixel delta: the same finger travel should mean the same
      // zoom whether the fingers started close together or far apart.
      const ratio = current / baseline
      const STEP = 1.25
      if (ratio > STEP) {
        onZoom(-1)
        baseline = current
      } else if (ratio < 1 / STEP) {
        onZoom(1)
        baseline = current
      }
    }

    const onPointerUp = (e: PointerEvent) => {
      active.delete(e.pointerId)
      if (active.size < 2) baseline = null
    }

    element.addEventListener('wheel', onWheel, { passive: false })
    element.addEventListener('pointerdown', onPointerDown)
    element.addEventListener('pointermove', onPointerMove)
    window.addEventListener('pointerup', onPointerUp)
    window.addEventListener('pointercancel', onPointerUp)
    return () => {
      element.removeEventListener('wheel', onWheel)
      element.removeEventListener('pointerdown', onPointerDown)
      element.removeEventListener('pointermove', onPointerMove)
      window.removeEventListener('pointerup', onPointerUp)
      window.removeEventListener('pointercancel', onPointerUp)
    }
  }, [element, onZoom])
}
