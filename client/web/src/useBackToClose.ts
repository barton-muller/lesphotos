import { useEffect, useRef } from 'react'

/**
 * Make the browser/system back gesture close an overlay instead of leaving.
 *
 * On Android, swiping back from a full-screen photo exited the whole app,
 * because the viewer was React state with no corresponding history entry -
 * there was nothing for "back" to go back to. Pushing one when the overlay
 * opens makes back mean "close this", which is what every native gallery does
 * and is far more intuitive than hunting for a ✕.
 */
export function useBackToClose(open: boolean, onClose: () => void) {
  // onClose is typically an inline arrow, so it changes identity on every
  // render of the parent. Kept in a ref so the effect depends only on `open`.
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  useEffect(() => {
    if (!open) return

    // The listener is installed synchronously, but the history entry is pushed
    // on a later task.
    //
    // StrictMode double-invokes effects in development: setup, cleanup, setup.
    // Pushing inside the effect body meant the *first* cleanup ran a
    // history.back() against a real entry, whose popstate then closed the
    // overlay the instant it opened. Deferring the push means the throwaway
    // first pass has nothing to undo, and only the surviving effect pushes.
    let pushed = false
    let poppedByUser = false

    const timer = setTimeout(() => {
      history.pushState({ overlay: true }, '')
      pushed = true
    }, 0)

    const onPop = () => {
      poppedByUser = true
      onCloseRef.current()
    }
    window.addEventListener('popstate', onPop)

    return () => {
      clearTimeout(timer)
      window.removeEventListener('popstate', onPop)
      // Closed by ✕/Escape rather than by back: our entry is still on the
      // stack and has to come off, or the next back press would silently do
      // nothing. Skipped when the user's own back press already removed it -
      // calling back() again would navigate away from the app entirely.
      if (pushed && !poppedByUser && history.state?.overlay) history.back()
    }
  }, [open])
}
