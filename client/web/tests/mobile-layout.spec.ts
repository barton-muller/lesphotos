import { test, expect, login } from './fixtures'

// Regression tests for commit 7098ad5's narrow-layout fixes. All three ran
// as one Playwright script against a fakelib library before being
// committed here - see docs/testing-guide.md.
test.describe('phone layout (mobile-android project)', () => {
  test.skip(({ isMobile }) => !isMobile, 'desktop keeps the density buttons, nothing to check here')

  test('density +/- buttons are hidden; Select/Merge are not clipped into a scrollbar', async ({ page }) => {
    await login(page)

    await expect(page.locator('[data-testid=density-increase]')).toHaveCount(0)
    await expect(page.locator('[data-testid=density-decrease]')).toHaveCount(0)

    const overflows = await page
      .locator('#lesphotos-toolbar-slot')
      .evaluate((el) => el.scrollWidth > el.clientWidth)
    expect(overflows).toBe(false)
  })

  test('the profile menu labels bulk thumbnail download compactly', async ({ page }) => {
    await login(page)
    await page.click('[data-testid=profile-button]')
    const label = (await page.textContent('[data-testid=sync-all]'))?.trim()
    // Was "Download Thumbnails" (wrapped in the menu) - now icon + "Thumbs".
    expect(label).toBe('Thumbs')
  })
})
