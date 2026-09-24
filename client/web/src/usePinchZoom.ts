import { useEffect, useRef } from 'react'

/**
 * Pinch-to-zoom that changes grid density instead of scaling the page.
 *
 * Two gestures reach the same handler because browsers report them
 * differently:
 *
 *   - touch: two fingers, tracked by their own distance apart.
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
  // Through a ref: the caller's callback closes over the current column
  // count, so it changes on every step. As an effect dependency that tore
  // the listeners down mid-gesture and lost the finger baseline, capping a
  // whole pinch at one step.
  const onZoomRef = useRef(onZoom)
  useEffect(() => {
    onZoomRef.current = onZoom
  })

  useEffect(() => {
    if (!element) return
    const zoom = (delta: number) => onZoomRef.current(delta)

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
        zoom(wheelAccum > 0 ? 1 : -1)
        wheelAccum -= Math.sign(wheelAccum) * STEP
      }
    }

    // --- touch ------------------------------------------------------------
    // Touch events, not pointer events: only a non-passive touchmove can
    // preventDefault the browser's own pinch-zoom. With pointer events alone,
    // Android Chrome claimed the gesture as page zoom and sent pointercancel
    // before any movement reached us. One-finger scrolling is left alone.
    let baseline: number | null = null

    const distance = (t: TouchList) =>
      Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY)

    const onTouchMove = (e: TouchEvent) => {
      if (e.touches.length !== 2 || baseline === null) return
      if (e.cancelable) e.preventDefault()
      const current = distance(e.touches)
      // Ratio, not pixel delta: the same finger travel should mean the same
      // zoom whether the fingers started close together or far apart.
      const ratio = current / baseline
      const STEP = 1.25
      if (ratio > STEP) {
        zoom(-1)
        baseline = current
      } else if (ratio < 1 / STEP) {
        zoom(1)
        baseline = current
      }
    }

    // Move/end listeners go on each finger's own target, not on `element`:
    // a touch's events are always dispatched to the node it started on, and
    // a density change remounts the tiles. Once that node is detached its
    // events reach no ancestor at all - the pinch stalled after one step -
    // but they do still reach the node itself.
    const targets = new Set<EventTarget>()
    const onTouchEnd = (e: TouchEvent) => {
      if (e.touches.length < 2) baseline = null
      if (e.touches.length === 0) release()
    }
    const release = () => {
      for (const t of targets) {
        t.removeEventListener('touchmove', onTouchMove as EventListener)
        t.removeEventListener('touchend', onTouchEnd as EventListener)
        t.removeEventListener('touchcancel', onTouchEnd as EventListener)
      }
      targets.clear()
    }
    // Only once a second finger lands: a non-passive touchmove on the target
    // of every one-finger scroll would make the browser wait on JS each frame.
    const onTouchStart = (e: TouchEvent) => {
      if (e.touches.length !== 2) {
        baseline = null
        return
      }
      for (const touch of e.touches) {
        const t = touch.target
        if (targets.has(t)) continue
        targets.add(t)
        t.addEventListener('touchmove', onTouchMove as EventListener, { passive: false })
        t.addEventListener('touchend', onTouchEnd as EventListener)
        t.addEventListener('touchcancel', onTouchEnd as EventListener)
      }
      baseline = distance(e.touches)
    }

    element.addEventListener('wheel', onWheel, { passive: false })
    element.addEventListener('touchstart', onTouchStart, { passive: true })
    return () => {
      element.removeEventListener('wheel', onWheel)
      element.removeEventListener('touchstart', onTouchStart)
      release()
    }
  }, [element])
}
