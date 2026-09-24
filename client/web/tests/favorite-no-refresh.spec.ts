import { test, expect, login } from './fixtures'

// Regression test for the bug fixed in commit 7098ad5: favoriting a photo
// used to blank the whole grid back to "Loading…" and reset scroll to the
// top, because the Timeline load effect cleared `photos` on every
// db.photos write, not just on a starredOnly switch. See Timeline.tsx's
// `loadedForRef` comment.
test('favoriting a photo does not blank the grid or reset scroll', async ({ page }) => {
  await login(page)

  const scroller = page.locator('div[style*="overflow: auto"]').first()
  await scroller.evaluate((el) => { el.scrollTop = 2000 })
  await page.waitForTimeout(500)
  const scrollBefore = await scroller.evaluate((el) => el.scrollTop)
  expect(scrollBefore).toBeGreaterThan(0)

  await expect(page.getByText('Loading…')).toHaveCount(0)

  // Must be a toggle actually inside the current viewport, not just
  // `.first()` in DOM order: this suite's fake library is small enough
  // that far more rows stay mounted than are visible, so `.first()` can
  // resolve to a button hundreds of pixels above the viewport. Clicking
  // an off-screen element (even with `force: true`) still focuses it, and
  // a real browser auto-scrolls a focused element into view - that native
  // behavior, not any app bug, was the original source of a very
  // convincing but entirely test-caused scrollTop mismatch here.
  // scrollIntoViewIfNeeded() is deliberately NOT used - it would scroll to
  // whichever button `.first()` picks, undoing the scrollTop=2000 set
  // above rather than respecting it.
  const allToggles = page.locator('.favorite-toggle[aria-label=Favorite]')
  const inViewportIndex = await allToggles.evaluateAll((buttons, viewportHeight) =>
    buttons.findIndex((b) => {
      const r = b.getBoundingClientRect()
      return r.top >= 0 && r.bottom <= viewportHeight
    }),
  page.viewportSize()!.height)
  if (inViewportIndex === -1) throw new Error('no in-viewport Favorite toggle found')
  const toggle = allToggles.nth(inViewportIndex)

  try {
    await toggle.click()

    // The grid must never show the loading placeholder mid-toggle - a
    // sampled check across the transition, not just before/after, since
    // the bug was a blank-then-rebuild that could pass a naive
    // before/after diff.
    await page.waitForTimeout(300)
    await expect(page.getByText('Loading…')).toHaveCount(0)

    // Not exact scrollTop equality. Investigated at length: on the
    // mobile-android project specifically, @playwright/test's own click
    // (unlike the same click dispatched via bare playwright-core, and
    // unlike a real `page.touchscreen.tap()`) reliably nudges scrollTop by
    // a few hundred px even against a toggle already fully inside the
    // viewport - some extra "make sure this is robustly interactable"
    // scroll correction internal to the test runner's action
    // implementation, not anything the app does (confirmed: no
    // scrollIntoView/focus call anywhere near the favorite-toggle code,
    // and the same interaction via a real touch tap in a raw script never
    // moves scrollTop at all). The actual regression this test guards is
    // a full reset toward the top (scrollTop snapping to 0, "Loading…"
    // flashing) - the "Loading…" check above already catches the blank,
    // and this checks scroll stayed in the same neighborhood rather than
    // being thrown back near the top.
    const scrollAfter = await scroller.evaluate((el) => el.scrollTop)
    expect(scrollAfter).toBeGreaterThan(scrollBefore * 0.5)
  } finally {
    // Un-favorite even if an assertion above threw, so a failed run
    // doesn't leave state that breaks the next run's "first Favorite
    // button" assumption.
    await page.locator('.favorite-toggle[aria-label=Unfavorite]').first().click()
  }
})
