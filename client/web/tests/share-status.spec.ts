import { test, expect, login } from './fixtures'

// Regression test for commit 7098ad5: a successful native share used to
// show a redundant "Shared" status message. The OS share sheet is its own
// confirmation - PhotoViewer.tsx now only shows status text for the
// download fallback paths.
test('a successful native share shows no status message', async ({ page }) => {
  await page.addInitScript(() => {
    // @ts-expect-error - stubbing the Web Share API for the test.
    navigator.canShare = () => true
    // @ts-expect-error - stubbing the Web Share API for the test.
    navigator.share = async () => {}
  })
  await login(page)

  await page.locator('[data-testid=photo-tile]').first().click()
  const shareButton = page.locator('[data-testid=share-button]')
  await expect(shareButton).toBeEnabled({ timeout: 20_000 })
  await shareButton.click()

  await page.waitForTimeout(400)
  await expect(page.locator('[data-testid=share-status]')).toHaveCount(0)
})
