import { test, expect, login } from './fixtures'

// Regression test for commit 7098ad5 (usePinchZoom.ts): a pinch used to
// change density by at most one step before stalling, because changing
// the column count remounts the tiles under the fingers and a touch's
// move/end events only ever reach the node they started on - once that
// node was gone, the gesture went silent. The fix attaches move/end
// listeners to each finger's own touch target instead of the scroll
// container. See usePinchZoom.ts's own comments for the full story.
//
// Chromium-only: CDP's Input.synthesizePinchGesture is a Chrome DevTools
// Protocol call, not available on WebKit/Firefox. This project
// (mobile-android) runs on Chromium via device emulation, so it works
// here; see docs/testing-guide.md's "Emulation limits" section for what
// this test does and does NOT prove (it proves the density-changing
// gesture handler itself keeps working across multiple steps - it does
// NOT prove the browser's native pinch-zoom is actually suppressed,
// which Chromium's touch emulation doesn't reproduce even on old,
// known-broken code).
test.describe('pinch density (chromium)', () => {
  test.skip(({ browserName }) => browserName !== 'chromium')

  test('a sustained pinch changes density across multiple steps, not just one', async ({ page, context }) => {
    await login(page)

    const scroller = page.locator('div[style*="overflow: auto"]').first()
    const box = await scroller.boundingBox()
    if (!box) throw new Error('scroll container not found')

    const before = await page.evaluate(() => localStorage.getItem('lesphotos-columns'))

    const cdp = await context.newCDPSession(page)
    await cdp.send('Input.synthesizePinchGesture', {
      x: box.x + box.width / 2,
      y: box.y + box.height / 2,
      // A large scale factor over enough relative speed reliably crosses
      // several of usePinchZoom's 1.25x steps in one synthesized gesture -
      // the old, broken code stalled after the first regardless.
      scaleFactor: 2.5,
      relativeSpeed: 400,
      gestureSourceType: 'touch',
    })
    await page.waitForTimeout(800)

    const after = await page.evaluate(() => localStorage.getItem('lesphotos-columns'))
    const beforeN = Number(before ?? 6)
    const afterN = Number(after)
    expect(afterN).not.toBeNull()
    // Spreading fingers -> fewer columns (bigger tiles), see
    // usePinchZoom.ts's own comment on the direction convention. More than
    // one step is the actual regression check; the exact endpoint depends
    // on gesture timing, not worth pinning down further.
    expect(beforeN - afterN).toBeGreaterThan(1)
  })
})
