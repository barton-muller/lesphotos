import { test, expect, login } from './fixtures'

// Baseline: the app logs in and renders a real grid from a cold start
// (fresh Playwright context, no reused session - see CLAUDE.md's "test
// from a cold start" rule). Every other spec in this suite assumes this
// works, so it's worth its own explicit check rather than only being
// implied by the others passing.
test('logs in and renders the timeline grid', async ({ page }) => {
  await login(page)
  await expect(page.locator('[data-testid=photo-tile]').first()).toBeVisible()
  await expect(page.locator('[data-testid=profile-button]')).toBeVisible()
})

test('server /health reports a version', async ({ request, baseURL }) => {
  const resp = await request.get(`${baseURL}/lesphotos-api/health`)
  expect(resp.ok()).toBe(true)
  const body = await resp.json()
  expect(body.ok).toBe(true)
  expect(typeof body.version).toBe('string')
})
