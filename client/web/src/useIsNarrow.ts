import { useEffect, useState } from 'react'

/**
 * Breakpoint below which the app switches to its phone layout.
 *
 * Chosen from the constraint rather than a device list: the sidebar is 220px
 * and the scrubber rail reserves ~148px, which leaves under 400px of grid on a
 * 768px tablet in portrait - workable - but nothing usable on a 390px phone.
 */
export const NARROW_BREAKPOINT = 700

/**
 * True when the viewport is narrow enough to need the phone layout.
 *
 * Uses matchMedia rather than a resize listener: it fires only when the
 * breakpoint is actually crossed, not on every pixel of a resize, and it is
 * what the browser already evaluates for CSS media queries.
 */
export function useIsNarrow(breakpoint = NARROW_BREAKPOINT): boolean {
  const query = `(max-width: ${breakpoint}px)`
  const [narrow, setNarrow] = useState(
    () => typeof window !== 'undefined' && window.matchMedia(query).matches,
  )

  useEffect(() => {
    const mql = window.matchMedia(query)
    const onChange = (e: MediaQueryListEvent) => setNarrow(e.matches)
    // No setNarrow here: useState's initializer already read this same query,
    // and re-setting it on mount is a wasted second render on every load.
    // A breakpoint crossed between those two points still fires `change`.
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [query])

  return narrow
}
